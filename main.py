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
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse

# ───────────────────────────── CONFIG ─────────────────────────────
DEFAULT_PRODUCT_URL = os.getenv("DEFAULT_PRODUCT_URL",
    "https://shopee.com.br/XEIJAIYI-8pcs-Kit-De-Gel-De-Extens%C3%A3o-De-Unhas-De-Polietileno-15ml-Nude-Pink-All-In-One-Construtor-Cola-Com-Formas-Duplas-Clipes-Manicure-Set-For-Beginnerer-i.1006215031.25062459693?utm_content=----")

FB_PIXEL_ID     = os.getenv("FB_PIXEL_ID", "COLOQUE_SEU_PIXEL_ID_AQUI")
FB_ACCESS_TOKEN = os.getenv("FB_ACCESS_TOKEN", "COLOQUE_SEU_ACCESS_TOKEN_AQUI")
FB_ENDPOINT     = f"https://graph.facebook.com/v14.0/{FB_PIXEL_ID}/events?access_token={FB_ACCESS_TOKEN}"

SHOPEE_APP_ID     = os.getenv("SHOPEE_APP_ID", "18314810331")
SHOPEE_APP_SECRET = os.getenv("SHOPEE_APP_SECRET", "LO3QSEG45TYP4NYQBRXLA2YYUL3ZCUPN")
SHOPEE_ENDPOINT   = "https://open-api.affiliate.shopee.com.br/graphql"

VIDEO_ID     = os.getenv("VIDEO_ID", "v15")
REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379/0")
COUNTER_KEY  = os.getenv("UTM_COUNTER_KEY", "utm_counter")

USERDATA_TTL_SECONDS = int(os.getenv("USERDATA_TTL_SECONDS", "604800"))
USERDATA_KEY_PREFIX  = os.getenv("USERDATA_KEY_PREFIX", "ud:")
MAX_DELAY_SECONDS    = int(os.getenv("MAX_DELAY_SECONDS", str(7 * 24 * 60 * 60)))

CLICK_WINDOW_SECONDS = int(os.getenv("CLICK_WINDOW_SECONDS", "3600"))
MAX_CLICKS_PER_FP    = int(os.getenv("MAX_CLICKS_PER_FP", "2"))
FINGERPRINT_PREFIX   = os.getenv("FINGERPRINT_PREFIX", "fp:")

EMIT_INTERNAL_BLOCK_LOG = True
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "troque_este_token_admin")

# ───────────────────────────── APP / REDIS ─────────────────────────────
r = redis.from_url(REDIS_URL)
app = FastAPI(title="Shopee UTM + TikTok Bridge")

# ───────────────────────────── HELPERS ─────────────────────────────
def incr_and_make_utm() -> str:
    count = r.incr(COUNTER_KEY)
    return f"{VIDEO_ID}n{count}"

def normalize_utm(u: Optional[str]) -> Optional[str]:
    return str(u).split("-")[0] if u else None

def get_cookie_value(cookie_header: Optional[str], name: str) -> Optional[str]:
    if not cookie_header: return None
    try:
        items = [c.strip() for c in cookie_header.split(";")]
        for it in items:
            if it.startswith(name + "="):
                return it.split("=", 1)[1]
    except: pass
    return None

def replace_utm_content_only(raw_url: str, new_value: str) -> str:
    parsed = urllib.parse.urlsplit(raw_url)
    parts = parsed.query.split("&") if parsed.query else []
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
    if not fbclid: return None
    if creation_ts is None: creation_ts = int(time.time())
    return f"fb.1.{creation_ts}.{fbclid}"

def save_user_data(utm: str, data: Dict[str, Any]) -> None:
    r.setex(f"{USERDATA_KEY_PREFIX}{utm}", USERDATA_TTL_SECONDS, json.dumps(data))

def load_user_data(utm: str) -> Optional[Dict[str, Any]]:
    raw = r.get(f"{USERDATA_KEY_PREFIX}{utm}")
    if not raw: return None
    try: return json.loads(raw)
    except: return None

def fbc_creation_ts(fbc: Optional[str]) -> Optional[int]:
    try: return int(fbc.split(".")[2]) if fbc else None
    except: return None

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
    return resp.json()["data"]["generateShortLink"]["shortLink"]

def send_fb_event(event_name: str, event_id: str, event_source_url: str,
                  user_data: Dict[str, Any], custom_data: Dict[str, Any],
                  event_time: int) -> Dict[str, Any]:
    payload = {"data": [{
        "event_name": event_name,
        "event_time": int(event_time),
        "event_id": event_id,
        "action_source": "website",
        "event_source_url": event_source_url,
        "user_data": user_data,
        "custom_data": custom_data
    }]}
    rqs = requests.post(FB_ENDPOINT, json=payload, timeout=20)
    try: return rqs.json()
    except: return {"status_code": rqs.status_code, "text": rqs.text}

# ───────────────────────────── LISTAS / ANTI-BOT ─────────────────────────────
def parse_device_os(ua: str) -> Tuple[str, str]:
    if not ua: return ("-","-")
    m = re.search(r"iPhone OS (\d+)", ua) or re.search(r"CPU iPhone OS (\d+)", ua)
    if m: return ("iOS", m.group(1))
    m = re.search(r"Android (\d+)", ua)
    if m: return ("Android", m.group(1))
    return ("iOS","-") if "iPhone" in ua else ("Android","-") if "Android" in ua else ("Other","-")

def make_fingerprint(ip: str, ua: str) -> str:
    osfam, osmaj = parse_device_os(ua)
    return hashlib.sha1(f"{ip}|{osfam}|{osmaj}".encode()).hexdigest()

