# Later Scraper

API de scraping de contenido social para [Later](https://github.com/eduardoguette/later).
Reemplaza a Bright Data: extrae caption/autor/thumbnail/vídeo/stats de
**Instagram, TikTok, X (Twitter), YouTube y Facebook** y los devuelve en una
forma canónica que consumen las Edge Functions de Supabase de Later.

## Stack

- **FastAPI** + **uvicorn**
- **[Scrapling](https://github.com/D4Vinci/Scrapling)** con navegador stealth
  (Camoufox/Chromium) para Instagram y TikTok
- **fxtwitter** (API pública) para X
- **youtube-transcript-api** + página watch para YouTube (transcripción nativa)
- **UA de Googlebot** para Facebook (og:tags reales sin login; el audio sale
  del manifest DASH embebido, vía `audio_url`)

## API

Autenticación: header `Authorization: Bearer <LATER_SCRAPER_TOKEN>`.

### `POST /scrape`

```json
// request
{ "url": "https://www.instagram.com/reel/XXXX/" }

// response (ScrapeResult)
{
  "platform": "instagram",
  "title": null,
  "caption": "…",
  "author": "…",
  "hashtags": ["…"],
  "views": 123, "likes": 456, "comments": 7,
  "duration_sec": null,
  "thumbnail": "https://…",
  "video_url": "https://…",   // para transcribir con Whisper (puede ser null)
  "audio_url": null,
  "transcript": null           // YouTube trae transcript nativo; el resto null
}
```

- `422` si la URL no es de una plataforma soportada.
- `502` si el scrape falla.

### `GET /healthz`

Sin auth. `{ "ok": true }`.

## Configuración

Copia `.env.example` → `.env`:

| Variable | Descripción |
|----------|-------------|
| `LATER_SCRAPER_TOKEN` | Token Bearer. **Debe coincidir** con el secret `SCRAPER_TOKEN` de Supabase en Later. |
| `SCRAPER_MAX_CONCURRENT` | Navegadores stealth concurrentes (default 2). |

## Despliegue (vm-eddy / Dokploy)

La VM corre Dokploy + Traefik (TLS automático). Se despliega como app Docker
desde este repo de GitHub, con dominio `scraper.eduardoguette.com` → puerto
interno `8080`. Ver `docker-compose.yml` para despliegue manual.

## Desarrollo local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
export LATER_SCRAPER_TOKEN=dev-token
uvicorn app.main:app --reload --port 8090
```
