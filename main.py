import os
import json
import time
import hashlib
import requests
import urllib.parse
import csv
import redis
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, Request, Query, UploadFile, File
from fastapi.responses import RedirectResponse, JSONResponse

# ───────────────────────────── CONFIG ─────────────────────────────

DEFAULT_PRODUCT_URL = os.getenv("DEFAULT_PRODUCT_URL", "https://shopee.com.br/")

FB_PIXEL_ID     = os.getenv("FB_PIXEL_ID") or os.getenv("META_PIXEL_ID")
FB_ACCESS_TOKEN = os.getenv("FB_ACCESS_TOKEN") or os.getenv("META_ACCESS_TOKEN")
if not FB_PIXEL_ID or not FB_ACCESS_TOKEN:
    raise RuntimeError("Defina FB_PIXEL_ID e FB_ACCESS_TOKEN (ou META_PIXEL_ID / META_ACCESS_TOKEN) nas variáveis de ambiente.")

FB_ENDPOINT = f"https://graph.facebook.com/v14.0/{FB_PIXEL_ID}/events?access_token={FB_ACCESS_TOKEN}"

SHOPEE_APP_ID     = os.getenv("SHOPEE_APP_ID", "18314810331")
SHOPEE_APP_SECRET = os.getenv("SHOPEE_APP_SECRET", "LO3QSEG45TYP4NYQBRXLA2YYUL3ZCUPN")
SHOPEE_ENDPOINT   = "https://open-api.affiliate.shopee.com.br/graphql"

VIDEO_ID     = os.getenv("VIDEO_ID", "v15")
REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379/0")
COUNTER_KEY  = os.getenv("UTM_COUNTER_KEY", "utm_counter")

# TTL do cache (7 dias por padrão)
USERDATA_TTL_SECONDS = int(os.getenv("USERDATA_TTL_SECONDS", "604800"))
USERDATA_KEY_PREFIX  = os.getenv("USERDATA_KEY_PREFIX", "ud:")

# Janela máxima que você pretende enviar compras atrasadas (7 dias)
MAX_DELAY_SECONDS = int(os.getenv("MAX_DELAY_SECONDS", str(7 * 24 * 60 * 60)))

# ───────────────────────────── APP / REDIS ─────────────────────────────

r = redis.from_url(REDIS_URL)
app = FastAPI(title="Shopee UTM + Meta CAPI Server")

# ───────────────────────────── HELPERS ─────────────────────────────

def incr_and_make_utm() -> str:
    """Gera um utm_content único no formato v[num]n[num]."""
    count = r.incr(COUNTER_KEY)
    return f"{VIDEO_ID}n{count}"

def normalize_utm(u: Optional[str]) -> Optional[str]:
    """Remove sufixos após '-' (ex.: v15n10---- -> v15n10)."""
    if not u:
        return None
    return str(u).split("-")[0]

def get_cookie_value(cookie_header: Optional[str], name: str) -> Optional[str]:
    if not cookie_header:
        return None
    try:
        items = [c.strip() for c in cookie_header.split(";")]
        for it in items:
            if it.startswith(name + "="):
                return it.split("=", 1)[1]
    except Exception:
        pass
    return None

def replace_utm_content_only(raw_url: str, new_value: str) -> str:
    """
    Substitui SOMENTE o valor de utm_content na query string,
    mantendo caminho/ordem/nomes de parâmetros exatamente como estavam.
    Se não existir, adiciona no final.
    """
    parsed = urllib.parse.urlsplit(raw_url)
    if not parsed.query:
        new_query = f"utm_content={new_value}"
    else:
        parts = parsed.query.split("&")
        found = False
        for i, part in enumerate(parts):
            if part.startswith("utm_content="):
                parts[i] = "utm_content=" + new_value
                found = True
                break
        if not found:
            parts.append("utm_content=" + new_value)
        new_query = "&".join(parts)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))

def build_fbc_from_fbclid(fbclid: Optional[str], creation_ts: Optional[int] = None) -> Optional[str]:
    if not fbclid:
        return None
    if creation_ts is None:
        creation_ts = int(time.time())
    return f"fb.1.{creation_ts}.{fbclid}"

