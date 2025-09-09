# main.py
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import hashlib
import requests
import urllib.parse
import csv
import ipaddress
import redis
from typing import Optional, Dict, Any, List, Tuple

from fastapi import FastAPI, Request, Query, UploadFile, File, Body
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse  # <- HTMLResponse adicionado

# ───────────────────────────── CONFIG ÚNICA ─────────────────────────────
DEFAULT_PRODUCT_URL = os.getenv("DEFAULT_PRODUCT_URL", "https://shopee.com.br/XEIJAIYI-8pcs-Kit-De-Gel-De-Extens%C3%A3o-De-Unhas-De-Polietileno-15ml-Nude-Pink-All-In-One-Construtor-Cola-Com-Formas-Duplas-Clipes-Manicure-Set-For-Beginnerer-i.1006215031.25062459693?sp_atk=7d9b4afa-fe7b-46a4-8d67-40beca78c014&uls_trackid=53eafnvh01ho&utm_campaign=id_KZh1YNURmU&utm_content=----&utm_medium=affiliates&utm_source=an_18314810331&utm_term=dh2byqcm489v&xptdk=7d9b4afa-fe7b-46a4-8d67-40beca78c014")

# Credenciais da Meta (Conversions API) — mantido
FB_PIXEL_ID     = os.getenv("FB_PIXEL_ID") or os.getenv("META_PIXEL_ID") or "COLOQUE_SEU_PIXEL_ID_AQUI"
FB_ACCESS_TOKEN = os.getenv("FB_ACCESS_TOKEN") or os.getenv("META_ACCESS_TOKEN") or "COLOQUE_SEU_ACCESS_TOKEN_AQUI"
FB_ENDPOINT     = f"https://graph.facebook.com/v14.0/{FB_PIXEL_ID}/events?access_token={FB_ACCESS_TOKEN}"

# Shopee Affiliate — mantido
SHOPEE_APP_ID     = os.getenv("SHOPEE_APP_ID", "18314810331")
SHOPEE_APP_SECRET = os.getenv("SHOPEE_APP_SECRET", "LO3QSEG45TYP4NYQBRXLA2YYUL3ZCUPN")
SHOPEE_ENDPOINT   = "https://open-api.affiliate.shopee.com.br/graphql"

# ID lógico (vídeo/campanha) para compor UTM único
VIDEO_ID     = os.getenv("VIDEO_ID", "v15")

# Redis
REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379/0")
COUNTER_KEY  = os.getenv("UTM_COUNTER_KEY", "utm_counter")

# TTL do cache (7 dias)
USERDATA_TTL_SECONDS = int(os.getenv("USERDATA_TTL_SECONDS", "604800"))
USERDATA_KEY_PREFIX  = os.getenv("USERDATA_KEY_PREFIX", "ud:")

# Janela máxima para compras atrasadas (7 dias)
MAX_DELAY_SECONDS = int(os.getenv("MAX_DELAY_SECONDS", str(7 * 24 * 60 * 60)))

# Controle anti-bot / repetição
CLICK_WINDOW_SECONDS   = int(os.getenv("CLICK_WINDOW_SECONDS", "3600"))
MAX_CLICKS_PER_FP      = int(os.getenv("MAX_CLICKS_PER_FP", "2"))
FINGERPRINT_PREFIX     = os.getenv("FINGERPRINT_PREFIX", "fp:")

# Evento custom para auditoria interna
EMIT_INTERNAL_BLOCK_LOG = True

# Segurança básica em endpoints admin
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "troque_este_token_admin")

# ───────────────────────────── LISTAS EMBUTIDAS ─────────────────────────────
SEEDED_WHITELIST_IPS = []
SEED_WHITELIST_CIDRS = []
SEED_WHITELIST_UA_SUBSTR = []

SEEDED_BLACKLIST_IPS = []
SEED_BLACKLIST_CIDRS = []
SEED_BLACKLIST_UA_SUBSTR = []

# ───────────────────────────── APP / REDIS ─────────────────────────────
r = redis.from_url(REDIS_URL)
app = FastAPI(title="Shopee UTM + Meta CAPI Server c/ Anti-Bot, Whitelist/Blacklist + TikTok Bridge")

# ───────────────────────────── HELPERS BÁSICOS ─────────────────────────────

def incr_and_make_utm() -> str:
    count = r.incr(COUNTER_KEY)
    return f"{VIDEO_ID}n{count}"

def normalize_utm(u: Optional[str]) -> Optional[str]:
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
        new_query = "&join".replace("join", "join").join(parts)  # mantém ordem original; não reordena
        new_query = "&".join(parts)  # (garantia)
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

