import os
import json
import time
import hashlib
import requests
import urllib.parse
import csv
import redis
from fastapi import FastAPI, Request, Query, UploadFile, File
from fastapi.responses import RedirectResponse

# ─── CONFIGURAÇÕES ───────────────────────────────────────────────────────────
DEFAULT_PRODUCT_URL = os.getenv(
    "DEFAULT_PRODUCT_URL",
    "https://shopee.com.br/product/1006215031/25062459693?gads_t_sig=VTJGc2RHVmtYMTlxTFVSVVRrdENkWHlFU0hvQlZFVENpb1FnT09uNDlDSlFlak9NK3REcVdCSmhxWE5KOFJPaitxczVrMlZKVi9IZnBqNzdBck9lTFYydUVucnVPaytVNldBWjRaQjMxdTF0RTVSOWxYclJRSktpbU9SVUI1a0djdGxxczBFOERYZWYzM2xKYmIvUHNrOHVFVWxLUktmMXVSSjVrdlpWY0RRPQ&uls_trackid=53c9g0ka00a6&utm_campaign=id_x8Yuftr1lW&utm_content=----&utm_medium=affiliates&utm_source=an_18314810331&utm_term=dfo9czqqfhwm"
)

FB_PIXEL_ID     = os.getenv("FB_PIXEL_ID") or os.getenv("META_PIXEL_ID")
FB_ACCESS_TOKEN = os.getenv("FB_ACCESS_TOKEN") or os.getenv("META_ACCESS_TOKEN")
if not FB_PIXEL_ID or not FB_ACCESS_TOKEN:
    raise RuntimeError(
        "As variáveis de ambiente FB_PIXEL_ID e FB_ACCESS_TOKEN (ou META_PIXEL_ID e META_ACCESS_TOKEN) devem estar definidas."
    )
FB_ENDPOINT = f"https://graph.facebook.com/v14.0/{FB_PIXEL_ID}/events?access_token={FB_ACCESS_TOKEN}"

SHOPEE_APP_ID     = os.getenv("SHOPEE_APP_ID", "18314810331")
SHOPEE_APP_SECRET = os.getenv("SHOPEE_APP_SECRET", "LO3QSEG45TYP4NYQBRXLA2YYUL3ZCUPN")
SHOPEE_ENDPOINT   = "https://open-api.affiliate.shopee.com.br/graphql"

VIDEO_ID     = os.getenv("VIDEO_ID", "v15")
REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379/0")
COUNTER_KEY  = os.getenv("UTM_COUNTER_KEY", "utm_counter")

# TTL do user_data salvo (em segundos). Padrão: 7 dias.
USERDATA_TTL_SECONDS = int(os.getenv("USERDATA_TTL_SECONDS", "604800"))
USERDATA_KEY_PREFIX  = os.getenv("USERDATA_KEY_PREFIX", "ud:")

# Conecta ao Redis
r = redis.from_url(REDIS_URL)

app = FastAPI()

# ─── UTILS ────────────────────────────────────────────────────────────────────
def next_utm() -> str:
    count = r.incr(COUNTER_KEY)
    print(f"Gerando UTM: {VIDEO_ID}n{count}")
    return f"{VIDEO_ID}n{count}"

def normalize_utm(u: str | None) -> str | None:
    """Normaliza utm_content caso venha com sufixos como '----'."""
    if not u:
        return None
    return u.split("-")[0]  # pega somente o prefixo (ex.: v15n123)

def build_fbc(fbclid: str | None) -> str | None:
    if not fbclid:
        return None
    return f"fb.1.{int(time.time())}.{fbclid}"

def send_fb_event(event_name: str, event_id: str, event_source_url: str,
                  user_data: dict, custom_data: dict):
    payload = {"data": [{
        "event_name": event_name,
        "event_time": int(time.time()),
        "event_id": event_id,
        "action_source": "website",
        "event_source_url": event_source_url,
        "user_data": user_data,
        "custom_data": custom_data
    }]}
    try:
        resp = requests.post(FB_ENDPOINT, json=payload, timeout=10)
        # Log curto de status/resposta pra debug
        print(f"[MetaAPI] Status: {resp.status_code}")
        try:
            print(f"[MetaAPI] Response: {resp.text[:400]}")
        except Exception:
            pass
        resp.raise_for_status()
    except Exception as e:
        print(f"[MetaAPI] Exception sending event: {e}")

def save_user_data(utm: str, data: dict):
    key = f"{USERDATA_KEY_PREFIX}{utm}"
    r.setex(key, USERDATA_TTL_SECONDS, json.dumps(data, ensure_ascii=False))
    print(f"[UserData] Saved for {utm} (TTL={USERDATA_TTL_SECONDS}s)")

def load_user_data(utm: str) -> dict | None:
    key = f"{USERDATA_KEY_PREFIX}{utm}"
    raw = r.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

