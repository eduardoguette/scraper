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
  - facebook  → HTML plano con UA de Googlebot. Facebook le sirve a crawlers
    conocidos (SEO) los og:tags reales incluso para /share/r/ sin login;
    con cualquier otro UA (o sin UA de bot) devuelve un muro de login. El
    audio va aparte en el manifest DASH embebido (video mudo + audio-only).
"""

from __future__ import annotations

import html as html_module
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
# Facebook le sirve og:tags reales a crawlers conocidos (SEO) sin exigir login;
# con UA de navegador normal devuelve un muro de login/checkpoint.
_GOOGLEBOT_UA = "Googlebot/2.1 (+http://www.google.com/bot.html)"

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


# ─────────────────────────── genérico (no-social) ───────────────────────────

def extract_generic(url: str) -> ScrapeResult:
    """Fetch genérico con navegador stealth para páginas no-social (noticias,
    blogs, etc.) detrás de un WAF/reto JS que bloquea un fetch plano desde una
    IP de datacenter. Devuelve el HTML renderizado tal cual — el caller
    (enrich-web en Later) hace su propia extracción de título/descripción/
    JSON-LD sobre ese HTML, igual que si hubiera llegado por fetch directo."""
    from scrapling.fetchers import StealthyFetcher

    page = StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=60000)
    return ScrapeResult(platform="web", html=str(page.body))


# ─────────────────────────── facebook ───────────────────────────

# "4,4 mill." / "126 mil" (es) o "4.4M" / "126K" (en) → multiplicador.
_FB_STAT_MULT = {"mill.": 1_000_000, "mil": 1_000, "M": 1_000_000, "K": 1_000}


def _parse_fb_stat(num: str, suffix: str | None) -> int | None:
    if not num:
        return None
    if suffix in ("mill.", "mil", "M", "K"):
        try:
            value = float(num.replace(",", ".")) * _FB_STAT_MULT[suffix]
        except ValueError:
            return None
    else:
        digits = re.sub(r"[^\d]", "", num)
        if not digits:
            return None
        value = int(digits)
    return int(value)


def _fb_stats_from_title(og_title: str | None) -> tuple[int | None, int | None]:
    """og:title de un reel viene como
    'N reproducciones · M reacciones | caption | Página'."""
    text = html_module.unescape(og_title or "")
    views = likes = None
    m = re.search(r"([\d.,]+)\s*(mill\.|mil|M|K)?\s*(?:reproducciones|views)", text, re.I)
    if m:
        views = _parse_fb_stat(m.group(1), m.group(2))
    m = re.search(r"([\d.,]+)\s*(mill\.|mil|M|K)?\s*(?:reacciones|reactions|likes)", text, re.I)
    if m:
        likes = _parse_fb_stat(m.group(1), m.group(2))
    return views, likes


def _fb_author_from_title(og_title: str | None) -> str | None:
    """El último segmento tras '|' en og:title es el nombre de la página."""
    text = html_module.unescape(og_title or "")
    parts = [p.strip() for p in text.split("|")]
    return parts[-1] if len(parts) > 1 and parts[-1] else None


def _fb_dash_media(body: str) -> tuple[str | None, str | None]:
    """El manifest DASH embebido separa vídeo (mudo) y audio; devolvemos la
    representación de mayor bitrate de cada uno. Sin esto no hay audio para
    transcribir: Facebook no expone un mp4 progresivo (con voz) a crawlers."""
    m = re.search(r'"dash_manifest_xml_string"\s*:\s*"((?:\\.|[^"\\])*)"', body)
    if not m:
        return None, None
    try:
        xml_str = json.loads('"' + m.group(1) + '"')
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_str)
    except Exception:
        return None, None

    ns = "{urn:mpeg:dash:schema:mpd:2011}"
    video_url = audio_url = None
    best_video_bw = best_audio_bw = -1
    for adaptation_set in root.iter(f"{ns}AdaptationSet"):
        content_type = adaptation_set.get("contentType")
        for representation in adaptation_set.iter(f"{ns}Representation"):
            bandwidth = int(representation.get("bandwidth") or 0)
            base_url = representation.find(f"{ns}BaseURL")
            url = base_url.text if base_url is not None else None
            if not url:
                continue
            if content_type == "video" and bandwidth > best_video_bw:
                best_video_bw, video_url = bandwidth, url
            elif content_type == "audio" and bandwidth > best_audio_bw:
                best_audio_bw, audio_url = bandwidth, url
    return video_url, audio_url


def extract_facebook(url: str) -> ScrapeResult:
    with httpx.Client(
        follow_redirects=True, timeout=_HTTP_TIMEOUT,
        headers={"User-Agent": _GOOGLEBOT_UA, "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"},
    ) as c:
        r = c.get(url)
        r.raise_for_status()
        body = r.text

    def meta(prop: str) -> str | None:
        m = re.search(
            rf'<meta[^>]*property="{re.escape(prop)}"[^>]*content="([^"]*)"', body, re.I,
        )
        return html_module.unescape(m.group(1)) if m else None

    og_title = meta("og:title")
    caption = meta("og:description") or ""
    thumbnail = meta("og:image")
    views, likes = _fb_stats_from_title(og_title)
    author = _fb_author_from_title(og_title)
    # video_url es mudo en el DASH de Facebook (pistas separadas); el audio
    # real va en audio_url, el campo que Whisper usa como fallback.
    _, audio_url = _fb_dash_media(body)

    return ScrapeResult(
        platform="facebook",
        caption=caption,
        author=author,
        hashtags=_hashtags_from(caption),
        views=views,
        likes=likes,
        thumbnail=thumbnail,
        audio_url=audio_url,
    )


# ─────────────────────────── dispatch ───────────────────────────

def identify_and_extract(url: str, want_html: bool = False) -> ScrapeResult | None:
    """Detecta plataforma y delega. Si no es una plataforma social soportada:
    devuelve None (comportamiento de siempre) salvo que `want_html=True`, en
    cuyo caso cae al fetch genérico con navegador stealth."""
    if "instagram.com/share/" in url:
        url = _resolve_redirect(url)

    result: ScrapeResult | None = None
    if re.search(r"instagram\.com/(?:reel|reels|p)/", url):
        result = extract_instagram(url)
    elif re.search(r"(?:facebook\.com|fb\.watch)/", url):
        result = extract_facebook(url)
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
        if not want_html:
            return None
        result = extract_generic(url)

    # Enlaces con etiqueta desde el caption (listas de recursos, etc.).
    result.links = extract_links(result.caption)
    return result