def allow_viewcontent(ip: str, ua: str, utm: str) -> Tuple[bool,str,int]:
    fp = make_fingerprint(ip, ua)
    key = f"{FINGERPRINT_PREFIX}{fp}"
    cnt = r.incr(key)
    if cnt == 1: r.expire(key, CLICK_WINDOW_SECONDS)
    if cnt > MAX_CLICKS_PER_FP: return False,"rate_limited",cnt
    return True,"ok",cnt

# ───────────────────────────── BRIDGE PARA TIKTOK ─────────────────────────────
def _tiktok_bridge_html(target_url: str) -> str:
    """
    Mostra botão full-screen para captar gesto do usuário e abrir a Shopee.
    Ordem:
      1) Universal link (https://s.shopee.com.br/...)
      2) Android intent com com.shopee.br (Brasil)
      3) Android intent fallback com com.shopee.app (global)
    """
    safe_url = target_url  # não alterar parâmetros da Shopee
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Abrir no app da Shopee</title>
<style>
  :root{{--bg:#0b1526;--card:#111827;--txt:#e5e7eb;--btn:#ff4d00;}}
  *{{box-sizing:border-box}}
  body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);color:var(--txt);font-family:system-ui,-apple-system,Segoe UI,Roboto}}
  .wrap{{max-width:640px;width:92%;text-align:center;background:var(--card);padding:28px;border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.4)}}
  h1{{font-size:20px;margin:0 0 8px}}
  p{{opacity:.9;line-height:1.45;margin:0}}
  .btn{{display:inline-block;margin-top:18px;padding:16px 18px;border-radius:12px;background:var(--btn);color:#fff;text-decoration:none;font-weight:800;font-size:16px}}
  .muted{{opacity:.75;font-size:12px;margin-top:10px}}
</style>
<script>
(function(){{
  var targetUrl = {json.dumps(safe_url)};
  function openNow(){{
    try{{ window.location.href = targetUrl; }}catch(e){{}}
    var ua = navigator.userAgent || "";
    var isAndroid = /Android/i.test(ua);

    // Android: tenta pacote brasileiro primeiro
    if(isAndroid){{
      var intentBr = "intent://" + targetUrl.replace(/^https?:\\/\\//,"") +
                     "#Intent;scheme=https;package=com.shopee.br;S.browser_fallback_url=" +
                     encodeURIComponent(targetUrl) + ";end";
      setTimeout(function(){{ try{{ window.location.href = intentBr; }}catch(e){{}} }}, 120);

      // Fallback para pacote global
      var intentGlobal = "intent://" + targetUrl.replace(/^https?:\\/\\//,"") +
                         "#Intent;scheme=https;package=com.shopee.app;S.browser_fallback_url=" +
                         encodeURIComponent(targetUrl) + ";end";
      setTimeout(function(){{ try{{ window.location.href = intentGlobal; }}catch(e){{}} }}, 600);
    }}
  }}
  window._openShopee = openNow;
  // NÃO chama automaticamente — requer gesto do usuário (tap no botão)
}})();
</script>
</head>
<body>
  <div class="wrap">
    <h1>Abrir no app da Shopee</h1>
    <p>Toque no botão abaixo para abrir no app. Se não abrir, toque novamente.</p>
    <a class="btn" href="javascript:_openShopee()">Abrir no app da Shopee</a>
    <p class="muted">Dica: confirme “Abrir no app” se o TikTok solicitar.</p>
  </div>
</body>
</html>"""

# ───────────────────────────── ROTAS ─────────────────────────────
@app.get("/")
def redirect_to_shopee(request: Request,
    link: str = Query(DEFAULT_PRODUCT_URL, description="URL Shopee")):
    utm_value = incr_and_make_utm()
    headers = request.headers
    cookie_header = headers.get("cookie") or headers.get("Cookie")
    fbp_cookie = get_cookie_value(cookie_header,"_fbp")
    fbclid = request.query_params.get("fbclid")
    ip_addr = request.client.host if request.client else "0.0.0.0"
    user_agent = headers.get("user-agent","-")

    vc_time = int(time.time())
    allowed,reason,cnt = allow_viewcontent(ip_addr,user_agent,utm_value)
    fbc_val = build_fbc_from_fbclid(fbclid,vc_time)
    user_data_vc={"client_ip_address":ip_addr,"client_user_agent":user_agent}
    if fbp_cookie:user_data_vc["fbp"]=fbp_cookie
    if fbc_val:user_data_vc["fbc"]=fbc_val

    if allowed:
        try: send_fb_event("ViewContent",utm_value,link,user_data_vc,{"content_type":"product"},vc_time)
        except: pass

    save_user_data(utm_value,{
        "user_data":user_data_vc,"event_source_url":link,"vc_time":vc_time,
        "allowed_vc":allowed,"reason":reason,"count_in_window":cnt
    })

    try: dest = generate_short_link(link,utm_value)
    except: dest = replace_utm_content_only(link,utm_value)

    ua_lower = user_agent.lower()
    is_tiktok = ("tiktok" in ua_lower) or ("ttwebview" in ua_lower)

    if is_tiktok:
        # Em webview do TikTok: exigir gesto do usuário (botão) para abrir app
        return HTMLResponse(content=_tiktok_bridge_html(dest), status_code=200)

    # Fora do TikTok: redirect normal
    return RedirectResponse(dest, status_code=302)

# ───────────────────────────── RUN LOCAL ─────────────────────────────
if __name__=="__main__":
    import uvicorn
    uvicorn.run("main:app",host="0.0.0.0",port=int(os.getenv("PORT","10000")),reload=False)
