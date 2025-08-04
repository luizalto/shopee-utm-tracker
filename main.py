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
        return resp.json().get("data", {}).get("generateShortLink", {}).get("shortLink") or origin_url
    return origin_url

@app.get("/", response_class=HTMLResponse)
async def redirect_to_shopee(product: str = Query(..., description="URL original Shopee (URL-encoded)")):
    """
    Captura o parâmetro `product`, gera UTM incremental, encurta via API Shopee
    e redireciona automaticamente ao app via Intent.
    """
    # Decodifica e prepara URL original
    origin_url = urllib.parse.unquote_plus(product)

    # Incrementa contador e monta sub-id
    count = r.incr(COUNTER_KEY)
    sub_id = f"v15n{count}"

    # Gera shortLink via API
    short_link = generate_short_link(origin_url, [sub_id])

    # Monta Android Intent URI
    parsed = urllib.parse.urlparse(origin_url)
    host_path = parsed.netloc + parsed.path
    intent_uri = (
        f"intent://{host_path}#Intent;scheme=https;package=com.shopee.br;"
        f"S.browser_fallback_url={urllib.parse.quote(short_link, safe='')};end"
    )

    # Página minimalista e click automático
    html = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
      <meta charset="UTF-8">
      <title>Redirecionando...</title>
      <style>
        body {{ display:flex;justify-content:center;align-items:center;height:100vh;margin:0;font-size:20px;text-align:center; }}
        #open-btn {{display:none;}}
      </style>
    </head>
    <body>
      <p>Você está sendo redirecionado para o app da Shopee...</p>
      <a id="open-btn" href="{intent_uri}">Abrir</a>
      <script>
        window.onload = () => document.getElementById('open-btn').click();
      </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)