def save_user_data(utm: str, data: Dict[str, Any]) -> None:
    key = f"{USERDATA_KEY_PREFIX}{utm}"
    r.setex(key, USERDATA_TTL_SECONDS, json.dumps(data))

def load_user_data(utm: str) -> Optional[Dict[str, Any]]:
    key = f"{USERDATA_KEY_PREFIX}{utm}"
    raw = r.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

def generate_short_link(origin_url: str, utm_content: str) -> str:
    """
    Usa o endpoint GraphQL oficial para gerar short link (s.shopee...)
    com sub_id3 = utm_content.
    """
    payload_obj = {
        "query": (
            "mutation{generateShortLink(input:{"
            f"originUrl:\"{origin_url}\","
            f"subIds:[\"\",\"\",\"{utm_content}\",\"\",\"\"]"
            "}){shortLink}}"
        )
    }
    payload = json.dumps(payload_obj, separators=(',', ':'), ensure_ascii=False)

    timestamp = str(int(time.time()))
    base_str  = SHOPEE_APP_ID + timestamp + payload + SHOPEE_APP_SECRET
    signature = hashlib.sha256(base_str.encode("utf-8")).hexdigest()

    headers = {
        "Authorization": f"SHA256 Credential={SHOPEE_APP_ID}, Timestamp={timestamp}, Signature={signature}",
        "Content-Type": "application/json"
    }
    resp = requests.post(SHOPEE_ENDPOINT, headers=headers, data=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data["data"]["generateShortLink"]["shortLink"]

def send_fb_event(event_name: str,
                  event_id: str,
                  event_source_url: str,
                  user_data: Dict[str, Any],
                  custom_data: Dict[str, Any],
                  event_time: int) -> Dict[str, Any]:
    """Envia um evento para a Conversions API com event_time controlado."""
    payload = {
        "data": [{
            "event_name": event_name,
            "event_time": int(event_time),
            "event_id": event_id,
            "action_source": "website",
            "event_source_url": event_source_url,
            "user_data": user_data,
            "custom_data": custom_data
        }]
    }
    rqs = requests.post(FB_ENDPOINT, json=payload, timeout=20)
    try:
        out = rqs.json()
    except Exception:
        out = {"status_code": rqs.status_code, "text": rqs.text}
    return out

def fbc_creation_ts(fbc: Optional[str]) -> Optional[int]:
    """Extrai creation_time do FBC (formato fb.1.<creation>.<fbclid>)."""
    if not fbc:
        return None
    try:
        parts = fbc.split(".")
        return int(parts[2])
    except Exception:
        return None

# ───────────────────────────── ROUTES ─────────────────────────────

@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}

@app.get("/")
def redirect_to_shopee(
    request: Request,
    link: str = Query(DEFAULT_PRODUCT_URL, description="URL completa da Shopee")
):
    """
    Gera um UTM único, envia ViewContent, salva user_data+vc_time no Redis
    e redireciona para short link da Shopee (fallback: URL original com utm).
    """
    # 1) Gera UTM
    utm_value = incr_and_make_utm()

    # 2) Dados do cliente
    headers = request.headers
    cookie_header = headers.get("cookie") or headers.get("Cookie")
    fbp_cookie = get_cookie_value(cookie_header, "_fbp")
    fbclid     = request.query_params.get("fbclid")

    client_host = request.client.host if request.client else None
    if client_host and client_host.startswith("::ffff:"):
        client_host = client_host.split("::ffff:")[-1]
    ip_addr    = client_host or headers.get("x-forwarded-for") or "0.0.0.0"
    user_agent = headers.get("user-agent", "-")

    # 3) Captura o tempo do VC e constrói FBC com o MESMO creation_time
    vc_time = int(time.time())
    fbc_val = build_fbc_from_fbclid(fbclid, creation_ts=vc_time)

    # 4) user_data para VC
    user_data_vc: Dict[str, Any] = {
        "client_ip_address": ip_addr,
        "client_user_agent": user_agent
    }
    if fbp_cookie:
        user_data_vc["fbp"] = fbp_cookie
    if fbc_val:
        user_data_vc["fbc"] = fbc_val

    # 5) Envia VC com event_time = vc_time
    custom_data_vc = {"content_type": "product"}
    try:
        send_fb_event("ViewContent", utm_value, link, user_data_vc, custom_data_vc, vc_time)
    except Exception as e:
        print(f"[CAPI VC] erro: {e}")

    # 6) Salva no Redis para reutilizar no Purchase (inclui vc_time)
    save_user_data(utm_value, {
        "user_data": user_data_vc,
        "event_source_url": link,
        "vc_time": vc_time
    })

    # 7) Construção do destino: short link oficial; fallback: URL original + utm_content
    try:
        short_link = generate_short_link(link, utm_value)
        dest = short_link
    except Exception as e:
        print(f"[ShopeeShortLink] Falha: {e}. Fallback para URL original.")
        dest = replace_utm_content_only(link, utm_value)

    # 8) Redireciona
    return RedirectResponse(dest, status_code=302)