# ─── GERAÇÃO DE SHORT LINK DA SHOPEE ───────────────────────────────────────────
def generate_short_link(full_url: str, utm_content: str) -> str:
    payload_obj = {
        "query": (
            "mutation{generateShortLink(input:{"
            f"originUrl:\"{full_url}\","
            f"subIds:[\"\",\"\",\"{utm_content}\",\"\",\"\"]"
            "}){shortLink}}"
        )
    }
    payload = json.dumps(payload_obj, separators=(',', ':'), ensure_ascii=False)

    timestamp = str(int(time.time()))
    base_str  = SHOPEE_APP_ID + timestamp + payload + SHOPEE_APP_SECRET
    signature = hashlib.sha256(base_str.encode('utf-8')).hexdigest()

    headers = {
        "Authorization": (
            f"SHA256 Credential={SHOPEE_APP_ID}, Timestamp={timestamp}, Signature={signature}"
        ),
        "Content-Type": "application/json"
    }

    resp = requests.post(SHOPEE_ENDPOINT, headers=headers, data=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    link = data['data']['generateShortLink']['shortLink']
    print(f"[ShopeeShortLink] Gerado ({utm_content}): {link}")
    return link

# ─── ENDPOINT PRINCIPAL (ViewContent + redirect) ──────────────────────────────
@app.get("/", response_class=RedirectResponse)
async def redirect_to_shopee(
    request: Request,
    product: str = Query(None, description="URL original Shopee (URL-encoded) ou vazio para usar DEFAULT_PRODUCT_URL")
):
    # URL do produto
    url_in = urllib.parse.unquote_plus(product) if product else DEFAULT_PRODUCT_URL

    # Gera UTM e coleta dados de identificação
    utm_value = next_utm()
    ip_addr   = request.client.host
    user_agent = request.headers.get("user-agent", "")

    # Captura cookies e fbclid (se houver)
    fbp_cookie = request.cookies.get("_fbp")
    fbclid = request.query_params.get("fbclid")
    fbc_val = build_fbc(fbclid)

    # Monta user_data para o VC
    user_data_vc = {
        "client_ip_address": ip_addr,
        "client_user_agent": user_agent
    }
    if fbp_cookie:
        user_data_vc["fbp"] = fbp_cookie
    if fbc_val:
        user_data_vc["fbc"] = fbc_val

    # Envia ViewContent
    custom_data_vc = {
        "content_ids": [urllib.parse.urlparse(url_in).path.split('/')[-1]],
        "content_type": "product"
    }
    send_fb_event("ViewContent", utm_value, url_in, user_data_vc, custom_data_vc)

    # Salva o mesmo user_data no Redis pra usar no Purchase
    save_user_data(utm_value, {
        "user_data": user_data_vc,
        "event_source_url": url_in
    })

    # Gera short link e redireciona
    try:
        short_link = generate_short_link(url_in, utm_value)
    except Exception as e:
        print(f"[ShopeeShortLink] Falha: {e}")
        short_link = url_in

    return RedirectResponse(url=short_link, status_code=302)

# ─── ENDPOINT DE UPLOAD CSV (Purchase) ────────────────────────────────────────
@app.post("/upload_csv")
async def upload_csv(request: Request, file: UploadFile = File(...)):
    """
    Para cada linha com vendas>0:
      - lê 'utm_content'
      - normaliza a UTM (ex.: remove '----')
      - carrega user_data salvo no ViewContent
      - se achar, envia Purchase com o MESMO user_data
      - se não achar, pula (evita erro 400 da Meta)
    """
    processed = []
    event_url = str(request.url)

    # Lê CSV
    content = file.file.read().decode('utf-8').splitlines()
    reader = csv.DictReader(content)

    for row in reader:
        raw_utm = row.get('utm_content')
        utm = normalize_utm(raw_utm)
        vendas = int(row.get('vendas', 0) or 0)
        valor = float(row.get('valor', 0) or 0)

        if vendas <= 0:
            processed.append({"utm_content": raw_utm, "status": "ignored_no_sales"})
            continue

        if not utm:
            processed.append({"utm_content": raw_utm, "status": "skipped_no_utm"})
            continue

        cache = load_user_data(utm)
        if not cache or not cache.get("user_data"):
            # Sem user_data salvo — não envia pra evitar 400
            processed.append({"utm_content": raw_utm, "utm_norm": utm, "status": "skipped_no_user_data"})
            continue

        user_data_purchase = cache["user_data"]
        # Usa a mesma origem se possível; senão, usa a URL do upload
        event_source_url = cache.get("event_source_url") or event_url

        custom_data_purchase = {
            "currency": "BRL",
            "value": valor,
            "num_purchases": vendas
        }

        send_fb_event("Purchase", utm, event_source_url, user_data_purchase, custom_data_purchase)
        processed.append({"utm_content": raw_utm, "utm_norm": utm, "status": "sent"})

    return {"processed": processed}
