import os
import re
import time
import json
import random
import hashlib
import threading
import datetime

import redis
import requests

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from urllib.parse import urlsplit, urlunsplit, unquote

app = FastAPI()

# =============================
# CONFIG
# =============================

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

SHOPEE_APP_ID = os.getenv("SHOPEE_APP_ID")
SHOPEE_APP_SECRET = os.getenv("SHOPEE_APP_SECRET")
SHOPEE_ENDPOINT = "https://open-api.affiliate.shopee.com.br/graphql"

# META — envio server-side (CAPI) de PageView/ViewContent, com dedup por event_id
# (o navegador dispara os mesmos eventos com os MESMOS event_ids; o Meta deduplica)
FB_PIXEL_ID = "1182369083930935"
FB_ACCESS_TOKEN = "EAASMgIKKCoYBTGbQyZCXioYYZBASb6JH07sBC3af21rTPQIt0sqaGepsV4p6JxrrvzqKGWGmJGtyEHbjVVtKZB6q34GYXFSS7aZBv79RU25tlC1m8Vgi5TToHxsLwlIAC5UJrsOZAeVZCgEZA15ntXw4IydBnHJst2iZArgSqdAifFc2X5duS1VHWUO4ZBnh4bAZDZD"
FB_API_VERSION = "v21.0"

USE_SEQUENCE = os.getenv("USE_SEQUENCE", "true").lower() == "true"

SHEETS_URL = os.getenv("SHEETS_URL", "")

RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "30"))
POOL_MAX_TOTAL = int(os.getenv("POOL_MAX_TOTAL", "100000"))
POOL_TTL = int(os.getenv("POOL_TTL_SECONDS", "172800"))       # 48h
POOL_MIN = int(os.getenv("POOL_MIN_PER_UTM", "10"))
POOL_META_HOURS = int(os.getenv("POOL_META_HOURS", "4"))
POOL_MARGIN = int(os.getenv("POOL_MARGIN", "2"))
# Reservas por UTM: R dinâmico = pico de cliques/minuto observado (teto 500).
# O índice do rodízio amarra ao ordinal do clique DENTRO do minuto: cada
# RESERVA<k> só reencontra gente do seu minuto → 1 candidato por compra → ~100%.
RESERVA_MAX = int(os.getenv("RESERVA_MAX", "500"))
RESERVA_MIN = int(os.getenv("RESERVA_MIN", "5"))
# ORÇAMENTO CENTRALIZADO de chamadas à API da Shopee (compartilhado entre todas
# as instâncias do Render via Redis). O limitador da Shopee conta TENTATIVAS —
# chamada que falha por rate limit também consome a cota. Sem esse teto, as
# threads de reenchimento disparam centenas de chamadas/min e sufocam as
# gerações ao vivo (medido: 9.456 chamadas mortas/30min derrubaram tudo).
# Prioridade: atender clique ao vivo > criar reserva. Criação de reserva é
# barata em volume (5-40 por UTM, reusada por rodízio) — ao vivo é 1 chamada
# por clique sem reserva.
API_BUDGET_PER_MIN = int(os.getenv("API_BUDGET_PER_MIN", "20"))       # teto total/min (fleet)
RESERVE_BUDGET_PER_MIN = int(os.getenv("RESERVE_BUDGET_PER_MIN", "4")) # fatia para criar reservas
LIVE_BUDGET_PER_MIN = max(1, API_BUDGET_PER_MIN - RESERVE_BUDGET_PER_MIN)

def try_consume_budget(kind, limit):
    """Consome 1 unidade do orçamento `kind` do minuto corrente (fleet-wide).
    Retorna True se dentro do limite. 1 INCR atômico no Redis — seguro com
    várias instâncias."""
    try:
        k = f"apibudget:{kind}:{int(time.time()) // 60}"
        pipe = r.pipeline()
        pipe.incr(k)
        pipe.expire(k, 180)
        n, _ = pipe.execute()
        return n <= limit
    except Exception:
        return True  # sem Redis não trava o atendimento

def api_block(kind, seconds=15):
    """Disjuntor: depois de um rate limit, para de gastar chamadas por alguns
    segundos (o limitador da Shopee precisa respirar)."""
    try:
        r.setex(f"apiblock:{kind}", seconds, "1")
    except Exception:
        pass

def api_blocked(kind):
    try:
        return bool(r.exists(f"apiblock:{kind}"))
    except Exception:
        return False

def desired_reservas(utm_base):
    rcmax = int(r.get(f"rcmax:{utm_base}") or 0)
    return max(RESERVA_MIN, min(RESERVA_MAX, int(rcmax * 1.25) + 1))

