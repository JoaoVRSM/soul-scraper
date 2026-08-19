from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import secrets
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .crawler import CrawlConfig, SiteCrawler
from .models import CrawlResult, PageRecord
from .reporting import (
    build_catalog_export,
    build_movies_export,
    build_series_export,
    write_reports,
)
from .validator import LinkCheck, LinkValidator


WEB_DIR = Path(__file__).with_name("web")
REPORT_ROOT = Path("resultados") / "web"
API_PREFIX = "/api/v1"
PROVIDERS = ("byse", "doodstream", "mixdrop", "streamtape")
DOWNLOAD_FILES = {
    "catalogo-links.json",
    "filmes-links.json",
    "series-links.json",
    "resultado.json",
    "referencias.csv",
    "paginas.csv",
}


class CrawlRequest(BaseModel):
    url: str = Field(
        min_length=3,
        max_length=2048,
        description="Site autorizado que será analisado. O https:// é opcional.",
        examples=["https://seu-site.com"],
    )
    max_pages: int = Field(default=5000, ge=1, le=100_000, description="Limite de páginas.")
    max_depth: int = Field(default=20, ge=0, le=100, description="Profundidade máxima de links.")
    concurrency: int = Field(default=12, ge=1, le=64, description="Requisições HTTP simultâneas.")
    browser_concurrency: int = Field(default=4, ge=1, le=12, description="Abas simultâneas do navegador.")
    timeout: float = Field(default=20.0, ge=1, le=180, description="Timeout por página, em segundos.")
    delay: float = Field(default=0.0, ge=0, le=60, description="Pausa entre requisições, em segundos.")
    max_mb: float = Field(default=8.0, ge=0.1, le=100, description="Tamanho máximo de cada resposta.")
    subdomains: bool = Field(default=True, description="Inclui subdomínios do site.")
    respect_robots: bool = Field(default=True, description="Respeita as regras do robots.txt.")
    render_js: bool = Field(default=True, description="Abre páginas dinâmicas no Chromium.")
    sitemap: bool = Field(default=True, description="Usa o sitemap para descobrir páginas.")
    validate_links: bool = Field(default=True, description="Confere a saúde dos players encontrados.")

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip()
        if "://" not in value:
            value = f"https://{value}"
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("Use um endereço de site HTTP ou HTTPS")
        return value


class ScrapeRequest(CrawlRequest):
    max_pages: int = Field(
        default=300,
        ge=1,
        le=100_000,
        description="Limite de páginas. O padrão equilibrado é 300.",
    )
    max_depth: int = Field(
        default=10,
        ge=0,
        le=100,
        description="Profundidade máxima. O padrão equilibrado é 10.",
    )
    browser_concurrency: int = Field(
        default=2,
        ge=1,
        le=12,
        description="Abas simultâneas do navegador. O padrão é 2.",
    )
    output: Literal["catalog", "full", "movies", "series"] = Field(
        default="catalog",
        description="Formato do JSON: catálogo, relatório completo, filmes ou séries.",
    )
    download: bool = Field(
        default=False,
        description="Força o navegador a baixar a resposta como arquivo .json.",
    )


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=500)


JobStatus = Literal[
    "queued",
    "running",
    "validating",
    "reextracting",
    "completed",
    "failed",
    "cancelled",
]


class ApiLinks(BaseModel):
    self: str
    result: str
    pages: str
    findings: str
    catalog: str


class JobResponse(BaseModel):
    id: str
    target_url: str
    status: JobStatus
    created_at: str
    updated_at: str
    processed: int
    discovered: int
    findings_count: int
    validated_count: int
    validation_total: int
    providers: dict[str, int]
    health: dict[str, int]
    latest_pages: list[dict[str, Any]]
    error: str | None
    api: ApiLinks
    downloads: dict[str, str]


class JobListResponse(BaseModel):
    total: int
    offset: int
    items: list[JobResponse]


class FindingResponse(BaseModel):
    source_page: str
    provider: str
    reference: str
    resolved_url: str | None
    context: str
    movie_name: str | None
    tmdb_id: int | None
    media_type: str | None
    tag: str | None
    attribute: str | None
    depth: int
    health_status: str
    health_http_status: int | None
    health_final_url: str | None
    health_checked_at: str | None
    health_error: str | None


