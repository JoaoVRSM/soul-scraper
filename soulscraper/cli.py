from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import typer
from rich.console import Console
from rich.table import Table

from .crawler import CrawlConfig, SiteCrawler
from .reporting import default_output_dir, write_reports
from .validator import LinkValidator


app = typer.Typer(
    name="site-soul-scraper",
    help="Auditoria profunda de links e referencias de players em um dominio autorizado.",
    no_args_is_help=True,
)
console = Console()


@app.callback()
def main() -> None:
    """Ferramentas de auditoria autorizada de sites."""


@app.command()
def crawl(
    url: str = typer.Argument(..., help="URL inicial, incluindo http:// ou https://."),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Pasta de saida ou caminho de um arquivo JSON."
    ),
    max_pages: int = typer.Option(5000, min=1, help="Limite total de URLs internas."),
    max_depth: int = typer.Option(20, min=0, help="Profundidade maxima a partir da URL inicial."),
    concurrency: int = typer.Option(
        12, min=1, max=64, help="Numero de requisicoes simultaneas."
    ),
    browser_concurrency: int = typer.Option(
        4,
        "--browser-concurrency",
        min=1,
        max=12,
        help="Numero maximo de abas Chromium simultaneas.",
    ),
    timeout: float = typer.Option(20.0, min=1.0, help="Timeout por pagina em segundos."),
    delay: float = typer.Option(
        0.0, min=0.0, help="Pausa antes de cada requisicao, em segundos."
    ),
    max_mb: float = typer.Option(
        8.0, min=0.1, help="Maximo lido por resposta antes de truncar."
    ),
    subdomains: bool = typer.Option(
        True, "--subdomains/--no-subdomains", help="Inclui todos os subdominios do site."
    ),
    respect_robots: bool = typer.Option(
        True,
        "--respect-robots/--ignore-robots",
        help="Respeita robots.txt. Ignore apenas quando voce administra o alvo.",
    ),
    render_js: bool = typer.Option(
        False,
        "--render-js",
        help="Renderiza cada pagina com Chromium para capturar DOM criado por JavaScript.",
    ),
    sitemap: bool = typer.Option(
        True, "--sitemap/--no-sitemap", help="Tenta descobrir URLs por /sitemap.xml."
    ),
    validate_links: bool = typer.Option(
        False,
        "--validate-links",
        help="Testa a saúde dos links encontrados e usa Chromium em bloqueios.",
    ),
) -> None:
    """Rastreia o site e gera resultado.json, referencias.csv e paginas.csv."""
    config = CrawlConfig(
        start_url=url,
        max_pages=max_pages,
        max_depth=max_depth,
        concurrency=concurrency,
        browser_concurrency=browser_concurrency,
        request_timeout=timeout,
        delay=delay,
        max_bytes=int(max_mb * 1024 * 1024),
        include_subdomains=subdomains,
        respect_robots=respect_robots,
        render_js=render_js,
        discover_sitemap=sitemap,
    )

    console.print(
        f"[bold cyan]Auditando[/] {config.start_url} "
        f"[dim](ate {config.max_pages} URLs, profundidade {config.max_depth})[/]"
    )

    async def progress(page, processed: int, discovered: int) -> None:
        status = page.status if page.status is not None else "erro"
        host_path = f"{urlsplit(page.url).netloc}{urlsplit(page.url).path}"
        finding = (
            f" [bold magenta]+{page.findings_found} referencia(s)[/]"
            if page.findings_found
            else ""
        )
        console.print(
            f"[dim]{processed:>5}/{discovered:<5}[/] [{status}] "
            f"{host_path[:100]}{finding}"
        )

    try:
        result = asyncio.run(SiteCrawler(config, progress=progress).crawl())
        if validate_links:
            console.print("[bold yellow]Validando links encontrados...[/]")
            result = asyncio.run(
                LinkValidator(
                    timeout=min(timeout, 20),
                    concurrency=min(concurrency, 24),
                    browser_concurrency=browser_concurrency,
                    browser_fallback=True,
                ).validate_result(result)
            )
    except (ValueError, RuntimeError) as exc:
        console.print(f"[bold red]Erro:[/] {exc}")
        raise typer.Exit(code=2) from exc
    except KeyboardInterrupt:
        console.print("\n[yellow]Auditoria interrompida pelo usuario.[/]")
        raise typer.Exit(code=130)

    destination = write_reports(result, output or default_output_dir())
    totals = Counter(finding.provider for finding in result.findings)
    table = Table(title="Resumo da auditoria")
    table.add_column("Item")
    table.add_column("Total", justify="right")
    table.add_row("Paginas processadas", str(len(result.pages)))
    table.add_row("Referencias encontradas", str(len(result.findings)))
    for provider in ("byse", "doodstream", "mixdrop", "streamtape"):
        table.add_row(provider, str(totals.get(provider, 0)))
    if validate_links:
        healthy = sum(
            finding.health_status in {"working", "working_browser", "redirected"}
            for finding in result.findings
        )
        broken = sum(
            finding.health_status in {"dead", "malformed", "blocked_private"}
            for finding in result.findings
        )
        table.add_row("Links saudaveis", str(healthy))
        table.add_row("Links quebrados", str(broken))
    console.print(table)
    console.print(f"[bold green]Relatorios salvos em:[/] {destination.resolve()}")


@app.command()
def web(
    host: str = typer.Option(
        "127.0.0.1", help="Endereco local do servidor. Use 0.0.0.0 apenas em rede confiavel."
    ),
    port: int = typer.Option(8787, min=1, max=65535, help="Porta da interface."),
) -> None:
    """Abre o painel web para configurar e acompanhar auditorias."""
    try:
        import uvicorn
    except ImportError as exc:
        console.print(
            '[bold red]Erro:[/] dependencias web ausentes. Execute: '
            'python -m pip install -e "."'
        )
        raise typer.Exit(code=2) from exc

    console.print(f"[bold cyan]Site Soul Scraper[/] em http://{host}:{port}")
    console.print("[dim]Pressione Ctrl+C para encerrar.[/]")
    uvicorn.run("soulscraper.webapp:app", host=host, port=port, log_level="warning")
