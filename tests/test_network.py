from __future__ import annotations

import socket

import httpx
import pytest

from web_translator.network import (
    NetworkBudget,
    NetworkError,
    build_public_client,
    fetch_limited,
)


def test_public_client_blocks_private_dns_before_mocked_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []
    transport = httpx.MockTransport(
        lambda request: requested.append(str(request.url))
        or httpx.Response(200, content=b"%PDF-1.7\n")
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    budget = NetworkBudget(max_bytes=1024, max_redirects=5, deadline_seconds=10.0)
    with build_public_client(budget=budget, transport=transport) as client:
        with pytest.raises(NetworkError, match="non-public DNS"):
            fetch_limited(client, "https://example.com/report.pdf", 1024, "PDF")
    assert requested == []


def test_redirect_bodies_count_toward_network_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, content=b"redirect", headers={"location": "/final"})
        return httpx.Response(200, content=b"final")

    budget = NetworkBudget(max_bytes=len(b"redirectfinal") - 1, max_redirects=5, deadline_seconds=10.0)
    with build_public_client(budget=budget, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(NetworkError, match="downloaded bytes"):
            fetch_limited(client, "https://example.com/start", 1024, "PDF")


def test_public_client_ignores_environment_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    budget = NetworkBudget(max_bytes=1024, max_redirects=5, deadline_seconds=10.0)
    with build_public_client(
        budget=budget,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"PDF")),
    ) as client:
        assert client._trust_env is False


def test_public_client_rejects_non_mock_transport() -> None:
    transport = httpx.HTTPTransport()
    try:
        budget = NetworkBudget(max_bytes=1024, max_redirects=5, deadline_seconds=10.0)
        with pytest.raises(NetworkError, match="MockTransport"):
            build_public_client(budget=budget, transport=transport)
    finally:
        transport.close()


def test_network_budget_does_not_allow_dns_resolver_substitution() -> None:
    with pytest.raises(TypeError, match="resolve_public_addresses"):
        NetworkBudget(
            max_bytes=1024,
            max_redirects=5,
            deadline_seconds=10.0,
            resolve_public_addresses=lambda *_: ["127.0.0.1"],
        )
