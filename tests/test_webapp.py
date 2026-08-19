import asyncio

from fastapi.testclient import TestClient

from soulscraper.models import CrawlResult, Finding, PageRecord
from soulscraper.webapp import CrawlJob, CrawlRequest, ScrapeRequest, app, manager


client = TestClient(app)


def test_web_interface_and_assets_are_served() -> None:
    dashboard_home = client.get("/")
    portal = client.get("/integracao")
    styles = client.get("/static/api.css")
    script = client.get("/static/api.js")
    dashboard_alias = client.get("/painel")

    assert dashboard_home.status_code == 200
    assert 'id="crawl-form"' in dashboard_home.text
    assert 'href="/integracao"' in dashboard_home.text
    assert dashboard_alias.status_code == 200
    assert 'id="crawl-form"' in dashboard_alias.text
    assert portal.status_code == 200
    assert "Seu site pede" in portal.text
    assert "O JSON volta pronto" in portal.text
    assert "https://sua-api.com/api/v1/scrape" in script.text
    assert 'id="try-form"' not in portal.text
    assert "JavaScript" in portal.text
    assert "PHP" in portal.text
    assert 'href="/"' in portal.text
    assert "Abrir o sistema" in portal.text
    assert styles.status_code == 200
    assert "--green: #b8ff3d" in styles.text
    assert script.status_code == 200
    assert "/api/v1/scrape" in script.text
    assert "download-result" not in script.text


def test_health_and_url_without_protocol_is_easy_to_use() -> None:
    assert client.get("/api/health").json()["status"] == "ok"

    request = CrawlRequest(url="example.com")
    assert request.url == "https://example.com"

    response = client.post("/api/jobs", json={"url": "ftp://example.com"})
    assert response.status_code == 422
    assert "HTTP ou HTTPS" in response.text

    simple_request = ScrapeRequest(url="seu-site.com")
    assert simple_request.url == "https://seu-site.com"
    assert simple_request.max_pages == 300
    assert simple_request.max_depth == 10
    assert simple_request.browser_concurrency == 2


def test_online_deployment_can_require_basic_auth(monkeypatch) -> None:
    monkeypatch.setenv("SOULSCRAPER_BASIC_USER", "auditor")
    monkeypatch.setenv("SOULSCRAPER_BASIC_PASSWORD", "segredo-forte")
    auth_client = TestClient(app)

    denied = auth_client.get("/", headers={"Accept": "application/json"})
    login_page = auth_client.get("/", headers={"Accept": "text/html"})
    allowed = auth_client.get("/painel", auth=("auditor", "segredo-forte"))

    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == 'Basic realm="Soul Scraper"'
    assert login_page.status_code == 200
    assert 'id="login-form"' in login_page.text
    assert allowed.status_code == 200
    assert "soul_scraper_auth=" in allowed.headers["set-cookie"]
    assert 'id="crawl-form"' in allowed.text


def test_online_login_creates_browser_session(monkeypatch) -> None:
    monkeypatch.setenv("SOULSCRAPER_BASIC_USER", "auditor")
    monkeypatch.setenv("SOULSCRAPER_BASIC_PASSWORD", "segredo-forte")
    browser_client = TestClient(app, base_url="https://testserver")

    denied = browser_client.post(
        "/login", json={"username": "auditor", "password": "incorreta"}
    )
    accepted = browser_client.post(
        "/login", json={"username": "auditor", "password": "segredo-forte"}
    )
    dashboard_home = browser_client.get("/")
    portal = browser_client.get("/integracao")
    dashboard = browser_client.get("/painel")
    health = browser_client.get("/api/v1/health")

    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert dashboard.status_code == 200
    assert 'id="crawl-form"' in dashboard_home.text
    assert 'id="try-form"' not in portal.text
    assert 'id="crawl-form"' in dashboard.text
    assert health.json()["status"] == "ok"


def test_openapi_exposes_versioned_api_contract() -> None:
    schema = client.get("/api/openapi.json")

    assert schema.status_code == 200
    paths = schema.json()["paths"]
    assert "/api/v1/scrape" in paths
    assert "/api/v1" in paths
    assert "/api/v1/crawls" in paths
    assert "/api/v1/crawls/{job_id}/result" in paths
    assert "/api/v1/crawls/{job_id}/pages" in paths
    assert "/api/v1/crawls/{job_id}/catalog" in paths
    assert client.get("/docs").status_code == 200
    guide = client.get("/api/v1")
    assert guide.json()["request"]["body"] == {"url": "https://seu-site.com"}
    assert guide.json()["flow"][-1] == "seu_site"