r = redis.from_url(REDIS_URL)
session = requests.Session()
COUNTER_KEY = "utm_counter"
POOL_TOTAL_KEY = "pool:total"
BASE_INTERVAL = 60.0 / max(1, RATE_LIMIT_PER_MIN)

# =============================
# UTILS
# =============================

def sha256(s):
    return hashlib.sha256(s.encode()).hexdigest()

def gen_fbp(ts):
    return f"fb.1.{ts}.{random.randint(10**15, 10**16 - 1)}"

def gen_fbc(fbp, ts):
    return f"fb.1.{ts}.{sha256(fbp)[:16]}"

def gen_uid():
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(random.choice(chars) for _ in range(13))

def get_cookie(cookie_header, name):
    if not cookie_header:
        return None
    for c in cookie_header.split(";"):
        c = c.strip()
        if c.startswith(name + "="):
            return c.split("=")[1]
    return None

def next_number():
    return int(r.incr(COUNTER_KEY))

def clean(s):
    return "".join(c for c in str(s) if c.isalnum())

def extract_uid(uc):
    m = re.search(r"ID([a-z0-9]{13})", uc or "")
    if m:
        return m.group(1)
    return (uc or "")[-13:] if len(uc or "") >= 13 else (uc or "na")

def utm_prefix(uc):
    m = re.match(r"(N\d+[UT]\d+)", uc or "")
    return m.group(1) if m else (uc or "")[:16]

def set_utm(url, value):
    parts = urlsplit(url)
    query = parts.query.split("&") if parts.query else []
    replaced = False
    for i, q in enumerate(query):
        if q.startswith("utm_content="):
            query[i] = "utm_content=" + value
            replaced = True
    if not replaced:
        query.append("utm_content=" + value)
    new_query = "&".join(query)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))

# =============================
# SHOPEE
# =============================

class ShopeeApiError(Exception):
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, dict) else {"raw": str(payload)}
        super().__init__(json.dumps(self.payload, ensure_ascii=False)[:300])

def is_rate_limit(payload):
    try:
        for err in payload.get("errors", []):
            if "rate limit" in str(err).lower():
                return True
            if isinstance(err, dict) and err.get("extensions", {}).get("code") == 10030:
                return True
    except Exception:
        pass
    return False

def generate_short_link(origin_url, subid, retries=1, timeout_s=30):
    payload = {
        "query": f"""
        mutation {{
            generateShortLink(
                input:{{
                    originUrl:"{origin_url}",
                    subIds:["","","{subid}","",""]
                }}
            ){{
                shortLink
            }}
        }}
        """
    }
    payload = json.dumps(payload, separators=(",", ":"))
    last_err = None
    for attempt in range(retries + 1):
        ts = str(int(time.time()))
        signature = sha256(SHOPEE_APP_ID + ts + payload + SHOPEE_APP_SECRET)
        headers = {
            "Authorization": f"SHA256 Credential={SHOPEE_APP_ID}, Timestamp={ts}, Signature={signature}",
            "Content-Type": "application/json",
        }
        try:
            resp = session.post(SHOPEE_ENDPOINT, data=payload, headers=headers, timeout=timeout_s)
            j = resp.json()
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.4 * (attempt + 1))
            continue
        if "data" in j:
            return j["data"]["generateShortLink"]["shortLink"]
        err = ShopeeApiError(j)
        last_err = err
        if attempt < retries:
            time.sleep(0.4 * (attempt + 1))
            continue
        raise err
    raise last_err if last_err else Exception("shopee_unknown")

# =============================
# PISCINA
# =============================

def pool_key(utm_base):
    return f"pool:{utm_base}"

def lastlink_key(utm_base):
    return f"lastlink:{utm_base}"

def dlog_key(utm_base):
    return f"dlog:{utm_base}"

def pool_total():
    return int(r.get(POOL_TOTAL_KEY) or 0)

def push_pool(utm_base, entry):
    if pool_total() >= POOL_MAX_TOTAL:
        return False
    r.lpush(pool_key(utm_base), json.dumps(entry))
    r.incr(POOL_TOTAL_KEY)
    return True

def pop_pool(utm_base):
    key = pool_key(utm_base)
    for _ in range(50):
        raw = r.rpop(key)
        if not raw:
            return None
        try:
            entry = json.loads(raw)
        except Exception:
            continue
        r.decr(POOL_TOTAL_KEY)
        if time.time() - entry.get("ts", 0) <= POOL_TTL:
            return entry
    return None

