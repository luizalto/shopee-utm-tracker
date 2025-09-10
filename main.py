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
from fastapi.responses import RedirectResponse, JSONResponse

# ───────────────────────────── CONFIG ─────────────────────────────
DEFAULT_PRODUCT_URL = os.getenv("DEFAULT_PRODUCT_URL", "https://shopee.com.br/")
# Meta (Conversions API)
FB_PIXEL_ID     = os.getenv("FB_PIXEL_ID") or os.getenv("META_PIXEL_ID") or "COLOQUE_SEU_PIXEL_ID_AQUI"
FB_ACCESS_TOKEN = os.getenv("FB_ACCESS_TOKEN") or os.getenv("META_ACCESS_TOKEN") or "COLOQUE_SEU_ACCESS_TOKEN_AQUI"
FB_ENDPOINT     = f"https://graph.facebook.com/v14.0/{FB_PIXEL_ID}/events?access_token={FB_ACCESS_TOKEN}"
# Shopee Affiliate
SHOPEE_APP_ID     = os.getenv("SHOPEE_APP_ID", "18314810331")
SHOPEE_APP_SECRET = os.getenv("SHOPEE_APP_SECRET", "LO3QSEG45TYP4NYQBRXLA2YYUL3ZCUPN")
SHOPEE_ENDPOINT   = "https://open-api.affiliate.shopee.com.br/graphql"

VIDEO_ID = os.getenv("VIDEO_ID", "v15")

# Redis
REDIS_URL   = os.getenv("REDIS_URL", "redis://localhost:6379/0")
COUNTER_KEY = os.getenv("UTM_COUNTER_KEY", "utm_counter")

# Storage de clique/usuário (7 dias)
USERDATA_TTL_SECONDS = int(os.getenv("USERDATA_TTL_SECONDS", "604800"))
USERDATA_KEY_PREFIX  = os.getenv("USERDATA_KEY_PREFIX", "ud:")

# Janela máxima para compras atrasadas (7 dias)
MAX_DELAY_SECONDS = int(os.getenv("MAX_DELAY_SECONDS", str(7 * 24 * 60 * 60)))

# Anti-bot / repetição
CLICK_WINDOW_SECONDS = int(os.getenv("CLICK_WINDOW_SECONDS", "3600"))
MAX_CLICKS_PER_FP    = int(os.getenv("MAX_CLICKS_PER_FP", "2"))
FINGERPRINT_PREFIX   = os.getenv("FINGERPRINT_PREFIX", "fp:")

EMIT_INTERNAL_BLOCK_LOG = True

# Segurança admin
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "troque_este_token_admin")

# ── Scoring (consome regras/modelo já treinados) ───────────────────────────
ATC_BASELINE_RATE = float(os.getenv("ATC_BASELINE_RATE", "0.05"))
ATC_THRESHOLD     = float(os.getenv("ATC_THRESHOLD", "0.08"))
ATC_MAX_RATE      = float(os.getenv("ATC_MAX_RATE", "0.25"))
RULES_KEY         = os.getenv("RULES_KEY", "rules:hour_category")  # JSON
MODEL_KEY         = os.getenv("MODEL_KEY", "model:logreg")         # pickle bytes
MODEL_META_KEY    = os.getenv("MODEL_META_KEY", "model:meta")

# ───────────────────────────── APP / REDIS ─────────────────────────────
r = redis.from_url(REDIS_URL)
app = FastAPI(title="Shopee UTM + Meta CAPI (scoring by uploaded rules/model)")

# ───────────────────────────── HELPERS ─────────────────────────────
def incr_and_make_utm() -> str:
    return f"{VIDEO_ID}n{r.incr(COUNTER_KEY)}"

def normalize_utm(u: Optional[str]) -> Optional[str]:
    return str(u).split("-")[0] if u else None

def get_cookie_value(cookie_header: Optional[str], name: str) -> Optional[str]:
    if not cookie_header:
        return None
    try:
        for it in [c.strip() for c in cookie_header.split(";")]:
            if it.startswith(name + "="):
                return it.split("=", 1)[1]
    except Exception:
        pass
    return None

def replace_utm_content_only(raw_url: str, new_value: str) -> str:
    parsed = urllib.parse.urlsplit(raw_url)
    parts = parsed.query.split("&") if parsed.query else []
    for i, part in enumerate(parts):
        if part.startswith("utm_content="):
            parts[i] = "utm_content=" + new_value
            break
    else:
        parts.append("utm_content=" + new_value)
    new_query = "&".join([p for p in parts if p])
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
    except Exception: return None