class PageResponse(BaseModel):
    url: str
    final_url: str
    depth: int
    status: int | None
    content_type: str
    elapsed_ms: int
    bytes_read: int
    links_found: int
    findings_found: int
    movie_name: str | None
    tmdb_id: int | None
    media_type: str | None
    rendered: bool
    error: str | None


class FindingsResponse(BaseModel):
    total: int
    offset: int
    items: list[FindingResponse]


class PagesResponse(BaseModel):
    total: int
    offset: int
    items: list[PageResponse]


class CrawlResultResponse(BaseModel):
    summary: dict[str, Any]
    config: dict[str, Any]
    pages: list[PageResponse]
    findings: list[FindingResponse]


@dataclass
class CrawlJob:
    id: str
    request: CrawlRequest
    status: JobStatus = "queued"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    processed: int = 0
    discovered: int = 0
    findings_count: int = 0
    validated_count: int = 0
    validation_total: int = 0
    providers: dict[str, int] = field(
        default_factory=lambda: {provider: 0 for provider in PROVIDERS}
    )
    health: dict[str, int] = field(default_factory=lambda: {"unchecked": 0})
    latest_pages: list[dict[str, Any]] = field(default_factory=list)
    result: CrawlResult | None = None
    error: str | None = None
    output_dir: Path | None = None
    task: asyncio.Task | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target_url": self.request.url,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "processed": self.processed,
            "discovered": self.discovered,
            "findings_count": self.findings_count,
            "validated_count": self.validated_count,
            "validation_total": self.validation_total,
            "providers": self.providers,
            "health": self.health,
            "latest_pages": self.latest_pages[-100:],
            "error": self.error,
            "api": {
                "self": f"{API_PREFIX}/crawls/{self.id}",
                "result": f"{API_PREFIX}/crawls/{self.id}/result",
                "pages": f"{API_PREFIX}/crawls/{self.id}/pages",
                "findings": f"{API_PREFIX}/crawls/{self.id}/findings",
                "catalog": f"{API_PREFIX}/crawls/{self.id}/catalog",
            },
            "downloads": (
                {
                    name: f"{API_PREFIX}/crawls/{self.id}/download/{name}"
                    for name in sorted(DOWNLOAD_FILES)
                }
                if self.status == "completed"
                else {}
            ),
        }


