import os
import time
import re
import requests
import redis

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from dotenv import load_dotenv

# 1) Carrega .env
load_dotenv()
PIXEL_ID     = os.getenv("FB_PIXEL_ID")
ACCESS_TOKEN = os.getenv("FB_ACCESS_TOKEN")
VIDEO_ID     = int(os.getenv("VIDEO_ID", "15"))
REDIS_URL    = os.getenv("REDIS_URL")  # use a Internal Key Value URL sem senha

if not PIXEL_ID or not ACCESS_TOKEN or not REDIS_URL:
    raise RuntimeError("Defina FB_PIXEL_ID, FB_ACCESS_TOKEN e REDIS_URL nas Env Vars")

# 2) Conecta no Redis
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# 3) Regex para utm_content
pattern = re.compile(r"(?:^|&)(utm_content)=([^&]*)")

app = FastAPI()

def send_fb_event(event_name: str, event_id: str, url: str, ip: str, ua: str,
                  currency: str = None, value: float = None):
    endpoint = f"https://graph.facebook.com/v15.0/{PIXEL_ID}/events?access_token={ACCESS_TOKEN}"
    payload = {
        "data": [{
            "event_name":      event_name,
            "event_time":      int(time.time()),
            "action_source":   "website",
            "event_id":        event_id,
            "event_source_url": url,
            "user_data": {
                "client_ip_address": ip,
                "client_user_agent": ua
            }
        }]
    }
    if event_name.lower() == "purchase" and currency and value is not None:
        payload["data"][0]["custom_data"] = {"currency": currency, "value": value}
    resp = requests.post(endpoint, json=payload)
    if resp.status_code != 200:
        print("Erro Conversions API:", resp.status_code, resp.text)

@app.get("/{full_path:path}")
async def track_click(full_path: str, request: Request):
    # 4) Lê a query string exata
    raw_qs = request.scope["query_string"].decode("utf-8")
    if not raw_qs or "utm_content=" not in raw_qs:
        raise HTTPException(400, "Parâmetros faltando ou utm_content não encontrado")

    # 5) Incrementa contador no Redis
    contador = redis_client.incr("click_counter")
    novo_valor = f"v{VIDEO_ID}n{contador}----"

    # 6) Substitui apenas utm_content, preservando ordem dos outros params
    parts = raw_qs.split("&")
    new_parts = []
    for p in parts:
        if p.startswith("utm_content=") or "utm_content=" in p:
            key, _ = p.split("=", 1)
            new_parts.append(f"{key}={novo_valor}")
        else:
            new_parts.append(p)
    new_qs = "&".join(new_parts)

    # 7) Monta URLs
    web_link = f"https://shopee.com.br/{full_path}?{new_qs}"
    try:
        item_id, shop_id = full_path.split("-i.")[-1].split(".")
        app_link = f"shopee://product?itemId={item_id}&shopId={shop_id}&utm_content={novo_valor}"
    except ValueError:
        app_link = web_link

    ua = request.headers.get("user-agent", "")

    # 8) Se for Instagram in-app, devolve HTML que já dispara o deep-link
    if "Instagram" in ua:
        html = (
            f"<!DOCTYPE html><html><head>"
            f"<meta http-equiv=\"refresh\" content=\"0;url={app_link}\"/>"
            f"<script>window.location='{app_link}';</script>"
            f"</head><body></body></html>"
        )
        return HTMLResponse(html, status_code=200)

    # 9) Caso contrário, dispara evento e faz redirect
    send_fb_event(
        event_name="ViewContent",
        event_id=novo_valor,
        url=web_link,
        ip=request.client.host,
        ua=ua
    )
    return RedirectResponse(web_link, status_code=307)
