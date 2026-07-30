from __future__ import annotations

import base64
import html
import json
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from .models import Finding
from .providers import identify_providers, provider_markers


ABSOLUTE_URL_RE = re.compile(r"(?i)(?:https?:)?//[^\s\"'<>\\]+")
CSS_URL_RE = re.compile(r"(?i)(?:url\(\s*|@import\s+)[\"']?([^\"')\s;]+)")
PERCENT_URL_RE = re.compile(r"(?i)https?%3a%2f%2f[^\s\"'<>]+")
RELATIVE_QUOTED_URL_RE = re.compile(
    r"""(?x)
    ["']
    (
        (?:/|\./|\.\./)
        [^"'\\\s<>]{1,1000}
    )
    ["']
    """
)
BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/])([A-Za-z0-9+/]{24,4096}={0,2})(?![A-Za-z0-9+/])")
LOC_RE = re.compile(r"(?is)<loc\b[^>]*>\s*(.*?)\s*</loc>")
META_REFRESH_RE = re.compile(r"(?i)(?:^|;)\s*url\s*=\s*(.+?)\s*$")

URL_ATTRIBUTES = {
    "href",
    "src",
    "action",
    "poster",
    "data",
    "data-src",
    "data-href",
    "data-url",
    "data-link",
    "data-embed",
    "data-player",
    "data-video",
    "formaction",
}

BINARY_EXTENSIONS = {
    ".7z",
    ".avi",
    ".avif",
    ".bmp",
    ".doc",
    ".docx",
    ".eot",
    ".flac",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".ogg",
    ".otf",
    ".pdf",
    ".png",
    ".rar",
    ".svg",
    ".tar",
    ".ttf",
    ".wav",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".xls",
    ".xlsx",
    ".zip",
}


@dataclass
class Extraction:
    crawl_links: set[str]
    findings: list[Finding]
    movie_name: str | None = None
    tmdb_id: int | None = None
    media_type: str | None = None


TMDB_URL_RE = re.compile(
    r"(?i)(?:themoviedb\.org|tmdb\.org)/(movie|tv)/(\d{1,12})"
)
TMDB_KEY_RE = re.compile(
    r"""(?ix)
    ["']?(?:tmdb[_-]?id|id[_-]?tmdb)["']?
    \s*[:=]\s*
    ["']?(\d{1,12})
    """
)
TMDB_DATA_RE = re.compile(
    r"""(?ix)data-(?:tmdb-id|tmdbid)\s*=\s*["'](\d{1,12})["']"""
)
TITLE_KEYS = ("movie_name", "movieName", "movie_title", "movieTitle", "title", "name")
MEDIA_TYPE_KEYS = {"mediatype", "tmdbtype", "contenttype"}


def _normalize_media_type(value: object) -> str | None:
    normalized = re.sub(r"[^a-z]", "", str(value).lower())
    if normalized in {"tv", "serie", "series", "tvseries", "tvshow"}:
        return "serie"
    if normalized in {"movie", "film", "filme"}:
        return "filme"
    return None


