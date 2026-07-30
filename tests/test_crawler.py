from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from soulscraper.crawler import CrawlConfig, SiteCrawler


def test_browser_concurrency_is_configurable_and_bounded() -> None:
    powerful = CrawlConfig(
        start_url="https://example.test",
        concurrency=24,
        browser_concurrency=6,
    )
    bounded = CrawlConfig(
        start_url="https://example.test",
        browser_concurrency=99,
    )

    assert powerful.browser_concurrency == 6
    assert bounded.browser_concurrency == 12


def test_episode_routes_have_priority_over_static_assets() -> None:
    assert SiteCrawler._crawl_priority(
        "https://example.test/episodios/online/serie-1x1"
    ) < SiteCrawler._crawl_priority(
        "https://example.test/uploads/javascript/player.js"
    )


class DemoHandler(BaseHTTPRequestHandler):
    requested_paths: list[str] = []

    def do_GET(self) -> None:
        self.requested_paths.append(self.path)
        pages = {
            "/": (
                "text/html",
                '<a href="/catalogo">Catalogo</a>'
                '<iframe src="https://doodstream.com/e/root"></iframe>',
            ),
            "/catalogo": (
                "text/html",
                r'<script>const player="https:\/\/mixdrop.co\/e\/catalogo"</script>',
            ),
            "/sitemap.xml": (
                "application/xml",
                f"<urlset><url><loc>http://127.0.0.1:{self.server.server_port}"
                "/oculta</loc></url></urlset>",
            ),
            "/oculta": (
                "text/html",
                '<div data-player="https://streamtape.com/e/hidden"></div>',
            ),
            "/dynamic": (
                "text/html",
                """
                <html><body>
                <script>
                  const host = ['player', 'by', 'se.example'].join('');
                  const frame = document.createElement('iframe');
                  frame.src = 'https://' + host + '/e/from-js';
                  document.body.appendChild(frame);
                </script>
                </body></html>
                """,
            ),
            "/dynamic-api": (
                "text/html",
                """
                <html><body>
                <script>
                  fetch('/api/player').then(response => response.json()).then(data => {
                    document.body.dataset.loaded = data.players.length;
                  });
                </script>
                </body></html>
                """,
            ),
            "/api/player": (
                "application/json",
                """
                {
                  "players": [
                    {"label": "DoodStream", "url": "https://playmogo.com/e/"}
                  ],
                  "servers_dub": "doodstream=hidden123"
                }
                """,
            ),
        }
        if self.path not in pages:
            self.send_response(404)
            self.end_headers()
            return
        content_type, body = pages[self.path]
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args) -> None:
        pass


@pytest.fixture
def demo_server():
    DemoHandler.requested_paths = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()


@pytest.mark.asyncio
async def test_crawler_follows_internal_pages_and_sitemap_only(demo_server) -> None:
    url = f"http://127.0.0.1:{demo_server.server_port}/"
    config = CrawlConfig(
        start_url=url,
        max_pages=10,
        max_depth=5,
        concurrency=3,
        respect_robots=False,
        discover_sitemap=True,
    )

    result = await SiteCrawler(config).crawl()

    assert {page.status for page in result.pages} == {200}
    assert {finding.provider for finding in result.findings} == {
        "doodstream",
        "mixdrop",
        "streamtape",
    }
    assert set(DemoHandler.requested_paths) == {"/", "/catalogo", "/sitemap.xml", "/oculta"}
    assert len(result.pages) == 4


@pytest.mark.asyncio
async def test_javascript_rendering_finds_runtime_dom_reference(demo_server) -> None:
    url = f"http://127.0.0.1:{demo_server.server_port}/dynamic"
    config = CrawlConfig(
        start_url=url,
        max_pages=1,
        max_depth=0,
        concurrency=1,
        respect_robots=False,
        discover_sitemap=False,
        render_js=True,
    )

    result = await SiteCrawler(config).crawl()

    assert result.pages[0].rendered is True
    assert {finding.provider for finding in result.findings} == {"byse"}
    assert any(
        finding.resolved_url == "https://playerbyse.example/e/from-js"
        for finding in result.findings
    )


@pytest.mark.asyncio
async def test_javascript_rendering_scans_xhr_json_bodies(demo_server) -> None:
    url = f"http://127.0.0.1:{demo_server.server_port}/dynamic-api"
    config = CrawlConfig(
        start_url=url,
        max_pages=1,
        max_depth=0,
        concurrency=1,
        respect_robots=False,
        discover_sitemap=False,
        render_js=True,
    )

    result = await SiteCrawler(config).crawl()

    assert any(
        finding.provider == "doodstream"
        and finding.resolved_url == "https://playmogo.com/e/hidden123"
        and finding.context == "player-api-dub-embed"
        for finding in result.findings
    )
