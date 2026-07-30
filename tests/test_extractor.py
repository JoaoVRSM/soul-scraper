from soulscraper.extractor import extract_document, normalize_url
from soulscraper.providers import identify_providers


def in_example_scope(url: str) -> bool:
    return "example.test" in url


def test_detects_attributes_scripts_css_and_escaped_urls() -> None:
    html = r"""
    <html>
      <a href="/catalogo/filme">Filme</a>
      <iframe src="https://doodstream.com/e/abc"></iframe>
      <div data-player='{"file":"https:\/\/mixdrop.co\/e\/xyz"}'></div>
      <style>.poster { background: url(https://streamtape.com/get_video?id=1) }</style>
      <script>const backup = "https://byse.example/embed/42";</script>
    </html>
    """

    result = extract_document(html, "https://example.test/", 0, in_example_scope)

    assert "https://example.test/catalogo/filme" in result.crawl_links
    assert {item.provider for item in result.findings} == {
        "byse",
        "doodstream",
        "mixdrop",
        "streamtape",
    }
    assert all(item.resolved_url for item in result.findings)


def test_detects_base64_and_text_only_reference() -> None:
    encoded = "aHR0cHM6Ly9taXhkcm9wLmNvL2UvYWJj"
    html = f"<script>const packed='{encoded}'; const provider='streamtape';</script>"

    result = extract_document(html, "https://example.test/", 2, in_example_scope)

    by_provider = {item.provider: item for item in result.findings}
    assert by_provider["mixdrop"].resolved_url == "https://mixdrop.co/e/abc"
    assert by_provider["streamtape"].resolved_url is None
    assert by_provider["streamtape"].context == "text-marker"


def test_normalize_removes_fragment_and_rejects_non_http() -> None:
    assert normalize_url("../b?q=1#video", "https://Example.test/a/") == (
        "https://example.test/b?q=1"
    )
    assert normalize_url("javascript:alert(1)", "https://example.test/") is None


def test_provider_aliases() -> None:
    assert identify_providers("https://dood.to/e/123") == {"doodstream"}
    assert identify_providers("https://mixdrop.ag/e/123") == {"mixdrop"}
    assert identify_providers("https://streamtape.net/e/123") == {"streamtape"}
    assert identify_providers("https://playerbyse.example/e/123") == {"byse"}


def test_discovers_relative_routes_in_javascript_but_skips_binary_assets() -> None:
    html = """
    <script>fetch('/api/catalog?page=2')</script>
    <img src="/assets/capa.webp">
    """

    result = extract_document(html, "https://example.test/", 0, in_example_scope)

    assert "https://example.test/api/catalog?page=2" in result.crawl_links
    assert "https://example.test/assets/capa.webp" not in result.crawl_links


def test_meta_content_type_is_not_a_link_but_refresh_is() -> None:
    html = """
    <meta http-equiv="Content-Type" content="text/html; charset=utf-8">
    <meta http-equiv="refresh" content="0; URL=/nova-pagina">
    """

    result = extract_document(html, "https://example.test/", 0, in_example_scope)

    assert result.crawl_links == {"https://example.test/nova-pagina"}


def test_correlates_player_api_labels_prefixes_and_ids() -> None:
    payload = r"""
    {
      "players": [
        {"label": "Byse", "url": "https:\/\/bysebuho.com\/e\/", "downloadUrl": "https:\/\/bysebuho.com\/d\/"},
        {"label": "DoodStream", "url": "https:\/\/playmogo.com\/e\/", "downloadUrl": "https:\/\/playmogo.com\/d\/"},
        {"label": "MixDrop", "url": "https:\/\/mixdrop.top\/e\/"},
        {"label": "Streamtape", "url": "https:\/\/streamtape.com\/e\/"}
      ],
      "servers_dub": "mixdrop=mix1&amp;streamtape=tape1&amp;byse=byse1&amp;doodstream=dood1",
      "servers_leg": "doodstream=dood2"
    }
    """

    result = extract_document(payload, "https://example.test/movie", 1, in_example_scope)
    resolved = {(item.provider, item.resolved_url, item.context) for item in result.findings}

    assert (
        "doodstream",
        "https://playmogo.com/e/dood1",
        "player-api-dub-embed",
    ) in resolved
    assert (
        "doodstream",
        "https://playmogo.com/e/dood2",
        "player-api-leg-embed",
    ) in resolved
    assert (
        "byse",
        "https://bysebuho.com/d/byse1",
        "player-api-dub-download",
    ) in resolved


def test_extracts_movie_name_and_tmdb_id_from_html_metadata() -> None:
    html = """
    <html>
      <head>
        <meta property="og:title" content="Dia D">
      </head>
      <body>
        <iframe src="https://streamtape.com/e/movie1"></iframe>
        <a href="https://www.themoviedb.org/movie/123456">TMDB</a>
      </body>
    </html>
    """

    result = extract_document(html, "https://example.test/movie", 0, in_example_scope)

    assert result.movie_name == "Dia D"
    assert result.tmdb_id == 123456
    assert all(item.movie_name == "Dia D" for item in result.findings)
    assert all(item.tmdb_id == 123456 for item in result.findings)


def test_cleans_common_seo_words_from_movie_name() -> None:
    html = """
    <meta property="og:title" content="Assistir Dia D Dublado Online Grátis">
    <iframe src="https://streamtape.com/e/movie1"></iframe>
    """

    result = extract_document(html, "https://example.test/movie", 0, in_example_scope)

    assert result.movie_name == "Dia D"


def test_extracts_movie_metadata_from_player_json() -> None:
    payload = r"""
    {
      "movieTitle": "Dia D",
      "tmdbId": "987654",
      "players": [
        {"label": "Byse", "url": "https:\/\/bysebuho.com\/e\/"}
      ],
      "servers_dub": "byse=video1"
    }
    """

    result = extract_document(payload, "https://example.test/movie", 0, in_example_scope)

    assert result.movie_name == "Dia D"
    assert result.tmdb_id == 987654
    assert result.findings[0].movie_name == "Dia D"
    assert result.findings[0].tmdb_id == 987654


def test_identifies_series_from_tmdb_url_and_page_route() -> None:
    html = """
    <meta property="og:title" content="Minha Série Online">
    <a href="https://www.themoviedb.org/tv/555">TMDB</a>
    <iframe src="https://streamtape.com/e/episode1"></iframe>
    """

    result = extract_document(
        html,
        "https://example.test/series/minha-serie",
        0,
        in_example_scope,
    )

    assert result.movie_name == "Minha Série"
    assert result.tmdb_id == 555
    assert result.media_type == "serie"
    assert all(item.media_type == "serie" for item in result.findings)


def test_identifies_episode_routes_as_series() -> None:
    html = '<iframe src="https://streamtape.com/e/episode1"></iframe>'

    result = extract_document(
        html,
        "https://example.test/episodios/online/minha-serie-1x2-dublado",
        1,
        in_example_scope,
    )

    assert result.media_type == "serie"
    assert result.findings[0].media_type == "serie"
