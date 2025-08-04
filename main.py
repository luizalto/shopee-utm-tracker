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

# ─── LINK PADRÃO DO PRODUTO SHOPEE ───
# Cole aqui o link completo do produto Shopee que será usado quando nenhum parâmetro for passado
DEFAULT_PRODUCT_URL = "https://shopee.com.br/XEIJAIYI-8pcs-Kit-De-Gel-De-Extens%C3%A3o-De-Unhas-De-Polietileno-15ml-Nude-Pink-All-In-One-Construtor-Cola-Com-Formas-Duplas-Clipes-Manicure-Set-For-Beginnerer-i.1006215031.25062459693?sp_atk=7d9b4afa-fe7b-46a4-8d67-40beca78c014&uls_trackid=53c5r00o00b3&utm_campaign=id_K6tYTxT2w8&utm_content=----&utm_medium=affiliates&utm_source=an_18314810331&utm_term=dfkmaxk3b6rb&xptdk=7d9b4afa-fe7b-46a4-8d67-40beca78c014"

# ─── CONFIGURAÇÕES SHOPEE ───
APP_ID            = os.getenv("SHOPEE_APP_ID", "18314810331")
APP_SECRET        = os.getenv("SHOPEE_APP_SECRET", "LO3QSEG45TYP4NYQBRXLA2YYUL3ZCUPN")
SHOPEE_ENDPOINT   = "https://open-api.affiliate.shopee.com.br/graphql"

# ─── CONFIGURAÇÕES META (Facebook Conversions) ───
PIXEL_ID      = os.getenv("FB_PIXEL_ID") or os.getenv("META_PIXEL_ID")
ACCESS_TOKEN  = os.getenv("FB_ACCESS_TOKEN") or os.getenv("META_ACCESS_TOKEN")
if not PIXEL_ID or not ACCESS_TOKEN:
    raise RuntimeError(
        "As variáveis de ambiente FB_PIXEL_ID e FB_ACCESS_TOKEN (ou META_PIXEL_ID e META_ACCESS_TOKEN) devem estar definidas."
    )
FB_ENDPOINT   = f"https://graph.facebook.com/v14.0/{PIXEL_ID}/events?access_token={ACCESS_TOKEN}"

# ─── REDIS PARA CONTADOR DE UTM ───
redis_url   = os.getenv("REDIS_URL", "redis://localhost:6379/0")
r           = redis.from_url(redis_url)
COUNTER_KEY = "utm_counter"

app = FastAPI()

def generate_short_link(origin_url: str, sub_ids: list) -> str:
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
        resp = requests.post(
            SHOPEE_ENDPOINT,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
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
        resp = requests.post(FB_ENDPOINT, json=payload, timeout=5)
        print(f"[MetaAPI] Status: {resp.status_code}")
        print(f"[MetaAPI] Response: {resp.text}")
        resp.raise_for_status()
    except Exception as e:
        print(f"[MetaAPI] Exception sending event: {e}")

@app.get("/", response_class=HTMLResponse)
async def redirect_to_shopee(request: Request, product: str = Query(None, description="URL original Shopee (URL-encoded), ou vazio para usar default")):
    # Escolhe URL padrão ou parâmetro
    url_in = urllib.parse.unquote_plus(product) if product else DEFAULT_PRODUCT_URL
    parsed = urllib.parse.urlparse(url_in)

    # Preserva ordem original dos parâmetros e insere utm_content após utm_campaign
    original_query = parsed.query or ""
    segments = original_query.split('&') if original_query else []
    # Remove utm_content anterior
    segments = [seg for seg in segments if not seg.startswith('utm_content=')]
    # Incrementa contador e define utm_value com sufixo de dashes
    count = r.incr(COUNTER_KEY)
    sub_id = f"v15n{count}"
    utm_value = f"{sub_id}----"
    new_segment = f"utm_content={utm_value}"
    # Insere após utm_campaign
    inserted = False
    new_segments = []
    for seg in segments:
        new_segments.append(seg)
        if seg.startswith('utm_campaign=') and not inserted:
            new_segments.append(new_segment)
            inserted = True
    if not inserted:
        new_segments.append(new_segment)
    new_query = '&'.join(new_segments)

    # Atualiza URL
    updated_url = urllib.parse.urlunparse(parsed._replace(query=new_query))
    print(f"[ShopeeRedirect] Updated URL: {updated_url}")

    # Gera shortLink
    short_link = generate_short_link(updated_url, [sub_id])
    print(f"[ShopeeRedirect] Short link: {short_link}")

    # Dispara ViewContent
    user_data = {
        "client_ip_address": request.client.host,
        "client_user_agent": request.headers.get("user-agent", "")
    }
    custom_data = {"content_ids": [parsed.path.split('/')[-1]], "content_type": "product"}
    send_fb_event("ViewContent", sub_id, updated_url, user_data, custom_data)

    # Detecta mobile
    ua = request.headers.get("user-agent", "").lower()
    is_mobile = any(m in ua for m in ["android", "iphone", "ipad"])
    host_path = parsed.netloc + parsed.path
    intent_link = (
        f"intent://{host_path}#Intent;scheme=https;package=com.shopee.br;"
        f"S.browser_fallback_url={urllib.parse.quote(short_link, safe='')};end"
    )

    if not is_mobile:
        return RedirectResponse(url=short_link)

    # Mobile: HTML minimalista c/ click automático
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
async def upload_csv(request: Request, file: UploadFile = File(...)):
    event_url = str(request.url)
    content = file.file.read().decode('utf-8').splitlines()
    reader = csv.DictReader(content)
    results = []
    for row in reader:
        utm = row.get('utm_content')
        vendas = int(row.get('vendas', 0) or 0)
        valor = float(row.get('valor', 0) or 0)
        if vendas > 0:
            user_data = {}
            custom_data = {"currency": "BRL", "value": valor, "num_purchases": vendas}
            send_fb_event("Purchase", utm, event_url, user_data, custom_data)
            results.append({"utm_content": utm, "status": "sent"})
    return {"processed": results}