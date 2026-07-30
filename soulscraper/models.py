from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Finding:
    source_page: str
    provider: str
    reference: str
    resolved_url: str | None
    context: str
    movie_name: str | None = None
    tmdb_id: int | None = None
    media_type: str | None = None
    tag: str | None = None
    attribute: str | None = None
    depth: int = 0
    health_status: str = "unchecked"
    health_http_status: int | None = None
    health_final_url: str | None = None
    health_checked_at: str | None = None
    health_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PageRecord:
    url: str
    final_url: str
    depth: int
    status: int | None
    content_type: str = ""
    elapsed_ms: int = 0
    bytes_read: int = 0
    links_found: int = 0
    findings_found: int = 0
    movie_name: str | None = None
    tmdb_id: int | None = None
    media_type: str | None = None
    rendered: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CrawlResult:
    start_url: str
    started_at: str
    finished_at: str
    config: dict[str, Any]
    pages: list[PageRecord] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        provider_totals: dict[str, int] = {}
        health_totals: dict[str, int] = {}
        for finding in self.findings:
            provider_totals[finding.provider] = provider_totals.get(finding.provider, 0) + 1
            health_totals[finding.health_status] = (
                health_totals.get(finding.health_status, 0) + 1
            )
        return {
            "summary": {
                "start_url": self.start_url,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "pages_processed": len(self.pages),
                "findings": len(self.findings),
                "providers": provider_totals,
                "health": health_totals,
            },
            "config": self.config,
            "pages": [page.to_dict() for page in self.pages],
            "findings": [finding.to_dict() for finding in self.findings],
        }