class JobManager:
    def __init__(self, max_active: int = 1) -> None:
        self.jobs: dict[str, CrawlJob] = {}
        self.max_active = max_active
        self._lock = asyncio.Lock()

    async def create(self, request: CrawlRequest) -> CrawlJob:
        async with self._lock:
            active = sum(
                job.status in {"queued", "running", "validating", "reextracting"}
                for job in self.jobs.values()
            )
            if active >= self.max_active:
                raise HTTPException(
                    status_code=429,
                    detail="Já existe uma auditoria em execução. Aguarde ou cancele antes de iniciar outra.",
                )
            job = CrawlJob(id=uuid4().hex[:12], request=request)
            self.jobs[job.id] = job
            job.task = asyncio.create_task(self._run(job), name=f"web-crawl-{job.id}")
            return job

    def get(self, job_id: str) -> CrawlJob:
        job = self.jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Auditoria não encontrada.")
        return job

    def list(self) -> list[CrawlJob]:
        return sorted(self.jobs.values(), key=lambda job: job.created_at, reverse=True)

    async def cancel(self, job_id: str) -> CrawlJob:
        job = self.get(job_id)
        if job.status not in {"queued", "running", "validating", "reextracting"}:
            raise HTTPException(status_code=409, detail="Esta auditoria já terminou.")
        if job.task:
            job.task.cancel()
        return job

    async def revalidate(self, job_id: str) -> CrawlJob:
        job = self.get(job_id)
        if job.status != "completed" or not job.result:
            raise HTTPException(status_code=409, detail="A auditoria ainda não está pronta.")
        job.task = asyncio.create_task(
            self._run_revalidation(job), name=f"web-validate-{job.id}"
        )
        return job

    async def reextract_broken(self, job_id: str) -> CrawlJob:
        job = self.get(job_id)
        if job.status != "completed" or not job.result:
            raise HTTPException(status_code=409, detail="A auditoria ainda não está pronta.")
        broken_statuses = {"dead", "timeout", "malformed", "unknown", "rate_limited"}
        sources = sorted(
            {
                finding.source_page
                for finding in job.result.findings
                if finding.health_status in broken_statuses
            }
        )
        if not sources:
            raise HTTPException(
                status_code=409, detail="Nenhuma página com link quebrado foi encontrada."
            )
        if len(sources) > 50:
            raise HTTPException(
                status_code=409,
                detail="Há mais de 50 páginas quebradas. Refine a auditoria antes de reextrair.",
            )
        job.task = asyncio.create_task(
            self._run_reextraction(job, sources), name=f"web-reextract-{job.id}"
        )
        return job

    async def _run(self, job: CrawlJob) -> None:
        request = job.request
        config = CrawlConfig(
            start_url=request.url,
            max_pages=request.max_pages,
            max_depth=request.max_depth,
            concurrency=request.concurrency,
            browser_concurrency=request.browser_concurrency,
            request_timeout=request.timeout,
            delay=request.delay,
            max_bytes=int(request.max_mb * 1024 * 1024),
            include_subdomains=request.subdomains,
            respect_robots=request.respect_robots,
            render_js=request.render_js,
            discover_sitemap=request.sitemap,
        )

        async def progress(page: PageRecord, processed: int, discovered: int) -> None:
            job.processed = processed
            job.discovered = discovered
            job.findings_count += page.findings_found
            job.latest_pages.append(page.to_dict())
            if len(job.latest_pages) > 100:
                del job.latest_pages[:-100]
            job.updated_at = datetime.now(timezone.utc).isoformat()

        job.status = "running"
        job.updated_at = datetime.now(timezone.utc).isoformat()
        try:
            result = await SiteCrawler(config, progress=progress).crawl()
            job.result = result
            self._refresh_summary(job)
            if request.validate_links:
                await self._validate(job)
            self._write_reports(job)
            job.status = "completed"
        except asyncio.CancelledError:
            job.status = "cancelled"
        except Exception as exc:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.updated_at = datetime.now(timezone.utc).isoformat()

    async def _validate(self, job: CrawlJob, *, only_broken: bool = False) -> None:
        if not job.result:
            return
        job.status = "validating"
        job.validated_count = 0
        job.validation_total = 0
        job.updated_at = datetime.now(timezone.utc).isoformat()

        async def validation_progress(
            checked: int, total: int, check: LinkCheck
        ) -> None:
            job.validated_count = checked
            job.validation_total = total
            job.updated_at = datetime.now(timezone.utc).isoformat()

        validator = LinkValidator(
            timeout=min(job.request.timeout, 20),
            concurrency=min(job.request.concurrency, 24),
            browser_concurrency=job.request.browser_concurrency,
            browser_fallback=True,
        )
        await validator.validate_result(
            job.result,
            progress=validation_progress,
            only_broken=only_broken,
        )
        self._refresh_summary(job)

    async def _run_revalidation(self, job: CrawlJob) -> None:
        try:
            await self._validate(job)
            self._write_reports(job)
            job.status = "completed"
            job.error = None
        except asyncio.CancelledError:
            job.status = "cancelled"
        except Exception as exc:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.updated_at = datetime.now(timezone.utc).isoformat()

    async def _run_reextraction(self, job: CrawlJob, sources: list[str]) -> None:
        if not job.result:
            return
        job.status = "reextracting"
        job.error = None
        job.processed = 0
        job.discovered = len(sources)
        job.updated_at = datetime.now(timezone.utc).isoformat()
        replacement_findings = []
        replaced_sources: set[str] = set()
        new_pages = []
        request = job.request
        try:
            for source in sources:
                config = CrawlConfig(
                    start_url=source,
                    max_pages=1,
                    max_depth=0,
                    concurrency=1,
                    browser_concurrency=1,
                    request_timeout=request.timeout,
                    delay=request.delay,
                    max_bytes=int(request.max_mb * 1024 * 1024),
                    include_subdomains=request.subdomains,
                    respect_robots=request.respect_robots,
                    render_js=True,
                    discover_sitemap=False,
                )
                refreshed = await SiteCrawler(config).crawl()
                job.processed += 1
                job.updated_at = datetime.now(timezone.utc).isoformat()
                if refreshed.findings:
                    replacement_findings.extend(refreshed.findings)
                    replaced_sources.add(source)
                new_pages.extend(refreshed.pages)

            job.result.findings = [
                finding
                for finding in job.result.findings
                if finding.source_page not in replaced_sources
            ] + replacement_findings
            job.result.pages.extend(new_pages)
            job.result.findings.sort(
                key=lambda item: (
                    item.provider,
                    item.source_page,
                    item.resolved_url or item.reference,
                )
            )
            self._refresh_summary(job)
            await self._validate(job)
            self._write_reports(job)
            job.status = "completed"
        except asyncio.CancelledError:
            job.status = "cancelled"
        except Exception as exc:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.updated_at = datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _refresh_summary(job: CrawlJob) -> None:
        if not job.result:
            return
        counts = Counter(finding.provider for finding in job.result.findings)
        health_counts = Counter(finding.health_status for finding in job.result.findings)
        job.providers = {provider: counts.get(provider, 0) for provider in PROVIDERS}
        job.health = dict(health_counts)
        job.findings_count = len(job.result.findings)

    @staticmethod
    def _write_reports(job: CrawlJob) -> None:
        if not job.result:
            return
        output_dir = REPORT_ROOT / job.id
        write_reports(job.result, output_dir)
        job.output_dir = output_dir.resolve()


