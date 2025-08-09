import os
import time
import requests
import redis
import urllib.parse
import csv
from fastapi import FastAPI, Request, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response

# ─── LINK PADRÃO DO PRODUTO SHOPEE ───
# Cole aqui o link completo do produto Shopee que será usado quando nenhum parâmetro for passado
DEFAULT_PRODUCT_URL = "https://shopee.com.br/XEIJAIYI-8pcs-Kit-De-Gel-De-Extens%C3%A3o-De-Unhas-De-Polietileno-15ml-Nude-Pink-All-In-One-Construtor-Cola-Com-Formas-Duplas-Clipes-Manicure-Set-For-Beginnerer-i.1006215031.25062459693?sp_atk=7d9b4afa-fe7b-46a4-8d67-40beca78c014&uls_trackid=53c5r00o00b3&utm_campaign=id_K6tYTxT2w8&utm_content=----&utm_medium=affiliates&utm_source=an_18314810331&utm_term=dfkmaxk3b6rb&xptdk=7d9b4afa-fe7b-46a4-8d67-40beca78c014"

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
        print(f"[MetaAPI] Status: {resp.status_code}")
        print(f"[MetaAPI] Response: {resp.text}")
        resp.raise_for_status()
    except Exception as e:
        print(f"[MetaAPI] Exception sending event: {e}")

@app.get("/", response_class=HTMLResponse)
async def redirect_to_shopee(request: Request, product: str = Query(None, description="URL original Shopee (URL-encoded), ou vazio para usar default")):
    # Define URL do produto: parâmetro ou default
    url_in = urllib.parse.unquote_plus(product) if product else DEFAULT_PRODUCT_URL
    parsed = urllib.parse.urlparse(url_in)

    # Injeta novo utm_content em raw query
    segments = (parsed.query or "").split('&') if parsed.query else []
    # Remove utm_content existente
    segments = [seg for seg in segments if not seg.startswith('utm_content=')]

    # Incrementa contador e define utm_value
    count = r.incr(COUNTER_KEY)
    sub_id = f"v15n{count}"
    utm_value = sub_id
    new_seg = f"utm_content={utm_value}"

    # Insere após utm_campaign ou no fim
    new_segments = []
    inserted = False
    for seg in segments:
        new_segments.append(seg)
        if seg.startswith('utm_campaign=') and not inserted:
            new_segments.append(new_seg)
            inserted = True
    if not inserted:
        new_segments.append(new_seg)

    new_query = '&'.join(new_segments)
    updated_url = urllib.parse.urlunparse(parsed._replace(query=new_query))
    print(f"[ShopeeRedirect] Updated URL: {updated_url}")

    # Dispara ViewContent
    user_data = {
        "client_ip_address": request.client.host,
        "client_user_agent": request.headers.get("user-agent", "")
    }
    custom_data = {"content_ids": [parsed.path.split('/')[-1]], "content_type": "product"}
    send_fb_event("ViewContent", sub_id, updated_url, user_data, custom_data)

    # Detecta mobile vs desktop
    ua = request.headers.get("user-agent", "").lower()
    is_mobile = any(m in ua for m in ["android", "iphone", "ipad"])
    if not is_mobile:
        return RedirectResponse(url=updated_url)

    # Deep-link para mobile
    host_path = parsed.netloc + parsed.path
    intent_link = (
        f"intent://{host_path}#Intent;scheme=https;package=com.shopee.br;"
        f"S.browser_fallback_url={urllib.parse.quote(updated_url, safe='')};end"
    )
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
            send_fb_event("Purchase", utm, event_url, {}, {"currency":"BRL","value":valor,"num_purchases":vendas})
            results.append({"utm_content": utm, "status": "sent"})
    return {"processed": results}
