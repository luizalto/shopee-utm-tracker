import os
import time
import re
import requests
import redis

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from dotenv import load_dotenv

load_dotenv()
PIXEL_ID     = os.getenv("FB_PIXEL_ID")
ACCESS_TOKEN = os.getenv("FB_ACCESS_TOKEN")
VIDEO_ID     = int(os.getenv("VIDEO_ID", "15"))
REDIS_URL    = os.getenv("REDIS_URL")
if not PIXEL_ID or not ACCESS_TOKEN or not REDIS_URL:
    raise RuntimeError("Defina FB_PIXEL_ID, FB_ACCESS_TOKEN e REDIS_URL nas Env Vars")

redis_client = redis.from_url(REDIS_URL, decode_responses=True)
pattern = re.compile(r"(?:^|&)(utm_content)=([^&]*)")

app = FastAPI()

def send_fb_event(name, eid, url, ip, ua):
    endpoint = f"https://graph.facebook.com/v15.0/{PIXEL_ID}/events?access_token={ACCESS_TOKEN}"
    payload = {
        "data": [{
            "event_name": name,
            "event_time": int(time.time()),
            "action_source": "website",
            "event_id": eid,
            "event_source_url": url,
            "user_data": {"client_ip_address": ip, "client_user_agent": ua}
        }]
    }
    requests.post(endpoint, json=payload)

@app.get("/r/{short_code}")
async def redirect_affiliate(short_code: str, request: Request):
    """
    Proxy curto: /r/AKQ4gdI2kq  →  s.shopee.com.br/AKQ4gdI2kq
    Com UTM automático + deep-link no Instagram.
    """
    ua = request.headers.get("user-agent", "")
    # 1) Incrementa nosso contador
    contador = redis_client.incr("click_counter")
    novo = f"v{VIDEO_ID}n{contador}----"

    # 2) Monta o affiliate-web e (se quiser) o deep-link
    affiliate_web = f"https://s.shopee.com.br/{short_code}"
    # Se quiser deep-link ao app, precisa extrair itemId/shopId:
    # (exemplo fixo abaixo; ideal é mapear via DB)
    item_id, shop_id = "1006215031", "25062459693"
    app_link = f"shopee://product?itemId={item_id}&shopId={shop_id}&utm_content={novo}"

    # 3) No Instagram in-app, redireciona direto ao app
    if "Instagram" in ua:
        return RedirectResponse(app_link, status_code=302)

    # 4) Caso padrão, dispara evento e vai pro short-link oficial
    send_fb_event("ViewContent", novo, affiliate_web, request.client.host, ua)
    return RedirectResponse(affiliate_web, status_code=307)