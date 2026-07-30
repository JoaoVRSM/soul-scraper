from __future__ import annotations

import re
from urllib.parse import urlsplit


# Os servicos trocam de TLD com frequencia. A identificacao usa marcas no host
# e tambem no texto, sem depender de uma lista fragil de dominios completos.
PROVIDER_PATTERNS: dict[str, re.Pattern[str]] = {
    "byse": re.compile(r"(?i)(?:^|[^a-z0-9])byse(?:[^a-z0-9]|$)"),
    "doodstream": re.compile(
        r"(?i)(?:^|[^a-z0-9])(?:doodstream|doodcdn|doodvideo|dooood|dood)(?:[^a-z0-9]|$)"
    ),
    "mixdrop": re.compile(r"(?i)(?:^|[^a-z0-9])mixdrop(?:[^a-z0-9]|$)"),
    "streamtape": re.compile(r"(?i)(?:^|[^a-z0-9])streamtape(?:[^a-z0-9]|$)"),
}

HOST_MARKERS: dict[str, tuple[str, ...]] = {
    "byse": ("byse",),
    "doodstream": ("doodstream", "doodcdn", "doodvideo", "dooood", "dood"),
    "mixdrop": ("mixdrop",),
    "streamtape": ("streamtape",),
}


def identify_providers(value: str) -> set[str]:
    """Retorna todos os provedores mencionados em uma URL ou trecho de texto."""
    haystacks = [value]
    found: set[str] = set()
    try:
        hostname = urlsplit(value).hostname
        if hostname:
            haystacks.insert(0, f".{hostname}.")
            lowered_host = hostname.lower()
            for provider, markers in HOST_MARKERS.items():
                if any(marker in lowered_host for marker in markers):
                    found.add(provider)
    except ValueError:
        pass

    for provider, pattern in PROVIDER_PATTERNS.items():
        if any(pattern.search(item) for item in haystacks):
            found.add(provider)
    return found


def provider_markers(value: str) -> list[tuple[str, str]]:
    markers: list[tuple[str, str]] = []
    for provider, pattern in PROVIDER_PATTERNS.items():
        for match in pattern.finditer(value):
            marker = match.group(0).strip(" .:/_-")
            markers.append((provider, marker or provider))
    return markers
