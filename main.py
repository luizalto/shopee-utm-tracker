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
from typing import Optional, Dict, Any, List, Tuple

from fastapi import FastAPI, Request, Query, UploadFile, File, Body
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse

# ───────────────────────────── CONFIG ─────────────────────────────
DEFAULT_PRODUCT_URL = os.getenv("DEFAULT_PRODUCT_URL",
    "https://shopee.com.br/XEIJAIYI-8pcs-Kit-De-Gel-De-Extens%C3%A3o-De-Unhas-De-Polietileno-15ml-Nude-Pink-All-In-One-Construtor-Cola-Com-Formas-Duplas-Clipes-Manicure-Set-For-Beginnerer-i.1006215031.25062459693?utm_content=----")

FB_PIXEL_ID     = os.getenv("FB_PIXEL_ID", "TEST_PIXEL")
FB_ACCESS_TOKEN = os.getenv("FB_ACCESS_TOKEN", "TEST_TOKEN")
FB_ENDPOINT     = f"https://graph.facebook.com/v14.0/{FB_PIXEL_ID}/events?access_token={FB_ACCESS_TOKEN}"

SHOPEE_APP_ID     = os.getenv("SHOPEE_APP_ID", "18314810331")
SHOPEE_APP_SECRET = os.getenv("SHOPEE_APP_SECRET", "LO3QSEG45TYP4NYQBRXLA2YYUL3ZCUPN")
SHOPEE_ENDPOINT   = "https://open-api.affiliate.shopee.com.br/graphql"

VIDEO_ID     = os.getenv("VIDEO_ID", "v15")
COUNTER_KEY  = os.getenv("UTM_COUNTER_KEY", "utm_counter")

USERDATA_TTL_SECONDS = int(os.getenv("USERDATA_TTL_SECONDS", "604800"))  # 7 dias
USERDATA_KEY_PREFIX  = os.getenv("USERDATA_KEY_PREFIX", "ud:")
MAX_DELAY_SECONDS    = int(os.getenv("MAX_DELAY_SECONDS", str(7 * 24 * 60 * 60)))

CLICK_WINDOW_SECONDS = int(os.getenv("CLICK_WINDOW_SECONDS", "3600"))
MAX_CLICKS_PER_FP    = int(os.getenv("MAX_CLICKS_PER_FP", "2"))
FINGERPRINT_PREFIX   = os.getenv("FINGERPRINT_PREFIX", "fp:")

EMIT_INTERNAL_BLOCK_LOG = True
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "troque_este_token_admin")

# ───────────────────────────── REDIS (com fallback) ─────────────────────────────
class _MemoryStore:
    """Fallback em memória para ambientes sem Redis (anti-502)."""
    def __init__(self):
        self.kv: Dict[str, Any] = {}
        self.exp: Dict[str, int] = {}

    def _now(self) -> int:
        return int(time.time())

    def incr(self, key: str) -> int:
        self._gc()
        v = int(self.kv.get(key, "0"))
        v += 1
        self.kv[key] = str(v)
        return v

    def expire(self, key: str, ttl: int) -> None:
        self.exp[key] = self._now() + int(ttl)

    def ttl(self, key: str) -> int:
        self._gc()
        if key not in self.exp:
            return -1
        return max(0, self.exp[key] - self._now())

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.kv[key] = value
        self.exp[key] = self._now() + int(ttl)

    def get(self, key: str):
        self._gc()
        return self.kv.get(key)

    def delete(self, key: str):
        self.kv.pop(key, None)
        self.exp.pop(key, None)

    def _gc(self):
        now = self._now()
        for k in list(self.exp.keys()):
            if self.exp[k] <= now:
                self.kv.pop(k, None)
                self.exp.pop(k, None)

def _build_store():
    url = os.getenv("REDIS_URL")
    if not url:
        return _MemoryStore()
    try:
        import redis  # type: ignore
        return redis.from_url(url)
    except Exception as e:
        print("[WARN] Redis indisponível, usando memória:", str(e))
        return _MemoryStore()

r = _build_store()

# ───────────────────────────── APP ─────────────────────────────
app = FastAPI(title="Shopee UTM + Meta CAPI + TikTok Bridge (resiliente)")

# ───────────────────────────── HELPERS ─────────────────────────────
def incr_and_make_utm() -> str:
    try:
        count = r.incr(COUNTER_KEY)
    except Exception as e:
        print("[ERR] incr falhou, usando contador local:", e)
        # contador local de emergência
        local_key = "_local_counter"
        v = int(r.get(local_key) or "0")
        v += 1
        r.setex(local_key, 365*24*3600, str(v))
        count = v
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
    except:  # noqa
        pass
    return None

