# Later Scraper — Scrapling (navegador stealth) + FastAPI.
# En Docker instalamos las dependencias de Chromium como root en build,
# evitando el parche de LD_LIBRARY_PATH que hace falta en un host sin sudo.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Dependencias de Python (incluye scrapling[fetchers], playwright, patchright).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Navegadores + sus dependencias del sistema. `install-deps` necesita root
# (lo tenemos en build) y trae libatk, libgbm, etc.
RUN playwright install-deps chromium \
    && playwright install chromium \
    && patchright install chromium

COPY app ./app

EXPOSE 8080
# Traefik/Dokploy enruta el dominio a este puerto interno del contenedor.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