def fbc_creation_ts(fbc: Optional[str]) -> Optional[int]:
    if not fbc:
        return None
    try:
        parts = fbc.split(".")
        return int(parts[2])
    except Exception:
        return None

def generate_short_link(origin_url: str, utm_content: str) -> str:
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
    payload = {
        "data": [{
            "event_name": event_name,
            "event_time": int(event_time),
            "event_id": event_id,
            "action_source": "website",
            "event_source_url": event_source_url,
            "user_data": user_data,
            "custom_data": custom_data
        }]}
    rqs = requests.post(FB_ENDPOINT, json=payload, timeout=20)
    try:
        out = rqs.json()
    except Exception:
        out = {"status_code": rqs.status_code, "text": rqs.text}
    return out

# ───────────────────────────── UA / FINGERPRINT / LISTAS ─────────────────────────────

def parse_device_os(ua: str) -> Tuple[str, str]:
    if not ua:
        return ("-", "-")
    m = re.search(r"iPhone OS (\d+)_?", ua) or re.search(r"CPU iPhone OS (\d+)", ua)
    if m:
        return ("iOS", m.group(1))
    m = re.search(r"Android (\d+)", ua)
    if m:
        return ("Android", m.group(1))
    if "iPhone" in ua or "iPad" in ua:
        return ("iOS", "-")
    if "Android" in ua:
        return ("Android", "-")
    return ("Other", "-")

def make_fingerprint(ip: str, ua: str) -> str:
    osfam, osmaj = parse_device_os(ua)
    base = f"{ip}|{osfam}|{osmaj}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()

def _ip_in_cidrs(ip: str, cidrs: List[str]) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip)
        for c in cidrs:
            try:
                if ip_obj in ipaddress.ip_network(c, strict=False):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False

class ListManager:
    def __init__(self):
        self.whitelist_ips: set = set(SEEDED_WHITELIST_IPS)
        self.whitelist_cidrs: List[str] = list(SEED_WHITELIST_CIDRS)
        self.whitelist_ua_substr: set = set(SEED_WHITELIST_UA_SUBSTR)

        self.blacklist_ips: set = set(SEEDED_BLACKLIST_IPS)
        self.blacklist_cidrs: List[str] = list(SEED_BLACKLIST_CIDRS)
        self.blacklist_ua_substr: set = set(SEED_BLACKLIST_UA_SUBSTR)

    def is_whitelisted(self, ip: str, ua: str) -> bool:
        if ip in self.whitelist_ips: return True
        if _ip_in_cidrs(ip, self.whitelist_cidrs): return True
        for sub in self.whitelist_ua_substr:
            if sub and sub in ua: return True
        return False

    def is_blacklisted(self, ip: str, ua: str) -> bool:
        if ip in self.blacklist_ips: return True
        if _ip_in_cidrs(ip, self.blacklist_cidrs): return True
        for sub in self.blacklist_ua_substr:
            if sub and sub in ua: return True
        return False

    # Admin ops
    def add_whitelist_ip(self, ip: str): self.whitelist_ips.add(ip)
    def add_blacklist_ip(self, ip: str): self.blacklist_ips.add(ip)
    def add_whitelist_cidr(self, cidr: str): self.whitelist_cidrs.append(cidr)
    def add_blacklist_cidr(self, cidr: str): self.blacklist_cidrs.append(cidr)
    def add_whitelist_ua(self, substr: str): self.whitelist_ua_substr.add(substr)
    def add_blacklist_ua(self, substr: str): self.blacklist_ua_substr.add(substr)

    def dump(self) -> Dict[str, Any]:
        return {
            "whitelist_ips": sorted(list(self.whitelist_ips)),
            "whitelist_cidrs": list(self.whitelist_cidrs),
            "whitelist_ua_substr": sorted(list(self.whitelist_ua_substr)),
            "blacklist_ips": sorted(list(self.blacklist_ips)),
            "blacklist_cidrs": list(self.blacklist_cidrs),
            "blacklist_ua_substr": sorted(list(self.blacklist_ua_substr)),
        }

LISTS = ListManager()

def fp_counter_key(fp: str) -> str:
    return f"{FINGERPRINT_PREFIX}{fp}"