# Tela de carregamento mostrada quando NENHUMA camada consegue atender:
# 3 pontinhos e nova tentativa automática com espera crescente — nunca erro.
LOADING_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Redirecionando…</title>
<style>
body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#fff;font-family:system-ui,sans-serif}
.dots span{display:inline-block;width:14px;height:14px;margin:0 5px;border-radius:50%;background:#555;animation:b 1.2s infinite ease-in-out}
.dots span:nth-child(2){animation-delay:.15s}.dots span:nth-child(3){animation-delay:.3s}
@keyframes b{0%,80%,100%{transform:scale(.5);opacity:.3}40%{transform:scale(1);opacity:1}}
p.msg{position:fixed;bottom:24px;width:100%;text-align:center;color:#999;font-size:13px}
</style></head>
<body><div class="dots"><span></span><span></span><span></span></div>
<p class="msg">Preparando seu link…</p>
<script>
var delays=[5000,10000,15000],i=0;
(function retry(){setTimeout(function(){location.reload()},i<delays.length?delays[i++]:20000)})();
</script></body></html>"""

def loading_response():
    return HTMLResponse(LOADING_HTML)

def create_planob_entry(link, utm_base, idx=1, retries=0, timeout_s=1.5):
    """Plano B: utm marcado como RESERVA<nn> — identificável no relatório da
    Shopee e com N reservas por UTM pra refinar o casamento por horário."""
    utm = f"{utm_prefix(utm_base)}RESERVA{idx:02d}"
    origin = set_utm(link, utm)
    short = generate_short_link(origin, utm, retries=retries, timeout_s=timeout_s)
    return {"short": short, "utm": utm, "ts": time.time()}

def read_planob(utm_base):
    raw = r.get(f"planob:{utm_base}")
    if not raw:
        return []
    try:
        lst = json.loads(raw)
    except Exception:
        return []
    return [e for e in lst if time.time() - e.get("ts", 0) <= 30 * 3600]

def ensure_planob(utm_base, link, count=5, retries=0, max_create=1):
    """Mantém `count` reservas da UTM. Cria no máximo `max_create` por chamada
    (ritmo controlado: criar várias de uma vez estoura o rate limit da API)."""
    lst = read_planob(utm_base)
    if len(lst) >= count and all(time.time() - e.get("ts", 0) < 24 * 3600 for e in lst):
        return
    criados = 0
    while len(lst) < count and criados < max_create:
        if api_blocked("reserva") or not try_consume_budget("reserva", RESERVE_BUDGET_PER_MIN):
            break
        try:
            lst.append(create_planob_entry(link, utm_base, idx=len(lst) + 1, retries=retries))
            criados += 1
        except Exception as e:
            print("Erro planob:", str(e)[:150])
            if "10030" in str(e) or "Rate limit" in str(e):
                api_block("reserva")
            break
    if lst:
        r.setex(f"planob:{utm_base}", 30 * 3600, json.dumps(lst))

def create_pool_entry(link, utm_base, retries=1, timeout_s=30):
    utm = f"{utm_prefix(utm_base)}ID{gen_uid()}S{next_number() if USE_SEQUENCE else ''}"
    origin = set_utm(link, utm)
    short = generate_short_link(origin, utm, retries=retries, timeout_s=timeout_s)
    return {"short": short, "utm": utm, "ts": time.time()}

# =============================
# ENCHADOR (produz links fora de pico / ritmo constante)
# =============================

def demand_meta(utm_base):
    now = time.time()
    window = POOL_META_HOURS * 3600
    try:
        stamps = r.lrange(dlog_key(utm_base), 0, -1)
        recent = sum(1 for t in stamps if now - float(t) <= window)
    except Exception:
        recent = 0
    return max(POOL_MIN, recent * POOL_MARGIN)

def filler_loop():
    """ENCHEDOR DE RESERVAS: mantém R dinâmico reservas por UTM ativa, dentro
    do orçamento 'reserva' (RESERVE_BUDGET_PER_MIN/min, compartilhado entre as
    instâncias via Redis). Prioriza a UTM mais quente primeiro: o orçamento é
    pequeno de propósito (criação é barata; ao vivo tem prioridade)."""
    time.sleep(30)  # espera o serviço aquecer
    while True:
        try:
            candidatos = []
            for key in r.scan_iter("dlog:*", count=500):
                utm_base = key.decode().split(":", 1)[1]
                if not r.exists(key):
                    r.delete(f"planob:{utm_base}")
                    continue
                link = r.get(lastlink_key(utm_base))
                if not link:
                    continue
                rcmax = int(r.get(f"rcmax:{utm_base}") or 0)
                candidatos.append((rcmax, utm_base, link.decode()))
            # mais quentes primeiro: se o orçamento do minuto acabar no meio,
            # quem mais precisa já foi atendido
            candidatos.sort(reverse=True)
            for rcmax, utm_base, link in candidatos:
                try:
                    ensure_planob(utm_base, link, count=desired_reservas(utm_base))
                except Exception:
                    pass
        except Exception as e:
            print("Erro filler:", str(e)[:200])
        time.sleep(20)

threading.Thread(target=filler_loop, daemon=True).start()

# =============================
# META (CAPI server-side, dedup por event_id)
# =============================

def send_meta_events(events, pixel_id):
    if not events:
        return
    try:
        url = f"https://graph.facebook.com/{FB_API_VERSION}/{pixel_id or FB_PIXEL_ID}/events"
        session.post(url, params={"access_token": FB_ACCESS_TOKEN}, json={"data": events}, timeout=10)
    except Exception as e:
        print("Erro CAPI Meta:", str(e)[:200])

def build_event(name, event_id, ts, fbp, fbc, ip, ua, external_id, utm):
    return {
        "event_name": name,
        "event_id": event_id,
        "event_time": ts,
        "action_source": "website",
        "user_data": {
            "fbc": fbc,
            "fbp": fbp,
            "client_ip_address": ip,
            "client_user_agent": ua,
            "external_id": sha256(external_id.lower().strip()),
        },
        "custom_data": {"content_name": utm, "content_type": "product"},
    }

# =============================
# PLANILHA (gravada pelo tracker, mesmo schema de sempre)
# =============================

def write_sheet_row(payload):
    if not SHEETS_URL:
        return
    try:
        session.post(SHEETS_URL, json=payload, timeout=8)
    except Exception as e:
        print("Erro planilha:", str(e)[:200])

# =============================
# CLICK
# =============================

@app.get("/{full_path:path}")
def click(request: Request, full_path: str):
    link = request.query_params.get("link")
    if not link and full_path.startswith("http"):
        link = full_path
    if not link:
        raise HTTPException(400, "missing link")
    link = unquote(link)

    ts = int(time.time())
    cookie = request.headers.get("cookie")
    ip = request.client.host
    ua = request.headers.get("user-agent", "")
    fbclid = request.query_params.get("fbclid")

    fbp = request.query_params.get("fbp") or get_cookie(cookie, "_fbp") or gen_fbp(ts)
    fbc = request.query_params.get("fbc") or get_cookie(cookie, "_fbc") or gen_fbc(fbp, ts)
    if fbclid:
        fbc = f"fb.1.{ts}.{fbclid}"

    uc_raw = request.query_params.get("uc", "default")
    pos = request.query_params.get("pos", "")
    if pos not in ["Left", "Middle", "Right"]:
        pos = ""
    pos = clean(pos)
    uc = clean(uc_raw)
    utm_base = f"{uc}{pos}"
    uid = extract_uid(uc)

    # demanda (alimenta a meta da piscina e a lista de observação)
    dlog = dlog_key(utm_base)
    r.rpush(dlog, ts)
    r.ltrim(dlog, -300, -1)
    r.expire(dlog, 48 * 3600)
    r.setex(lastlink_key(utm_base), 7 * 24 * 3600, link)
    # ordinal do clique DENTRO do minuto (define a reserva do rodízio em tempo real)
    rc_key = f"rc:{utm_base}:{ts // 60}"
    n_minuto = int(r.incr(rc_key))
    r.expire(rc_key, 2 * 3600)
    if n_minuto > int(r.get(f"rcmax:{utm_base}") or 0):
        r.setex(f"rcmax:{utm_base}", 48 * 3600, n_minuto)

    # ===== atendimento: RESERVA é o caminho principal (instantâneo) =====
    # Rodízio amarrado ao TEMPO: o n-ésimo clique do minuto pega a reserva
    # (n mod R); ao chegar na última, volta pra 1. Cada RESERVA<nn> só
    # reencontra gente do próprio minuto → 1 candidato no casamento por
    # horário do sync → ~100% de identidade com external_id da pessoa (coluna B).
    entry = None
    served_from = None
    reserves = read_planob(utm_base)
    if reserves:
        alvo = desired_reservas(utm_base)
        # Rodízio sobre as reservas QUE EXISTEM agora: clique 1→RESERVA01,
        # 2→RESERVA02, ... última→volta pro 1. Usar o tamanho real da lista
        # (e não o alvo) é o que faz o rodízio acompanhar o crescimento:
        # com 1 reserva serve a 1; quando a 2 nasce, alterna 1,2; e assim
        # vai até o alvo. Com módulo pelo alvo, enquanto existisse só 1
        # reserva todo clique caía no fallback e sempre servia RESERVA01.
        idx = (n_minuto - 1) % len(reserves)
        entry = reserves[idx]
        served_from = "reserva"
        r.incr(f"lr:{utm_base}")
        r.expire(f"lr:{utm_base}", 24 * 3600)
        # demanda cresceu: escalone reservas até o pico observado (em fundo,
        # 3 por vez — UTM individual não derruba o rate limit da API)
        if len(reserves) < alvo:
            threading.Thread(target=ensure_planob, args=(utm_base, link, alvo, 0, 3), daemon=True).start()
    if not entry:
        # AO VIVO em segundo plano: 1 tentativa rápida (0,9s) — SOMENTE se
        # houver orçamento no minuto. Fora do orçamento vai direto pra tela de
        # espera (que tenta de novo em segundos), sem torrar a cota da Shopee.
        if not api_blocked("live") and try_consume_budget("live", LIVE_BUDGET_PER_MIN):
            utm = f"{utm_base}{next_number() if USE_SEQUENCE else ''}"
            origin = set_utm(link, utm)
            try:
                short = generate_short_link(origin, utm, retries=0, timeout_s=0.9)
                entry = {"short": short, "utm": utm, "ts": ts}
                served_from = "live"
                # primeira vez da UTM: planta a reserva imediatamente (em fundo,
                # orçamento de reserva — não compete com o ao vivo)
                threading.Thread(target=ensure_planob, args=(utm_base, link), daemon=True).start()
            except Exception as e:
                print("Erro shortlink live:", str(e)[:200])
                if "10030" in str(e) or "Rate limit" in str(e):
                    api_block("live")
                # última chance: criar a reserva AGORA e servir ela (anexa à
                # lista existente em vez de sobrescrever — sobrescrever apagava
                # as reservas que já existiam e travava o crescimento no 01)
                if try_consume_budget("reserva", RESERVE_BUDGET_PER_MIN):
                    try:
                        existing = read_planob(utm_base)
                        used = {e2.get("utm", "") for e2 in existing}
                        idx_livre = 1
                        while f"{utm_prefix(utm_base)}RESERVA{idx_livre:02d}" in used:
                            idx_livre += 1
                        pb = create_planob_entry(link, utm_base, idx=idx_livre, retries=0, timeout_s=1.5)
                        existing.append(pb)
                        r.setex(f"planob:{utm_base}", 30 * 3600, json.dumps(existing))
                        entry = pb
                        served_from = "reserva"
                    except Exception:
                        entry = None
    if not entry:
        # NUNCA mostrar erro: tela de carregamento que tenta de novo sozinha
        return loading_response()

    utm = entry["utm"]

    # registro do clique (fonte do sync / backup)
    r.setex(f"click:{utm}", 604800, json.dumps({
        "utm": utm, "ip": ip, "ua": ua, "fbp": fbp, "fbc": fbc, "uid": uid,
    }))

    # ===== planilha: grava quando o HTML novo envia o parâmetro fbp (mesmo vazio —
    # visitante de 1ª viagem não tem cookie ainda). Presença do parâmetro = HTML novo.
    # ASSÍNCRONA: o redirecionamento não espera o Google responder (economiza 1-3s/clique) =====
    if "fbp" in request.query_params:
        tz = datetime.timezone(datetime.timedelta(hours=-3))
        now_br = datetime.datetime.now(tz).strftime("%d/%m/%Y, %H:%M:%S")
        sheet_payload = {
            "data_hora": now_br,
            "external_id": uid,
            "fbc": fbc,
            "fbp": fbp,
            "client_ip_address": ip,
            "client_user_agent": ua,
            "uc_completo": utm,
            "geo_data": request.query_params.get("geo", ""),
            "page_url": request.query_params.get("pageurl", ""),
        }
        threading.Thread(target=write_sheet_row, args=(sheet_payload,), daemon=True).start()

    # ===== Meta server-side (dedup com o navegador pelo event_id) =====
    events = []
    px = request.query_params.get("px") or FB_PIXEL_ID
    pveid = request.query_params.get("pveid")
    vceid = request.query_params.get("vceid")
    if pveid:
        events.append(build_event("PageView", pveid, ts, fbp, fbc, ip, ua, uid, utm))
    if vceid:
        events.append(build_event("ViewContent", vceid, ts, fbp, fbc, ip, ua, uid, utm))
    if events:
        threading.Thread(target=send_meta_events, args=(events, px), daemon=True).start()

    resp = RedirectResponse(entry["short"])
    resp.set_cookie("_fbp", fbp, max_age=63072000)
    resp.set_cookie("_fbc", fbc, max_age=63072000)
    return resp

# =============================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
