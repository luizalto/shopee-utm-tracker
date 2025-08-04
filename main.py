from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse
import urllib.parse
import redis
import os
import time
import json
import hashlib
import requests

# ─── CONFIGURAÇÕES ───
APP_ID     = "18314810331"
APP_SECRET = "LO3QSEG45TYP4NYQBRXLA2YYUL3ZCUPN"
SHOPEE_ENDPOINT = "https://open-api.affiliate.shopee.com.br/graphql"
ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "SEU_TOKEN_DE_ACESSO_INSTAGRAM")
IG_API_URL = "https://graph.facebook.com/v19.0"

# ─── FASTAPI ───
app = FastAPI()

# ─── REDIS ───
redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
r = redis.Redis.from_url(redis_url)
COUNTER_KEY = "utm_counter"

# ─── FUNÇÃO: gerar link curto na Shopee ───
def generate_short_link(full_url: str) -> str:
    payload_obj = {
        "query": (
            "mutation{generateShortLink(input:{"
            f"originUrl:\"{full_url}\","
            "subIds:[\"\",\"\",\"\",\"\",\"\"]"
            "}){shortLink}}"
        )
    }
    payload = json.dumps(payload_obj, separators=(',', ':'), ensure_ascii=False)
    timestamp = str(int(time.time()))
    base_str = APP_ID + timestamp + payload + APP_SECRET
    signature = hashlib.sha256(base_str.encode('utf-8')).hexdigest()

    headers = {
        "Authorization": f"SHA256 Credential={APP_ID}, Timestamp={timestamp}, Signature={signature}",
        "Content-Type": "application/json"
    }

    resp = requests.post(SHOPEE_ENDPOINT, headers=headers, data=payload)
    if resp.status_code == 200:
        return resp.json()['data']['generateShortLink']['shortLink']
    else:
        print(f"❌ Shopee erro: {resp.status_code} - {resp.text}")
        return full_url

# ─── FUNÇÃO: gerar nova utm_content ───
def gerar_utm(prefixo="v15n"):
    current = r.incr(COUNTER_KEY)
    return f"{prefixo}{current}----"

# ─── FUNÇÃO: responder no Instagram ───
def enviar_mensagem_instagram(user_id: str, mensagem: str):
    url = f"{IG_API_URL}/{user_id}/messages"
    payload = {
        "messaging_product": "instagram",
        "recipient": {"id": user_id},
        "message": {"text": mensagem}
    }
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code != 200:
        print("❌ Erro ao enviar mensagem:", response.text)

# ─── ROTA GET: Redirecionamento padrão ───
@app.get("/{path:path}")
async def redirect_handler(request: Request, path: str):
    original_query = str(request.url.query)
    original_link = f"https://shopee.com.br/{path}?{original_query}"

    parsed_url = urllib.parse.urlparse(original_link)
    query_params = urllib.parse.parse_qs(parsed_url.query)

    content_raw = query_params.get("utm_content", [""])[0]
    prefix = "".join(filter(str.isalpha, content_raw)) or "v15n"

    utm = gerar_utm(prefix)
    query_params["utm_content"] = [utm]

    updated_query = "&".join([
        f"{key}={urllib.parse.quote_plus(value[0])}" for key, value in query_params.items()
    ])
    final_url = f"https://shopee.com.br/{path}?{updated_query}"
    short_link = generate_short_link(final_url)

    return RedirectResponse(short_link, status_code=302)

# ─── ROTA POST: Webhook Instagram ───
@app.post("/webhook")
async def instagram_webhook(request: Request):
    data = await request.json()

    try:
        entry = data.get("entry", [])[0]
        messaging = entry.get("messaging", [])[0]
        sender_id = messaging["sender"]["id"]

        # Aqui você escolhe o link base da Shopee que quer anunciar:
        base_link = "https://shopee.com.br/SEU_PRODUTO_AQUI?utm_source=an_18314810331&utm_medium=affiliates&utm_campaign=id_z91sQ22saU&utm_term=dfhg1iq2f12w&utm_content=v15n"
        
        parsed = urllib.parse.urlparse(base_link)
        params = urllib.parse.parse_qs(parsed.query)
        params["utm_content"] = [gerar_utm("v15n")]

        nova_query = "&".join([f"{k}={v[0]}" for k, v in params.items()])
        link_final = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{nova_query}"
        short_link = generate_short_link(link_final)

        # Envia a resposta automática com o link já encurtado
        enviar_mensagem_instagram(sender_id, f"Oi! Olha esse achadinho incrível 👇\n{short_link}")
        return JSONResponse({"status": "ok"})

    except Exception as e:
        print("Erro no webhook:", str(e))
        return JSONResponse({"error": str(e)}, status_code=400)

# ─── ROTA GET: Verificação do webhook (obrigatório) ───
@app.get("/webhook")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    VERIFY_TOKEN = os.getenv("IG_VERIFY_TOKEN", "meu_token_webhook")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return int(challenge)
    return JSONResponse(status_code=403, content={"error": "Token inválido"})
