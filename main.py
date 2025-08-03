from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
import urllib.parse
import redis
import os

app = FastAPI()

# Conexão Redis
redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
r = redis.Redis.from_url(redis_url)

# Chave do contador global
COUNTER_KEY = "utm_counter"

@app.get("/{path:path}")
async def redirect_handler(request: Request, path: str):
    # Pega a URL original com query string completa
    original_query = str(request.url.query)
    
    # Constrói a URL base Shopee com os parâmetros existentes
    original_link = f"https://shopee.com.br/{path}?{original_query}"
    
    # Analisa os parâmetros
    parsed_url = urllib.parse.urlparse(original_link)
    query_params = urllib.parse.parse_qs(parsed_url.query)

    # Garante que o parâmetro utm_content existe com prefixo para substituição
    content_raw = query_params.get("utm_content", [""])[0]
    prefix = "".join(filter(str.isalpha, content_raw)) or "v15n"

    # Incrementa o contador
    current = r.incr(COUNTER_KEY)

    # Atualiza utm_content com o novo valor
    query_params["utm_content"] = [f"{prefix}{current}----"]

    # Reconstrói a query string na ordem original
    updated_query = "&".join([
        f"{key}={urllib.parse.quote_plus(value[0])}" for key, value in query_params.items()
    ])

    # Reconstrói a URL final para redirecionar
    final_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}?{updated_query}"

    return RedirectResponse(final_url, status_code=302)