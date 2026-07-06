"""API scraper de Later — reemplazo de Bright Data.

Expone POST /scrape {url} → ScrapeResult, autenticado con Bearer token
(LATER_SCRAPER_TOKEN). Corre en vm-eddy tras systemd. Las Edge Functions de
Later lo llaman en lugar de la API de Bright Data.

El scraping con navegador (Instagram/TikTok) es pesado en RAM, así que un
semáforo limita cuántos fetches stealth corren a la vez. Cada fetch se ejecuta
en un threadpool porque Scrapling es síncrono y bloquea el event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .extractors import identify_and_extract
from .models import ScrapeRequest, ScrapeResult

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scraper")

def _load_token() -> str:
    """Token desde env var o, preferentemente, desde un fichero (Docker secret).
    LATER_SCRAPER_TOKEN_FILE o el secret montado en /run/secrets/."""
    path = os.environ.get("LATER_SCRAPER_TOKEN_FILE") or "/run/secrets/later_scraper_token"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
            if value:
                return value
    except OSError:
        pass
    return os.environ.get("LATER_SCRAPER_TOKEN", "")


TOKEN = _load_token()
# Máximo de navegadores stealth concurrentes. La VM tiene 8 GB; cada Chromium
# headless come ~0.7–1 GB. 2 deja margen de sobra.
MAX_CONCURRENT = int(os.environ.get("SCRAPER_MAX_CONCURRENT", "2"))

app = FastAPI(title="Later Scraper", version="1.0.0")
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)


async def require_token(authorization: str = Header(default="")) -> None:
    if not TOKEN:
        raise HTTPException(status_code=500, detail="LATER_SCRAPER_TOKEN no configurado")
    expected = f"Bearer {TOKEN}"
    # Comparación en tiempo constante para no filtrar el token por timing.
    import hmac
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="no autorizado")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "later-scraper", "version": "1.0.0"}


@app.post("/scrape", response_model=ScrapeResult, dependencies=[Depends(require_token)])
async def scrape(body: ScrapeRequest) -> ScrapeResult | JSONResponse:
    url = (body.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url requerida")

    async with _semaphore:
        try:
            # Scrapling es síncrono → threadpool para no bloquear el loop.
            result = await asyncio.to_thread(identify_and_extract, url)
        except Exception as err:  # noqa: BLE001
            log.exception("scrape failed for %s", url)
            raise HTTPException(status_code=502, detail=f"scrape error: {err}") from err

    if result is None:
        return JSONResponse(
            status_code=422,
            content={"error": "Solo URLs de Instagram, TikTok, X o YouTube"},
        )

    log.info(
        "scraped %s platform=%s caption=%d video=%s transcript=%s",
        url, result.platform, len(result.caption or ""),
        bool(result.video_url), bool(result.transcript),
    )
    return result
