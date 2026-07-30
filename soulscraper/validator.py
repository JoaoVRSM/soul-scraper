from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from .extractor import normalize_url
from .models import CrawlResult, Finding


ValidationProgress = Callable[[int, int, "LinkCheck"], Awaitable[None] | None]


@dataclass(frozen=True)
class LinkCheck:
    url: str
    status: str
    http_status: int | None = None
    final_url: str | None = None
    checked_at: str = ""
    error: str | None = None


class LinkValidator:
    """Valida URLs com leitura mínima e fallback opcional em Chromium."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        concurrency: int = 8,
        browser_concurrency: int = 4,
        browser_fallback: bool = True,
        enforce_public_hosts: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
    ) -> None:
        self.timeout = timeout
        self.concurrency = max(1, min(concurrency, 24))
        self.browser_concurrency = max(1, min(browser_concurrency, 12))
        self.browser_fallback = browser_fallback
        self.enforce_public_hosts = enforce_public_hosts
        self.transport = transport
        self.user_agent = user_agent

    async def validate_result(
        self,
        result: CrawlResult,
        progress: ValidationProgress | None = None,
        *,
        only_broken: bool = False,
    ) -> CrawlResult:
        actionable = self._actionable_findings(result.findings)
        if only_broken:
            broken = {"dead", "timeout", "malformed", "unknown", "rate_limited"}
            actionable = [
                finding for finding in actionable if finding.health_status in broken
            ]

        urls = list(dict.fromkeys(finding.resolved_url for finding in actionable if finding.resolved_url))
        referers = {
            finding.resolved_url: finding.source_page
            for finding in actionable
            if finding.resolved_url
        }
        checks = await self.validate_urls(urls, progress=progress, referers=referers)
        if not checks:
            return result

        result.findings = [
            replace(
                finding,
                health_status=checks[finding.resolved_url].status,
                health_http_status=checks[finding.resolved_url].http_status,
                health_final_url=checks[finding.resolved_url].final_url,
                health_checked_at=checks[finding.resolved_url].checked_at,
                health_error=checks[finding.resolved_url].error,
            )
            if finding.resolved_url in checks
            else finding
            for finding in result.findings
        ]
        return result

    async def validate_urls(
        self,
        urls: list[str],
        progress: ValidationProgress | None = None,
        referers: dict[str, str] | None = None,
    ) -> dict[str, LinkCheck]:
        if not urls:
            return {}
        semaphore = asyncio.Semaphore(self.concurrency)
        completed = 0
        results: dict[str, LinkCheck] = {}

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers={"User-Agent": self.user_agent},
            transport=self.transport,
        ) as client:

            async def run(url: str) -> None:
                nonlocal completed
                async with semaphore:
                    check = await self._check_http(
                        client, url, (referers or {}).get(url, url)
                    )
                results[url] = check
                completed += 1
                if progress:
                    callback_result = progress(completed, len(urls), check)
                    if asyncio.iscoroutine(callback_result):
                        await callback_result

            await asyncio.gather(*(run(url) for url in urls))

        fallback_urls = [
            url for url, check in results.items() if check.status == "browser_required"
        ]
        if self.browser_fallback and fallback_urls:
            browser_results = await self._check_with_browser(fallback_urls)
            results.update(browser_results)
        return results

    async def _check_http(
        self, client: httpx.AsyncClient, url: str, referer: str
    ) -> LinkCheck:
        normalized = normalize_url(url, url)
        checked_at = datetime.now(timezone.utc).isoformat()
        if not normalized:
            return LinkCheck(
                url=url,
                status="malformed",
                checked_at=checked_at,
                error="URL HTTP/HTTPS inválida",
            )

        current = normalized
        redirected = False
        for _ in range(6):
            if self.enforce_public_hosts and not await self._is_public_url(current):
                return LinkCheck(
                    url=normalized,
                    status="blocked_private",
                    final_url=current,
                    checked_at=checked_at,
                    error="Host privado, reservado ou não resolvido",
                )
            try:
                async with client.stream(
                    "GET",
                    current,
                    headers={
                        "Referer": referer,
                        "Range": "bytes=0-2048",
                        "Accept": "text/html,video/*;q=0.8,*/*;q=0.5",
                    },
                    follow_redirects=False,
                ) as response:
                    status = response.status_code
                    location = response.headers.get("location")
            except httpx.TimeoutException:
                return LinkCheck(
                    url=normalized,
                    status="timeout",
                    final_url=current,
                    checked_at=checked_at,
                    error="Tempo limite excedido",
                )
            except httpx.HTTPError as exc:
                return LinkCheck(
                    url=normalized,
                    status="unknown",
                    final_url=current,
                    checked_at=checked_at,
                    error=f"{type(exc).__name__}: {exc}",
                )

            if status in {301, 302, 303, 307, 308} and location:
                next_url = normalize_url(urljoin(current, location), current)
                if not next_url:
                    return LinkCheck(
                        url=normalized,
                        status="malformed",
                        http_status=status,
                        final_url=current,
                        checked_at=checked_at,
                        error="Redirect com URL inválida",
                    )
                current = next_url
                redirected = True
                continue
            if status in {200, 206}:
                return LinkCheck(
                    url=normalized,
                    status="redirected" if redirected else "working",
                    http_status=status,
                    final_url=current,
                    checked_at=checked_at,
                )
            if status in {401, 403, 406, 451, 503}:
                health = "browser_required"
            elif status == 429:
                health = "rate_limited"
            elif status in {404, 410}:
                health = "dead"
            else:
                health = "unknown"
            return LinkCheck(
                url=normalized,
                status=health,
                http_status=status,
                final_url=current,
                checked_at=checked_at,
            )

        return LinkCheck(
            url=normalized,
            status="unknown",
            final_url=current,
            checked_at=checked_at,
            error="Muitos redirects",
        )

    async def _check_with_browser(self, urls: list[str]) -> dict[str, LinkCheck]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {}

        results: dict[str, LinkCheck] = {}
        semaphore = asyncio.Semaphore(
            min(self.browser_concurrency, self.concurrency)
        )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-extensions",
                    "--no-first-run",
                ],
            )
            context = await browser.new_context(
                user_agent=self.user_agent,
                service_workers="block",
            )

            async def check(url: str) -> None:
                async with semaphore:
                    checked_at = datetime.now(timezone.utc).isoformat()
                    page = await context.new_page()

                    async def route_handler(route) -> None:
                        request = route.request
                        if request.resource_type in {
                            "image",
                            "media",
                            "font",
                            "stylesheet",
                        }:
                            await route.abort()
                            return
                        if (
                            request.resource_type == "document"
                            and self.enforce_public_hosts
                            and not await self._is_public_url(request.url)
                        ):
                            await route.abort()
                            return
                        await route.continue_()

                    await page.route("**/*", route_handler)
                    try:
                        response = await page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=int(self.timeout * 1000),
                        )
                        status_code = response.status if response else None
                        if status_code in {200, 206}:
                            status = "working_browser"
                        elif status_code in {404, 410}:
                            status = "dead"
                        else:
                            status = "browser_required"
                        results[url] = LinkCheck(
                            url=url,
                            status=status,
                            http_status=status_code,
                            final_url=page.url,
                            checked_at=checked_at,
                        )
                    except Exception as exc:
                        results[url] = LinkCheck(
                            url=url,
                            status="browser_required",
                            checked_at=checked_at,
                            error=f"Chromium: {type(exc).__name__}: {exc}",
                        )
                    finally:
                        await page.close()

            await asyncio.gather(*(check(url) for url in urls))
            await context.close()
            await browser.close()
        return results

    async def _is_public_url(self, url: str) -> bool:
        try:
            host = urlsplit(url).hostname
            if not host:
                return False
            try:
                addresses = [ipaddress.ip_address(host)]
            except ValueError:
                loop = asyncio.get_running_loop()
                info = await loop.getaddrinfo(
                    host,
                    None,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                )
                addresses = list(
                    {
                        ipaddress.ip_address(item[4][0])
                        for item in info
                        if item[4] and item[4][0]
                    }
                )
            return bool(addresses) and all(
                not (
                    address.is_private
                    or address.is_loopback
                    or address.is_link_local
                    or address.is_reserved
                    or address.is_multicast
                    or address.is_unspecified
                )
                for address in addresses
            )
        except (OSError, ValueError):
            return False

    @staticmethod
    def _actionable_findings(findings: list[Finding]) -> list[Finding]:
        api_full_keys = {
            (finding.source_page, finding.provider)
            for finding in findings
            if finding.context.startswith("player-api-")
            and not finding.context.endswith("-prefix")
            and finding.resolved_url
        }
        actionable: list[Finding] = []
        for finding in findings:
            if not finding.resolved_url or finding.context == "text-marker":
                continue
            if finding.context == "player-api-prefix":
                continue
            if (
                (finding.source_page, finding.provider) in api_full_keys
                and not finding.context.startswith("player-api-")
            ):
                continue
            actionable.append(finding)
        return actionable