@app.post("/upload_csv")
async def upload_csv(file: UploadFile = File(...)):
    """
    Recebe CSV com colunas:
      - utm_content (ou: utm, sub_id3, subid3, sub_id_3)  [obrigatória]
      - value        (ou: valor, price, amount)            [opcional]
      - num_purchases(ou: vendas, quantity, qty, purchases)[opcional]
    NÃO lê tempo do CSV: usa o vc_time salvo no Redis (do ViewContent) como event_time do Purchase.
    """
    content = await file.read()
    text = content.decode("utf-8", errors="replace").splitlines()
    reader = csv.DictReader(text)

    processed: List[Dict[str, Any]] = []
    now_ts = int(time.time())
    min_allowed = now_ts - MAX_DELAY_SECONDS

    for row in reader:
        raw_utm = (
            row.get("utm_content") or row.get("utm") or
            row.get("sub_id3") or row.get("subid3") or row.get("sub_id_3")
        )
        utm = normalize_utm(raw_utm)
        if not utm:
            processed.append({"row": row, "status": "skipped_no_utm"})
            continue

        # Valor e quantidade
        valor_raw = row.get("value") or row.get("valor") or row.get("price") or row.get("amount")
        vendas_raw = row.get("num_purchases") or row.get("vendas") or row.get("quantity") or row.get("qty") or row.get("purchases")

        try:
            valor = float(str(valor_raw).replace(",", ".")) if valor_raw not in (None, "") else 0.0
        except Exception:
            valor = 0.0
        try:
            vendas = int(float(vendas_raw)) if vendas_raw not in (None, "",) else 1
        except Exception:
            vendas = 1

        # Recupera user_data + vc_time salvo no VC
        cache = load_user_data(utm)
        if not cache or not cache.get("user_data"):
            processed.append({"utm_content": raw_utm, "utm_norm": utm, "status": "skipped_no_user_data"})
            continue

        user_data_purchase = cache["user_data"]
        event_source_url   = cache.get("event_source_url") or DEFAULT_PRODUCT_URL
        vc_time            = cache.get("vc_time")

        # Se por algum motivo vc_time não existir, usa agora
        event_time = int(vc_time) if isinstance(vc_time, int) else now_ts

        # Coerência com FBC e janelas
        # 1) Não no futuro
        if event_time > now_ts:
            event_time = now_ts
        # 2) Dentro de 7 dias
        if event_time < min_allowed:
            event_time = min_allowed
        # 3) Maior ou igual ao creation_time do FBC
        click_ts = fbc_creation_ts(user_data_purchase.get("fbc"))
        if click_ts and event_time < click_ts:
            event_time = click_ts + 1

        custom_data_purchase = {
            "currency": "BRL",
            "value": valor,
            "num_purchases": vendas
        }

        try:
            resp = send_fb_event("Purchase", utm, event_source_url, user_data_purchase, custom_data_purchase, event_time)
            processed.append({"utm_content": raw_utm, "utm_norm": utm, "status": "sent", "capi": resp})
        except Exception as e:
            processed.append({"utm_content": raw_utm, "utm_norm": utm, "status": "error", "error": str(e)})

    return JSONResponse({"processed": processed})
