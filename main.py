import os
import time
import json
import hashlib
import requests
import urllib.parse
from fastapi import FastAPI, Request, Query
from fastapi.responses import RedirectResponse, HTMLResponse

# CONFIGURAÇÕES
APP_ID = os.getenv("SHOPEE_APP_ID", "18314810331")
APP_SECRET = os.getenv("SHOPEE_APP_SECRET", "LO3QSEG45TYP4NYQBRXLA2YYUL3ZCUPN")
ENDPOINT = "https://open-api.affiliate.shopee.com.br/graphql"
DEFAULT_PRODUCT_URL = os.getenv(
    "DEFAULT_PRODUCT_URL",
    "https://shopee.com.br/seu-produto-exemplo-i.123456789.987654321"
)
VIDEO_ID = "v15"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COUNTER_FILE = os.path.join(BASE_DIR, "utm_counter.json")

app = FastAPI()

def load_count():
    if os.path.exists(COUNTER_FILE):
        try:
            with open(COUNTER_FILE, "r") as f:
                data = json.load(f)
                return int(data.get("count", 0))
        except Exception:
            return 0
    return 0

def save_count(n):
    with open(COUNTER_FILE, "w") as f:
        json.dump({"count": n}, f)

def next_utm():
    count = load_count() + 1
    save_count(count)
    print(f"Gerando UTM: {VIDEO_ID}n{count} (contador anterior: {count - 1})")
    return f"{VIDEO_ID}n{count}"

def generate_short_link(origin_url: str, utm_content: str) -> str:
    payload_obj = {
        "query": (
            "mutation{generateShortLink(input:{"
            f"originUrl:\"{origin_url}\","
            f"subIds:[\"\",\"\",\"{utm_content}\",\"\",\"\"]"
            "}){shortLink}}"
        )
    }
    payload = json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=False)
    timestamp = str(int(time.time()))
    base_str = APP_ID + timestamp + payload + APP_SECRET
    signature = hashlib.sha256(base_str.encode("utf-8")).hexdigest()

    headers = {
        "Authorization": f"SHA256 Credential={APP_ID}, Timestamp={timestamp}, Signature={signature}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(ENDPOINT, headers=headers, data=payload, timeout=10)
        print(f"[ShopeeAPI] Status: {resp.status_code}")
        print(f"[ShopeeAPI] Response: {resp.text}")
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            print(f"[ShopeeAPI] Erros: {data['errors']}")
            return origin_url
        short_link = data.get("data", {}).get("generateShortLink", {}).get("shortLink")
        if short_link:
            print(f"Short link gerado ({utm_content}): {short_link}")
            return short_link
        print("[ShopeeAPI] shortLink não encontrado na resposta")
        return origin_url
    except Exception as e:
        print(f"[ShopeeAPI] Exceção ao gerar short link: {e}")
        return origin_url

@app.get("/", response_class=HTMLResponse)
async def redirect_to_shopee(request: Request, product: str = Query(None)):
    origin_url = urllib.parse.unquote_plus(product) if product else DEFAULT_PRODUCT_URL
    utm_content = next_utm()
    short_link = generate_short_link(origin_url, utm_content)

    ua = request.headers.get("user-agent", "").lower()
    is_mobile = any(m in ua for m in ["android", "iphone", "ipad"])

    if not is_mobile:
        return RedirectResponse(url=short_link)
    else:
        parsed = urllib.parse.urlparse(origin_url)
        host_path = parsed.netloc + parsed.path
        intent_link = (
            f"intent://{host_path}#Intent;scheme=https;package=com.shopee.br;"
            f"S.browser_fallback_url={urllib.parse.quote(short_link, safe='')};end"
        )
        html = f"""
        <!DOCTYPE html>
        <html lang="pt-BR">
          <head><meta charset="UTF-8"><title>Redirecionando...</title></head>
          <body style="display:flex;justify-content:center;align-items:center;flex-direction:column;height:100vh;margin:0;font-size:20px;text-align:center;">
            <p>Você está sendo redirecionado para o app da Shopee...</p>
            <a id="open-btn" href="{intent_link}">Abrir</a>
            <script>window.onload=()=>document.getElementById('open-btn').click();</script>
          </body>
        </html>
        """
        return HTMLResponse(content=html)
