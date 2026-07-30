from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import tldextract

from .extractor import extract_document, normalize_url
from .models import CrawlResult, Finding, PageRecord
from .providers import identify_providers


ProgressCallback = Callable[[PageRecord, int, int], Awaitable[None] | None]


@dataclass
class CrawlConfig:
    start_url: str
    max_pages: int = 5000
    max_depth: int = 20
    concurrency: int = 12
    browser_concurrency: int = 4
    request_timeout: float = 20.0
    delay: float = 0.0
    max_bytes: int = 8_000_000
    include_subdomains: bool = True
    respect_robots: bool = True
    render_js: bool = False
    discover_sitemap: bool = True
    user_agent: str = "SiteSoulScraper/1.0 (+auditoria autorizada)"

    def __post_init__(self) -> None:
        normalized = normalize_url(self.start_url, self.start_url)
        if not normalized:
            raise ValueError("A URL inicial precisa usar http:// ou https://.")
        self.start_url = normalized
        self.max_pages = max(1, self.max_pages)
        self.max_depth = max(0, self.max_depth)
        self.concurrency = min(64, max(1, self.concurrency))
        self.browser_concurrency = min(12, max(1, self.browser_concurrency))
        self.max_bytes = max(1024, self.max_bytes)


@dataclass
class FetchResult:
    status: int | None
    final_url: str
    content_type: str
    body: str
    bytes_read: int
    elapsed_ms: int
    location: str | None = None
    error: str | None = None