def allow_viewcontent(ip: str, ua: str, utm: str) -> Tuple[bool, str, int]:
    if LISTS.is_whitelisted(ip, ua):
        return True, "whitelist", 0
    if LISTS.is_blacklisted(ip, ua):
        return False, "blacklist", 0
    fp = make_fingerprint(ip, ua)
    key = fp_counter_key(fp)
    cnt = r.incr(key)
    if cnt == 1:
        r.expire(key, CLICK_WINDOW_SECONDS)
    if cnt > MAX_CLICKS_PER_FP:
        return False, "rate_limited", int(cnt)
    return True, "ok", int(cnt)

# ───────────────────────────── BRIDGE HTML (somente para TikTok) ─────────────────────────────

def _tiktok_bridge_html(target_url: str) -> str:
    """
    HTML que tenta abrir o app da Shopee:
      1) universal link (s.shopee.com.br/...) — iOS/Android
      2) intent:// (Android fallback)
      3) botão 'Abrir no app' se o webview bloquear
    Não reescreve nem reordena parâmetros do target_url.
    """
    safe_url = target_url  # não alterar!
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Abrindo Shopee…</title>
<style>
  body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,'Helvetica Neue',Arial;margin:0;background:#0b1526;color:#fff;display:flex;min-height:100vh;align-items:center;justify-content:center}}
  .card{{max-width:520px;width:92%;background:#111827;border-radius:16px;padding:24px;box-shadow:0 10px 30px rgba(0,0,0,.4)}}
  h1{{font-size:20px;margin:0 0 10px}}
  p{{color:#cbd5e1;line-height:1.45}}
  .btn{{display:inline-block;margin-top:16px;padding:14px 18px;border-radius:12px;background:#ff4d00;color:#fff;text-decoration:none;font-weight:700}}
  .note{{font-size:12px;opacity:.8;margin-top:10px}}
</style>
<script>
(function(){
  var targetUrl = {json.dumps(safe_url)};
  if(!targetUrl) return;

  function tryOpen(){
    try{{ window.location.href = targetUrl; }}catch(e){{}}
    var ua = navigator.userAgent || "";
    var isAndroid = /Android/i.test(ua);

    var intentUrl = "intent://" + targetUrl.replace(/^https?:\\/\\//,"") +
                    "#Intent;scheme=https;package=com.shopee.app;S.browser_fallback_url=" +
                    encodeURIComponent(targetUrl) + ";end";

    setTimeout(function(){ if(isAndroid){{ try{{ window.location.href = intentUrl; }}catch(e){{}} }} }, 900);
    setTimeout(function(){ var b = document.getElementById('open-btn'); if(b) b.style.display='inline-block'; }, 2000);
  }

  window.reopenApp = function(){ tryOpen(); };

  document.addEventListener('DOMContentLoaded', function(){ tryOpen(); });
})();
</script>
</head>
<body>
  <div class="card">
    <h1>Abrindo no app da Shopee…</h1>
    <p id="msg">Se não abrir automaticamente, toque no botão abaixo.</p>
    <a id="open-btn" class="btn" style="display:none" href="javascript:reopenApp()">Abrir no app</a>
    <p class="note">Dica: autorize “Abrir em Shopee” se o TikTok pedir confirmação.</p>
  </div>
</body>
</html>"""

# ───────────────────────────── ROUTES ─────────────────────────────

@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time()), "pixel": FB_PIXEL_ID, "video_id": VIDEO_ID}

@app.get("/version")
def version():
    return {"name": "utm-meta-capi-anti-bot", "version": "1.1.0-tiktok-bridge"}

@app.get("/")
def redirect_to_shopee(
    request: Request,
    link: str = Query(DEFAULT_PRODUCT_URL, description="URL completa da Shopee")
):
    """
    Fluxo mantido:
      1) Gera UTM único
      2) Whitelist/Blacklist/Rate-limit
      3) Envia ViewContent (Meta CAPI) se permitido
      4) Salva user_data no Redis
      5) Gera short link oficial da Shopee (fallback: URL com utm_content)
      6) Redireciona SEMPRE — porém:
         - Se origem for TikTok webview, devolve HTML Bridge que força abrir o app.
         - Caso contrário, 302 normal pro shortlink.
    """
    # 1) UTM
    utm_value = incr_and_make_utm()

    # 2) Coleta dados do cliente
    headers = request.headers
    cookie_header = headers.get("cookie") or headers.get("Cookie")
    fbp_cookie = get_cookie_value(cookie_header, "_fbp")
    fbclid     = request.query_params.get("fbclid")

    client_host = request.client.host if request.client else None
    if client_host and client_host.startswith("::ffff:"):
        client_host = client_host.split("::ffff:")[-1]
    ip_addr    = client_host or headers.get("x-forwarded-for") or "0.0.0.0"
    user_agent = headers.get("user-agent", "-")

    # 3) Decisão de envio do VC
    vc_time = int(time.time())
    allowed, reason, cnt = allow_viewcontent(ip_addr, user_agent, utm_value)

    fbc_val = build_fbc_from_fbclid(fbclid, creation_ts=vc_time)
    user_data_vc: Dict[str, Any] = {"client_ip_address": ip_addr, "client_user_agent": user_agent}
    if fbp_cookie: user_data_vc["fbp"] = fbp_cookie
    if fbc_val:    user_data_vc["fbc"] = fbc_val

    capi_vc_resp: Dict[str, Any] = {"skipped": True, "reason": reason}
    if allowed:
        try:
            capi_vc_resp = send_fb_event(
                "ViewContent",
                utm_value,
                link,
                user_data_vc,
                {"content_type": "product"},
                vc_time
            )
        except Exception as e:
            capi_vc_resp = {"error": str(e)}

    # 4) Salva no Redis
    save_user_data(utm_value, {
        "user_data": user_data_vc,
        "event_source_url": link,
        "vc_time": vc_time,
        "allowed_vc": allowed,
        "reason": reason,
        "count_in_window": cnt
    })

    # 5) Short link
    try:
        short_link = generate_short_link(link, utm_value)
        dest = short_link
        print("[VC] short_link_ok utm=", utm_value, " | dest=", dest)
    except Exception as e:
        print(f"[ShopeeShortLink] Falha: {e}. Fallback para URL original.")
        dest = replace_utm_content_only(link, utm_value)
        print("[VC] short_link_fallback utm=", utm_value, " | dest=", dest)

    # LOG
    print("[VC] utm=", utm_value,
          "| vc_time=", vc_time,
          "| fbc=", fbc_val,
          "| fbp=", fbp_cookie,
          "| ip=", ip_addr,
          "| ua=", (user_agent[:160] + "..." if len(user_agent) > 160 else user_agent),
          "| allowed=", allowed,
          "| reason=", reason,
          "| count_window=", cnt,
          "| link=", link,
          "| capi_resp=", capi_vc_resp)

    if (not allowed) and EMIT_INTERNAL_BLOCK_LOG:
        print("[BLOCKED_VC]", json.dumps({
            "utm": utm_value, "ip": ip_addr, "ua": user_agent, "reason": reason, "cnt": cnt
        }, ensure_ascii=False))

    # 6) Direcionamento:
    #    - TikTok webview => HTML bridge que tenta abrir app (universal link + intent://)
    #    - Caso contrário => 302 normal para o shortlink/URL Shopee
    ua_lower = user_agent.lower()
    is_tiktok = ("tiktok" in ua_lower) or ("ttwebview" in ua_lower)

    if is_tiktok:
        html = _tiktok_bridge_html(dest)
        return HTMLResponse(content=html, status_code=200)

    return RedirectResponse(dest, status_code=302)

@app.post("/upload_csv")
async def upload_csv(file: UploadFile = File(...)):
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
            print("[PURCHASE] skipped_no_utm row=", row)
            continue

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

        cache = load_user_data(utm)
        if not cache or not cache.get("user_data"):
            processed.append({"utm_content": raw_utm, "utm_norm": utm, "status": "skipped_no_user_data"})
            print("[PURCHASE] skipped_no_user_data utm=", utm, "| row=", row)
            continue

        user_data_purchase = cache["user_data"]
        event_source_url   = cache.get("event_source_url") or DEFAULT_PRODUCT_URL
        vc_time            = cache.get("vc_time")
        event_time = int(vc_time) if isinstance(vc_time, int) else now_ts

        if event_time > now_ts:
            event_time = now_ts
        if event_time < min_allowed:
            event_time = min_allowed
        click_ts = fbc_creation_ts(user_data_purchase.get("fbc"))
        if click_ts and event_time < click_ts:
            event_time = click_ts + 1

        if cache.get("allowed_vc") is False:
            processed.append({"utm_content": raw_utm, "utm_norm": utm, "status": "skipped_blocked_vc"})
            print("[PURCHASE] skipped_blocked_vc utm=", utm)
            continue

        custom_data_purchase = {"currency": "BRL", "value": valor, "num_purchases": vendas}

        print("[PURCHASE] utm=", utm,
              "| value=", valor,
              "| num_purchases=", vendas,
              "| event_time=", event_time,
              "| fbc=", user_data_purchase.get("fbc"),
              "| fbp=", user_data_purchase.get("fbp"),
              "| source_url=", event_source_url)

        try:
            resp = send_fb_event("Purchase", utm, event_source_url, user_data_purchase, custom_data_purchase, event_time)
            processed.append({"utm_content": raw_utm, "utm_norm": utm, "status": "sent", "capi": resp})
            print("[PURCHASE] sent utm=", utm, "| capi_resp=", resp)
        except Exception as e:
            processed.append({"utm_content": raw_utm, "utm_norm": utm, "status": "error", "error": str(e)})
            print("[PURCHASE] error utm=", utm, "| error=", str(e))

    return JSONResponse({"processed": processed})

# ───────────────────────────── ENDPOINTS ADMIN ─────────────────────────────

def _require_admin(token: str):
    if token != ADMIN_TOKEN:
        raise ValueError("unauthorized")

@app.get("/admin/lists")
def admin_lists(token: str):
    try:
        _require_admin(token)
        return {"ok": True, "lists": LISTS.dump()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=401)

@app.post("/admin/whitelist/ip")
def admin_add_whitelist_ip(ip: str = Body(..., embed=True), token: str = Body(..., embed=True)):
    try:
        _require_admin(token)
        LISTS.add_whitelist_ip(ip)
        return {"ok": True, "added": ip, "lists": LISTS.dump()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=401)

@app.post("/admin/blacklist/ip")
def admin_add_blacklist_ip(ip: str = Body(..., embed=True), token: str = Body(..., embed=True)):
    try:
        _require_admin(token)
        LISTS.add_blacklist_ip(ip)
        return {"ok": True, "added": ip, "lists": LISTS.dump()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=401)

@app.post("/admin/whitelist/cidr")
def admin_add_whitelist_cidr(cidr: str = Body(..., embed=True), token: str = Body(..., embed=True)):
    try:
        _require_admin(token)
        LISTS.add_whitelist_cidr(cidr)
        return {"ok": True, "added": cidr, "lists": LISTS.dump()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=401)

@app.post("/admin/blacklist/cidr")
def admin_add_blacklist_cidr(cidr: str = Body(..., embed=True), token: str = Body(..., embed=True)):
    try:
        _require_admin(token)
        LISTS.add_blacklist_cidr(cidr)
        return {"ok": True, "added": cidr, "lists": LISTS.dump()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=401)

@app.post("/admin/whitelist/ua")
def admin_add_whitelist_ua(substr: str = Body(..., embed=True), token: str = Body(..., embed=True)):
    try:
        _require_admin(token)
        LISTS.add_whitelist_ua(substr)
        return {"ok": True, "added": substr, "lists": LISTS.dump()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=401)

@app.post("/admin/blacklist/ua")
def admin_add_blacklist_ua(substr: str = Body(..., embed=True), token: str = Body(..., embed=True)):
    try:
        _require_admin(token)
        LISTS.add_blacklist_ua(substr)
        return {"ok": True, "added": substr, "lists": LISTS.dump()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=401)

@app.get("/admin/fp_status")
def admin_fp_status(ip: str, ua: str, token: str):
    try:
        _require_admin(token)
        fp = make_fingerprint(ip, ua)
        key = fp_counter_key(fp)
        val = r.get(key)
        ttl = r.ttl(key)
        return {"ok": True, "fingerprint": fp, "count": int(val) if val else 0, "ttl": ttl}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=401)

@app.post("/admin/fp_reset")
def admin_fp_reset(ip: str = Body(..., embed=True), ua: str = Body(..., embed=True), token: str = Body(..., embed=True)):
    try:
        _require_admin(token)
        fp = make_fingerprint(ip, ua)
        key = fp_counter_key(fp)
        r.delete(key)
        return {"ok": True, "reset_fp": fp}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=401)

@app.get("/admin/config")
def admin_config(token: str):
    try:
        _require_admin(token)
        return {
            "ok": True,
            "config": {
                "CLICK_WINDOW_SECONDS": CLICK_WINDOW_SECONDS,
                "MAX_CLICKS_PER_FP": MAX_CLICKS_PER_FP,
                "USERDATA_TTL_SECONDS": USERDATA_TTL_SECONDS,
                "MAX_DELAY_SECONDS": MAX_DELAY_SECONDS,
                "VIDEO_ID": VIDEO_ID,
                "DEFAULT_PRODUCT_URL": DEFAULT_PRODUCT_URL,
                "FB_PIXEL_ID": FB_PIXEL_ID,
                "SHOPEE_APP_ID": SHOPEE_APP_ID,
            }
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=401)

# ───────────────────────────── UVICORN (local) ─────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
