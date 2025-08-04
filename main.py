import os
import time
import json
import hashlib
import requests
import redis
import urllib.parse
from fastapi import FastAPI, Request
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
    Chama a API Shopee para gerar um link curto com os sub_ids informados.
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
    body = json.dumps(payload, separators=(',', ':'))

    timestamp = str(int(time.time()))
    factor = APP_ID + timestamp + body + APP_SECRET
    signature = hashlib.sha256(factor.encode('utf-8')).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"SHA256 Credential={APP_ID}, Timestamp={timestamp}, Signature={signature}"
    }

    resp = requests.post(ENDPOINT, headers=headers, data=body)
    if resp.status_code == 200:
        data = resp.json()
        return data.get("data", {}).get("generateShortLink", {}).get("shortLink") or origin_url
    return origin_url

@app.get("/{full_path:path}")
async def redirect_shopee(request: Request, full_path: str):
    """
    Captura qualquer caminho e query, gera UTM incremental, obtém shortLink
    e redireciona via Android Intent para o app da Shopee.
    """
    # Reconstrói a URL original do produto Shopee
    original_query = request.url.query
    original_url = f"https://shopee.com.br/{full_path}"
    if original_query:
        original_url += f"?{original_query}"

    # Gera UTM dinâmico usando contador Redis
    count = r.incr(COUNTER_KEY)
    utm = f"v15n{count}"

    # Obtém shortLink pela API Shopee
    short_link = generate_short_link(original_url, [utm])

    # Constrói Android Intent URI (com fallback para shortLink)
    parsed = urllib.parse.urlparse(original_url)
    host_path = parsed.netloc + parsed.path
    intent_link = (
        f"intent://{host_path}#Intent;scheme=https;package=com.shopee.br;"
        f"S.browser_fallback_url={urllib.parse.quote(short_link, safe='')};end"
    )

    # HTML com clique automático em botão invisível
    html = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
      <meta charset="UTF-8">
      <title>Redirecionando...</title>
      <style>
        body {{font-size:22px;text-align:center;display:flex;flex-direction:column;justify-content:center;align-items:center;height:100vh;}}
        #open-btn {{display:none;}}
      </style>
    </head>
    <body>
      <p>Você está sendo redirecionado para o app da Shopee...</p>
      <a id="open-btn" href="{intent_link}">Abrir</a>
      <script>
        window.onload = function() {{ document.getElementById('open-btn').click(); }}
      </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)