def fbc_creation_ts(fbc: Optional[str]) -> Optional[int]:
    if not fbc: return None
    try: return int(fbc.split(".")[2])
    except Exception: return None

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
    ts = str(int(time.time()))
    signature = hashlib.sha256((SHOPEE_APP_ID + ts + payload + SHOPEE_APP_SECRET).encode("utf-8")).hexdigest()
    headers = {"Authorization": f"SHA256 Credential={SHOPEE_APP_ID}, Timestamp={ts}, Signature={signature}",
               "Content-Type": "application/json"}
    resp = requests.post(SHOPEE_ENDPOINT, headers=headers, data=payload, timeout=20)
    resp.raise_for_status()
    return resp.json()["data"]["generateShortLink"]["shortLink"]

def send_fb_event(event_name: str, event_id: str, event_source_url: str,
                  user_data: Dict[str, Any], custom_data: Dict[str, Any], event_time: int) -> Dict[str, Any]:
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
    try: return rqs.json()
    except Exception: return {"status_code": rqs.status_code, "text": rqs.text}

# ───────────────────────────── Anti-bot / fingerprint ───────────────────────
def parse_device_os(ua: str) -> Tuple[str, str]:
    if not ua: return ("-", "-")
    m = re.search(r"iPhone OS (\d+)_?", ua) or re.search(r"CPU iPhone OS (\d+)", ua)
    if m: return ("iOS", m.group(1))
    m = re.search(r"Android (\d+)", ua)
    if m: return ("Android", m.group(1))
    if "iPhone" in ua or "iPad" in ua: return ("iOS", "-")
    if "Android" in ua: return ("Android", "-")
    return ("Other", "-")

def make_fingerprint(ip: str, ua: str) -> str:
    osfam, osmaj = parse_device_os(ua)
    return hashlib.sha1(f"{ip}|{osfam}|{osmaj}".encode("utf-8")).hexdigest()

def fp_counter_key(fp: str) -> str:
    return f"{FINGERPRINT_PREFIX}{fp}"

def allow_viewcontent(ip: str, ua: str, utm: str) -> Tuple[bool, str, int]:
    # (listas omitidas para brevidade; pode integrar se quiser)
    fp = make_fingerprint(ip, ua)
    key = fp_counter_key(fp)
    cnt = r.incr(key)
    if cnt == 1:
        r.expire(key, CLICK_WINDOW_SECONDS)
    if cnt > MAX_CLICKS_PER_FP:
        return False, "rate_limited", int(cnt)
    return True, "ok", int(cnt)

# ───────────────────────────── Regras / Modelo (consumo) ───────────────────
def save_rules(rules: Dict[str, Any]) -> None:
    r.set(RULES_KEY, json.dumps(rules, ensure_ascii=False))

def load_rules() -> Dict[str, Any]:
    raw = r.get(RULES_KEY)
    if not raw:
        return {"by_hour": {}, "by_category": {}, "trained_at": None, "global_rate": ATC_BASELINE_RATE}
    try: return json.loads(raw)
    except Exception: return {"by_hour": {}, "by_category": {}, "trained_at": None, "global_rate": ATC_BASELINE_RATE}

def save_model_pickle_bytes(b: bytes) -> None:
    r.set(MODEL_KEY, b)
    r.set(MODEL_META_KEY, json.dumps({"saved_at": int(time.time())}))

def load_model_pickle_bytes() -> Optional[bytes]:
    raw = r.get(MODEL_KEY)
    return bytes(raw) if raw else None

def _norm_category(cat: Optional[str]) -> str:
    return (cat or "").strip().lower()

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def score_click_prob_rules(hour: Optional[int], category: Optional[str]) -> float:
    rules = load_rules()
    base = rules.get("global_rate", ATC_BASELINE_RATE) or ATC_BASELINE_RATE
    hfac = float(rules.get("by_hour", {}).get(str(int(hour)) if hour is not None else "", 1.0)) if hour is not None else 1.0
    cfac = float(rules.get("by_category", {}).get(_norm_category(category), 1.0)) if category else 1.0
    return _clamp(base * hfac * cfac, 0.0, ATC_MAX_RATE)

def model_predict_proba(features: Dict[str, Any]) -> Optional[float]:
    raw = load_model_pickle_bytes()
    if not raw: return None
    try:
        import pickle, numpy as np
        model = pickle.loads(raw)
        hour = int(features.get("hour")) if features.get("hour") is not None else -1
        cat  = _norm_category(features.get("category"))
        gr   = float(features.get("global_rate", ATC_BASELINE_RATE))
        x = [gr] + [1.0 if h==hour else 0.0 for h in range(24)]
        hv=0
        for ch in cat: hv=(hv*131+ord(ch))%(10**9+7)
        bucket=hv%5
        x += [1.0 if b==bucket else 0.0 for b in range(5)]
        return float(model.predict_proba([x])[0][1])
    except Exception as e:
        print("[MODEL] predict error:", e)
        return None

