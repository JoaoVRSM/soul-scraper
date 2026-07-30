import json

from soulscraper.models import CrawlResult, Finding
from soulscraper.reporting import (
    build_catalog_export,
    build_movies_export,
    build_series_export,
    write_reports,
)


def result_with_links() -> CrawlResult:
    common = {
        "source_page": "https://example.test/filme/dia-d",
        "movie_name": "Dia D",
        "tmdb_id": 1275779,
        "media_type": "filme",
        "depth": 0,
    }
    return CrawlResult(
        start_url=common["source_page"],
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
        config={},
        findings=[
            Finding(
                **common,
                provider="byse",
                reference="https://byse.example/e/abc",
                resolved_url="https://byse.example/e/abc",
                context="player-api-dub-embed",
                health_status="working",
            ),
            Finding(
                **common,
                provider="byse",
                reference="https://byse.example/e/",
                resolved_url="https://byse.example/e/",
                context="document-text",
            ),
            Finding(
                **common,
                provider="doodstream",
                reference="https://dood.example/e/def",
                resolved_url="https://dood.example/e/def",
                context="html-attribute",
                health_status="working_browser",
            ),
            Finding(
                **common,
                provider="mixdrop",
                reference="https://mixdrop.example/e/dead",
                resolved_url="https://mixdrop.example/e/dead",
                context="html-attribute",
                health_status="dead",
            ),
            Finding(
                **common,
                provider="streamtape",
                reference='"streamtape"',
                resolved_url=None,
                context="text-marker",
            ),
        ],
    )


def test_build_movies_export_is_simple_and_uses_only_actionable_links() -> None:
    exported = build_movies_export(result_with_links())

    assert exported == [
        {
            "nome": "Dia D",
            "tmdb_id": 1275779,
            "tipo": "filme",
            "byse": {
                "dublado": ["https://byse.example/e/abc"],
                "legendado": [],
                "nao_identificado": [],
            },
            "doodstream": {
                "dublado": [],
                "legendado": [],
                "nao_identificado": ["https://dood.example/e/def"],
            },
            "mixdrop": {
                "dublado": [],
                "legendado": [],
                "nao_identificado": [],
            },
            "streamtape": {
                "dublado": [],
                "legendado": [],
                "nao_identificado": [],
            },
        }
    ]


def test_write_reports_creates_downloadable_movies_json(tmp_path) -> None:
    write_reports(result_with_links(), tmp_path)

    payload = json.loads((tmp_path / "filmes-links.json").read_text(encoding="utf-8"))

    assert payload[0]["nome"] == "Dia D"
    assert payload[0]["tmdb_id"] == 1275779
    assert payload[0]["byse"]["dublado"] == ["https://byse.example/e/abc"]


def test_catalog_separates_movies_and_series(tmp_path) -> None:
    result = result_with_links()
    result.findings.append(
        Finding(
            source_page="https://example.test/series/minha-serie",
            movie_name="Minha Série 1x2",
            tmdb_id=555,
            media_type="serie",
            provider="streamtape",
            reference="https://streamtape.example/e/episode1",
            resolved_url="https://streamtape.example/e/episode1",
            context="player-api-dub-embed",
        )
    )

    write_reports(result, tmp_path)
    catalog = build_catalog_export(result)

    assert [item["nome"] for item in build_movies_export(result)] == ["Dia D"]
    assert [item["nome"] for item in build_series_export(result)] == ["Minha Série"]
    assert [item["nome"] for item in catalog["filmes"]] == ["Dia D"]
    assert [item["nome"] for item in catalog["series"]] == ["Minha Série"]
    series_payload = json.loads(
        (tmp_path / "series-links.json").read_text(encoding="utf-8")
    )[0]
    assert series_payload["tipo"] == "serie"
    assert series_payload["episodios"][0]["temporada"] == 1
    assert series_payload["episodios"][0]["episodio"] == 2
    assert series_payload["episodios"][0]["streamtape"]["dublado"] == [
        "https://streamtape.example/e/episode1"
    ]
