import os
import time
import json
import hashlib
import requests
import redis
import urllib.parse
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

# ─── CONFIGURAÇÕES SHOPEE ───
APP_ID     = os.getenv("SHOPEE_APP_ID", "18314810331")
APP_SECRET = os.getenv("SHOPEE_APP_SECRET", "LO3QSEG45TYP4NYQBRXLA2YYUL3ZCUPN")
ENDPOINT   = "https://open-api.affiliate.shopee.com.br/graphql"

# ─── REDIS PARA CONTADOR DE UTM ───
redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
r = redis.from_url(redis_url)
COUNTER_KEY = "utm_counter"

app = FastAPI()

def generate_short_link(origin_url: str, sub_ids: list) -> str:
    """
    Gera um shortLink via Shopee Affiliate API com os sub_ids fornecidos.
    """
    payload = {
        "query": """
        mutation Generate($url: String!, $subs: [String]) {
          generateShortLink(input:{originUrl:$url, subIds:$subs}) {
            shortLink
          }
        }
        """,
        "variables": {"url": origin_url, "subs": sub_ids}
    }
    body = json.dumps(payload, separators=(",",":"))

    ts = str(int(time.time()))
    factor = APP_ID + ts + body + APP_SECRET
    signature = hashlib.sha256(factor.encode('utf-8')).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"SHA256 Credential={APP_ID}, Timestamp={ts}, Signature={signature}"
    }

    resp = requests.post(ENDPOINT, headers=headers, data=body)
    if resp.status_code == 200:
        data = resp.json()
        return data.get("data", {}).get("generateShortLink", {}).get("shortLink") or origin_url
    return origin_url

@app.get("/", response_class=HTMLResponse)
async def landing(product: str = Query(..., description="URL original do produto Shopee")):
    """
    Landing page onde o parâmetro `product` é a URL original Shopee.
    Exemplo: /?product=https%3A%2F%2Fshopee.com.br%2FApple-Iphone-11-128GB...
    """
    encoded = urllib.parse.quote_plus(product)
    html = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
      <meta charset="UTF-8">
      <title>Oferta Imperdível</title>
    </head>
    <body style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;">
      <h1>Oferta Shopee</h1>
      <a href="/abrir?link={encoded}" style="padding:15px 30px;background:#0049A9;color:#fff;text-decoration:none;border-radius:4px;font-size:18px;">
        Saiba Mais
      </a>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

@app.get("/abrir", response_class=HTMLResponse)
async def abrir(request: Request):
    """
    Gera UTM dinâmico, chama Shopee e retorna botão "Abrir a Shopee" com deep link para app.
    """
    # Recupera o link do produto (via query string ou padrão)
    product_url = request.query_params.get(
        "product",
        "https://shopee.com.br/Apple-Iphone-11-128GB-Local-Set-i.52377417.6309028319"
    )
    # Gera contador de UTM
    count = r.incr(COUNTER_KEY)
    utm = f"v15n{count}"
    # Gera shortLink via Shopee API
    short_link = generate_short_link(product_url, [utm])

    # Constrói Android intent URI para tentar abrir o app
    # Remove protocolo para ficar no formato intent://host/path
    path = urllib.parse.urlparse(product_url).netloc + urllib.parse.urlparse(product_url).path
    intent_link = (
        f"intent://{path}#Intent;scheme=https;package=com.shopee.br;"
        f"S.browser_fallback_url={urllib.parse.quote(short_link, safe='')};end"
    )

    # HTML com botão que dispara o intent
    html = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
      <meta charset="UTF-8">
      <title>Abrir Shopee</title>
    </head>
    <body style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;">
      <p>Clique no botão abaixo para abrir no app da Shopee:</p>
      <a id="open-btn" href="{intent_link}" style="padding:15px 30px;background:#0049A9;color:#fff;text-decoration:none;border-radius:4px;font-size:18px;">
        Abrir a Shopee
      </a>
      <script>
        // Em alguns navegadores in-app, pode ser necessário forçar o Intent
        document.getElementById('open-btn').addEventListener('click', function(e) {{
          e.preventDefault();
          window.location = '{intent_link}';
        }});
      </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)
