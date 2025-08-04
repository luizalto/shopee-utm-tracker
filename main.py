import os
import time
import json
import hashlib
import requests
import redis
import urllib.parse
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

# ─── CONFIGURAÇÕES DA SHOPEE ───
APP_ID     = os.getenv("SHOPEE_APP_ID", "18314810331")
APP_SECRET = os.getenv("SHOPEE_APP_SECRET", "LO3QSEG45TYP4NYQBRXLA2YYUL3ZCUPN")
ENDPOINT   = "https://open-api.affiliate.shopee.com.br/graphql"

# ─── CONFIGURAÇÃO DO REDIS ───
redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
r = redis.from_url(redis_url)
COUNTER_KEY = "utm_counter"

app = FastAPI()

# ─── FUNÇÃO PARA GERAR LINK ENCURTADO ───
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
    body = json.dumps(payload, separators=(",", ":"))
    timestamp = str(int(time.time()))
    factor = APP_ID + timestamp + body + APP_SECRET
    signature = hashlib.sha256(factor.encode()).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"SHA256 Credential={APP_ID}, Timestamp={timestamp}, Signature={signature}"
    }

    resp = requests.post(ENDPOINT, headers=headers, data=body)
    if resp.status_code == 200:
        data = resp.json()
        return data.get("data", {}).get("generateShortLink", {}).get("shortLink") or origin_url
    return origin_url

# ─── ROTA PRINCIPAL QUE ABRE DIRETO A SHOPEE ───
@app.get("/", response_class=HTMLResponse)
async def abrir_shopee(product: str = Query(..., description="URL original Shopee (urlencoded)")):
    origin_url = urllib.parse.unquote_plus(product)
    count = r.incr(COUNTER_KEY)
    utm = f"v15n{count}"
    short_link = generate_short_link(origin_url, [utm])

    parsed = urllib.parse.urlparse(origin_url)
    host_path = parsed.netloc + parsed.path

    intent_link = (
        f"intent://{host_path}#Intent;scheme=https;package=com.shopee.br;"
        f"S.browser_fallback_url={urllib.parse.quote(short_link, safe='')};end"
    )

    html = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
      <meta charset="UTF-8">
      <title>Abrir Shopee</title>
    </head>
    <body style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;font-size:22px;text-align:center;">
      <p style="margin-bottom: 30px;">Clique no botão abaixo para abrir no app da Shopee:</p>
      <a id="open-btn" href="{intent_link}" style="padding:20px 40px;background:#0049A9;color:#fff;text-decoration:none;border-radius:8px;font-size:24px;">
        Abrir a Shopee
      </a>
      <script>
        document.getElementById('open-btn').addEventListener('click', function(e) {{
          e.preventDefault();
          window.location = '{intent_link}';
        }});
      </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)
