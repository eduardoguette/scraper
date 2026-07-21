"""Forma canónica que devuelve el scraper.

Es un reemplazo directo de lo que antes entregaba Bright Data: las Edge
Functions de Later (`_shared/reel-enrich.ts` → `normalize()`) consumen estos
campos. Todos son opcionales salvo `platform`, y el lado de Supabase degrada
con gracia cuando faltan (p.ej. sin `video_url` no transcribe; sin `caption`
usa el fallback de visión)."""

from __future__ import annotations

from pydantic import BaseModel


class ScrapeRequest(BaseModel):
    url: str
    # Si es True y la URL no es de una plataforma social soportada, en vez de
    # devolver 422 hacemos un fetch genérico con navegador stealth y devolvemos
    # el HTML renderizado en `html` (el caller decide cómo extraerlo). Si es
    # False (default), el comportamiento no cambia — compatibilidad total con
    # callers existentes (recover-cover, enrich-reel).
    html: bool = False


class ScrapeResult(BaseModel):
    platform: str
    # YouTube trae título real del vídeo; reels/posts no.
    title: str | None = None
    caption: str = ""
    author: str | None = None
    hashtags: list[str] = []
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    duration_sec: float | None = None
    thumbnail: str | None = None
    # Enlaces extraídos del caption, con su etiqueta ("detalle"). Para posts que
    # son listas curadas de recursos (el resumen de IA los aplana y pierde).
    links: list[dict] = []
    # Media para transcribir con Whisper en Supabase. `video_url` es preferente
    # (lleva la voz real publicada); `audio_url` es el fallback.
    video_url: str | None = None
    audio_url: str | None = None
    # Transcripción ya resuelta en origen (YouTube nativo). Evita pasar por
    # Whisper cuando está disponible.
    transcript: str | None = None
    # HTML renderizado por el navegador stealth — solo presente cuando
    # `platform == "web"` (modo genérico, ver ScrapeRequest.html).
    html: str | None = None
