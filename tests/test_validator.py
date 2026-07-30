import httpx
import pytest

from soulscraper.models import CrawlResult, Finding
from soulscraper.validator import LinkValidator


@pytest.mark.asyncio
async def test_validator_classifies_working_redirect_dead_and_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        cases = {
            "/ok": httpx.Response(206),
            "/redirect": httpx.Response(302, headers={"Location": "/ok"}),
            "/dead": httpx.Response(404),
            "/blocked": httpx.Response(403),
        }
        return cases[request.url.path]

    validator = LinkValidator(
        transport=httpx.MockTransport(handler),
        enforce_public_hosts=False,
        browser_fallback=False,
    )
    urls = [
        "https://media.example/ok",
        "https://media.example/redirect",
        "https://media.example/dead",
        "https://media.example/blocked",
    ]

    checks = await validator.validate_urls(urls)

    assert checks[urls[0]].status == "working"
    assert checks[urls[1]].status == "redirected"
    assert checks[urls[1]].final_url == "https://media.example/ok"
    assert checks[urls[2]].status == "dead"
    assert checks[urls[3]].status == "browser_required"


@pytest.mark.asyncio
async def test_validate_result_updates_only_actionable_full_links() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    findings = [
        Finding(
            source_page="https://catalog.example/movie",
            provider="doodstream",
            reference="DoodStream",
            resolved_url=None,
            context="text-marker",
        ),
        Finding(
            source_page="https://catalog.example/movie",
            provider="doodstream",
            reference="https://playmogo.com/e/",
            resolved_url="https://playmogo.com/e/",
            context="document-text",
        ),
        Finding(
            source_page="https://catalog.example/movie",
            provider="doodstream",
            reference="https://playmogo.com/e/abc",
            resolved_url="https://playmogo.com/e/abc",
            context="player-api-dub-embed",
        ),
    ]
    result = CrawlResult(
        start_url="https://catalog.example/movie",
        started_at="now",
        finished_at="now",
        config={},
        findings=findings,
    )
    validator = LinkValidator(
        transport=httpx.MockTransport(handler),
        enforce_public_hosts=False,
        browser_fallback=False,
    )

    await validator.validate_result(result)

    assert result.findings[0].health_status == "unchecked"
    assert result.findings[1].health_status == "unchecked"
    assert result.findings[2].health_status == "working"
    assert result.findings[2].health_http_status == 200
