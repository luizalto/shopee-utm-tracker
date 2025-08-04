import os
import time
import json
import hashlib
import requests
import redis
import urllib.parse
import uuid
import csv
from fastapi import FastAPI, Request, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse

# ─── CONFIGURAÇÕES SHOPEE ───
APP_ID            = os.getenv("SHOPEE_APP_ID", "18314810331")
APP_SECRET        = os.getenv("SHOPEE_APP_SECRET", "LO3QSEG45TYP4NYQBRXLA2YYUL3ZCUPN")
SHOPEE_ENDPOINT   = "https://open-api.affiliate.shopee.com.br/graphql"

# ─── CONFIGURAÇÕES META ───
PIXEL_ID          = os.getenv("META_PIXEL_ID")
ACCESS_TOKEN      = os.getenv("META_ACCESS_TOKEN")
FB_ENDPOINT       = f"https://graph.facebook.com/v14.0/{PIXEL_ID}/events?access_token={ACCESS_TOKEN}"

# ─── REDIS PARA CONTADOR DE UTM ───
redis_url         = os.getenv("REDIS_URL", "redis://localhost:6379/0")
r                 = redis.from_url(redis_url)
COUNTER_KEY       = "utm_counter"

app = FastAPI()

def generate_short_link(origin_url: str, sub_ids: list) -> str:
    """
    Gera um shortLink via Shopee GraphQL API usando a mutação `generateShortLink`.
    Usa json=payload e inclui operationName para garantir execução.
    Faz logs de status e corpo para debug.
    """
    payload = {
        "operationName": "Generate",
        "query": """
        mutation Generate($url: String!, $subs: [String]) {
          generateShortLink(input:{originUrl:$url, subIds:$subs}) {
            shortLink
          }
        }
        """,
        "variables": {"url": origin_url, "subs": sub_ids}
    }
    try:
        resp = requests.post(SHOPEE_ENDPOINT, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
        print(f"[ShopeeAPI] Status: {resp.status_code}")
        print(f"[ShopeeAPI] Response: {resp.text}")
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            print(f"[ShopeeAPI] Errors: {data['errors']}")
        short = data.get("data", {}).get("generateShortLink", {}).get("shortLink")
        if short:
            return short
        raise ValueError("shortLink field missing in Shopee response")
    except Exception as e:
        print(f"[ShopeeAPI] Exception generating short link: {e}")
        # fallback para a URL original
        return origin_url


def send_fb_event(event_name: str, event_id: str, event_source_url: str, user_data: dict, custom_data: dict):
    payload = {
        "data": [{
            "event_name": event_name,
            "event_time": int(time.time()),
            "event_id": event_id,
            "action_source": "website",
            "event_source_url": event_source_url,
            "user_data": user_data,
            "custom_data": custom_data
        }]
    }
    try:
        requests.post(FB_ENDPOINT, json=payload, timeout=5)
    except Exception as e:
        print(f"[MetaAPI] Exception sending event: {e}")


@app.get("/", response_class=HTMLResponse)
async def redirect_to_shopee(request: Request, product: str = Query(..., description="URL original Shopee (URL-encoded)")):
    # Decodifica produto e parseia URL
    decoded = urllib.parse.unquote_plus(product)
    parsed = urllib.parse.urlparse(decoded)

    # Injeta novo utm_content
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    params.pop('utm_content', None)
    count = r.incr(COUNTER_KEY)
    sub_id = f"v15n{count}"
    params['utm_content'] = [sub_id]
    new_query = urllib.parse.urlencode(params, doseq=True)
    updated_url = urllib.parse.urlunparse(parsed._replace(query=new_query))

    # Gera shortLink usando a API da Shopee
    short_link = generate_short_link(updated_url, [sub_id])

    # Logs para debug
    print(f"[ShopeeRedirect] Updated URL: {updated_url}")
    print(f"[ShopeeRedirect] Short link: {short_link}")

    # Dispara evento ViewContent ao Meta
    user_data = {
        "client_ip_address": request.client.host,
        "client_user_agent": request.headers.get("user-agent", "")
    }
    custom_data = {"content_ids": [parsed.path.split('/')[-1]], "content_type": "product"}
    send_fb_event("ViewContent", sub_id, updated_url, user_data, custom_data)

    # Decide redirecionamento mobile vs desktop
    ua = request.headers.get("user-agent", "").lower()
    is_mobile = any(m in ua for m in ["android", "iphone", "ipad"])
    host_path = parsed.netloc + parsed.path
    intent_link = (
        f"intent://{host_path}#Intent;scheme=https;package=com.shopee.br;"
        f"S.browser_fallback_url={urllib.parse.quote(short_link, safe='')};end"
    )

    if not is_mobile:
        return RedirectResponse(url=short_link)

    # Mobile: HTML com click automático para abrir no app
    html = f"""
    <!DOCTYPE html>
    <html lang=\"pt-BR\">
      <head><meta charset=\"UTF-8\"><title>Redirecionando...</title></head>
      <body style=\"display:flex;justify-content:center;align-items:center;flex-direction:column;height:100vh;margin:0;font-size:20px;text-align:center;\">
        <p>Você está sendo redirecionado para o app da Shopee...</p>
        <a id=\"open-btn\" href=\"{intent_link}\">Abrir</a>
        <script>window.onload=()=>document.getElementById('open-btn').click();</script>
      </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.post("/upload_csv")
async def upload_csv(file: UploadFile = File(...)):
    content = file.file.read().decode('utf-8').splitlines()
    reader = csv.DictReader(content)
    results = []
    for row in reader:
        utm = row.get('utm_content')
        vendas = int(row.get('vendas', 0) or 0)
        valor = float(row.get('valor', 0) or 0)
        if vendas > 0:
            user_data = {}
            custom_data = {"currency": "BRL", "value": valor}
            send_fb_event("Purchase", utm, "", user_data, custom_data)
            results.append({"utm_content": utm, "status": "sent"})
    return {"processed": results}
