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
REDIS_URL    = os.getenv("REDIS_URL")  # URL fornecida pelo Redis do Render

if not PIXEL_ID or not ACCESS_TOKEN or not REDIS_URL:
    raise RuntimeError("Defina FB_PIXEL_ID, FB_ACCESS_TOKEN e REDIS_URL no .env")

# conecta no Redis
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

pattern = re.compile(r"(?:^|&)(utm_content)=([^&]*)")
app = FastAPI()

@app.get("/{full_path:path}")
async def track_click(full_path: str, request: Request):
    # ... validações iniciais ...

    # incrementa no Redis e já obtém o novo valor
    contador = redis_client.incr("click_counter")
    novo_valor = f"v{VIDEO_ID}n{contador}----"

    # preserva todos os params e só altera utm_content...
    # (mesma lógica de split/join que fizemos antes)

    # monta web_link e app_link
    # dispara evento no Facebook
    # retorna RedirectResponse
