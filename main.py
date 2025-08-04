from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import os, time, json, hashlib, urllib.parse
import requests
import redis

# ─── CONFIGURAÇÕES SHOPEE ───
APP_ID     = "18314810331"
APP_SECRET = "LO3QSEG45TYP4NYQBRXLA2YYUL3ZCUPN"
ENDPOINT   = "https://open-api.affiliate.shopee.com.br/graphql"

# ─── REDIS PARA CONTADOR DE UTM ───
redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
r = redis.from_url(redis_url)
COUNTER_KEY = "utm_counter"

app = FastAPI()


def generate_short_link(origin_url: str) -> str:
    """
    Encurta a URL via Shopee Affiliate API, usando only originUrl.
    """
    # Monta payload GraphQL
    payload = {
        "query": f"mutation{{generateShortLink(input:{{originUrl:\"{origin_url}\"}}){{shortLink}}}}"
    }
    body = json.dumps(payload, separators=(",","":""))

    # Cabeçalhos de autenticação
    ts = str(int(time.time()))
    factor = APP_ID + ts + body + APP_SECRET
    signature = hashlib.sha256(factor.encode('utf-8')).hexdigest()
    headers = {
        "Authorization": f"SHA256 Credential={APP_ID}, Timestamp={ts}, Signature={signature}",
        "Content-Type": "application/json"
    }

    # Requisição
    resp = requests.post(ENDPOINT, headers=headers, data=body)
    if resp.status_code == 200:
        return resp.json()["data"]["generateShortLink"]["shortLink"]
    # Fallback para URL original em caso de erro
    return origin_url


@app.get("/", response_class=HTMLResponse)
async def landing():
    """
    Página de entrada com botão "Saiba Mais".
    """
    html = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head><meta charset="UTF-8"><title>Oferta Imperdível</title></head>
    <body style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;">
      <h1>iPhone 11 128GB</h1>
      <a href="/abrir" style="padding:15px 30px;background:#0049A9;color:#fff;text-decoration:none;border-radius:4px;font-size:18px;">Saiba Mais</a>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/abrir", response_class=HTMLResponse)
async def abrir():
    """
    Gera UTM dinâmico e retorna página com botão "Abrir a Shopee".
    """
    # URL base do produto
    base_url = "https://shopee.com.br/Apple-Iphone-11-128GB-Local-Set-i.52377417.6309028319"
    # Incrementa contador e cria utm_content
    count = r.incr(COUNTER_KEY)
    utm = f"v15n{count}"
    # Monta URL final com utm_content
    parsed = urllib.parse.urlparse(base_url)
    qs = {"utm_content": utm}
    final_url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(qs)))

    # Chama Shopee para gerar o shortLink
    short_link = generate_short_link(final_url)

    # Retorna HTML com botão para abrir o shortLink
    html = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head><meta charset="UTF-8"><title>Abrir Shopee</title></head>
    <body style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;">
      <p>Clique no botão abaixo para abrir na Shopee:</p>
      <a href="{short_link}" style="padding:15px 30px;background:#0049A9;color:#fff;text-decoration:none;border-radius:4px;font-size:18px;">Abrir a Shopee</a>
    </body>
    </html>
    """
    return HTMLResponse(content=html)