def score_click_prob(hour: Optional[int], category: Optional[str]) -> float:
    rules = load_rules()
    p_model = model_predict_proba({"hour": hour, "category": category, "global_rate": rules.get("global_rate", ATC_BASELINE_RATE)})
    return _clamp(p_model, 0.0, ATC_MAX_RATE) if p_model is not None else score_click_prob_rules(hour, category)

def maybe_send_add_to_cart(event_id: str, event_source_url: str, user_data: Dict[str, Any],
                           subid1: Optional[str], category: Optional[str], score_p: float) -> Optional[Dict[str, Any]]:
    if score_p < ATC_THRESHOLD: return None
    try:
        resp = send_fb_event(
            "AddToCart", event_id, event_source_url, user_data,
            {
                "content_category": category or "",
                "content_ids": [subid1 or "na"],
                "contents": [{"id": subid1 or "na", "quantity": 1}],
                "currency": "BRL",
                "value": 0
            },
            int(time.time())
        )
        print("[ATC] sent", {"event_id": event_id, "p": score_p, "resp": resp})
        return resp
    except Exception as e:
        print("[ATC] error", {"event_id": event_id, "p": score_p, "error": str(e)})
        return {"error": str(e)}

# ───────────────────────────── ROUTES ─────────────────────────────
@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time()), "pixel": FB_PIXEL_ID, "video_id": VIDEO_ID}

@app.get("/version")
def version():
    return {"name": "utm-meta-capi", "version": "3.0.0-upload-only"}

@app.get("/")
def redirect_to_shopee(
    request: Request,
    link: str = Query(DEFAULT_PRODUCT_URL, description="URL completa da Shopee"),
    cat: Optional[str] = Query(None, description="Categoria para scoring (use seu SubID-3)"),
):
    """
    1) Gera UTM único
    2) Anti-bot e envia ViewContent (se permitido)
    3) Calcula probabilidade (regras/modelo CARREGADOS) e, se >= THRESHOLD, envia AddToCart simulado
    4) Salva user_data no Redis
    5) Gera short link e redireciona
    """
    utm_value = incr_and_make_utm()

    headers = request.headers
    cookie_header = headers.get("cookie") or headers.get("Cookie")
    fbp_cookie = get_cookie_value(cookie_header, "_fbp")
    fbclid     = request.query_params.get("fbclid")

    client_host = request.client.host if request.client else None
    if client_host and client_host.startswith("::ffff:"):
        client_host = client_host.split("::ffff:")[-1]
    ip_addr    = client_host or headers.get("x-forwarded-for") or "0.0.0.0"
    user_agent = headers.get("user-agent", "-")

    vc_time = int(time.time())
    allowed, reason, cnt = allow_viewcontent(ip_addr, user_agent, utm_value)

    fbc_val = build_fbc_from_fbclid(fbclid, creation_ts=vc_time)
    user_data_vc: Dict[str, Any] = {"client_ip_address": ip_addr, "client_user_agent": user_agent}
    if fbp_cookie: user_data_vc["fbp"] = fbp_cookie
    if fbc_val:    user_data_vc["fbc"] = fbc_val

    capi_vc_resp: Dict[str, Any] = {"skipped": True, "reason": reason}
    if allowed:
        try:
            capi_vc_resp = send_fb_event("ViewContent", utm_value, link, user_data_vc, {"content_type": "product"}, vc_time)
        except Exception as e:
            capi_vc_resp = {"error": str(e)}

    # Scoring e ATC
    try:
        hour_now = time.localtime(vc_time).tm_hour
        atc_p = score_click_prob(hour_now, cat)
        atc_resp = maybe_send_add_to_cart(utm_value, link, user_data_vc, None, cat, atc_p)
    except Exception as e:
        atc_p = None
        atc_resp = {"error": str(e)}

    save_user_data(utm_value, {
        "user_data": user_data_vc, "event_source_url": link, "vc_time": vc_time,
        "allowed_vc": allowed, "reason": reason, "count_in_window": cnt,
        "atc_prob": atc_p, "atc_resp": atc_resp
    })

    try:
        dest = generate_short_link(link, utm_value)
    except Exception as e:
        print(f"[ShopeeShortLink] Falha: {e}. Fallback URL original.")
        dest = replace_utm_content_only(link, utm_value)

    print("[VC]", json.dumps({
        "utm": utm_value, "vc_time": vc_time, "ip": ip_addr,
        "ua": user_agent[:160], "allowed": allowed, "reason": reason,
        "cnt": cnt, "cat": cat, "atc_p": atc_p, "link": link, "vc_resp": capi_vc_resp
    }, ensure_ascii=False))

    if (not allowed) and EMIT_INTERNAL_BLOCK_LOG:
        print("[BLOCKED_VC]", json.dumps({"utm": utm_value, "ip": ip_addr, "ua": user_agent, "reason": reason, "cnt": cnt}, ensure_ascii=False))

    return RedirectResponse(dest, status_code=302)

