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
    safe_url = target_url
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Abrindo Shopee…</title>
<style>
body{{font-family:system-ui;display:flex;align-items:center;justify-content:center;min-height:100vh;background:#0b1526;color:#fff}}
.card{{max-width:520px;width:92%;background:#111827;border-radius:16px;padding:24px;text-align:center}}
.btn{{margin-top:16px;padding:14px 18px;border-radius:12px;background:#ff4d00;color:#fff;text-decoration:none;font-weight:700;display:none}}
</style>
<script>
(function(){{
  var targetUrl = {json.dumps(safe_url)};
  function tryOpen(){{
    try{{window.location.href=targetUrl;}}catch(e){{}}
    var ua=navigator.userAgent||"",isAndroid=/Android/i.test(ua);
    var intentUrl="intent://"+targetUrl.replace(/^https?:\\/\\//,"")+"#Intent;scheme=https;package=com.shopee.app;S.browser_fallback_url="+encodeURIComponent(targetUrl)+";end";
    setTimeout(function(){{if(isAndroid)try{{window.location.href=intentUrl;}}catch(e){{}}}},900);
    setTimeout(function(){{document.getElementById('btn').style.display='inline-block';}},2000);
  }}
  window.reopenApp=function(){{tryOpen();}};
  document.addEventListener('DOMContentLoaded',tryOpen);
}})();
</script>
</head>
<body>
<div class="card">
  <h1>Abrindo no app da Shopee…</h1>
  <p>Se não abrir, toque abaixo:</p>
  <a id="btn" class="btn" href="javascript:reopenApp()">Abrir no app</a>
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

    ua_lower=user_agent.lower()
    if "tiktok" in ua_lower or "ttwebview" in ua_lower:
        return HTMLResponse(content=_tiktok_bridge_html(dest),status_code=200)
    return RedirectResponse(dest,status_code=302)

# ───────────────────────────── RUN LOCAL ─────────────────────────────
if __name__=="__main__":
    import uvicorn
    uvicorn.run("main:app",host="0.0.0.0",port=int(os.getenv("PORT","10000")),reload=False)
