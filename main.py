import os
import time
import json
import hashlib
import requests
import redis
import urllib.parse
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse

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
    Chama a API Shopee para gerar um shortLink com os sub_ids informados.
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
    body = json.dumps(payload, separators=(",", ":"))
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

@app.get("/", response_class=HTMLResponse)
async def redirect_to_shopee(request: Request, product: str = Query(..., description="URL original Shopee (urlencoded)")):
    """
    Decodifica a URL do produto, atualiza/injeta utm_content dinamicamente,
    encurta via API Shopee e redireciona ao app (mobile) ou web (desktop).
    """
    # Decodifica e parseia a URL original
    decoded = urllib.parse.unquote_plus(product)
    parsed = urllib.parse.urlparse(decoded)

    # Parseia query params e remove utm_content se existir
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    params.pop('utm_content', None)

    # Gera sub-id incremental
    count = r.incr(COUNTER_KEY)
    sub_id = f"v15n{count}"
    params['utm_content'] = [sub_id]

    # Reconstrói URL do produto com UTM atualizado
    new_query = urllib.parse.urlencode(params, doseq=True)
    updated_url = urllib.parse.urlunparse(parsed._replace(query=new_query))

    # Encurta via API Shopee
    short_link = generate_short_link(updated_url, [sub_id])

    # Monta Android Intent URI para mobile
    host_path = parsed.netloc + parsed.path
    intent_link = (
        f"intent://{host_path}#Intent;scheme=https;package=com.shopee.br;"
        f"S.browser_fallback_url={urllib.parse.quote(short_link, safe='')};end"
    )

    # Detecta ambiente (mobile vs desktop)
    ua = request.headers.get("user-agent", "").lower()
    is_mobile = any(m in ua for m in ["android", "iphone", "ipad"])

    if not is_mobile:
        # Desktop: redireciona direto para o link curto no navegador
        return RedirectResponse(url=short_link)

    # Mobile: mostra página e dispara o Intent
    html = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
      <meta charset="UTF-8">
      <title>Redirecionando...</title>
      <style>
        body {{ display:flex;justify-content:center;align-items:center;flex-direction:column;height:100vh;margin:0;font-size:20px;text-align:center; }}
        #open-btn {{ display:none; }}
      </style>
    </head>
    <body>
      <p>Você está sendo redirecionado para o app da Shopee...</p>
      <a id="open-btn" href="{intent_link}">Abrir</a>
      <script>
        window.onload = () => document.getElementById('open-btn').click();
      </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)
