"""Bounded, SSRF-safe HTTP downloads for public URLs."""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable

import httpcore
import httpx

from web_translator.paths import validate_public_url


USER_AGENT = "web-translator/0.1 (+https://github.com/starryeye/web-translator)"


class NetworkError(RuntimeError):
    """A public HTTP download could not be completed safely."""


class NetworkBudget:
    """Whole-download limits shared by requests and redirect response bodies."""

    def __init__(
        self,
        *,
        max_bytes: int,
        max_redirects: int,
        deadline_seconds: float,
        max_total_redirects: int | None = None,
        error_prefix: str = "network resource budget exceeded",
        resolve_public_addresses: Callable[[str, int], list[str]] | None = None,
    ) -> None:
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.max_total_redirects = (
            max_redirects if max_total_redirects is None else max_total_redirects
        )
        self.deadline = time.monotonic() + deadline_seconds
        self.downloaded_bytes = 0
        self.redirects = 0
        self.error_prefix = error_prefix
        self.resolve_public_addresses = (
            _resolve_public_addresses
            if resolve_public_addresses is None
            else resolve_public_addresses
        )

    def check_deadline(self) -> None:
        if time.monotonic() > self.deadline:
            raise NetworkError(f"{self.error_prefix}: deadline")

    def before_request(self, _request: httpx.Request) -> None:
        self.check_deadline()

    def after_response(self, response: httpx.Response) -> None:
        self.check_deadline()
        if response.has_redirect_location:
            if self.redirects >= self.max_total_redirects:
                raise NetworkError(f"{self.error_prefix}: redirects")
            self.redirects += 1
            if response.is_stream_consumed:
                self.add_downloaded(len(response.content))
            else:
                response.stream = _BudgetedResponseStream(response.stream, self)

    def add_downloaded(self, size: int) -> None:
        self.check_deadline()
        if self.downloaded_bytes + size > self.max_bytes:
            raise NetworkError(f"{self.error_prefix}: downloaded bytes")
        self.downloaded_bytes += size

    def request_timeout(self) -> float:
        self.check_deadline()
        remaining = self.deadline - time.monotonic()
        return min(30.0, max(0.001, remaining))


class _BudgetedResponseStream(httpx.SyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream, budget: NetworkBudget) -> None:
        self.stream = stream
        self.budget = budget

    def __iter__(self):
        for chunk in self.stream:
            self.budget.add_downloaded(len(chunk))
            yield chunk

    def close(self) -> None:
        self.stream.close()


def build_public_client(
    *,
    budget: NetworkBudget,
    transport: httpx.BaseTransport | None = None,
    user_agent: str = USER_AGENT,
) -> httpx.Client:
    if transport is not None and type(transport) is not httpx.MockTransport:
        raise NetworkError(
            "transport injection accepts only non-network httpx.MockTransport instances"
        )
    client = httpx.Client(
        follow_redirects=True,
        max_redirects=budget.max_redirects,
        timeout=30.0,
        transport=(
            transport
            if transport is not None
            else PinnedHTTPTransport(budget.resolve_public_addresses)
        ),
        trust_env=False,
        headers={"user-agent": user_agent, "accept-encoding": "identity"},
        event_hooks={
            "request": [
                lambda request: validate_network_boundary(
                    request, budget.resolve_public_addresses
                ),
                budget.before_request,
            ],
            "response": [budget.after_response],
        },
    )
    client._web_translator_network_budget = budget  # type: ignore[attr-defined]
    return client


def fetch_limited(
    client: httpx.Client,
    url: str,
    limit: int,
    label: str,
) -> tuple[httpx.Response, bytes]:
    """Fetch one public URL, enforcing its response and cumulative byte limits."""
    budget = _client_budget(client)
    try:
        with client.stream("GET", url, timeout=budget.request_timeout()) as response:
            response.raise_for_status()
            return response, _read_limited(response, limit, label, budget)
    except NetworkError:
        raise
    except (httpx.HTTPError, OSError) as error:
        raise NetworkError(f"failed to fetch {label}: {error}") from error


