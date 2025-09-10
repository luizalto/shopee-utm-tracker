# main.py
# -*- coding: utf-8 -*-

import os, re, json, time, hashlib, requests, urllib.parse, csv, ipaddress, redis
from typing import Optional, Dict, Any, List, Tuple
from fastapi import FastAPI, Request, Query, UploadFile, File, Body
from fastapi.responses import RedirectResponse, JSONResponse

# ───────────────────────────── CONFIG ─────────────────────────────
DEFAULT_PRODUCT_URL = os.getenv("DEFAULT_PRODUCT_URL", "https://shopee.com.br/")
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

MAX_DELAY_SECONDS = int(os.getenv("MAX_DELAY_SECONDS", str(7 * 24 * 60 * 60)))
CLICK_WINDOW_SECONDS   = int(os.getenv("CLICK_WINDOW_SECONDS", "3600"))
MAX_CLICKS_PER_FP      = int(os.getenv("MAX_CLICKS_PER_FP", "2"))
FINGERPRINT_PREFIX     = os.getenv("FINGERPRINT_PREFIX", "fp:")

EMIT_INTERNAL_BLOCK_LOG = True
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "troque_este_token_admin")

ATC_BASELINE_RATE   = float(os.getenv("ATC_BASELINE_RATE", "0.05"))
ATC_THRESHOLD       = float(os.getenv("ATC_THRESHOLD", "0.08"))
ATC_MAX_RATE        = float(os.getenv("ATC_MAX_RATE", "0.25"))
RULES_KEY           = os.getenv("RULES_KEY", "rules:hour_category")
MODEL_KEY           = os.getenv("MODEL_KEY", "model:logreg")
MODEL_META_KEY      = os.getenv("MODEL_META_KEY", "model:meta")

r = redis.from_url(REDIS_URL)
app = FastAPI(title="Shopee UTM + Meta CAPI Server")

# ───────────────────────────── HELPERS ─────────────────────────────
def incr_and_make_utm(): return f"{VIDEO_ID}n{r.incr(COUNTER_KEY)}"
def normalize_utm(u): return str(u).split("-")[0] if u else None
def _norm_category(cat): return (cat or "").strip().lower()
def _clamp(x, lo, hi): return max(lo, min(hi, x))

def save_rules(rules: Dict[str, Any]): r.set(RULES_KEY, json.dumps(rules, ensure_ascii=False))
def load_rules():
    raw = r.get(RULES_KEY)
    if not raw: return {"by_hour": {}, "by_category": {}, "trained_at": None, "global_rate": ATC_BASELINE_RATE}
    return json.loads(raw)

def save_model_pickle_bytes(b: bytes): r.set(MODEL_KEY, b); r.set(MODEL_META_KEY, json.dumps({"saved_at": int(time.time())}))
def load_model_pickle_bytes(): return bytes(r.get(MODEL_KEY)) if r.get(MODEL_KEY) else None

# ───────────────────────────── SCORING ─────────────────────────────
def score_click_prob_rules(hour: Optional[int], category: Optional[str]) -> float:
    rules = load_rules()
    base = rules.get("global_rate", ATC_BASELINE_RATE)
    hour_factor = float(rules.get("by_hour", {}).get(str(hour), 1.0)) if hour is not None else 1.0
    cat_factor  = float(rules.get("by_category", {}).get(_norm_category(category), 1.0)) if category else 1.0
    return _clamp(base * hour_factor * cat_factor, 0.0, ATC_MAX_RATE)

def model_predict_proba(features: Dict[str, Any]) -> Optional[float]:
    raw = load_model_pickle_bytes()
    if not raw: return None
    try:
        import pickle, numpy as np
        model = pickle.loads(raw)
        hour, cat, gr = features["hour"], _norm_category(features["category"]), features["global_rate"]
        x = [gr] + [1.0 if h==hour else 0.0 for h in range(24)]
        hv=0
        for ch in cat: hv=(hv*131+ord(ch))%(10**9+7)
        bucket=hv%5
        x += [1.0 if b==bucket else 0.0 for b in range(5)]
        return float(model.predict_proba([x])[0][1])
    except Exception as e:
        print("[MODEL] predict error:",e); return None

def score_click_prob(hour, category):
    rules = load_rules()
    p_model = model_predict_proba({"hour":hour,"category":category,"global_rate":rules.get("global_rate",ATC_BASELINE_RATE)})
    return _clamp(p_model,0.0,ATC_MAX_RATE) if p_model is not None else score_click_prob_rules(hour,category)