class Scope:
    def __init__(self, start_url: str, include_subdomains: bool) -> None:
        self.start_host = (urlsplit(start_url).hostname or "").lower()
        self.include_subdomains = include_subdomains
        extractor = tldextract.TLDExtract(suffix_list_urls=())
        parts = extractor(self.start_host)
        self.site_root = (
            f"{parts.domain}.{parts.suffix}" if parts.domain and parts.suffix else self.start_host
        )

    def contains(self, url: str) -> bool:
        try:
            host = (urlsplit(url).hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        if not host:
            return False
        if self.include_subdomains:
            return host == self.site_root or host.endswith(f".{self.site_root}")
        return host == self.start_host


class SiteCrawler:
    def __init__(
        self,
        config: CrawlConfig,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.config = config
        self.scope = Scope(config.start_url, config.include_subdomains)
        self.progress = progress
        self._seen: set[str] = set()
        self._seen_lock = asyncio.Lock()
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_lock = asyncio.Lock()
        self._pages: list[PageRecord] = []
        self._findings: list[Finding] = []
        self._finding_keys: set[tuple[str, str, str, str]] = set()
        self._browser = None
        self._browser_context = None
        self._playwright = None
        # O HTTP pode ter concorrencia alta, mas abas do Chromium consomem muito
        # mais memoria. Duas abas simultaneas mantem a varredura estavel.
        self._render_semaphore = asyncio.Semaphore(
            min(config.browser_concurrency, config.concurrency)
        )

    async def crawl(self) -> CrawlResult:
        started = datetime.now(timezone.utc)
        timeout = httpx.Timeout(self.config.request_timeout)
        limits = httpx.Limits(
            max_connections=self.config.concurrency,
            max_keepalive_connections=self.config.concurrency,
        )
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml,text/plain,application/json;q=0.9,*/*;q=0.5",
        }

        workers: list[asyncio.Task] = []
        async with httpx.AsyncClient(timeout=timeout, limits=limits, headers=headers) as client:
            self.client = client
            try:
                if self.config.render_js:
                    await self._start_browser()

                queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
                await self._enqueue(queue, self.config.start_url, 0)
                if self.config.discover_sitemap:
                    sitemap = urljoin(self.config.start_url, "/sitemap.xml")
                    await self._enqueue(queue, sitemap, 0)

                workers = [
                    asyncio.create_task(self._worker(queue), name=f"crawler-{number}")
                    for number in range(self.config.concurrency)
                ]
                await queue.join()
            finally:
                for worker in workers:
                    worker.cancel()
                if workers:
                    await asyncio.gather(*workers, return_exceptions=True)
                if self._browser_context:
                    await self._browser_context.close()
                    self._browser_context = None
                if self._browser:
                    await self._browser.close()
                    self._browser = None
                if self._playwright:
                    await self._playwright.stop()
                    self._playwright = None

        finished = datetime.now(timezone.utc)
        return CrawlResult(
            start_url=self.config.start_url,
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            config=asdict(self.config),
            pages=sorted(self._pages, key=lambda page: (page.depth, page.url)),
            findings=sorted(
                self._findings,
                key=lambda item: (item.provider, item.source_page, item.resolved_url or item.reference),
            ),
        )

    async def _start_browser(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Renderizacao JS solicitada, mas Playwright nao esta instalado. "
                'Execute: python -m pip install -e ".[render]"'
            ) from exc
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-default-apps",
                "--disable-extensions",
                "--no-first-run",
            ],
        )
        self._browser_context = await self._browser.new_context(
            user_agent=self.config.user_agent,
            ignore_https_errors=False,
            service_workers="block",
        )

    async def _worker(self, queue: asyncio.Queue[tuple[str, int]]) -> None:
        while True:
            url, depth = await queue.get()
            try:
                await self._process(queue, url, depth)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # uma pagina ruim nao derruba a auditoria toda
                page = PageRecord(
                    url=url,
                    final_url=url,
                    depth=depth,
                    status=None,
                    error=f"{type(exc).__name__}: {exc}",
                )
                self._pages.append(page)
                await self._notify(page)
            finally:
                queue.task_done()

    async def _enqueue(
        self, queue: asyncio.Queue[tuple[str, int]], url: str, depth: int
    ) -> bool:
        normalized = normalize_url(url, self.config.start_url)
        if (
            not normalized
            or not self.scope.contains(normalized)
            or depth > self.config.max_depth
        ):
            return False
        async with self._seen_lock:
            if normalized in self._seen or len(self._seen) >= self.config.max_pages:
                return False
            self._seen.add(normalized)
        await queue.put((normalized, depth))
        return True

    async def _process(
        self, queue: asyncio.Queue[tuple[str, int]], url: str, depth: int
    ) -> None:
        if self.config.respect_robots and not await self._robots_allowed(url):
            page = PageRecord(
                url=url,
                final_url=url,
                depth=depth,
                status=None,
                error="Bloqueado por robots.txt",
            )
            self._pages.append(page)
            await self._notify(page)
            return

        result = await self._fetch(url)
        page = PageRecord(
            url=url,
            final_url=result.final_url,
            depth=depth,
            status=result.status,
            content_type=result.content_type,
            elapsed_ms=result.elapsed_ms,
            bytes_read=result.bytes_read,
            error=result.error,
        )

        if result.location:
            redirected = normalize_url(result.location, url)
            if redirected:
                self._record_provider_url(url, redirected, depth, "redirect-location")
                if self.scope.contains(redirected):
                    await self._enqueue(queue, redirected, depth)

        body = result.body
        can_extract = self._extractable_content(result.content_type)
        findings_before = len(self._findings)
        browser_internal_urls: set[str] = set()
        browser_documents: list[tuple[str, str]] = []
        if body and can_extract and self.config.render_js and "html" in result.content_type.lower():
            try:
                rendered_body, rendered_url, browser_urls, browser_documents = await self._render(
                    url
                )
                if rendered_body:
                    body = f"{body}\n<!-- RENDERED DOM -->\n{rendered_body}"
                    page.rendered = True
                    page.final_url = rendered_url
                for browser_url in browser_urls:
                    self._record_provider_url(url, browser_url, depth, "browser-request")
                    if self.scope.contains(browser_url):
                        browser_internal_urls.add(browser_url)
            except Exception as exc:
                page.error = self._append_error(page.error, f"Playwright: {exc}")

        if body and can_extract:
            extractions = [
                extract_document(body, page.final_url, depth, self.scope.contains)
            ]
            for _, response_body in browser_documents:
                extractions.append(
                    extract_document(
                        response_body, page.final_url, depth, self.scope.contains
                    )
                )
            movie_name = next(
                (item.movie_name for item in extractions if item.movie_name), None
            )
            tmdb_id = next(
                (item.tmdb_id for item in extractions if item.tmdb_id is not None), None
            )
            media_type = next(
                (item.media_type for item in extractions if item.media_type), None
            )
            page.movie_name = movie_name
            page.tmdb_id = tmdb_id
            page.media_type = media_type
            extracted_links = set(browser_internal_urls)
            for extraction in extractions:
                extracted_links.update(extraction.crawl_links)
                for finding in extraction.findings:
                    if (
                        movie_name != finding.movie_name
                        or tmdb_id != finding.tmdb_id
                        or media_type != finding.media_type
                    ):
                        finding = replace(
                            finding,
                            movie_name=movie_name or finding.movie_name,
                            tmdb_id=tmdb_id if tmdb_id is not None else finding.tmdb_id,
                            media_type=media_type or finding.media_type,
                        )
                    self._record_finding(finding)
            page.links_found = len(extracted_links)
            if depth < self.config.max_depth:
                for link in sorted(
                    extracted_links,
                    key=lambda item: (self._crawl_priority(item), item),
                ):
                    await self._enqueue(queue, link, depth + 1)
        page.findings_found = len(self._findings) - findings_before

        self._pages.append(page)
        await self._notify(page)

    async def _fetch(self, url: str) -> FetchResult:
        if self.config.delay:
            await asyncio.sleep(self.config.delay)
        started = time.perf_counter()
        try:
            async with self.client.stream("GET", url, follow_redirects=False) as response:
                content_type = response.headers.get("content-type", "")
                location = response.headers.get("location")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    remaining = self.config.max_bytes - size
                    if remaining <= 0:
                        break
                    chunks.append(chunk[:remaining])
                    size += min(len(chunk), remaining)
                raw = b"".join(chunks)
                encoding = response.encoding or "utf-8"
                body = raw.decode(encoding, errors="replace")
                elapsed = int((time.perf_counter() - started) * 1000)
                error = None
                if size >= self.config.max_bytes:
                    error = f"Resposta truncada em {self.config.max_bytes} bytes"
                return FetchResult(
                    status=response.status_code,
                    final_url=str(response.url),
                    content_type=content_type,
                    body=body,
                    bytes_read=size,
                    elapsed_ms=elapsed,
                    location=location,
                    error=error,
                )
        except (httpx.HTTPError, UnicodeError) as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            return FetchResult(
                status=None,
                final_url=url,
                content_type="",
                body="",
                bytes_read=0,
                elapsed_ms=elapsed,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _render(
        self, url: str
    ) -> tuple[str, str, set[str], list[tuple[str, str]]]:
        async with self._render_semaphore:
            return await self._render_page(url)

    async def _render_page(
        self, url: str
    ) -> tuple[str, str, set[str], list[tuple[str, str]]]:
        page = await self._browser_context.new_page()
        requested_urls: set[str] = set()
        responses = []

        def response_handler(response) -> None:
            if len(responses) >= 100:
                return
            resource_type = response.request.resource_type
            content_type = response.headers.get("content-type", "").lower()
            if resource_type not in {"xhr", "fetch"} and "json" not in content_type:
                return
            if response.status >= 400 or not self.scope.contains(response.url):
                return
            responses.append(response)

        async def route_handler(route) -> None:
            request = route.request
            normalized_request = normalize_url(request.url, url)
            if normalized_request:
                requested_urls.add(normalized_request)
            if request.resource_type in {"image", "media", "font", "stylesheet"}:
                await route.abort()
                return
            if request.resource_type == "document" and not self.scope.contains(request.url):
                await route.abort()
                return
            await route.continue_()

        await page.route("**/*", route_handler)
        page.on("response", response_handler)
        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(self.config.request_timeout * 1000),
            )
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            rendered = (await page.content())[: self.config.max_bytes]
            captured: list[tuple[str, str]] = []
            capture_budget = min(self.config.max_bytes, 2_000_000)
            used = 0
            prioritized = sorted(
                responses,
                key=lambda response: (
                    response.request.resource_type not in {"xhr", "fetch"},
                    "json" not in response.headers.get("content-type", "").lower(),
                ),
            )
            seen_responses: set[str] = set()
            for response in prioritized:
                response_url = normalize_url(response.url, url)
                if (
                    not response_url
                    or response_url in seen_responses
                    or not self.scope.contains(response_url)
                    or response.status >= 400
                ):
                    continue
                resource_type = response.request.resource_type
                content_type = response.headers.get("content-type", "").lower()
                if resource_type not in {"xhr", "fetch"} and "json" not in content_type:
                    continue
                try:
                    response_text = await response.text()
                except Exception:
                    continue
                encoded_size = len(response_text.encode("utf-8", errors="ignore"))
                if not response_text or used + encoded_size > capture_budget:
                    continue
                used += encoded_size
                seen_responses.add(response_url)
                captured.append((response_url, response_text))
            return rendered, page.url, requested_urls, captured
        finally:
            await page.close()

    async def _robots_allowed(self, url: str) -> bool:
        host = urlsplit(url).netloc.lower()
        async with self._robots_lock:
            if host in self._robots:
                parser = self._robots[host]
                return True if parser is None else parser.can_fetch(self.config.user_agent, url)

            robots_url = f"{urlsplit(url).scheme}://{host}/robots.txt"
            parser = RobotFileParser()
            parser.set_url(robots_url)
            try:
                response = await self.client.get(robots_url, follow_redirects=True)
                if response.status_code < 400:
                    parser.parse(response.text.splitlines())
                    self._robots[host] = parser
                    return parser.can_fetch(self.config.user_agent, url)
            except httpx.HTTPError:
                pass
            self._robots[host] = None
            return True

    def _record_provider_url(
        self, source: str, target: str, depth: int, context: str
    ) -> None:
        for provider in identify_providers(target):
            self._record_finding(
                Finding(
                    source_page=source,
                    provider=provider,
                    reference=target,
                    resolved_url=target,
                    context=context,
                    depth=depth,
                )
            )

    def _record_finding(self, finding: Finding) -> None:
        key = (
            finding.source_page,
            finding.provider,
            finding.resolved_url or finding.reference,
            finding.context,
        )
        if key not in self._finding_keys:
            self._finding_keys.add(key)
            self._findings.append(finding)

    async def _notify(self, page: PageRecord) -> None:
        if not self.progress:
            return
        result = self.progress(page, len(self._pages), len(self._seen))
        if asyncio.iscoroutine(result):
            await result

    @staticmethod
    def _extractable_content(content_type: str) -> bool:
        if not content_type:
            return True
        lowered = content_type.lower()
        return any(
            marker in lowered
            for marker in ("html", "xml", "text/", "javascript", "json", "xhtml")
        )

    @staticmethod
    def _crawl_priority(url: str) -> int:
        path = urlsplit(url).path.lower()
        if "/episodios/" in path:
            return 0
        if "/series/online/" in path or "/filmes/online/" in path:
            return 1
        if any(
            marker in path
            for marker in ("/uploads/", "/applications/", "/cdn-cgi/")
        ):
            return 9
        return 5

    @staticmethod
    def _append_error(current: str | None, extra: str) -> str:
        return f"{current}; {extra}" if current else extra


def result_as_json(result: CrawlResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