# Importa CSV de compras reais (dispara Purchase)
@app.post("/upload_csv")
async def upload_csv(file: UploadFile = File(...)):
    content = await file.read()
    text = content.decode("utf-8", errors="replace").splitlines()
    reader = csv.DictReader(text)

    processed: List[Dict[str, Any]] = []
    now_ts = int(time.time())
    min_allowed = now_ts - MAX_DELAY_SECONDS

    for row in reader:
        # UTM/SubID-3 (variações comuns + case diferente)
        raw_utm = (
            row.get("utm_content") or row.get("utm") or
            row.get("sub_id3") or row.get("subid3") or row.get("sub_id_3") or
            row.get("Sub_id3") or row.get("SUBID3")
        )
        utm = normalize_utm(raw_utm)
        if not utm:
            processed.append({"row": row, "status": "skipped_no_utm"})
            continue

        valor_raw  = row.get("value") or row.get("valor") or row.get("price") or row.get("amount")
        vendas_raw = row.get("num_purchases") or row.get("vendas") or row.get("quantity") or row.get("qty") or row.get("purchases")

        try: valor = float(str(valor_raw).replace(",", ".")) if valor_raw not in (None, "") else 0.0
        except Exception: valor = 0.0
        try: vendas = int(float(vendas_raw)) if vendas_raw not in (None, "") else 1
        except Exception: vendas = 1

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

        try:
            resp = send_fb_event("Purchase", utm, event_source_url, user_data_purchase, custom_data_purchase, event_time)
            processed.append({"utm_content": raw_utm, "utm_norm": utm, "status": "sent", "capi": resp})
        except Exception as e:
            processed.append({"utm_content": raw_utm, "utm_norm": utm, "status": "error", "error": str(e)})

    return JSONResponse({"processed": processed})

# ───────────────────────────── ADMIN (sem treino) ───────────────────────────
def _require_admin(token: str):
    if token != ADMIN_TOKEN:
        raise ValueError("unauthorized")

@app.get("/admin/config")
def admin_config(token: str):
    try:
        _require_admin(token)
        return {"ok": True, "config": {
            "CLICK_WINDOW_SECONDS": CLICK_WINDOW_SECONDS,
            "MAX_CLICKS_PER_FP": MAX_CLICKS_PER_FP,
            "USERDATA_TTL_SECONDS": USERDATA_TTL_SECONDS,
            "MAX_DELAY_SECONDS": MAX_DELAY_SECONDS,
            "VIDEO_ID": VIDEO_ID,
            "DEFAULT_PRODUCT_URL": DEFAULT_PRODUCT_URL,
            "FB_PIXEL_ID": FB_PIXEL_ID,
            "SHOPEE_APP_ID": SHOPEE_APP_ID,
            "ATC_BASELINE_RATE": ATC_BASELINE_RATE,
            "ATC_THRESHOLD": ATC_THRESHOLD,
            "ATC_MAX_RATE": ATC_MAX_RATE
        }}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=401)

@app.get("/admin/rules")
def admin_get_rules(token: str):
    try:
        _require_admin(token)
        return {"ok": True, "rules": load_rules(), "baseline": ATC_BASELINE_RATE, "threshold": ATC_THRESHOLD}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=401)

@app.get("/admin/preview_score")
def admin_preview_score(token: str, hour: Optional[int] = None, category: Optional[str] = None):
    try:
        _require_admin(token)
        pr = score_click_prob_rules(hour, category)
        pf = score_click_prob(hour, category)
        return {"ok": True, "p_rules": pr, "p_final": pf, "hour": hour, "category": category}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=401)

# Recebe ARTEFATOS treinados localmente
@app.post("/admin/upload_rules")
async def admin_upload_rules(token: str = Query(...), file: UploadFile = File(...)):
    try:
        _require_admin(token)
        content = await file.read()
        rules = json.loads(content.decode("utf-8"))
        save_rules(rules)
        return {"ok": True, "msg": "Regras carregadas", "rules_snapshot": {"by_hour": len(rules.get("by_hour",{})), "by_category": len(rules.get("by_category",{}))}}
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Erro lendo regras: {e}"}, status_code=400)

@app.post("/admin/upload_model")
async def admin_upload_model(token: str = Query(...), file: UploadFile = File(...)):
    try:
        _require_admin(token)
        content = await file.read()
        save_model_pickle_bytes(content)
        return {"ok": True, "msg": "Modelo carregado"}
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Erro lendo modelo: {e}"}, status_code=400)

# ───────────────────────────── START ─────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), reload=False)