def validate_network_boundary(
    request: httpx.Request,
    resolve_public_addresses: Callable[[str, int], list[str]] | None = None,
) -> None:
    """Resolve and reject unsafe addresses immediately before a request is handled."""
    try:
        url = validate_public_url(str(request.url))
    except ValueError as error:
        raise NetworkError(f"unsafe request URL: {request.url}") from error
    host = url.host
    assert host is not None
    port = url.port or (443 if url.scheme == "https" else 80)
    resolver = (
        _resolve_public_addresses
        if resolve_public_addresses is None
        else resolve_public_addresses
    )
    resolver(host, port)


def _resolve_public_addresses(host: str, port: int) -> list[str]:
    try:
        answers = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise NetworkError(f"DNS resolution failed for {host}: {error}") from error
    if not answers:
        raise NetworkError(f"DNS resolution returned no addresses for {host}")
    public_addresses: list[str] = []
    for answer in answers:
        raw_address = str(answer[4][0]).partition("%")[0]
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as error:
            raise NetworkError(f"DNS returned an invalid address for {host}") from error
        if not address.is_global or address.is_multicast:
            raise NetworkError(f"non-public DNS result for {host}: {address}")
        normalized = str(address)
        if normalized not in public_addresses:
            public_addresses.append(normalized)
    return public_addresses


class _PinnedNetworkBackend(httpcore.SyncBackend):
    """Resolve once per connection and connect only to the approved numeric IPs."""

    def __init__(self, resolve_public_addresses: Callable[[str, int], list[str]]) -> None:
        self.resolve_public_addresses = resolve_public_addresses

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore.NetworkStream:
        addresses = self.resolve_public_addresses(host, port)
        last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for address in addresses:
            try:
                return super().connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last_error = error
        assert last_error is not None
        raise last_error


class PinnedHTTPTransport(httpx.HTTPTransport):
    """HTTP transport whose connection pool cannot re-resolve approved hosts."""

    def __init__(
        self,
        resolve_public_addresses: Callable[[str, int], list[str]] = _resolve_public_addresses,
    ) -> None:
        _assert_transport_compatibility(httpx.__version__, httpcore.__version__)
        super().__init__(trust_env=False)
        if not hasattr(self._pool, "_network_backend"):
            raise RuntimeError(
                "httpx/httpcore transport adapter is incompatible: missing network backend seam"
            )
        self._pool._network_backend = _PinnedNetworkBackend(resolve_public_addresses)


def _assert_transport_compatibility(httpx_version: str, httpcore_version: str) -> None:
    if not ((0, 28) <= _major_minor(httpx_version) < (0, 29)):
        raise RuntimeError(f"unsupported httpx version for pinned transport: {httpx_version}")
    if not ((1, 0) <= _major_minor(httpcore_version) < (1, 1)):
        raise RuntimeError(f"unsupported httpcore version for pinned transport: {httpcore_version}")


def _major_minor(version: str) -> tuple[int, int]:
    try:
        major, minor, *_ = version.split(".")
        return int(major), int(minor)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"invalid dependency version: {version}") from error


def _client_budget(client: httpx.Client) -> NetworkBudget:
    try:
        return client._web_translator_network_budget  # type: ignore[attr-defined,no-any-return]
    except AttributeError as error:
        raise NetworkError("client was not created by build_public_client") from error


def _read_limited(
    response: httpx.Response,
    limit: int,
    label: str,
    budget: NetworkBudget,
) -> bytes:
    content_encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if content_encoding not in {"", "identity"}:
        raise NetworkError(f"{label} returned unsupported Content-Encoding: {content_encoding}")
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise NetworkError(f"{label} exceeds the {limit}-byte size limit")
        except ValueError:
            pass
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            raise NetworkError(f"{label} exceeds the {limit}-byte size limit")
        budget.add_downloaded(len(chunk))
        chunks.append(chunk)
    return b"".join(chunks)