manager = JobManager()
app = FastAPI(
    title="Soul Scraper API",
    description=(
        "API para o seu site enviar a URL de outro domínio autorizado e receber "
        "o catálogo JSON. Para começar, envie somente o campo `url` em "
        "`POST /api/v1/scrape`."
    ),
    version="2.0.0",
    openapi_url="/api/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {
            "name": "Comece aqui",
            "description": "A forma mais simples: envie uma URL e receba o JSON pronto.",
        },
        {
            "name": "Raspagens em segundo plano",
            "description": "Use para sites grandes, com consulta de progresso e cancelamento.",
        },
        {
            "name": "API v1",
            "description": "Consulta detalhada dos resultados e operações avançadas.",
        },
    ],
)


def _valid_basic_auth(request: Request, username: str, password: str) -> bool:
    authorization = request.headers.get("Authorization", "")
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False

    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False

    supplied_username, separator, supplied_password = decoded.partition(":")
    return bool(separator) and secrets.compare_digest(
        supplied_username, username
    ) and secrets.compare_digest(supplied_password, password)


def _auth_cookie_value(username: str, password: str) -> str:
    return hashlib.sha256(f"{username}\0{password}".encode("utf-8")).hexdigest()


LOGIN_PAGE = """<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Acesso · Soul Scraper</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; color: #f4f7ef;
      background: radial-gradient(circle at 20% 15%, #243520 0, #0d130d 34%, #080a08 72%); }
    main { width: min(92vw, 430px); padding: 38px; border: 1px solid #344631; border-radius: 24px;
      background: rgba(14, 20, 14, .92); box-shadow: 0 28px 80px rgba(0, 0, 0, .46); }
    .eyebrow { margin: 0 0 12px; color: #b8ff3d; font-size: 12px; font-weight: 800; letter-spacing: .16em; }
    h1 { margin: 0; font-size: clamp(30px, 8vw, 43px); line-height: .98; }
    p { color: #aeb9aa; line-height: 1.55; }
    label { display: block; margin-top: 18px; color: #dce5d8; font-size: 13px; font-weight: 700; }
    input { width: 100%; margin-top: 8px; padding: 14px 15px; border: 1px solid #3a4937; border-radius: 12px;
      color: #fff; background: #090d09; outline: none; }
    input:focus { border-color: #b8ff3d; box-shadow: 0 0 0 3px rgba(184, 255, 61, .12); }
    button { width: 100%; margin-top: 24px; padding: 15px; border: 0; border-radius: 12px; cursor: pointer;
      color: #0b1009; background: #b8ff3d; font-weight: 900; letter-spacing: .06em; }
    #error { min-height: 20px; margin: 14px 0 0; color: #ff8d91; font-size: 13px; }
  </style>
</head>
<body>
  <main>
    <p class="eyebrow">SOUL SCRAPER · ÁREA PROTEGIDA</p>
    <h1>Entre para continuar.</h1>
    <p>O painel online é privado para impedir o uso não autorizado do crawler.</p>
    <form id="login-form">
      <label for="username">Usuário</label>
      <input id="username" name="username" autocomplete="username" required autofocus>
      <label for="password">Senha</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      <button type="submit">ENTRAR NO PAINEL</button>
      <p id="error" role="alert"></p>
    </form>
  </main>
  <script>
    document.querySelector("#login-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const error = document.querySelector("#error");
      error.textContent = "";
      const response = await fetch("/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: document.querySelector("#username").value,
          password: document.querySelector("#password").value
        })
      });
      if (!response.ok) { error.textContent = "Usuário ou senha incorretos."; return; }
      location.replace("/");
    });
  </script>
</body>
</html>"""


