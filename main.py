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
    "https://shopee.com.br/product/1006215031/25062459693?gads_t...dium=affiliates&utm_source=an_18314810331&utm_term=dfo9czqqfhwm"
)

# ─── CONFIGURAÇÕES META (Conversions API) ────────────────────────────────────
PIXEL_ID     = os.getenv("FB_PIXEL_ID") or os.getenv("META_PIXEL_ID")
ACCESS_TOKEN = os.getenv("FB_ACCESS_TOKEN") or os.getenv("META_ACCESS_TOKEN")
FB_CAPI_URL  = f"https://graph.facebook.com/v14.0/{PIXEL_ID}/events?access_token={ACCESS_TOKEN}"

# ─── REDIS PARA CONTADOR DE UTM ──────────────────────────────────────────────
redis_url    = os.getenv("REDIS_URL", "redis://localhost:6379/0")
r            = redis.from_url(redis_url)
COUNTER_KEY  = "utm_counter"
VIDEO_ID     = os.getenv("VIDEO_ID", "v15")

app = FastAPI()


def next_utm() -> str:
    count = r.incr(COUNTER_KEY)
    utm = f"{VIDEO_ID}n{count}"
    print(f"Gerando UTM: {utm}")
    return utm


def generate_short_link(long_url: str) -> str:
    # Lógica original de mutation GraphQL para encurtar o link via Shopee
    # (mantida igual ao original)
    pass


def send_fb_event(
    event_name: str,
    event_id: str,
    event_source_url: str,
    user_data: dict,
    custom_data: dict
):
    payload = {"data": [{
        "event_name":       event_name,
        "event_time":       int(time.time()),
        "event_id":         event_id,
        "action_source":    "website",
        "event_source_url": event_source_url,
        "user_data":        user_data,
        "custom_data":      custom_data
    }]}

    resp = requests.post(FB_CAPI_URL, json=payload, timeout=5)
    print(f"[MetaAPI] Status:   {resp.status_code}")
    print(f"[MetaAPI] Response: {resp.text}")
    try:
        resp.raise_for_status()
    except requests.HTTPError:
        print(f"[MetaAPI] Payload enviado:\n{json.dumps(payload, ensure_ascii=False, indent=2)}")
        raise


@app.get("/", response_class=RedirectResponse)
async def redirect_to_shopee(
    request: Request,
    product: str = Query(
        None,
        description="URL original da Shopee (URL-encoded). Se não informada, usa DEFAULT_PRODUCT_URL."
    )
):
    url_in = urllib.parse.unquote_plus(product) if product else DEFAULT_PRODUCT_URL
    parsed = urllib.parse.urlparse(url_in)
    segments = parsed.query.split("&") if parsed.query else []
    segments = [seg for seg in segments if not seg.startswith("utm_content=")]
    utm_value = next_utm()
    segments.append(f"utm_content={utm_value}")
    new_query = "&".join(segments)
    new_url = urllib.parse.urlunparse(parsed._replace(query=new_query))
    # Mantém a geração de short link como no original
    short_link = generate_short_link(new_url)
    return RedirectResponse(short_link)


@app.post("/upload_csv")
async def upload_csv(
    request: Request,
    file: UploadFile = File(...)
):
    event_url = str(request.url)

    # Lê CSV com utf-8-sig para remover BOM
    content_str = file.file.read().decode('utf-8-sig')
    lines       = content_str.splitlines()
    reader      = csv.DictReader(lines)
    print("CSV Headers:", reader.fieldnames)

    results = []
    for row in reader:
        utm    = row.get('utm_content')
        vendas = int(row.get('vendas', '0') or 0)
        valor  = float(row.get('valor', '0') or 0.0)

        if vendas > 0 and utm:
            send_fb_event(
                "Purchase",
                utm,
                event_url,
                {},  # user_data pode ser enriquecido conforme necessidade
                {
                    "currency":      "BRL",
                    "value":         valor,
                    "num_purchases": vendas
                }
            )
            results.append({"utm_content": utm, "status": "sent"})

    return {"processed": results}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
