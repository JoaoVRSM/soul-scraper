from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .models import CrawlResult


PROVIDERS = ("byse", "doodstream", "mixdrop", "streamtape")
LANGUAGES = ("dublado", "legendado", "nao_identificado")
DEFINITIVELY_BROKEN = {"dead", "malformed", "blocked_private"}


def _build_media_items(result: CrawlResult) -> list[dict]:
    """Cria itens enxutos de filmes e séries com URLs utilizáveis."""
    api_full_keys = {
        (finding.source_page, finding.provider)
        for finding in result.findings
        if finding.context.startswith("player-api-")
        and not finding.context.endswith("-prefix")
        and finding.resolved_url
    }
    movies: dict[tuple[str, str], dict] = {}
    seen_urls: dict[tuple[str, str], set[str]] = {}
    source_types = {
        finding.source_page: finding.media_type
        for finding in result.findings
        if finding.media_type
    }

    for finding in result.findings:
        if finding.provider not in PROVIDERS or not finding.resolved_url:
            continue
        if finding.context in {"text-marker", "player-api-prefix"}:
            continue
        if (
            (finding.source_page, finding.provider) in api_full_keys
            and not finding.context.startswith("player-api-")
        ):
            continue
        if finding.health_status in DEFINITIVELY_BROKEN:
            continue

        media_type = finding.media_type or source_types.get(finding.source_page)
        if media_type == "serie":
            # Cada episódio precisa preservar os próprios links antes de ser
            # agrupado na série final.
            identity = ("episode", finding.source_page)
        elif finding.tmdb_id is not None:
            identity = ("tmdb", f"{media_type or 'nao_identificado'}:{finding.tmdb_id}")
        else:
            identity = ("page", finding.source_page)
        movie = movies.setdefault(
            identity,
            {
                "nome": finding.movie_name,
                "tmdb_id": finding.tmdb_id,
                "tipo": media_type or "nao_identificado",
                **{
                    provider: {language: [] for language in LANGUAGES}
                    for provider in PROVIDERS
                },
            },
        )
        if not movie["nome"] and finding.movie_name:
            movie["nome"] = finding.movie_name
        if movie["tipo"] == "nao_identificado" and media_type:
            movie["tipo"] = media_type
        if finding.context.startswith("player-api-dub-"):
            language = "dublado"
        elif finding.context.startswith("player-api-leg-"):
            language = "legendado"
        else:
            language = "nao_identificado"
        url_key = (
            f"{identity[0]}:{identity[1]}",
            f"{finding.provider}:{language}",
        )
        provider_seen = seen_urls.setdefault(url_key, set())
        if finding.resolved_url not in provider_seen:
            provider_seen.add(finding.resolved_url)
            movie[finding.provider][language].append(finding.resolved_url)

    exported = list(movies.values())
    for movie in exported:
        for provider in PROVIDERS:
            for language in LANGUAGES:
                movie[provider][language].sort()
    exported.sort(key=lambda item: ((item["nome"] or "").casefold(), item["tmdb_id"] or 0))
    return exported


def build_movies_export(result: CrawlResult) -> list[dict]:
    return [item for item in _build_media_items(result) if item["tipo"] == "filme"]


def build_series_export(result: CrawlResult) -> list[dict]:
    episodes = [
        item for item in _build_media_items(result) if item["tipo"] == "serie"
    ]
    grouped: dict[tuple[str, str], dict] = {}
    for item in episodes:
        name = item["nome"] or "Série não identificada"
        match = re.search(r"(?i)\b(\d+)\s*x\s*(\d+)\b", name)
        season = int(match.group(1)) if match else None
        episode_number = int(match.group(2)) if match else None
        series_name = (
            name[: match.start()].strip(" |-–—")
            if match
            else name
        )
        identity = (
            ("tmdb", str(item["tmdb_id"]))
            if item["tmdb_id"] is not None
            else ("name", series_name.casefold())
        )
        series = grouped.setdefault(
            identity,
            {
                "nome": series_name,
                "tmdb_id": item["tmdb_id"],
                "tipo": "serie",
                "episodios": [],
            },
        )
        series["episodios"].append(
            {
                "nome": name,
                "temporada": season,
                "episodio": episode_number,
                **{provider: item[provider] for provider in PROVIDERS},
            }
        )

    exported = list(grouped.values())
    for series in exported:
        series["episodios"].sort(
            key=lambda item: (
                item["temporada"] is None,
                item["temporada"] or 0,
                item["episodio"] is None,
                item["episodio"] or 0,
                item["nome"].casefold(),
            )
        )
    exported.sort(key=lambda item: ((item["nome"] or "").casefold(), item["tmdb_id"] or 0))
    return exported


def build_catalog_export(result: CrawlResult) -> dict[str, list[dict]]:
    items = _build_media_items(result)
    return {
        "filmes": [item for item in items if item["tipo"] == "filme"],
        "series": build_series_export(result),
        "nao_identificados": [
            item for item in items if item["tipo"] == "nao_identificado"
        ],
    }


def write_reports(result: CrawlResult, output: Path) -> Path:
    if output.suffix.lower() == ".json":
        output_dir = output.parent
        json_path = output
    else:
        output_dir = output
        json_path = output_dir / "resultado.json"
    output_dir.mkdir(parents=True, exist_ok=True)

    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(result.to_dict(), handle, ensure_ascii=False, indent=2)

    movies_path = output_dir / "filmes-links.json"
    with movies_path.open("w", encoding="utf-8") as handle:
        json.dump(build_movies_export(result), handle, ensure_ascii=False, indent=2)

    series_path = output_dir / "series-links.json"
    with series_path.open("w", encoding="utf-8") as handle:
        json.dump(build_series_export(result), handle, ensure_ascii=False, indent=2)

    catalog_path = output_dir / "catalogo-links.json"
    with catalog_path.open("w", encoding="utf-8") as handle:
        json.dump(build_catalog_export(result), handle, ensure_ascii=False, indent=2)

    findings_path = output_dir / "referencias.csv"
    finding_fields = [
        "source_page",
        "movie_name",
        "tmdb_id",
        "media_type",
        "provider",
        "reference",
        "resolved_url",
        "context",
        "tag",
        "attribute",
        "depth",
        "health_status",
        "health_http_status",
        "health_final_url",
        "health_checked_at",
        "health_error",
    ]
    with findings_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=finding_fields)
        writer.writeheader()
        for finding in result.findings:
            writer.writerow(asdict(finding))

    pages_path = output_dir / "paginas.csv"
    page_fields = [
        "url",
        "final_url",
        "depth",
        "status",
        "content_type",
        "elapsed_ms",
        "bytes_read",
        "links_found",
        "findings_found",
        "movie_name",
        "tmdb_id",
        "media_type",
        "rendered",
        "error",
    ]
    with pages_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=page_fields)
        writer.writeheader()
        for page in result.pages:
            writer.writerow(asdict(page))
    return output_dir


def default_output_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path("resultados") / stamp