@app.middleware("http")
async def protect_online_deployment(request: Request, call_next):
    """Protege toda a interface quando credenciais forem definidas no ambiente."""
    username = os.getenv("SOULSCRAPER_BASIC_USER", "").strip()
    password = os.getenv("SOULSCRAPER_BASIC_PASSWORD", "")
    if not username or not password:
        return await call_next(request)
    if request.url.path == "/login":
        return await call_next(request)

    cookie_value = _auth_cookie_value(username, password)
    has_valid_cookie = secrets.compare_digest(
        request.cookies.get("soul_scraper_auth", ""), cookie_value
    )
    has_valid_basic_auth = _valid_basic_auth(request, username, password)
    if not has_valid_cookie and not has_valid_basic_auth:
        if request.method == "GET" and "text/html" in request.headers.get("Accept", ""):
            return HTMLResponse(LOGIN_PAGE, status_code=200)
        return PlainTextResponse(
            "Autenticacao necessaria.",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Soul Scraper"'},
        )

    response = await call_next(request)
    if has_valid_basic_auth and not has_valid_cookie:
        response.set_cookie(
            "soul_scraper_auth",
            cookie_value,
            max_age=43_200,
            httponly=True,
            secure=True,
            samesite="strict",
        )
    return response

cors_origins = [
    origin.strip()
    for origin in os.getenv("SOULSCRAPER_CORS_ORIGINS", "").split(",")
    if origin.strip()
]
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.post("/login", include_in_schema=False)
async def login(credentials: LoginRequest) -> JSONResponse:
    username = os.getenv("SOULSCRAPER_BASIC_USER", "").strip()
    password = os.getenv("SOULSCRAPER_BASIC_PASSWORD", "")
    valid = bool(username and password) and secrets.compare_digest(
        credentials.username, username
    ) and secrets.compare_digest(credentials.password, password)
    if not valid:
        return JSONResponse({"detail": "Credenciais invalidas."}, status_code=401)

    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        "soul_scraper_auth",
        _auth_cookie_value(username, password),
        max_age=43_200,
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return response


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(
        WEB_DIR / "index.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/painel", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(
        WEB_DIR / "index.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/integracao", include_in_schema=False)
async def api_portal() -> FileResponse:
    return FileResponse(
        WEB_DIR / "api.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get(f"{API_PREFIX}", tags=["Comece aqui"], summary="Como usar a API")
async def api_guide() -> dict[str, Any]:
    """Mostra o caminho mais curto para integrar a API."""
    return {
        "message": "Seu site envia a URL de origem e recebe o JSON na resposta.",
        "flow": ["site_de_origem", "soul_scraper_fastapi", "json", "seu_site"],
        "request": {
            "method": "POST",
            "endpoint": f"{API_PREFIX}/scrape",
            "body": {"url": "https://seu-site.com"},
        },
        "response": {
            "content_type": "application/json",
            "default_file": "catalogo-links.json",
            "use": "Seu site pode importar, salvar no banco ou exibir os dados.",
        },
        "interfaces": {
            "sistema_de_raspagem": "/",
            "portal_de_integracao": "/integracao",
            "documentacao_interativa": "/docs",
        },
    }


@app.get("/api/health", include_in_schema=False)
@app.get(f"{API_PREFIX}/health", tags=["Comece aqui"])
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "2.0.0"}


@app.post(
    f"{API_PREFIX}/scrape",
    tags=["Comece aqui"],
    summary="Envie uma URL e receba o JSON pronto",
    description=(
        "O único campo obrigatório é `url`. A requisição aguarda a raspagem "
        "terminar e devolve o formato escolhido diretamente na resposta."
    ),
    response_model=None,
)
async def scrape_and_wait(request: ScrapeRequest) -> JSONResponse:
    """Executa a raspagem na mesma requisição e retorna o formato JSON escolhido."""
    crawl_request = CrawlRequest.model_validate(
        request.model_dump(exclude={"output", "download"})
    )
    job = await manager.create(crawl_request)
    if not job.task:
        raise HTTPException(status_code=500, detail="A raspagem não pôde ser iniciada.")

    await job.task
    if job.status == "failed":
        raise HTTPException(status_code=502, detail=job.error or "A raspagem falhou.")
    if job.status == "cancelled" or not job.result:
        raise HTTPException(status_code=409, detail="A raspagem foi cancelada.")

    outputs: dict[str, tuple[Any, str]] = {
        "catalog": (build_catalog_export(job.result), "catalogo-links.json"),
        "full": (job.result.to_dict(), "resultado.json"),
        "movies": (build_movies_export(job.result), "filmes-links.json"),
        "series": (build_series_export(job.result), "series-links.json"),
    }
    payload, filename = outputs[request.output]
    disposition = "attachment" if request.download else "inline"
    return JSONResponse(
        content=payload,
        headers={
            "X-Crawl-Job-Id": job.id,
            "Content-Disposition": f'{disposition}; filename="{filename}"',
        },
    )


@app.post("/api/jobs", status_code=202, include_in_schema=False)
@app.post(
    f"{API_PREFIX}/crawls",
    status_code=202,
    tags=["Raspagens em segundo plano"],
    summary="Inicia uma raspagem com progresso",
)
async def create_job(request: CrawlRequest) -> JobResponse:
    try:
        job = await manager.create(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JobResponse.model_validate(job.public())


@app.get("/api/jobs/{job_id}", include_in_schema=False)
@app.get(f"{API_PREFIX}/crawls/{{job_id}}", tags=["API v1"])
async def get_job(job_id: str) -> JobResponse:
    return JobResponse.model_validate(manager.get(job_id).public())


@app.post("/api/jobs/{job_id}/cancel", include_in_schema=False)
@app.delete(f"{API_PREFIX}/crawls/{{job_id}}", tags=["API v1"])
@app.post(f"{API_PREFIX}/crawls/{{job_id}}/cancel", tags=["API v1"])
async def cancel_job(job_id: str) -> JobResponse:
    job = await manager.cancel(job_id)
    await asyncio.sleep(0)
    return JobResponse.model_validate(job.public())


@app.post("/api/jobs/{job_id}/validate", include_in_schema=False)
@app.post(f"{API_PREFIX}/crawls/{{job_id}}/validate", tags=["API v1"])
async def revalidate_job(job_id: str) -> JobResponse:
    return JobResponse.model_validate((await manager.revalidate(job_id)).public())


@app.post("/api/jobs/{job_id}/reextract", include_in_schema=False)
@app.post(f"{API_PREFIX}/crawls/{{job_id}}/reextract", tags=["API v1"])
async def reextract_job(job_id: str) -> JobResponse:
    return JobResponse.model_validate((await manager.reextract_broken(job_id)).public())


@app.get("/api/jobs/{job_id}/findings", include_in_schema=False)
@app.get(f"{API_PREFIX}/crawls/{{job_id}}/findings", tags=["API v1"])
async def get_findings(
    job_id: str,
    provider: str | None = Query(default=None),
    health: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> FindingsResponse:
    job = manager.get(job_id)
    if not job.result:
        return FindingsResponse(total=0, offset=offset, items=[])
    findings = job.result.findings
    if provider and provider != "all":
        findings = [item for item in findings if item.provider == provider]
    if health and health != "all":
        findings = [item for item in findings if item.health_status == health]
    if search:
        needle = search.casefold()
        findings = [
            item
            for item in findings
            if needle in item.source_page.casefold()
            or needle in item.reference.casefold()
            or needle in (item.resolved_url or "").casefold()
            or needle in (item.movie_name or "").casefold()
            or needle in (str(item.tmdb_id) if item.tmdb_id is not None else "")
        ]
    return FindingsResponse.model_validate({
        "total": len(findings),
        "offset": offset,
        "items": [item.to_dict() for item in findings[offset : offset + limit]],
    })


@app.get(f"{API_PREFIX}/crawls", tags=["API v1"])
async def list_jobs(
    status: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> JobListResponse:
    jobs = manager.list()
    if status:
        jobs = [job for job in jobs if job.status == status]
    return JobListResponse.model_validate({
        "total": len(jobs),
        "offset": offset,
        "items": [job.public() for job in jobs[offset : offset + limit]],
    })


def require_result(job_id: str) -> CrawlJob:
    job = manager.get(job_id)
    if not job.result:
        raise HTTPException(
            status_code=409,
            detail="O resultado ainda não está disponível. Consulte o status da raspagem.",
        )
    return job


@app.get(f"{API_PREFIX}/crawls/{{job_id}}/result", tags=["API v1"])
async def get_result(job_id: str) -> CrawlResultResponse:
    job = require_result(job_id)
    assert job.result is not None
    return CrawlResultResponse.model_validate(job.result.to_dict())


@app.get(f"{API_PREFIX}/crawls/{{job_id}}/pages", tags=["API v1"])
async def get_pages(
    job_id: str,
    search: str | None = Query(default=None, max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> PagesResponse:
    job = require_result(job_id)
    assert job.result is not None
    pages = job.result.pages
    if search:
        needle = search.casefold()
        pages = [
            page
            for page in pages
            if needle in page.url.casefold() or needle in page.final_url.casefold()
        ]
    return PagesResponse.model_validate({
        "total": len(pages),
        "offset": offset,
        "items": [page.to_dict() for page in pages[offset : offset + limit]],
    })


@app.get(f"{API_PREFIX}/crawls/{{job_id}}/catalog", tags=["API v1"])
async def get_catalog(job_id: str) -> dict[str, list[dict[str, Any]]]:
    job = require_result(job_id)
    assert job.result is not None
    return build_catalog_export(job.result)


@app.get(f"{API_PREFIX}/crawls/{{job_id}}/movies", tags=["API v1"])
async def get_movies(job_id: str) -> list[dict[str, Any]]:
    job = require_result(job_id)
    assert job.result is not None
    return build_movies_export(job.result)


@app.get(f"{API_PREFIX}/crawls/{{job_id}}/series", tags=["API v1"])
async def get_series(job_id: str) -> list[dict[str, Any]]:
    job = require_result(job_id)
    assert job.result is not None
    return build_series_export(job.result)


@app.get("/api/jobs/{job_id}/download/{filename}", include_in_schema=False)
@app.get(
    f"{API_PREFIX}/crawls/{{job_id}}/download/{{filename}}",
    tags=["API v1"],
)
async def download(job_id: str, filename: str) -> FileResponse:
    job = manager.get(job_id)
    if filename not in DOWNLOAD_FILES or not job.output_dir:
        raise HTTPException(status_code=404, detail="Relatório não encontrado.")
    target = job.output_dir / filename
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Relatório não encontrado.")
    media_type = "application/json" if target.suffix == ".json" else "text/csv"
    return FileResponse(target, filename=filename, media_type=media_type)
