from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse, PlainTextResponse
import urllib.parse
import redis
import os
import time
import json
import hashlib
import requests

# ─── CONFIGURAÇÕES ────────────────────────────────────
APP_ID         = "18314810331"
APP_SECRET     = "LO3QSEG45TYP4NYQBRXLA2YYUL3ZCUPN"
SHOPEE_API     = "https://open-api.affiliate.shopee.com.br/graphql"
ACCESS_TOKEN   = os.getenv("IG_ACCESS_TOKEN", "")
IG_API_URL     = "https://graph.facebook.com/v19.0"
VERIFY_TOKEN   = os.getenv("IG_VERIFY_TOKEN", "ig-verifica-rasant")
# ID da sua Página do Facebook conectada à conta Instagram
PAGE_ID        = os.getenv("FB_PAGE_ID", "<YOUR_FACEBOOK_PAGE_ID>")
REDIS_URL      = os.getenv("REDIS_URL", "redis://localhost:6379")
COUNTER_KEY    = "utm_counter"

# ─── FASTAPI & REDIS ─────────────────────────────────
app = FastAPI()
r   = redis.Redis.from_url(REDIS_URL)

# ─── UTIL: gera utm_content incremental ───────────────
def gerar_utm(prefix="v15n"):
    n = r.incr(COUNTER_KEY)
    return f"{prefix}{n}----"

# ─── UTIL: gera link curto via Shopee API ────────────
def generate_short_link(full_url: str) -> str:
    payload = {
        "query": (
            "mutation{generateShortLink(input:{"
            f"originUrl:\"{full_url}\","
            "subIds:[\"\",\"\",\"\",\"\",\"\"]"
            "}){shortLink}}"
        )
    }
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    ts   = str(int(time.time()))
    sig  = hashlib.sha256((APP_ID + ts + data + APP_SECRET).encode()).hexdigest()
    headers = {
        "Authorization": f"SHA256 Credential={APP_ID}, Timestamp={ts}, Signature={sig}",
        "Content-Type": "application/json"
    }
    resp = requests.post(SHOPEE_API, headers=headers, data=data)
    if resp.ok:
        return resp.json()["data"]["generateShortLink"]["shortLink"]
    print("❌ Shopee API erro:", resp.status_code, resp.text)
    return full_url

# ─── UTIL: envia DM no Instagram ────────────────────
def enviar_mensagem_instagram(user_id: str, mensagem: str):
    url = f"{IG_API_URL}/{PAGE_ID}/messages"
    payload = {
        "messaging_product": "instagram",
        "recipient": {"instagram_id": user_id},
        "message": {"text": mensagem},
        "messaging_type": "RESPONSE"
    }
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    resp = requests.post(url, headers=headers, json=payload)
    if not resp.ok:
        print("❌ Erro Instagram DM:", resp.status_code, resp.text)

# ─── WEBHOOK: Verificação (GET /webhook) ─────────────
@app.get("/webhook")
async def verify_webhook(request: Request):
    params    = request.query_params
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(challenge, status_code=200)
    return PlainTextResponse("Forbidden", status_code=403)

# ─── WEBHOOK: Recebimento de mensagens (POST /webhook) ─
@app.post("/webhook")
async def instagram_webhook(request: Request):
    data = await request.json()
    try:
        entry     = data["entry"][0]
        messaging = entry["messaging"][0]
        sender_id = messaging["sender"]["id"]

        # Monta seu link base (troque pelo seu produto)
        base_link = (
            "https://shopee.com.br/SEU_PRODUTO_AQUI?"
            "utm_source=an_18314810331&utm_medium=affiliates"
            "&utm_campaign=id_z91sQ22saU&utm_term=dfhg1iq2f12w"
            "&utm_content=v15n"
        )

        # Ajusta utm_content dinamicamente
        parsed    = urllib.parse.urlparse(base_link)
        params    = urllib.parse.parse_qs(parsed.query)
        params["utm_content"] = [gerar_utm("v15n")]
        new_query = urllib.parse.urlencode(params, doseq=True)
        final_url = urllib.parse.urlunparse(parsed._replace(query=new_query))
        short_link = generate_short_link(final_url)

        # Envia DM com link curto
        enviar_mensagem_instagram(sender_id, f"🔍 Achado: {short_link}")
        return JSONResponse({"status": "ok"})
    except Exception as e:
        print("Erro no webhook:", e)
        return JSONResponse({"error": str(e)}, status_code=400)

# ─── REDIRECIONAMENTO: cliques em qualquer outro path ─
@app.get("/{path:path}")
async def redirect_handler(request: Request, path: str):
    original = f"https://shopee.com.br/{path}?{request.url.query}"
    parsed   = urllib.parse.urlparse(original)
    params   = urllib.parse.parse_qs(parsed.query)
    prefix   = "".join(filter(str.isalpha, params.get("utm_content", [""])[0])) or "v15n"
    params["utm_content"] = [gerar_utm(prefix)]
    new_q    = urllib.parse.urlencode(params, doseq=True)
    final    = urllib.parse.urlunparse(parsed._replace(query=new_q))
    short    = generate_short_link(final)
    return RedirectResponse(short, status_code=302)