# ───────────────────────────── ADMIN ENDPOINTS ─────────────────────────────
@app.post("/admin/train_rules_from_csv")
async def admin_train_rules_from_csv(token: str = Query(...), file: UploadFile = File(...)):
    if token != ADMIN_TOKEN: return JSONResponse({"ok":False,"error":"unauthorized"},status_code=401)
    text = (await file.read()).decode("utf-8",errors="replace").splitlines()
    rows=list(csv.DictReader(text))
    if not rows: return {"ok":False,"error":"CSV vazio"}

    total_clicks=total_sales=0; by_hour={h:{"clicks":0,"sales":0} for h in range(24)}; by_cat={}
    from datetime import datetime

    for row in rows:
        subid3=row.get("subid3") or row.get("sub_id3")
        if not subid3: continue
        cat=_norm_category(subid3)

        status=(row.get("status") or row.get("order_status") or "").lower()
        is_sale=any(s in status for s in ["concluído","concluido","pago","paid","completed","complete"])

        click_ts=row.get("click_time") or row.get("click_date")
        hour=None
        if click_ts:
            for fmt in ("%Y-%m-%d %H:%M:%S","%d/%m/%Y %H:%M:%S","%Y-%m-%dT%H:%M:%S","%d/%m/%Y %H:%M"):
                try: hour=datetime.strptime(click_ts.strip()[:19],fmt).hour; break
                except: pass

        total_clicks+=1; total_sales+=is_sale
        if hour is not None: by_hour[hour]["clicks"]+=1; by_hour[hour]["sales"]+=is_sale
        if cat not in by_cat: by_cat[cat]={"clicks":0,"sales":0}
        by_cat[cat]["clicks"]+=1; by_cat[cat]["sales"]+=is_sale

    if total_clicks==0: return {"ok":False,"error":"Sem linhas válidas com subid3"}
    global_rate=total_sales/total_clicks
    hour_factors={str(h):float(_clamp((by_hour[h]["sales"]/by_hour[h]["clicks"] if by_hour[h]["clicks"] else global_rate)/global_rate,0.25,4.0)) for h in range(24)}
    cat_factors={c:float(_clamp((v["sales"]/v["clicks"] if v["clicks"] else global_rate)/global_rate,0.25,4.0)) for c,v in by_cat.items()}
    rules={"by_hour":hour_factors,"by_category":cat_factors,"trained_at":int(time.time()),"global_rate":global_rate}
    save_rules(rules)
    return {"ok":True,"trained":{"rows":len(rows),"global_rate":global_rate}}

@app.post("/admin/train_model_from_csv")
async def admin_train_model_from_csv(token: str = Query(...), file: UploadFile = File(...)):
    if token != ADMIN_TOKEN: return JSONResponse({"ok":False,"error":"unauthorized"},status_code=401)
    try: import numpy as np; from sklearn.linear_model import LogisticRegression; import pickle
    except: return JSONResponse({"ok":False,"error":"Instale scikit-learn e numpy"},status_code=400)

    text = (await file.read()).decode("utf-8",errors="replace").splitlines()
    rows=list(csv.DictReader(text))
    if not rows: return {"ok":False,"error":"CSV vazio"}

    X,y,total_clicks,total_sales=[],[],0,0; from datetime import datetime
    for row in rows:
        subid3=row.get("subid3") or row.get("sub_id3")
        if not subid3: continue
        cat=_norm_category(subid3)

        status=(row.get("status") or row.get("order_status") or "").lower()
        is_sale=1 if any(s in status for s in ["concluído","concluido","pago","paid","completed","complete"]) else 0

        click_ts=row.get("click_time") or row.get("click_date"); hour=-1
        if click_ts:
            for fmt in ("%Y-%m-%d %H:%M:%S","%d/%m/%Y %H:%M:%S","%Y-%m-%dT%H:%M:%S","%d/%m/%Y %H:%M"):
                try: hour=datetime.strptime(click_ts.strip()[:19],fmt).hour; break
                except: pass

        feats=[0.05]+[1.0 if h==hour else 0.0 for h in range(24)]
        hv=0
        for ch in cat: hv=(hv*131+ord(ch))%(10**9+7)
        bucket=hv%5; feats+=[1.0 if b==bucket else 0.0 for b in range(5)]
        X.append(feats); y.append(is_sale); total_clicks+=1; total_sales+=is_sale

    if not X: return {"ok":False,"error":"Sem linhas válidas com subid3"}
    global_rate=total_sales/total_clicks
    model=LogisticRegression(max_iter=200).fit(np.array(X),np.array(y))
    import pickle; save_model_pickle_bytes(pickle.dumps(model))
    rules=load_rules(); rules["global_rate"]=global_rate; save_rules(rules)
    return {"ok":True,"trained":{"rows":len(rows),"global_rate":global_rate}}

# ───────────────────────────── Upload de Regras/Modelo já prontos ─────────────────────────────
@app.post("/admin/upload_rules")
async def admin_upload_rules(token: str = Query(...), file: UploadFile = File(...)):
    if token != ADMIN_TOKEN: return JSONResponse({"ok":False,"error":"unauthorized"},status_code=401)
    try:
        rules=json.loads((await file.read()).decode("utf-8"))
        save_rules(rules); return {"ok":True,"msg":"Regras carregadas","rules":rules}
    except Exception as e: return JSONResponse({"ok":False,"error":str(e)},status_code=400)

@app.post("/admin/upload_model")
async def admin_upload_model(token: str = Query(...), file: UploadFile = File(...)):
    if token != ADMIN_TOKEN: return JSONResponse({"ok":False,"error":"unauthorized"},status_code=401)
    try:
        save_model_pickle_bytes(await file.read())
        return {"ok":True,"msg":"Modelo carregado"}
    except Exception as e: return JSONResponse({"ok":False,"error":str(e)},status_code=400)

# ───────────────────────────── START ─────────────────────────────
if __name__=="__main__":
    import uvicorn
    uvicorn.run("main:app",host="0.0.0.0",port=int(os.getenv("PORT","10000")),reload=False)