def replace_utm_content_only(raw_url: str, new_value: str) -> str:
    try:
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
        new_query = "&".join(p for p in parts if p)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))
    except Exception as e:
        print("[WARN] replace_utm_content_only falhou:", e)
        return raw_url

def build_fbc_from_fbclid(fbclid: Optional[str], creation_ts: Optional[int] = None) -> Optional[str]:
    if not fbclid: return None
    if creation_ts is None: creation_ts = int(time.time())
    return f"fb.1.{creation_ts}.{fbclid}"

def save_user_data(utm: str, data: Dict[str, Any]) -> None:
    try:
        r.setex(f"{USERDATA_KEY_PREFIX}{utm}", USERDATA_TTL_SECONDS, json.dumps(data))
    except Exception as e:
        print("[WARN] save_user_data falhou:", e)

def load_user_data(utm: str) -> Optional[Dict[str, Any]]:
    try:
        raw = r.get(f"{USERDATA_KEY_PREFIX}{utm}")
        if not raw: return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return json.loads(raw)
    except Exception as e:
        print("[WARN] load_user_data falhou:", e)
        return None

def fbc_creation_ts(fbc: Optional[str]) -> Optional[int]:
    try: return int(fbc.split(".")[2]) if fbc else None
    except: return None

def generate_short_link(origin_url: str, utm_content: str) -> str:
    try:
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
        resp = requests.post(SHOPEE_ENDPOINT, headers=headers, data=payload, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        return data["data"]["generateShortLink"]["shortLink"]
    except Exception as e:
        print("[WARN] generate_short_link falhou, usando fallback:", e)
        return replace_utm_content_only(origin_url, utm_content)

def send_fb_event(event_name: str, event_id: str, event_source_url: str,
                  user_data: Dict[str, Any], custom_data: Dict[str, Any],
                  event_time: int) -> Dict[str, Any]:
    try:
        payload = {"data": [{
            "event_name": event_name,
            "event_time": int(event_time),
            "event_id": event_id,
            "action_source": "website",
            "event_source_url": event_source_url,
            "user_data": user_data,
            "custom_data": custom_data
        }]}
        rqs = requests.post(FB_ENDPOINT, json=payload, timeout=10)
        return rqs.json()
    except Exception as e:
        print("[WARN] send_fb_event falhou:", e)
        return {"error": str(e)}

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

# listas vazias (pode popular via admin se quiser)
SEEDED_WHITELIST_IPS: List[str] = []
SEED_WHITELIST_CIDRS: List[str] = []
SEED_WHITELIST_UA_SUBSTR: List[str] = []
SEEDED_BLACKLIST_IPS: List[str] = []
SEED_BLACKLIST_CIDRS: List[str] = []
SEED_BLACKLIST_UA_SUBSTR: List[str] = []

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
    try:
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
    except Exception as e:
        print("[WARN] allow_viewcontent fallback allow:", e)
        return True, "fallback_allow", 0

# ───────────────────────────── BRIDGE PARA TIKTOK ─────────────────────────────
def _tiktok_bridge_html(target_url: str) -> str:
    """
    Exige gesto do usuário (tap) para abrir app. Tenta:
      1) universal link (https://s.shopee.com.br/...)
      2) intent com com.shopee.br
      3) intent com com.shopee.app
    """
    safe_url = target_url
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Abrir no app da Shopee</title>
<style>
  :root{{--bg:#0b1526;--card:#111827;--txt:#e5e7eb;--btn:#ff4d00}}
  *{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);color:var(--txt);font-family:system-ui,-apple-system,Segoe UI,Roboto}}
  .wrap{{max-width:640px;width:92%;text-align:center;background:var(--card);padding:28px;border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.4)}}
  h1{{font-size:20px;margin:0 0 8px}}p{{opacity:.9;line-height:1.45;margin:0}}
  .btn{{display:inline-block;margin-top:18px;padding:16px 18px;border-radius:12px;background:var(--btn);color:#fff;text-decoration:none;font-weight:800;font-size:16px}}
  .muted{{opacity:.75;font-size:12px;margin-top:10px}}
</style>
<script>
(function(){{
  var targetUrl = {json.dumps(safe_url)};
  function openNow(){{
    try{{ window.location.href = targetUrl; }}catch(e){{}}
    var ua = navigator.userAgent || ""; var isAndroid = /Android/i.test(ua);
    if(isAndroid){{
      var intentBr = "intent://" + targetUrl.replace(/^https?:\\/\\//,"") + "#Intent;scheme=https;package=com.shopee.br;S.browser_fallback_url=" + encodeURIComponent(targetUrl) + ";end";
      setTimeout(function(){{ try{{ window.location.href = intentBr; }}catch(e){{}} }}, 120);
      var intentGlobal = "intent://" + targetUrl.replace(/^https?:\\/\\//,"") + "#Intent;scheme=https;package=com.shopee.app;S.browser_fallback_url=" + encodeURIComponent(targetUrl) + ";end";
      setTimeout(function(){{ try{{ window.location.href = intentGlobal; }}catch(e){{}} }}, 600);
    }}
  }}
  window._openShopee = openNow;
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
@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time()), "pixel": FB_PIXEL_ID, "video_id": VIDEO_ID}

@app.get("/")
def redirect_to_shopee(
    request: Request,
    link: str = Query(DEFAULT_PRODUCT_URL, description="URL Shopee")
):
    try:
        utm_value = incr_and_make_utm()

        headers = request.headers
        cookie_header = headers.get("cookie") or headers.get("Cookie")
        fbp_cookie = get_cookie_value(cookie_header, "_fbp")
        fbclid = request.query_params.get("fbclid")
        ip_addr = request.client.host if request.client else "0.0.0.0"
        user_agent = headers.get("user-agent", "-")

        vc_time = int(time.time())
        allowed, reason, cnt = allow_viewcontent(ip_addr, user_agent, utm_value)
        fbc_val = build_fbc_from_fbclid(fbclid, vc_time)
        user_data_vc = {"client_ip_address": ip_addr, "client_user_agent": user_agent}
        if fbp_cookie: user_data_vc["fbp"] = fbp_cookie
        if fbc_val:    user_data_vc["fbc"] = fbc_val

        if allowed:
            send_fb_event("ViewContent", utm_value, link, user_data_vc, {"content_type": "product"}, vc_time)

        save_user_data(utm_value, {
            "user_data": user_data_vc,
            "event_source_url": link,
            "vc_time": vc_time,
            "allowed_vc": allowed,
            "reason": reason,
            "count_in_window": cnt
        })

        dest = generate_short_link(link, utm_value)

        ua_lower = user_agent.lower()
        is_tiktok = ("tiktok" in ua_lower) or ("ttwebview" in ua_lower)

        if is_tiktok:
            return HTMLResponse(content=_tiktok_bridge_html(dest), status_code=200)

        return RedirectResponse(dest, status_code=302)
    except Exception as e:
        # Nunca derruba o servidor
        print("[FATAL] erro no /:", e)
        return HTMLResponse("<h1>Serviço temporariamente indisponível.</h1>", status_code=200)

# ───────────────────────────── CSV → PURCHASE ─────────────────────────────
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
            continue

        valor_raw = row.get("value") or row.get("valor") or row.get("price") or row.get("amount")
        vendas_raw = row.get("num_purchases") or row.get("vendas") or row.get("quantity") or row.get("qty") or row.get("purchases")

        try: valor = float(str(valor_raw).replace(",", ".")) if valor_raw not in (None, "") else 0.0
        except: valor = 0.0
        try: vendas = int(float(vendas_raw)) if vendas_raw not in (None, "",) else 1
        except: vendas = 1

        cache = load_user_data(utm)
        if not cache or not cache.get("user_data"):
            processed.append({"utm_content": raw_utm, "utm_norm": utm, "status": "skipped_no_user_data"})
            continue

        user_data_purchase = cache["user_data"]
        event_source_url   = cache.get("event_source_url") or DEFAULT_PRODUCT_URL
        vc_time            = cache.get("vc_time")
        event_time = int(vc_time) if isinstance(vc_time, int) else now_ts

        if event_time > now_ts: event_time = now_ts
        if event_time < min_allowed: event_time = min_allowed
        click_ts = fbc_creation_ts(user_data_purchase.get("fbc"))
        if click_ts and event_time < click_ts: event_time = click_ts + 1

        if cache.get("allowed_vc") is False:
            processed.append({"utm_content": raw_utm, "utm_norm": utm, "status": "skipped_blocked_vc"})
            continue

        custom_data_purchase = {"currency": "BRL", "value": valor, "num_purchases": vendas}
        resp = send_fb_event("Purchase", utm, event_source_url, user_data_purchase, custom_data_purchase, event_time)
        processed.append({"utm_content": raw_utm, "utm_norm": utm, "status": "sent", "capi": resp})

    return JSONResponse({"processed": processed})

# ───────────────────────────── ADMIN (opcional) ─────────────────────────────
def _require_admin(token: str):
    if token != ADMIN_TOKEN:
        raise ValueError("unauthorized")

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

# ───────────────────────────── RUN LOCAL ─────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), reload=False)
