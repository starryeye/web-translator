from __future__ import annotations

import hashlib
import socket
from pathlib import Path

import httpx
import pytest

from web_translator.capture import MAX_CSS_IMPORT_DEPTH, CaptureError, capture_page


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "site"
FIXTURE_HTML = (FIXTURE_DIR / "index.html").read_text("utf-8")


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep capture tests offline while exercising DNS-result validation."""

    def resolve(host: str, port: int, *args: object, **kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def mock_transport(responses: dict[str, tuple[int, bytes, str]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        status, content, content_type = responses[str(request.url)]
        return httpx.Response(status, content=content, headers={"content-type": content_type})

    return httpx.MockTransport(handler)


def test_capture_rewrites_assets_and_preserves_links(tmp_path: Path) -> None:
    transport = mock_transport(
        {
            "https://example.com/docs/": (200, FIXTURE_HTML.encode(), "text/html; charset=utf-8"),
            "https://example.com/docs/theme.css": (
                200,
                b'.hero{background:url("img/bg.svg")}',
                "text/css",
            ),
            "https://example.com/docs/img/bg.svg": (200, b"<svg/>", "image/svg+xml"),
            "https://example.com/docs/logo.svg": (
                200,
                (FIXTURE_DIR / "logo.svg").read_bytes(),
                "image/svg+xml",
            ),
        }
    )

    result = capture_page("https://example.com/docs/", tmp_path, transport=transport)

    html = result.source_html.read_text("utf-8")
    assert 'href="assets/' in html
    assert 'src="assets/' in html
    assert 'href="https://example.com/next"' in html
    assert 'href="#section-1"' in html
    assert result.missing_optional_assets == []
    assert result.final_url == "https://example.com/docs/"
    assert result.fingerprints["source.html"] == hashlib.sha256(result.source_html.read_bytes()).hexdigest()
    css_path = tmp_path / result.asset_map["https://example.com/docs/theme.css"]
    assert 'url("' in css_path.read_text("utf-8")
    assert "img/bg.svg" not in css_path.read_text("utf-8")


def test_missing_stylesheet_is_fatal_but_image_is_warning(tmp_path: Path) -> None:
    html = '<link rel="stylesheet" href="main.css"><img src="missing.png">'
    transport = mock_transport(
        {
            "https://example.com/": (200, html.encode(), "text/html"),
            "https://example.com/main.css": (404, b"", "text/css"),
        }
    )

    with pytest.raises(CaptureError, match="critical stylesheet"):
        capture_page("https://example.com/", tmp_path, transport=transport)


def test_missing_image_is_warning_and_keeps_absolute_fallback(tmp_path: Path) -> None:
    html = '<img src="missing.png">'
    transport = mock_transport(
        {
            "https://example.com/": (200, html.encode(), "text/html"),
            "https://example.com/missing.png": (404, b"", "image/png"),
        }
    )

    result = capture_page("https://example.com/", tmp_path, transport=transport)

    assert result.missing_optional_assets == ["https://example.com/missing.png"]
    assert 'src="https://example.com/missing.png"' in result.source_html.read_text("utf-8")


def test_private_dns_result_is_rejected_before_initial_redirect_and_asset_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = '<img src="https://asset.example/image.png">'
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "start.example":
            return httpx.Response(302, headers={"location": "https://redirect.example/"})
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    def resolve(host: str, port: int, *args: object, **kwargs: object) -> list[tuple[object, ...]]:
        address = "10.0.0.1" if host == "redirect.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    with pytest.raises(CaptureError, match="non-public DNS"):
        capture_page("https://start.example/", tmp_path, transport=httpx.MockTransport(handler))
    assert requested == ["https://start.example/"]


def test_private_asset_dns_is_an_optional_warning_and_never_connects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = '<img src="https://asset.example/image.png">'
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    def resolve(host: str, port: int, *args: object, **kwargs: object) -> list[tuple[object, ...]]:
        address = "127.0.0.1" if host == "asset.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    result = capture_page("https://example.com/", tmp_path, transport=httpx.MockTransport(handler))

    assert requested == ["https://example.com/"]
    assert result.missing_optional_assets == ["https://asset.example/image.png"]


def test_srcset_and_recursive_css_imports_are_rewritten_once(tmp_path: Path) -> None:
    calls: list[str] = []
    html = (
        '<link rel="stylesheet" href="main.css">'
        '<img srcset="small.png 1x, large.png 2x" src="small.png">'
    )
    responses = {
        "https://example.com/": (200, html.encode(), "text/html"),
        "https://example.com/main.css": (200, b'@import "nested.css";', "text/css"),
        "https://example.com/nested.css": (200, b'@font-face{src:url("font.woff2")}', "text/css"),
        "https://example.com/font.woff2": (200, b"font", "font/woff2"),
        "https://example.com/small.png": (200, b"small", "image/png"),
        "https://example.com/large.png": (200, b"large", "image/png"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        status, content, content_type = responses[str(request.url)]
        return httpx.Response(status, content=content, headers={"content-type": content_type})

    result = capture_page("https://example.com/", tmp_path, transport=httpx.MockTransport(handler))

    assert calls.count("https://example.com/small.png") == 1
    assert set(result.asset_map) == set(responses) - {"https://example.com/"}
    assert "https://example.com/small.png" not in result.source_html.read_text("utf-8")


def test_capture_rejects_non_html_and_oversized_html(tmp_path: Path) -> None:
    non_html = mock_transport({"https://example.com/": (200, b"plain", "text/plain")})
    with pytest.raises(CaptureError, match="text/html"):
        capture_page("https://example.com/", tmp_path, transport=non_html)

    def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "content-length": str(10 * 1024 * 1024 + 1)},
            content=b"",
        )

    with pytest.raises(CaptureError, match="size limit"):
        capture_page("https://example.com/", tmp_path, transport=httpx.MockTransport(oversized))


def test_css_string_values_are_not_fetched_as_urls(tmp_path: Path) -> None:
    html = '<link rel="stylesheet" href="main.css">'
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/":
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        return httpx.Response(200, text='.label::before{content:"hello"}', headers={"content-type": "text/css"})

    capture_page("https://example.com/", tmp_path, transport=httpx.MockTransport(handler))

    assert requested == ["https://example.com/", "https://example.com/main.css"]


def test_missing_duplicate_asset_is_attempted_only_once(tmp_path: Path) -> None:
    html = '<img src="missing.png"><img src="missing.png">'
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/":
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        return httpx.Response(404, headers={"content-type": "image/png"})

    result = capture_page("https://example.com/", tmp_path, transport=httpx.MockTransport(handler))

    assert requested.count("https://example.com/missing.png") == 1
    assert result.missing_optional_assets == ["https://example.com/missing.png"]


def test_redirected_asset_final_url_is_not_downloaded_twice(tmp_path: Path) -> None:
    html = '<img src="alias.png"><img src="real.png">'
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/":
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        if request.url.path == "/alias.png":
            return httpx.Response(302, headers={"location": "/real.png"})
        return httpx.Response(200, content=b"image", headers={"content-type": "image/png"})

    result = capture_page("https://example.com/", tmp_path, transport=httpx.MockTransport(handler))

    assert requested.count("https://example.com/real.png") == 1
    assert result.asset_map["https://example.com/alias.png"] == result.asset_map["https://example.com/real.png"]


def test_css_import_depth_is_rejected_before_the_next_network_request(tmp_path: Path) -> None:
    html = '<link rel="stylesheet" href="level-0.css">'
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/":
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        level = int(request.url.path.removeprefix("/level-").removesuffix(".css"))
        return httpx.Response(
            200,
            text=f'@import "level-{level + 1}.css";',
            headers={"content-type": "text/css"},
        )

    with pytest.raises(CaptureError, match="import depth"):
        capture_page("https://example.com/", tmp_path, transport=httpx.MockTransport(handler))

    rejected = f"https://example.com/level-{MAX_CSS_IMPORT_DEPTH + 1}.css"
    assert rejected not in requested


def test_absolute_same_page_anchor_is_rewritten_to_local_fragment(tmp_path: Path) -> None:
    html = '<a href="/docs/#details">Details</a><a href="other#details">Other</a>'
    transport = mock_transport(
        {"https://example.com/docs/": (200, html.encode(), "text/html")}
    )

    result = capture_page("https://example.com/docs/", tmp_path, transport=transport)

    rendered = result.source_html.read_text("utf-8")
    assert 'href="#details"' in rendered
    assert 'href="https://example.com/docs/other#details"' in rendered


def test_dns_rebinding_is_rejected_by_connection_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolutions = 0

    def resolve(host: str, port: int, *args: object, **kwargs: object) -> list[tuple[object, ...]]:
        nonlocal resolutions
        resolutions += 1
        address = "93.184.216.34" if resolutions == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    def reject_connection(*args: object, **kwargs: object) -> object:
        pytest.fail("connection attempted after DNS rebound to a private address")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "create_connection", reject_connection)

    with pytest.raises(CaptureError, match="non-public DNS"):
        capture_page("https://example.com/", tmp_path)
    assert resolutions == 2


def test_asset_already_captured_under_redirect_target_is_not_fetched_again(tmp_path: Path) -> None:
    html = '<img src="real.png"><img src="alias.png">'
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/":
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        if request.url.path == "/alias.png":
            return httpx.Response(302, headers={"location": "/real.png"})
        return httpx.Response(200, content=b"image", headers={"content-type": "image/png"})

    result = capture_page("https://example.com/", tmp_path, transport=httpx.MockTransport(handler))

    assert requested.count("https://example.com/real.png") == 1
    assert result.asset_map["https://example.com/alias.png"] == result.asset_map["https://example.com/real.png"]


def test_data_url_srcset_candidate_does_not_hide_later_network_candidate(tmp_path: Path) -> None:
    html = '<img srcset="data:image/svg+xml,%3Csvg/%3E 1x, large.png 2x">'
    transport = mock_transport(
        {
            "https://example.com/": (200, html.encode(), "text/html"),
            "https://example.com/large.png": (200, b"large", "image/png"),
        }
    )

    result = capture_page("https://example.com/", tmp_path, transport=transport)

    rendered = result.source_html.read_text("utf-8")
    assert "data:image/svg+xml,%3Csvg/%3E 1x" in rendered
    assert "large.png" not in rendered
    assert "https://example.com/large.png" in result.asset_map
