import os
import time
import json
import hashlib
import requests
import redis
import urllib.parse
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

APP_ID     = os.getenv("SHOPEE_APP_ID", "18314810331")
APP_SECRET = os.getenv("SHOPEE_APP_SECRET", "LO3QSEG45TYP4NYQBRXLA2YYUL3ZCUPN")
ENDPOINT   = "https://open-api.affiliate.shopee.com.br/graphql"

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
    body = json.dumps(payload, separators=(",", ":"))
    ts = str(int(time.time()))
    factor = APP_ID + ts + body + APP_SECRET
    signature = hashlib.sha256(factor.encode()).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"SHA256 Credential={APP_ID}, Timestamp={ts}, Signature={signature}"
    }
    resp = requests.post(ENDPOINT, headers=headers, data=body)
    if resp.status_code == 200:
        return resp.json().get("data", {}).get("generateShortLink", {}).get("shortLink") or origin_url
    return origin_url

@app.get("/", response_class=HTMLResponse)
async def abrir_shopee(product: str = Query(...)):
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
      <title>Redirecionando...</title>
      <style>
        #open-btn {{
          display: none;
        }}
        body {{
          font-size: 22px;
          text-align: center;
          display: flex;
          flex-direction: column;
          justify-content: center;
          align-items: center;
          height: 100vh;
        }}
      </style>
    </head>
    <body>
      <p>Você está sendo redirecionado para o app da Shopee...</p>
      <a id="open-btn" href="{intent_link}">Abrir</a>
      <script>
        window.onload = function () {{
          document.getElementById('open-btn').click();
        }};
      </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)
