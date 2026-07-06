"""Extracción de enlaces con etiqueta desde el caption de un post.

Muchos posts sociales son listas curadas de recursos con la forma:

    → Google AI Studio
    aistudio.google.com

    → OpenRouter (modelos: free)
    openrouter.ai

El resumen de IA aplana esto y pierde los links. Aquí los recuperamos como
`[{title, url}]` asociando cada URL con su "detalle" (la línea-etiqueta de al
lado): el texto que precede a la URL en la misma línea, o la línea no vacía
anterior. Si no hay etiqueta clara, se usa el dominio.
"""

from __future__ import annotations

import re

# TLDs comunes para reconocer dominios "pelados" (sin http://) sin colar
# falsos positivos tipo "gemini-3.5-flash". Ampliable.
_COMMON_TLDS = (
    "com|org|net|io|ai|dev|app|co|xyz|me|gg|so|sh|to|tv|fm|es|eu|us|uk|de|fr|"
    "it|nl|ca|info|tech|design|studio|cloud|page|link|new|run|build"
)

# URL con esquema explícito, o dominio pelado con TLD común (+ ruta opcional).
_URL_RE = re.compile(
    r"(?<![\w@])(?:"
    r"https?://[^\s<>()\"']+"
    r"|(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:" + _COMMON_TLDS + r")"
    r"(?:/[^\s<>()\"']*)?"
    r")",
    re.IGNORECASE,
)

# Prefijos decorativos a quitar del inicio de una etiqueta.
_BULLET_RE = re.compile(r"^\s*(?:[-*•·▪◦→➡➤‣>»\d]+[.)]?\s*)+", re.UNICODE)
# Emojis / símbolos sueltos al inicio (rango amplio) + espacios.
_LEADING_SYMBOLS_RE = re.compile(
    r"^[\s←-⇿⌀-➿⬀-⯿\U0001F000-\U0001FAFF✓✔☑]+"
)

# Marcadores de "ítem de lista" al inicio de línea (→ Nombre, • Nombre, 🛡️ Nombre…).
_LIST_MARKER_RE = re.compile(
    r"^\s*(?:[-*•·▪◦→➡➤‣>»]|\d+[.)]|[✓✔☑]|[\U0001F000-\U0001FAFF←-➿])"
)


def _clean_label(text: str) -> str:
    # Quitar selectores de variación / ZWJ que dejan restos tras los emojis.
    text = text.replace("️", "").replace("︎", "").replace("‍", "")
    text = text.strip()
    text = _BULLET_RE.sub("", text)
    text = _LEADING_SYMBOLS_RE.sub("", text)
    text = text.strip(" \t:–-—·").strip()
    # Patrón "Nombre → descripción" o "Nombre - descripción": quedarse con el
    # nombre (antes del separador con espacios).
    text = re.split(r"\s+[→➡➤‣]\s+|\s+[-–—]\s+", text)[0].strip()
    # Etiqueta demasiado larga → probablemente es una frase, no un nombre.
    if len(text) > 80:
        text = text[:80].rsplit(" ", 1)[0] + "…"
    return text


def _normalize_url(url: str) -> str:
    url = url.rstrip(".,);:!?…'\"")
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _label_from_domain(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    host = (m.group(1) if m else url).replace("www.", "")
    return host


def extract_links(caption: str) -> list[dict]:
    """Devuelve [{title, url}] en orden de aparición, deduplicando por URL."""
    if not caption:
        return []

    lines = caption.split("\n")
    results: list[dict] = []
    seen: set[str] = set()

    for i, line in enumerate(lines):
        for m in _URL_RE.finditer(line):
            raw = m.group(0)
            url = _normalize_url(raw)
            key = url.lower().rstrip("/")
            if key in seen:
                continue

            # Etiqueta: texto antes de la URL en la misma línea (si aporta algo
            # más que un bullet)…
            before = line[: m.start()].strip()
            label = _clean_label(before) if before else ""
            # …o la línea no vacía anterior, PERO solo si es un ítem de lista
            # (empieza con →/•/emoji). Si es una frase descriptiva, mejor el
            # dominio que una etiqueta engañosa.
            if not label:
                j = i - 1
                while j >= 0 and not lines[j].strip():
                    j -= 1
                if (
                    j >= 0
                    and not _URL_RE.search(lines[j])
                    and _LIST_MARKER_RE.search(lines[j])
                ):
                    label = _clean_label(lines[j])
            if not label:
                label = _label_from_domain(url)

            seen.add(key)
            results.append({"title": label, "url": url})

    return results
