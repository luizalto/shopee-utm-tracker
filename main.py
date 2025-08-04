import os
import json
import time
import hashlib
import requests
import urllib.parse
import csv
import redis
from fastapi import FastAPI, Request, Query, UploadFile, File
from fastapi.responses import RedirectResponse

# ─── CONFIGURAÇÕES ───────────────────────────────────────────────────────────
DEFAULT_PRODUCT_URL = os.getenv(
    "DEFAULT_PRODUCT_URL",
    "https://shopee.com.br/XEIJAIYI-8pcs-Kit-De-Gel-De-Extens%C3%A3o-De-Unhas-De-Polietileno-15ml-"
    "Nude-Pink-All-In-One-Construtor-Cola-Com-Formas-Duplas-Clipes-Manicure-Set-For-Beginnerer-i.1006215031."
    "25062459693?sp_atk=7d9b4afa-fe7b-46a4-8d67-40beca78c014&uls_trackid=53c5r00o00b3&"
    "utm_campaign=id_K6tYTxT2w8&utm_medium=affiliates&utm_source=an_18314810331&utm_term=dfkmaxk3b6rb"
)

FB_PIXEL_ID     = os.getenv("FB_PIXEL_ID") or os.getenv("META_PIXEL_ID")
FB_ACCESS_TOKEN = os.getenv("FB_ACCESS_TOKEN") or os.getenv("META_ACCESS_TOKEN")
if not FB_PIXEL_ID or not FB_ACCESS_TOKEN:
    raise RuntimeError(
        "As variáveis de ambiente FB_PIXEL_ID e FB_ACCESS_TOKEN (ou META_PIXEL_ID e META_ACCESS_TOKEN) devem estar definidas."
    )
FB_ENDPOINT = f"https://graph.facebook.com/v14.0/{FB_PIXEL_ID}/events?access_token={FB_ACCESS_TOKEN}"

SHOPEE_APP_ID     = os.getenv("SHOPEE_APP_ID", "18314810331")
SHOPEE_APP_SECRET = os.getenv("SHOPEE_APP_SECRET", "LO3QSEG45TYP4NYQBRXLA2YYUL3ZCUPN")
SHOPEE_ENDPOINT   = "https://open-api.affiliate.shopee.com.br/graphql"

VIDEO_ID     = os.getenv("VIDEO_ID", "v15")
REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379/0")
COUNTER_KEY  = os.getenv("UTM_COUNTER_KEY", "utm_counter")

# Conecta ao Redis para contador de UTM
r = redis.from_url(REDIS_URL)

app = FastAPI()

# ─── GERAÇÃO DE UTM COM REDIS ─────────────────────────────────────────────────
def next_utm() -> str:
    count = r.incr(COUNTER_KEY)
    print(f"Gerando UTM: {VIDEO_ID}n{count}")
    return f"{VIDEO_ID}n{count}"

# ─── ENVIO DE EVENTOS AO META PIXEL ─────────────────────────────────────────────
def send_fb_event(event_name: str, event_id: str, event_source_url: str, user_data: dict, custom_data: dict):
    payload = {"data": [{
        "event_name": event_name,
        "event_time": int(time.time()),
        "event_id": event_id,
        "action_source": "website",
        "event_source_url": event_source_url,
        "user_data": user_data,
        "custom_data": custom_data
    }]}
    try:
        resp = requests.post(FB_ENDPOINT, json=payload, timeout=5)
        resp.raise_for_status()
    except Exception as e:
        print(f"[MetaAPI] Exception sending event: {e}")

# ─── GERAÇÃO DE SHORT LINK DA SHOPEE ────────────────────────────────────────────
def generate_short_link(full_url: str, utm_content: str) -> str:
    payload_obj = {
        "query": (
            "mutation{generateShortLink(input:{"
            f"originUrl:\"{full_url}\","
            f"subIds:[\"\",\"\",\"{utm_content}\",\"\",\"\"]"  # utm_content embutido
            "}){shortLink}}"
        )
    }
    payload = json.dumps(payload_obj, separators=(',', ':'), ensure_ascii=False)

    timestamp = str(int(time.time()))
    base_str  = SHOPEE_APP_ID + timestamp + payload + SHOPEE_APP_SECRET
    signature = hashlib.sha256(base_str.encode('utf-8')).hexdigest()

    headers = {
        "Authorization": (
            f"SHA256 Credential={SHOPEE_APP_ID}, Timestamp={timestamp}, Signature={signature}"
        ),
        "Content-Type": "application/json"
    }

    resp = requests.post(SHOPEE_ENDPOINT, headers=headers, data=payload)
    resp.raise_for_status()
    data = resp.json()
    link = data['data']['generateShortLink']['shortLink']
    print(f"[ShopeeShortLink] Gerado ({utm_content}): {link}")
    return link

# ─── ENDPOINT PRINCIPAL ────────────────────────────────────────────────────────
@app.get("/", response_class=RedirectResponse)
async def redirect_to_shopee(request: Request, product: str = Query(None, description="URL original Shopee (URL-encoded) ou vazio para usar DEFAULT_PRODUCT_URL")):
    # Define URL do produto
    url_in = urllib.parse.unquote_plus(product) if product else DEFAULT_PRODUCT_URL

    # Gera UTM e envia evento ViewContent
    utm_value = next_utm()
    user_data = {
        "client_ip_address": request.client.host,
        "client_user_agent": request.headers.get("user-agent", "")
    }
    custom_data = {
        "content_ids": [urllib.parse.urlparse(url_in).path.split('/')[-1]],
        "content_type": "product"
    }
    send_fb_event("ViewContent", utm_value, url_in, user_data, custom_data)

    # Gera e redireciona para o short link
    try:
        short_link = generate_short_link(url_in, utm_value)
    except Exception as e:
        print(f"[ShopeeShortLink] Falha: {e}")
        short_link = url_in

    return RedirectResponse(url=short_link, status_code=302)

# ─── ENDPOINT DE UPLOAD CSV ───────────────────────────────────────────────────
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
            send_fb_event("Purchase", utm, event_url, {}, {"currency": "BRL", "value": valor, "num_purchases": vendas})
            results.append({"utm_content": utm, "status": "sent"})
    return {"processed": results}
