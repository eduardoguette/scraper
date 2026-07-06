"""Extractores por plataforma. Cada uno devuelve un ScrapeResult.

Estrategia por plataforma (elegida por coste/fiabilidad desde una IP de
datacenter, verificada en vm-eddy):
  - instagram → navegador stealth (Scrapling). El HTML plano da un cascarón
    vacío; el stealth sí entrega og:title (caption), og:image y, cuando el
    post lo expone a usuarios no logueados, el video_url.
  - x         → api.fxtwitter.com (JSON público, gratis) con fallback a og.
  - tiktok    → oEmbed oficial (título/autor/thumbnail) + stealth para stats
    y video_url del JSON embebido.
  - youtube   → página watch (ytInitialPlayerResponse) + youtube-transcript-api
    para la transcripción nativa (gratis, exacta).
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, quote, urlparse

import httpx

from .links import extract_links
from .models import ScrapeResult

# UA de navegador móvil: los CDN de IG/TikTok y las páginas rechazan requests
# sin un UA creíble.
_BROWSER_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_HTTP_TIMEOUT = 20.0


# ─────────────────────────── helpers ───────────────────────────

def _unescape(s: str) -> str:
    """Decodifica escapes \\uXXXX y \\/ típicos del JSON embebido en HTML."""
    try:
        return s.encode().decode("unicode_escape")
    except Exception:
        return s.replace("\\/", "/")


def _first_int(*values) -> int | None:
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str):
            digits = re.sub(r"[^\d]", "", v)
            if digits:
                return int(digits)
    return None


def _hashtags_from(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"#([\wÀ-ɏ]+)", text or "")))


def _resolve_redirect(url: str) -> str:
    """Sigue redirects opacos (share links) hasta el permalink real."""
    try:
        with httpx.Client(follow_redirects=True, timeout=_HTTP_TIMEOUT,
                          headers={"User-Agent": _BROWSER_UA}) as c:
            r = c.head(url)
            if r.url:
                return str(r.url).split("?")[0]
    except Exception:
        pass
    return url


# ─────────────────────────── instagram ───────────────────────────

def _parse_ig_og_title(og_title: str | None) -> tuple[str | None, str]:
    """og:title de Instagram viene como:
        'Author on Instagram: "caption…"'  o  'Author on Instagram: caption'
    Devuelve (author, caption)."""
    if not og_title:
        return None, ""
    m = re.match(r"^(.*?) on Instagram(?:\s*[:\-–])?\s*(.*)$", og_title, re.DOTALL)
    if m:
        author = m.group(1).strip() or None
        caption = m.group(2).strip().strip('"').strip("“”").strip()
        return author, caption
    return None, og_title.strip()


def extract_instagram(url: str) -> ScrapeResult:
    from scrapling.fetchers import StealthyFetcher

    if "/share/" in url:
        url = _resolve_redirect(url)

    page = StealthyFetcher.fetch(
        url, headless=True, network_idle=True, timeout=60000
    )
    body = str(page.body)

    def og(prop: str) -> str | None:
        r = page.css(f'meta[property="{prop}"]::attr(content)')
        return str(r[0]) if r else None

    author, caption = _parse_ig_og_title(og("og:title"))
    og_desc = og("og:description") or ""

    # Stats: og:description a veces trae "N likes, M comments - author on ...".
    likes = comments = None
    stats = re.search(r"([\d,\.]+[KMB]?)\s+likes?,\s*([\d,\.]+[KMB]?)\s+comments?", og_desc, re.I)
    if stats:
        likes = _parse_count(stats.group(1))
        comments = _parse_count(stats.group(2))

    # video_url: probamos varias fuentes que IG expone a no-logueados según el post.
    video_url = og("og:video") or og("og:video:secure_url")
    if not video_url:
        m = re.search(r'"video_url"\s*:\s*"([^"]+)"', body)
        if m:
            video_url = _unescape(m.group(1))
    if not video_url:
        m = re.search(r'"contentUrl"\s*:\s*"([^"]+\.mp4[^"]*)"', body)
        if m:
            video_url = _unescape(m.group(1))

    thumbnail = og("og:image")

    return ScrapeResult(
        platform="instagram",
        caption=caption or og_desc,
        author=author,
        hashtags=_hashtags_from(caption or og_desc),
        likes=likes,
        comments=comments,
        thumbnail=thumbnail,
        video_url=video_url,
    )


def _parse_count(s: str) -> int | None:
    """'1,234' / '12.3K' / '4M' → entero."""
    if not s:
        return None
    s = s.strip().replace(",", "")
    mult = 1
    if s and s[-1].upper() in "KMB":
        mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[s[-1].upper()]
        s = s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return None


# ─────────────────────────── x / twitter ───────────────────────────

def extract_x(url: str, status_id: str) -> ScrapeResult:
    """api.fxtwitter.com: JSON público con texto, autor, media y stats."""
    api = f"https://api.fxtwitter.com/status/{status_id}"
    with httpx.Client(timeout=_HTTP_TIMEOUT, headers={"User-Agent": _DESKTOP_UA}) as c:
        r = c.get(api)
        r.raise_for_status()
        tweet = r.json().get("tweet") or {}

    text = tweet.get("text") or ""
    author = (tweet.get("author") or {}).get("name") or (tweet.get("author") or {}).get("screen_name")
    media = tweet.get("media") or {}
    videos = media.get("videos") or []
    photos = media.get("photos") or []

    video_url = None
    thumbnail = None
    if videos:
        video_url = videos[0].get("url")
        thumbnail = videos[0].get("thumbnail_url")
    if not thumbnail and photos:
        thumbnail = photos[0].get("url")

    return ScrapeResult(
        platform="x",
        caption=text,
        author=author,
        hashtags=_hashtags_from(text),
        views=_first_int(tweet.get("views")),
        likes=_first_int(tweet.get("likes")),
        comments=_first_int(tweet.get("replies")),
        thumbnail=thumbnail,
        video_url=video_url,
    )


# ─────────────────────────── tiktok ───────────────────────────

def extract_tiktok(url: str) -> ScrapeResult:
    if re.search(r"(?:vm|vt)\.tiktok\.com", url):
        url = _resolve_redirect(url)

    title = author = thumbnail = None
    # oEmbed oficial: título/autor/thumbnail sin navegador.
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, headers={"User-Agent": _DESKTOP_UA}) as c:
            r = c.get(f"https://www.tiktok.com/oembed?url={quote(url, safe='')}")
            if r.status_code == 200:
                d = r.json()
                title = d.get("title")
                author = d.get("author_name")
                thumbnail = d.get("thumbnail_url")
    except Exception:
        pass

    # Navegador stealth para video_url + stats del JSON embebido.
    video_url = None
    views = likes = comments = None
    caption = title or ""
    try:
        from scrapling.fetchers import StealthyFetcher

        page = StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=60000)
        body = str(page.body)
        state = _tiktok_state(body)
        if state:
            caption = state.get("desc") or caption
            author = author or (state.get("author") or {}).get("uniqueId")
            stats = state.get("stats") or {}
            views = _first_int(stats.get("playCount"))
            likes = _first_int(stats.get("diggCount"))
            comments = _first_int(stats.get("commentCount"))
            video = state.get("video") or {}
            video_url = video.get("playAddr") or video.get("downloadAddr")
            thumbnail = thumbnail or video.get("cover") or video.get("dynamicCover")
        if not video_url:
            m = re.search(r'"playAddr"\s*:\s*"([^"]+)"', body)
            if m:
                video_url = _unescape(m.group(1))
    except Exception:
        pass

    return ScrapeResult(
        platform="tiktok",
        title=title,
        caption=caption,
        author=author,
        hashtags=_hashtags_from(caption),
        views=views,
        likes=likes,
        comments=comments,
        thumbnail=thumbnail,
        video_url=video_url,
    )


def _tiktok_state(body: str) -> dict | None:
    """Extrae el itemStruct del __UNIVERSAL_DATA_FOR_REHYDRATION__ / SIGI_STATE."""
    m = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        body, re.DOTALL,
    )
    if m:
        try:
            data = json.loads(m.group(1))
            scope = data.get("__DEFAULT_SCOPE__", {})
            item = scope.get("webapp.video-detail", {}).get("itemInfo", {}).get("itemStruct")
            if item:
                return item
        except Exception:
            pass
    m = re.search(r'<script id="SIGI_STATE"[^>]*>(.*?)</script>', body, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            items = data.get("ItemModule", {})
            if items:
                return next(iter(items.values()))
        except Exception:
            pass
    return None


# ─────────────────────────── youtube ───────────────────────────

def youtube_video_id(url: str) -> str | None:
    try:
        u = urlparse(url)
        host = u.hostname.replace("www.", "") if u.hostname else ""
        if host == "youtu.be":
            return u.path.lstrip("/").split("/")[0] or None
        if host.endswith("youtube.com"):
            qs = parse_qs(u.query)
            if qs.get("v"):
                return qs["v"][0]
            m = re.search(r"/(?:shorts|embed|live)/([^/?]+)", u.path)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def extract_youtube(url: str, video_id: str) -> ScrapeResult:
    title = author = thumbnail = None
    duration = None

    # Página watch: ytInitialPlayerResponse trae título, autor, duración, thumb.
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, headers={"User-Agent": _DESKTOP_UA}) as c:
            r = c.get(f"https://www.youtube.com/watch?v={video_id}")
            body = r.text
        m = re.search(r"ytInitialPlayerResponse\s*=\s*(\{.*?\})\s*;\s*(?:var|</script>)", body, re.DOTALL)
        if m:
            data = json.loads(m.group(1))
            details = data.get("videoDetails", {})
            title = details.get("title")
            author = details.get("author")
            duration = _first_int(details.get("lengthSeconds"))
            thumbs = (details.get("thumbnail") or {}).get("thumbnails") or []
            if thumbs:
                thumbnail = thumbs[-1].get("url")
    except Exception:
        pass

    # oEmbed como fallback de metadatos.
    if not title:
        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT) as c:
                r = c.get(
                    "https://www.youtube.com/oembed",
                    params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
                )
                if r.status_code == 200:
                    d = r.json()
                    title = d.get("title")
                    author = d.get("author_name")
                    thumbnail = thumbnail or d.get("thumbnail_url")
        except Exception:
            pass

    if not thumbnail:
        thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    # Transcripción nativa (gratis y exacta). Preferimos manual sobre auto.
    transcript = _youtube_transcript(video_id)

    return ScrapeResult(
        platform="youtube",
        title=title,
        caption="",  # YouTube: el "texto" útil es el título + transcript
        author=author,
        duration_sec=duration,
        thumbnail=thumbnail,
        transcript=transcript,
    )


def _youtube_transcript(video_id: str) -> str | None:
    """youtube-transcript-api 1.x. Prefiere transcripción manual (humana) y
    cae a la automática. Idiomas preferidos: es, en, y luego cualquiera."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        api = YouTubeTranscriptApi()
        tlist = api.list(video_id)

        transcript = None
        for finder in (tlist.find_manually_created_transcript, tlist.find_generated_transcript):
            try:
                transcript = finder(["es", "en"])
                break
            except Exception:
                continue
        if transcript is None:
            transcript = next(iter(tlist), None)
        if transcript is None:
            return None

        fetched = transcript.fetch()
        # 1.x devuelve un FetchedTranscript iterable de snippets con .text.
        text = " ".join(getattr(s, "text", "") for s in fetched).strip()
        return text or None
    except Exception:
        return None


# ─────────────────────────── dispatch ───────────────────────────

def identify_and_extract(url: str) -> ScrapeResult | None:
    """Detecta plataforma y delega. Devuelve None si no es soportada."""
    if "instagram.com/share/" in url:
        url = _resolve_redirect(url)

    result: ScrapeResult | None = None
    if re.search(r"instagram\.com/(?:reel|reels|p)/", url):
        result = extract_instagram(url)
    else:
        m = re.search(r"(?:twitter|x)\.com/[^/]+/status/(\d+)", url)
        if m:
            result = extract_x(url, m.group(1))
        elif "tiktok.com/" in url:
            result = extract_tiktok(url)
        else:
            yt = youtube_video_id(url)
            if yt:
                result = extract_youtube(url, yt)

    if result is None:
        return None

    # Enlaces con etiqueta desde el caption (listas de recursos, etc.).
    result.links = extract_links(result.caption)
    return result