def _clean_movie_name(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = html.unescape(re.sub(r"<[^>]+>", " ", str(value)))
    cleaned = " ".join(cleaned.split()).strip(" |-–—")
    if not cleaned or len(cleaned) > 300:
        return None
    cleaned = re.sub(r"(?i)^assistir\s+(?:ao\s+)?", "", cleaned).strip()
    cleaned = re.sub(
        r"(?i)(?:\s+(?:dublado|legendado|online|gr[aá]tis))+$",
        "",
        cleaned,
    ).strip()
    # Remove sufixos comuns de SEO sem cortar títulos que contenham hífen.
    cleaned = re.sub(
        r"""(?ix)
        \s*(?:[-|–—]\s*)?
        (?:
            assistir\s+(?:online\s*)?(?:gr[aá]tis)? |
            filme\s+online |
            pobreflix(?:\s*hd)?
        )
        (?:\s*[-|–—].*)?$
        """,
        "",
        cleaned,
    ).strip(" |-–—")
    return cleaned or None


def _json_media_metadata(body: str) -> tuple[str | None, int | None, str | None]:
    stripped = body.strip()
    if not stripped or stripped[0] not in "[{":
        return None, None, None
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return None, None, None

    title: str | None = None
    tmdb_id: int | None = None
    media_type: str | None = None

    def visit(value) -> None:
        nonlocal title, tmdb_id, media_type
        if isinstance(value, dict):
            for key, child in value.items():
                normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if tmdb_id is None and normalized_key in {"tmdbid", "idtmdb"}:
                    try:
                        candidate = int(str(child).strip())
                    except (TypeError, ValueError):
                        candidate = 0
                    if 0 < candidate <= 999_999_999_999:
                        tmdb_id = candidate
                if title is None and key in TITLE_KEYS and isinstance(child, str):
                    title = _clean_movie_name(child)
                if media_type is None and (
                    normalized_key in MEDIA_TYPE_KEYS or normalized_key == "type"
                ):
                    media_type = _normalize_media_type(child)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return title, tmdb_id, media_type


def _html_movie_name(tree: HTMLParser) -> str | None:
    meta_candidates = (
        'meta[property="og:title"]',
        'meta[name="twitter:title"]',
        'meta[itemprop="name"]',
    )
    for selector in meta_candidates:
        node = tree.css_first(selector)
        if node:
            candidate = _clean_movie_name(node.attributes.get("content"))
            if candidate:
                return candidate
    heading = tree.css_first("h1")
    if heading:
        candidate = _clean_movie_name(heading.text(separator=" ", strip=True))
        if candidate:
            return candidate
    title = tree.css_first("title")
    if title:
        return _clean_movie_name(title.text(separator=" ", strip=True))
    return None


def _media_metadata(
    body: str, tree: HTMLParser, page_url: str
) -> tuple[str | None, int | None, str | None]:
    json_title, json_tmdb_id, json_media_type = _json_media_metadata(body)
    movie_name = json_title or _html_movie_name(tree)
    tmdb_id = json_tmdb_id
    media_type = json_media_type
    tmdb_url_match = TMDB_URL_RE.search(body)
    if tmdb_url_match:
        media_type = media_type or _normalize_media_type(tmdb_url_match.group(1))
        tmdb_id = tmdb_id or int(tmdb_url_match.group(2))
    if tmdb_id is None:
        for pattern in (TMDB_KEY_RE, TMDB_DATA_RE):
            match = pattern.search(body)
            if match:
                tmdb_id = int(match.group(1))
                break
    if media_type is None:
        path = urlsplit(page_url).path.lower()
        if re.search(r"/(?:series?|episodios?|tv)(?:/|$)", path):
            media_type = "serie"
        elif re.search(r"/(?:filmes?|movies?)(?:/|$)", path):
            media_type = "filme"
    return movie_name, tmdb_id, media_type


def normalize_url(value: str, base_url: str) -> str | None:
    value = html.unescape(value).strip().strip("\"'")
    value = value.rstrip(".,;)]}")
    if not value or value.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None
    if value.startswith("//"):
        value = f"{urlsplit(base_url).scheme}:{value}"
    try:
        absolute = urljoin(base_url, value)
        parts = urlsplit(absolute)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return None
    host = parts.hostname.lower().rstrip(".")
    netloc = f"{host}{port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def _decoded_variants(value: str) -> Iterable[str]:
    base = html.unescape(value)
    yield base

    escaped = re.sub(r"(?i)\\u003a|\\x3a", ":", base)
    escaped = re.sub(r"(?i)\\u002f|\\x2f|\\/", "/", escaped)
    if escaped != base:
        yield escaped

    for match in PERCENT_URL_RE.finditer(base):
        yield unquote(match.group(0))

    decoded_count = 0
    for match in BASE64_RE.finditer(base):
        if decoded_count >= 200:
            break
        token = match.group(1)
        try:
            padded = token + "=" * (-len(token) % 4)
            raw = base64.b64decode(padded, validate=True)
            decoded = raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if "http" in decoded.lower() or identify_providers(decoded):
            decoded_count += 1
            yield decoded


def _urls_from_text(value: str, base_url: str) -> set[str]:
    urls: set[str] = set()
    for variant in _decoded_variants(value):
        for match in ABSOLUTE_URL_RE.finditer(variant):
            normalized = normalize_url(match.group(0), base_url)
            if normalized:
                urls.add(normalized)
        for match in CSS_URL_RE.finditer(variant):
            normalized = normalize_url(match.group(1), base_url)
            if normalized:
                urls.add(normalized)
        for match in RELATIVE_QUOTED_URL_RE.finditer(variant):
            normalized = normalize_url(match.group(1), base_url)
            if normalized:
                urls.add(normalized)
    return urls


def _is_crawlable(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return not any(path.endswith(extension) for extension in BINARY_EXTENSIONS)


def _snippet(text: str, marker: str, radius: int = 90) -> str:
    index = text.lower().find(marker.lower())
    if index < 0:
        return marker
    start = max(0, index - radius)
    end = min(len(text), index + len(marker) + radius)
    return " ".join(text[start:end].split())


def _player_api_candidates(
    body: str, page_url: str
) -> list[tuple[str, str, str]]:
    """Correlaciona label, prefixo e ID em respostas JSON de APIs de player."""
    stripped = body.strip()
    if not stripped or stripped[0] not in "[{":
        return []
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return []

    objects: list[dict] = []

    def visit(value) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("players"), list):
                objects.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    candidates: list[tuple[str, str, str]] = []
    for data in objects:
        server_maps: dict[str, dict[str, str]] = {}
        for field, raw in data.items():
            if not field.startswith("servers_") or not isinstance(raw, str):
                continue
            audio = field.removeprefix("servers_") or "default"
            server_maps[audio] = {
                key.lower().strip(): value.strip()
                for key, value in parse_qsl(html.unescape(raw), keep_blank_values=False)
                if value.strip()
            }

        for player in data.get("players", []):
            if not isinstance(player, dict):
                continue
            label = str(player.get("label") or "")
            provider_set = identify_providers(label)
            base = normalize_url(str(player.get("url") or ""), page_url)
            if not provider_set and base:
                provider_set = identify_providers(base)
            if not provider_set:
                continue
            provider = sorted(provider_set)[0]
            key = re.sub(r"[^a-z0-9]", "", label.lower())
            download_base = normalize_url(str(player.get("downloadUrl") or ""), page_url)
            produced = False
            for audio, server_map in server_maps.items():
                media_id = server_map.get(key)
                if not media_id:
                    continue
                if base:
                    candidates.append(
                        (provider, f"{base.rstrip('/')}/{media_id}", f"player-api-{audio}-embed")
                    )
                    produced = True
                if download_base:
                    candidates.append(
                        (
                            provider,
                            f"{download_base.rstrip('/')}/{media_id}",
                            f"player-api-{audio}-download",
                        )
                    )
                    produced = True
            if not produced and base:
                candidates.append((provider, base, "player-api-prefix"))
    return candidates


def extract_document(
    body: str,
    page_url: str,
    depth: int,
    is_in_scope,
) -> Extraction:
    tree = HTMLParser(body)
    movie_name, tmdb_id, media_type = _media_metadata(body, tree, page_url)
    crawl_links: set[str] = set()
    findings: list[Finding] = []
    finding_keys: set[tuple[str, str, str, str]] = set()
    referenced_providers: set[str] = set()

    def add_url(value: str, context: str, tag: str | None, attribute: str | None) -> None:
        normalized = normalize_url(value, page_url)
        if not normalized:
            return
        if is_in_scope(normalized) and _is_crawlable(normalized):
            crawl_links.add(normalized)
        for provider in identify_providers(normalized):
            referenced_providers.add(provider)
            key = (provider, normalized, context, attribute or "")
            if key in finding_keys:
                continue
            finding_keys.add(key)
            findings.append(
                Finding(
                    source_page=page_url,
                    provider=provider,
                    reference=value[:2000],
                    resolved_url=normalized,
                    context=context,
                    movie_name=movie_name,
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    tag=tag,
                    attribute=attribute,
                    depth=depth,
                )
            )

    for node in tree.css("*"):
        tag = node.tag
        for attribute, value in node.attributes.items():
            if not value:
                continue
            if attribute.lower() in URL_ATTRIBUTES:
                add_url(value, "html-attribute", tag, attribute)
            elif tag == "meta" and attribute.lower() == "content":
                refresh = META_REFRESH_RE.search(value)
                if refresh:
                    add_url(refresh.group(1), "meta-refresh", tag, attribute)
            for extracted_url in _urls_from_text(value, page_url):
                add_url(extracted_url, "embedded-attribute", tag, attribute)

    for extracted_url in _urls_from_text(body, page_url):
        add_url(extracted_url, "document-text", None, None)

    # Sitemap XML e XHTML malformado nem sempre viram nos validos na arvore HTML.
    for match in LOC_RE.finditer(body):
        add_url(html.unescape(match.group(1)), "sitemap", "loc", None)

    for provider, candidate, context in _player_api_candidates(body, page_url):
        normalized = normalize_url(candidate, page_url)
        if not normalized:
            continue
        key = (provider, normalized, context, "")
        if key in finding_keys:
            continue
        finding_keys.add(key)
        referenced_providers.add(provider)
        findings.append(
            Finding(
                source_page=page_url,
                provider=provider,
                reference=candidate[:2000],
                resolved_url=normalized,
                context=context,
                movie_name=movie_name,
                tmdb_id=tmdb_id,
                media_type=media_type,
                depth=depth,
            )
        )

    # Registra referencias textuais/obfuscadas mesmo quando nao formam uma URL.
    cleaned_body = " ".join(tree.text(separator=" ", strip=True).split())
    for variant in _decoded_variants(body):
        for provider, marker in provider_markers(variant):
            if provider in referenced_providers:
                continue
            key = (provider, marker.lower(), "text-marker", "")
            if key in finding_keys:
                continue
            finding_keys.add(key)
            findings.append(
                Finding(
                    source_page=page_url,
                    provider=provider,
                    reference=_snippet(cleaned_body or variant, marker)[:2000],
                    resolved_url=None,
                    context="text-marker",
                    movie_name=movie_name,
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    depth=depth,
                )
            )

    return Extraction(
        crawl_links=crawl_links,
        findings=findings,
        movie_name=movie_name,
        tmdb_id=tmdb_id,
        media_type=media_type,
    )
