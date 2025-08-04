import os
import time
import requests
import redis
import urllib.parse
import csv
import hashlib
import json
from fastapi import FastAPI, Request, Query, UploadFile, File
from fastapi.responses import RedirectResponse

# ─── LINK PADRÃO DO PRODUTO SHOPEE ───
# Cole aqui o link completo do produto Shopee que será usado quando nenhum parâmetro for passado
default_url = os.getenv("DEFAULT_PRODUCT_URL") or (
    "https://shopee.com.br/XEIJAIYI-8pcs-Kit-De-Gel-De-Extens%C3%A3o-De-Unhas-De-Polietileno-15ml-"
    "Nude-Pink-All-In-One-Construtor-Cola-Com-Formas-Duplas-Clipes-Manicure-Set-For-Beginnerer-i.1006215031."
    "25062459693?sp_atk=7d9b4afa-fe7b-46a4-8d67-40beca78c014&uls_trackid=53c5r00o00b3&"
    "utm_campaign=id_K6tYTxT2w8&utm_medium=affiliates&utm_source=an_18314810331&utm_term=dfkmaxk3b6rb"
)

# ─── CONFIGURAÇÕES META (Facebook Conversions) ───
PIXEL_ID     = os.getenv("FB_PIXEL_ID") or os.getenv("META_PIXEL_ID")
ACCESS_TOKEN = os.getenv("FB_ACCESS_TOKEN") or os.getenv("META_ACCESS_TOKEN")
if not PIXEL_ID or not ACCESS_TOKEN:
    raise RuntimeError(
        "As variáveis de ambiente FB_PIXEL_ID e FB_ACCESS_TOKEN (ou META_PIXEL_ID e META_ACCESS_TOKEN) devem estar definidas."
    )
FB_ENDPOINT  = f"https://graph.facebook.com/v14.0/{PIXEL_ID}/events?access_token={ACCESS_TOKEN}"

# ─── REDIS PARA CONTADOR DE UTM ───
redis_url   = os.getenv("REDIS_URL", "redis://localhost:6379/0")
r           = redis.from_url(redis_url)
COUNTER_KEY = "utm_counter"

# ─── CONFIGURAÇÕES SHOPEE SHORT LINK ───
SHOPEE_APP_ID     = os.getenv("SHOPEE_APP_ID", "18314810331")
SHOPEE_APP_SECRET = os.getenv("SHOPEE_APP_SECRET", "LO3QSEG45TYP4NYQBRXLA2YYUL3ZCUPN")
SHOPEE_ENDPOINT   = "https://open-api.affiliate.shopee.com.br/graphql"

app = FastAPI()

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

def generate_short_link(full_url: str) -> str:
    """
    Gera um short link oficial da Shopee para a URL completa informada.
    """
    query = (
        "mutation{generateShortLink(input:{"
        f"originUrl:\"{full_url}\","
        "subIds:[\"\",\"\",\"\",\"\",\"\"]"  # utm_content já embutido na URL
        "}){shortLink}}"
    )
    payload_obj = {"query": query}
    payload = json.dumps(payload_obj, separators=(',', ':'), ensure_ascii=False)

    timestamp = str(int(time.time()))
    base_str  = SHOPEE_APP_ID + timestamp + payload + SHOPEE_APP_SECRET
    signature = hashlib.sha256(base_str.encode('utf-8')).hexdigest()

    headers = {
        "Authorization": f"SHA256 Credential={SHOPEE_APP_ID}, Timestamp={timestamp}, Signature={signature}",
        "Content-Type": "application/json"
    }

    resp = requests.post(SHOPEE_ENDPOINT, headers=headers, data=payload)
    resp.raise_for_status()
    data = resp.json()
    return data['data']['generateShortLink']['shortLink']

@app.get("/", response_class=RedirectResponse)
async def redirect_to_shopee(request: Request, product: str = Query(None, description="URL original Shopee (URL-encoded) ou vazio para usar default")):
    # Define URL do produto (param ou default)
    url_in = urllib.parse.unquote_plus(product) if product else default_url
    parsed = urllib.parse.urlparse(url_in)

    # Parseia e reconstroi query sem utm_content
    segments = parsed.query.split('&') if parsed.query else []
    segments = [seg for seg in segments if not seg.startswith('utm_content=')]

    # Incrementa contador e define novo utm_content
    count = r.incr(COUNTER_KEY)
    utm_value = f"v15n{count}"
    new_seg = f"utm_content={utm_value}"

    # Insere utm_content após utm_campaign ou no fim
    new_segments = []
    inserted = False
    for seg in segments:
        new_segments.append(seg)
        if seg.startswith('utm_campaign=') and not inserted:
            new_segments.append(new_seg)
            inserted = True
    if not inserted:
        new_segments.append(new_seg)

    # Monta URL com UTM atualizado
    updated_query = '&'.join(new_segments)
    updated_url = urllib.parse.urlunparse(parsed._replace(query=updated_query))
    print(f"[ShopeeRedirect] URL longa com UTM: {updated_url}")

    # Dispara evento ViewContent
    user_data = {
        "client_ip_address": request.client.host,
        "client_user_agent": request.headers.get("user-agent", "")
    }
    custom_data = {"content_ids": [parsed.path.split('/')[-1]], "content_type": "product"}
    send_fb_event("ViewContent", utm_value, updated_url, user_data, custom_data)

    # Gera e redireciona para o short link oficial da Shopee
    try:
        short_link = generate_short_link(updated_url)
        print(f"[ShopeeShortLink] Gerado: {short_link}")
    except Exception as e:
        print(f"[ShopeeShortLink] Falha: {e}")
        short_link = updated_url

    return RedirectResponse(url=short_link, status_code=302)

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