def test_direct_scrape_waits_and_returns_selected_json(monkeypatch) -> None:
    async def fake_create(request: CrawlRequest) -> CrawlJob:
        result = CrawlResult(
            start_url=request.url,
            started_at="2026-08-10T12:00:00+00:00",
            finished_at="2026-08-10T12:00:01+00:00",
            config={"max_pages": request.max_pages},
        )
        job = CrawlJob(
            id="direct-json",
            request=request,
            status="completed",
            result=result,
        )
        job.task = asyncio.create_task(asyncio.sleep(0))
        return job

    monkeypatch.setattr(manager, "create", fake_create)
    response = client.post(
        "/api/v1/scrape",
        json={
            "url": "https://example.com",
            "output": "catalog",
            "download": True,
            "max_pages": 10,
            "render_js": False,
            "validate_links": False,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"filmes": [], "series": [], "nao_identificados": []}
    assert response.headers["x-crawl-job-id"] == "direct-json"
    assert response.headers["content-disposition"] == (
        'attachment; filename="catalogo-links.json"'
    )


def test_versioned_api_serves_completed_crawl_data() -> None:
    job_id = "api-contract"
    request = CrawlRequest(url="https://example.com", validate_links=False)
    result = CrawlResult(
        start_url=request.url,
        started_at="2026-08-10T12:00:00+00:00",
        finished_at="2026-08-10T12:00:01+00:00",
        config={"max_pages": 1},
        pages=[
            PageRecord(
                url="https://example.com/movie/1",
                final_url="https://example.com/movie/1",
                depth=0,
                status=200,
                findings_found=1,
                movie_name="Filme de teste",
                tmdb_id=123,
                media_type="filme",
            )
        ],
        findings=[
            Finding(
                source_page="https://example.com/movie/1",
                provider="byse",
                reference="https://byse.example/embed/abc",
                resolved_url="https://byse.example/embed/abc",
                context="player-api-dub-url",
                movie_name="Filme de teste",
                tmdb_id=123,
                media_type="filme",
                health_status="working",
            )
        ],
    )
    job = CrawlJob(id=job_id, request=request, status="completed", result=result)
    manager._refresh_summary(job)
    manager.jobs[job_id] = job

    try:
        status = client.get(f"/api/v1/crawls/{job_id}")
        listing = client.get("/api/v1/crawls")
        pages = client.get(f"/api/v1/crawls/{job_id}/pages")
        findings = client.get(f"/api/v1/crawls/{job_id}/findings")
        result_response = client.get(f"/api/v1/crawls/{job_id}/result")
        catalog = client.get(f"/api/v1/crawls/{job_id}/catalog")
        movies = client.get(f"/api/v1/crawls/{job_id}/movies")
        series = client.get(f"/api/v1/crawls/{job_id}/series")

        assert status.status_code == 200
        assert status.json()["api"]["catalog"].endswith(f"/{job_id}/catalog")
        assert any(item["id"] == job_id for item in listing.json()["items"])
        assert pages.json()["items"][0]["status"] == 200
        assert findings.json()["items"][0]["provider"] == "byse"
        assert result_response.json()["summary"]["findings"] == 1
        assert catalog.json()["filmes"][0]["tmdb_id"] == 123
        assert movies.json()[0]["byse"]["dublado"] == [
            "https://byse.example/embed/abc"
        ]
        assert series.json() == []
    finally:
        manager.jobs.pop(job_id, None)


def test_result_endpoint_reports_when_crawl_is_not_ready() -> None:
    job_id = "api-pending"
    manager.jobs[job_id] = CrawlJob(
        id=job_id,
        request=CrawlRequest(url="https://example.com"),
    )
    try:
        response = client.get(f"/api/v1/crawls/{job_id}/result")
        assert response.status_code == 409
        assert "ainda não está disponível" in response.json()["detail"]
    finally:
        manager.jobs.pop(job_id, None)
