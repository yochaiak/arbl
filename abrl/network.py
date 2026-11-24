"""Origin validation and the Chromium worker's local egress guard."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import ssl
from collections.abc import Callable, Sequence
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from time import monotonic
from urllib.parse import urlsplit

import httpx

from .config import API_ORIGIN
from .lifecycle import public_tunnel_wait
from .proxy import MAX_PROXY_CONNECTIONS, ProxyBinding, classify_proxy_failure

FIXTURE_ORIGIN = API_ORIGIN
FIXTURE_PORT = urlsplit(FIXTURE_ORIGIN).port
PROXY_DESTINATION_DENIED = "proxy_destination_denied"
PROXY_TRANSPORT_REASONS = (
    "target_dns_unavailable",
    "proxy_gateway_dns_unavailable",
    "proxy_gateway_dns_denied",
    "proxy_gateway_timeout",
    "proxy_gateway_tls_failed",
    "proxy_tunnel_timeout",
    "proxy_tunnel_failed",
    "proxy_response_invalid",
    PROXY_DESTINATION_DENIED,
)
ConnectReport = Callable[[dict], None]
ConnectObserver = Callable[[str], ConnectReport | None]
PROXY_CHECK_TIMEOUT_SECONDS = 5
DECODO_PROXY_CHECK_TARGET = ("https://ip.decodo.com", "/json")
_PROXY_CHECK_TARGETS = {
    "oxylabs": ("https://ip.oxylabs.io", "/location"),
    "decodo": DECODO_PROXY_CHECK_TARGET,
}
_PROXY_CHECK_BODY_LIMIT = 8192
_DIAGNOSTIC_FIXTURE_PATHS = frozenset({"/lab/diagnostics", "/lab/diagnostics/echo"})
FIXTURE_PATHS = frozenset(
    {
        "/lab",
        "/lab/",
        "/lab/interaction",
        "/lab/form",
        "/lab/next",
        "/lab/finish",
        "/lab/repeat",
        "/lab/research",
        *_DIAGNOSTIC_FIXTURE_PATHS,
    }
)
_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_DIAGNOSTIC_FIXTURE_HEADERS = frozenset(
    {
        b"accept-language",
        b"sec-ch-ua",
        b"sec-ch-ua-mobile",
        b"sec-ch-ua-platform",
        b"sec-ch-ua-full-version-list",
        b"sec-ch-ua-platform-version",
    }
)


@contextmanager
def _timed_phase(timings: dict[str, int | float] | None, name: str):
    if timings is None:
        yield
        return
    started = monotonic()
    try:
        yield
    finally:
        timings[name] = round((monotonic() - started) * 1000)


class NetworkDenied(ValueError):
    """A URL, address, or proxy request violated the registered scope."""


class TemporaryDNSFailure(NetworkDenied):
    """A public destination could not be resolved due to a transient DNS error."""


def _transport_error(reason: str, *, retryable: bool = True) -> dict:
    event = {**classify_proxy_failure(None), "status": None, "reason": reason}
    if not retryable:
        event.update(action="pause_assignment", retryable=False)
    return event


def _report_connect(report: ConnectReport | None, source: str, reason: str, status: int | None = None) -> None:
    if report is not None:
        evidence: dict[str, str | int] = {"source": source, "reason": reason}
        if status is not None:
            evidence["http_status"] = status
        # A diagnostic subscriber must not affect the tunnel or its response.
        with suppress(Exception):
            report(evidence)


async def _close_tunnel(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=0.2)
    except TimeoutError:
        writer.transport.abort()
    except asyncio.CancelledError:
        writer.transport.abort()
        raise
    except OSError:
        pass


def first_proxy_tunnel_failure(metadata: dict, failure: dict | None, elapsed_ms: int | None) -> dict | None:
    """Prefer broker evidence only when the first target tunnel could not be established."""
    transport = metadata.get("transport_summary")
    issues = metadata.get("transport_events")
    if (
        not isinstance(transport, dict) or not isinstance(issues, list)
        or type(transport.get("attempts")) is not int or transport["attempts"] <= 0
        or type(transport.get("successes")) is not int or transport["successes"] != 0
        or (failure and failure.get("reason") not in (
            "navigation_failed", "navigation_timeout", "timeout", "browser_error", "main_document_http_error",
        ))
    ):
        return None
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        reason = issue.get("reason") or issue.get("kind")
        if reason in (*PROXY_TRANSPORT_REASONS, "authentication", "quota"):
            detail = {**(failure or {}), "phase": "proxy", "reason": reason}
            detail.setdefault("elapsed_ms", elapsed_ms)
            if type(status := issue.get("status")) is int and 100 <= status <= 599:
                detail["status"] = status
            if (tunnel_wait := public_tunnel_wait(issue.get("tunnel_wait"))) is not None:
                detail["tunnel_wait"] = tunnel_wait
            return detail
    return None


class ConnectionCapacityExceeded(ValueError):
    """A session's reserved target connection slots are occupied."""


def _public_ipv4(address: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def _url_parts(url: str):
    if not url or any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise NetworkDenied("Invalid URL")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise NetworkDenied("Invalid URL") from exc
    if not host or parts.username is not None or parts.password is not None:
        raise NetworkDenied("URL must identify an origin without credentials")
    return parts, host, port


def request_origin(url: str) -> str:
    """Return an exact canonical allowed origin, independent of its path."""
    parts, host, port = _url_parts(url)
    if (parts.scheme, host, port) == ("http", "127.0.0.1", FIXTURE_PORT):
        return FIXTURE_ORIGIN
    if parts.scheme != "https" or port not in {None, 443}:
        raise NetworkDenied("External origins require HTTPS on port 443")
    # urlsplit strips brackets from IPvFuture literals, whose interior can resemble an allowed DNS name.
    if parts.netloc.startswith("[") or not (_DOMAIN.fullmatch(host) or _public_ipv4(host)):
        raise NetworkDenied("Origin must use a public IPv4 address or DNS hostname")
    return f"https://{host}"


def normalize_origin(url: str) -> str:
    parts, _, _ = _url_parts(url)
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise NetworkDenied("Enter an origin without a path, query, or fragment")
    return request_origin(url)


def is_fixture_path(url: str) -> bool:
    return urlsplit(url).path in FIXTURE_PATHS


async def resolve_public_ipv4(host: str, port: int = 443) -> tuple[str, ...]:
    """Resolve once, validate all answers, and return literal addresses for dialing."""
    loop = asyncio.get_running_loop()
    try:
        addresses = await asyncio.wait_for(
            loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM),
            timeout=5,
        )
    except TimeoutError as exc:
        raise TemporaryDNSFailure("Temporary failure resolving the registered origin") from exc
    except OSError as exc:
        if isinstance(exc, socket.gaierror) and exc.errno == socket.EAI_AGAIN:
            raise TemporaryDNSFailure("Temporary failure resolving the registered origin") from exc
        raise NetworkDenied("No usable IPv4 DNS answer for the registered origin") from exc
    ips = tuple(dict.fromkeys(entry[4][0] for entry in addresses))
    if not ips or any(not _public_ipv4(ip) for ip in ips):
        raise NetworkDenied("Registered origin resolves to a non-public address")
    return ips


class EgressGuard:
    """Tunnel registered HTTPS destinations and serve a separate local fixture route.

    HTTPS remains encrypted between Chromium and the target. Direct tunnels
    dial a validated IPv4 address. Upstream providers independently resolve the
    target hostname.
    """

    def __init__(
        self,
        origin: str,
        resource_origins: list[str],
        deny_target: Callable[[str], None],
        upstream: ProxyBinding | None = None,
        bind_host: str = "127.0.0.1",
        bind_port: int = 0,
        navigation_origins: list[str] | None = None,
        traffic_budget_bytes: int = 104857600,
        max_connections: int = 32,
        vendor_control_origins: list[str] | None = None,
        allow_automatic_assets: bool = True,
        startup_origins: Sequence[str] = (),
        diagnostic_timings: dict[str, int | float] | None = None,
        observe_connect: ConnectObserver | None = None,
    ) -> None:
        self.origin = normalize_origin(origin)
        self.resources = {normalize_origin(item) for item in resource_origins}
        self.navigation_origins = {self.origin} | {
            normalize_origin(item) for item in navigation_origins or []
        }
        if self.origin != FIXTURE_ORIGIN and FIXTURE_ORIGIN in self.resources | self.navigation_origins:
            raise NetworkDenied("A remote target cannot access the local fixture")
        self.vendor_controls = {normalize_origin(item) for item in vendor_control_origins or []}
        self.allowed = self.resources | self.navigation_origins | self.vendor_controls
        self._startup_origins = {normalize_origin(item) for item in startup_origins} - self.allowed
        self._allow_automatic_assets = allow_automatic_assets
        self._asset_origins: set[str] = set()
        self.deny_target = deny_target
        self.upstream = upstream
        if upstream is not None and self.origin == FIXTURE_ORIGIN:
            raise NetworkDenied(
                "Provider proxy trials require an authorized HTTPS target, not the local HTTP fixture"
            )
        self.transport_events: list[dict] = []
        self._diagnostic_timings = diagnostic_timings
        self._observe_connect = observe_connect
        self.bind_host, self.bind_port = bind_host, bind_port
        self.transport_attempts = self.transport_successes = 0
        self.transport_latency_ms = 0.0
        self._connections: set[asyncio.Task] = set()
        self._closing = False
        self.forwarding_enabled = True
        if type(traffic_budget_bytes) is not int or not 1024 <= traffic_budget_bytes <= 2 * 1024**3:
            raise ValueError("Traffic budget must be an integer from 1024 bytes to 2 GiB")
        self.traffic_budget_bytes = traffic_budget_bytes
        self.upstream_bytes = self.downstream_bytes = self.vendor_control_bytes = 0
        self.budget_exhausted = False
        self._transfer_writers: set[asyncio.StreamWriter] = set()
        if type(max_connections) is not int or not 1 <= max_connections <= MAX_PROXY_CONNECTIONS:
            raise ValueError(f"Target connection limit must be an integer from 1 to {MAX_PROXY_CONNECTIONS}")
        self.max_connections = max_connections
        # Leave room for vendor control and for the next client to receive a capacity response.
        self._client_connection_limit = max(32, max_connections + len(self.vendor_controls) + 1)
        self.active_target_connections = self.peak_target_connections = self.connection_rejections = 0

    async def __aenter__(self) -> EgressGuard:
        self._server = await asyncio.start_server(
            self._handle, host=self.bind_host, port=self.bind_port, limit=16384
        )
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._closing = True
        self._server.close()
        active = tuple(self._connections)
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._connections.add(task)
        report = None
        try:
            if self._closing:
                return
            if len(self._connections) > self._client_connection_limit:
                await self._reply(writer, b"503 Service Unavailable")
                return
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            first, *headers = head[:-4].split(b"\r\n")
            method, address, version = first.decode("ascii").split()
            if method == "CONNECT" and self._observe_connect is not None:
                with suppress(Exception):
                    report = self._observe_connect(normalize_origin("https://" + address))
            if version not in {"HTTP/1.0", "HTTP/1.1"}:
                _report_connect(report, "local_proxy", "local_invalid_request", 403)
                raise NetworkDenied("Unsupported HTTP version")
            if not await self.authorize_request(method, address, headers, writer):
                return
            if self.budget_exhausted or not self.forwarding_allowed(method, address):
                reason = "local_budget_exhausted" if self.budget_exhausted else "local_forwarding_disabled"
                _report_connect(report, "local_proxy", reason, 503)
                await self._reply(writer, b"503 Service Unavailable")
                return
            if method == "CONNECT":
                origin = normalize_origin("https://" + address)
                with self._connection_slot(vendor_control=self.is_vendor_control(origin)):
                    await self._connect(reader, writer, address, report=report)
            elif self.origin == FIXTURE_ORIGIN and request_origin(address) == FIXTURE_ORIGIN:
                with self._connection_slot():
                    await self._forward_fixture(reader, writer, method, address, headers)
            else:
                raise NetworkDenied("External destinations require an HTTPS CONNECT tunnel")
        except ConnectionCapacityExceeded:
            _report_connect(report, "local_proxy", "local_capacity", 503)
            await self._reply(writer, b"503 Service Unavailable")
        except NetworkDenied:
            await self._reply(writer, b"403 Forbidden")
        except (UnicodeError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            _report_connect(report, "local_proxy", "local_invalid_request", 400)
            await self._reply(writer, b"400 Bad Request")
        except (TimeoutError, OSError):
            pass
        finally:
            writer.close()
            self._connections.discard(task)
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def authorize_request(self, method, address, headers, writer) -> bool:
        """Remote broker overrides this hook; local loopback guard needs no credentials."""
        return True

    def forwarding_allowed(self, method, address):
        if self.forwarding_enabled:
            return True
        if method != "CONNECT":
            return False
        try:
            origin = normalize_origin("https://" + address)
            return self.is_vendor_control(origin) or origin in self._startup_origins
        except NetworkDenied:
            return False

    def activate_forwarding(self) -> None:
        self._startup_origins.clear()
        self.forwarding_enabled = True

    def stop_forwarding(self) -> None:
        self.forwarding_enabled = False
        self._startup_origins.clear()
        for task in tuple(self._connections):
            task.cancel()

    def uses_upstream(self, origin):
        return self.upstream is not None and origin not in self.vendor_controls

    def is_vendor_control(self, origin):
        return origin in self.vendor_controls

    def allow_asset_origin(self, origin: str) -> bool:
        """Permit a page asset tunnel for the lifetime of this active session."""
        origin = normalize_origin(origin)
        if not origin.startswith("https://"):
            raise NetworkDenied("Page assets require a public HTTPS origin")
        if (
            not self._allow_automatic_assets
            or not self.forwarding_enabled
            or self._closing
            or self.budget_exhausted
            or self.is_vendor_control(origin)
        ):
            return False
        if origin not in self.allowed:
            self._asset_origins.add(origin)
        return True

    @contextmanager
    def _connection_slot(self, *, vendor_control=False):
        if vendor_control:
            yield
            return
        if self.active_target_connections >= self.max_connections:
            self.connection_rejections += 1
            if not any(event.get("kind") == "connection_capacity" for event in self.transport_events):
                self.transport_events.append(
                    {"kind": "connection_capacity", "action": "pause_assignment", "retryable": False}
                )
            raise ConnectionCapacityExceeded("Reserved proxy connection capacity is occupied")
        self.active_target_connections += 1
        self.peak_target_connections = max(self.peak_target_connections, self.active_target_connections)
        try:
            yield
        finally:
            self.active_target_connections -= 1

    def transport_usage(self):
        total = self.upstream_bytes + self.downstream_bytes
        return {
            "upstream_bytes": self.upstream_bytes,
            "downstream_bytes": self.downstream_bytes,
            "total_bytes": total,
            "vendor_control_bytes": self.vendor_control_bytes,
            "budget_consumed_bytes": total + self.vendor_control_bytes,
            "limit_bytes": self.traffic_budget_bytes,
            "budget_exhausted": self.budget_exhausted,
            "measurement": "browser_payload_transport_bytes",
            "billing_basis": "estimate_excludes_protocol_overhead",
        }

    def _charge_bytes(self, count: int, *, upstream: bool, vendor_control: bool = False) -> bool:
        # No await between reservation and update: all tunnels share one atomic
        # event-loop budget. Provider handshakes and broker control are excluded.
        used = self.upstream_bytes + self.downstream_bytes + self.vendor_control_bytes
        if self.budget_exhausted or used + count > self.traffic_budget_bytes:
            self.budget_exhausted = True
            self.forwarding_enabled = False
            self.deny_target("Browser traffic byte budget exhausted")
            for writer in tuple(self._transfer_writers):
                writer.close()
            return False
        if vendor_control:
            self.vendor_control_bytes += count
        elif upstream:
            self.upstream_bytes += count
        else:
            self.downstream_bytes += count
        return True

    async def _metered_pipe(self, reader, writer, *, upstream: bool, vendor_control: bool = False):
        self._transfer_writers.add(writer)
        try:
            while chunk := await reader.read(65536):
                if not self._charge_bytes(len(chunk), upstream=upstream, vendor_control=vendor_control):
                    return
                writer.write(chunk)
                await writer.drain()
        finally:
            self._transfer_writers.discard(writer)

    def transport_summary(self):
        return {
            "attempts": self.transport_attempts,
            "successes": self.transport_successes,
            "failures": self.transport_attempts - self.transport_successes,
            "latency_ms": self.transport_latency_ms / self.transport_successes
            if self.transport_successes
            else None,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "connection_limit": self.max_connections,
            "active_connections": self.active_target_connections,
            "peak_connections": self.peak_target_connections,
            "capacity_rejections": self.connection_rejections,
        }

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: bytes) -> None:
        try:
            writer.write(b"HTTP/1.1 " + status + b"\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except OSError:
            pass

    async def _connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        address: str,
        *,
        report: ConnectReport | None = None,
    ) -> None:
        url = f"https://{address}"
        parts, host, port = _url_parts(url)
        origin = normalize_origin(url)
        learned_asset = origin in self._asset_origins
        startup = not self.forwarding_enabled and origin in self._startup_origins
        if parts.path or port != 443 or (origin not in self.allowed and not learned_asset and not startup):
            _report_connect(report, "local_proxy", "local_scope_denied", 403)
            raise NetworkDenied("Destination outside the registered origins")
        use_upstream = self.uses_upstream(origin)
        measured = not (startup or self.is_vendor_control(origin))
        dns_started = monotonic()
        try:
            with _timed_phase(self._diagnostic_timings, "target_dns"):
                ips = await resolve_public_ipv4(host, port)
        except TemporaryDNSFailure:
            _report_connect(report, "local_proxy", "target_dns_unavailable", 502)
            if measured:
                self.transport_attempts += 1
                self.transport_events.append({
                    **_transport_error("target_dns_unavailable"),
                    "tunnel_wait": {
                        "stage": "target_dns", "stage_elapsed_ms": round((monotonic() - dns_started) * 1000),
                    },
                })
            await self._reply(writer, b"502 Bad Gateway")
            return
        except NetworkDenied as exc:
            _report_connect(report, "local_proxy", "local_dns_denied", 403)
            if origin == self.origin:
                self.deny_target(str(exc))
            raise
        started = monotonic()
        if measured:
            self.transport_attempts += 1
        # Only pre-registered origins use the provider's DNS. New page asset
        # tunnels use a validated address even when routed through the provider.
        upstream = None
        if use_upstream:
            authority = f"{ips[0] if learned_asset else host}:{port}"
            options = {"report": report} if report is not None else {}
            if startup and not learned_asset:
                options["record_failure"] = False
            upstream = await self._upstream_connect(authority, **options)
        else:
            for ip in ips[:3]:
                try:
                    upstream = await asyncio.wait_for(
                        asyncio.open_connection(ip, port, family=socket.AF_INET), timeout=4
                    )
                    break
                except (OSError, TimeoutError):
                    continue
        if upstream is None:
            if not use_upstream:
                _report_connect(report, "local_proxy", "local_direct_connect_failed", 502)
            await self._reply(writer, b"502 Bad Gateway")
            return
        if not use_upstream:
            _report_connect(report, "local_proxy", "connect_established", 200)
        if measured:
            self.transport_successes += 1
            latency_ms = (monotonic() - started) * 1000
            self.transport_latency_ms += latency_ms
            if self._diagnostic_timings is not None:
                self._diagnostic_timings["tunnel_establish"] = round(latency_ms, 2)
        source, sink = upstream
        transfers = []
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            transfers = [
                asyncio.create_task(
                    self._metered_pipe(
                        reader, sink, upstream=True, vendor_control=self.is_vendor_control(origin)
                    )
                ),
                asyncio.create_task(
                    self._metered_pipe(
                        source, writer, upstream=False, vendor_control=self.is_vendor_control(origin)
                    )
                ),
            ]
            await asyncio.wait(transfers, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for transfer in transfers:
                transfer.cancel()
            await asyncio.gather(*transfers, return_exceptions=True)
            if self._closing:
                sink.transport.abort()
            else:
                await _close_tunnel(sink)

    async def _upstream_connect(self, address: str, *, record_failure: bool = True, report: ConnectReport | None = None):
        """Authenticate to the provider over TLS; the browser never sees its credentials.

        Target DNS was checked locally, but the provider resolves the hostname
        again. The explicitly acknowledged boundary is reported in metadata.
        """
        binding = self.upstream
        assert binding is not None
        provider_source = "decodo" if getattr(binding, "provider", None) == "decodo" else "upstream_proxy"
        sink = None
        stage = "gateway_dns"
        stage_started = monotonic()
        try:
            with _timed_phase(self._diagnostic_timings, "gateway_dns"):
                gateway_ips = await resolve_public_ipv4(binding.host, binding.port)
            stage = "gateway_tls_connect"
            stage_started = monotonic()
            with _timed_phase(self._diagnostic_timings, "gateway_tls_connect"):
                source, sink = await asyncio.wait_for(
                    asyncio.open_connection(
                        gateway_ips[0],
                        binding.port,
                        family=socket.AF_INET,
                        ssl=ssl.create_default_context(),
                        server_hostname=binding.host,
                    ),
                    10,
                )
            request = (
                f"CONNECT {address} HTTP/1.1\r\nHost: {address}\r\n".encode()
                + b"Proxy-Authorization: "
                + binding.authorization()
                + b"\r\n\r\n"
            )
            stage = "proxy_connect"
            stage_started = monotonic()
            with _timed_phase(self._diagnostic_timings, "proxy_connect"):
                async with asyncio.timeout(15):
                    sink.write(request)
                    await sink.drain()
                    head = await source.readuntil(b"\r\n\r\n")
            if len(head) > 16384:
                raise ValueError("Proxy response exceeded header budget")
            lines = head.split(b"\r\n")
            status_line = lines[0].split(maxsplit=2)
            if (
                len(status_line) < 2 or status_line[0] not in {b"HTTP/1.0", b"HTTP/1.1"}
                or not re.fullmatch(rb"[1-5][0-9]{2}", status_line[1])
            ):
                raise ValueError("Invalid proxy response status")
            status = int(status_line[1])
            if status == 200:
                _report_connect(report, provider_source, "connect_established", status)
                return source, sink
            description = next(
                (
                    line.partition(b":")[2].decode("utf8", "replace")
                    for line in lines
                    if line.lower().startswith(b"x-error-description:")
                ),
                "",
            )
            event = classify_proxy_failure(status, description)
            # Store only the normalized reason; vendor responses can contain identifiers.
            if status == 403 and event["kind"] == "upstream_rejected":
                event["reason"] = PROXY_DESTINATION_DENIED
            elif event["kind"] in {"transport", "upstream_rejected"}:
                event["reason"] = "proxy_tunnel_failed"
            event["status"] = status
        except TemporaryDNSFailure:
            event = _transport_error("proxy_gateway_dns_unavailable")
        except NetworkDenied:
            event = _transport_error("proxy_gateway_dns_denied", retryable=False)
        except TimeoutError:
            reason = "proxy_gateway_timeout" if sink is None else "proxy_tunnel_timeout"
            event = {
                **_transport_error(reason),
                "tunnel_wait": {"stage": stage, "stage_elapsed_ms": round((monotonic() - stage_started) * 1000)},
            }
        except ssl.SSLEOFError:
            event = _transport_error("proxy_tunnel_failed")
        except ssl.SSLError:
            reason = "proxy_gateway_tls_failed" if sink is None else "proxy_tunnel_failed"
            event = _transport_error(reason, retryable=False)
        except (ValueError, asyncio.LimitOverrunError):
            event = _transport_error("proxy_response_invalid", retryable=False)
        except (OSError, asyncio.IncompleteReadError):
            event = _transport_error("proxy_tunnel_failed")
        except BaseException:
            if sink is not None:
                sink.transport.abort()
            raise
        upstream_status = event["status"]
        _report_connect(
            report, provider_source if upstream_status is not None else "upstream_connection",
            event.get("reason") or event["kind"], upstream_status,
        )
        if record_failure:
            self.transport_events.append(event)
        if sink is not None:
            await _close_tunnel(sink)
        return None

    async def _forward_fixture(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        address: str,
        headers: list[bytes],
    ) -> None:
        payload = await _fixture_request(reader, method, address, headers)
        source, sink = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", FIXTURE_PORT), 3)
        try:
            if not self._charge_bytes(len(payload), upstream=True):
                return
            sink.write(payload)
            await sink.drain()
            await self._metered_pipe(source, writer, upstream=False)
        finally:
            sink.close()
            try:
                await sink.wait_closed()
            except OSError:
                pass

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()


async def _fixture_request(
    reader: asyncio.StreamReader, method: str, address: str, headers: list[bytes]
) -> bytes:
    if method not in {"GET", "HEAD", "POST"} or not is_fixture_path(address):
        raise NetworkDenied("Local connection outside the lab fixture")
    parts = urlsplit(address)
    diagnostic = parts.path in _DIAGNOSTIC_FIXTURE_PATHS
    content_length = None
    forwarded = []
    for header in headers:
        name, separator, value = header.partition(b":")
        if (
            not separator
            or not re.fullmatch(rb"[A-Za-z0-9!#$%&'*+.^_`|~-]+", name)
            or re.search(rb"[\x00-\x08\x0a-\x1f\x7f]", value)
        ):
            raise NetworkDenied("Invalid HTTP header")
        lower_name = name.lower()
        if lower_name in {b"transfer-encoding", b"upgrade", b"expect"}:
            raise NetworkDenied("Unsupported fixture request encoding")
        if lower_name == b"content-length":
            if content_length is not None or not value.strip().isdigit():
                raise NetworkDenied("Invalid fixture request body")
            content_length = int(value)
            if content_length > 65536:
                raise NetworkDenied("Fixture request body exceeds 64 KiB")
        if lower_name in {b"content-type", b"cookie", b"user-agent", b"accept", b"origin", b"referer"} or (
            diagnostic and lower_name in _DIAGNOSTIC_FIXTURE_HEADERS
        ):
            forwarded.append(header)
    body = await asyncio.wait_for(reader.readexactly(content_length or 0), timeout=5)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    lines = [
        f"{method} {path} HTTP/1.1".encode("ascii"),
        f"Host: 127.0.0.1:{FIXTURE_PORT}".encode(),
        b"Connection: close",
        f"Content-Length: {len(body)}".encode(),
        *forwarded,
    ]
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


def normalize_proxy_check_ip(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 45 or "%" in value:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if not address.is_global or address.is_multicast or address.is_reserved:
        return None
    return str(address)


async def check_proxy_ip(binding: ProxyBinding) -> str | None:
    """Measure one provider diagnostic request with isolated transport accounting."""
    origin, path = _PROXY_CHECK_TARGETS[binding.provider]
    try:
        async with asyncio.timeout(PROXY_CHECK_TIMEOUT_SECONDS):
            async with EgressGuard(
                origin, [], lambda _: None, upstream=binding,
                traffic_budget_bytes=32_768, max_connections=1,
            ) as guard:
                async with httpx.AsyncClient(proxy=guard.url, trust_env=False, follow_redirects=False) as client:
                    async with client.stream(
                        "GET", origin + path,
                        headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                    ) as response:
                        if response.status_code != 200:
                            return None
                        body = bytearray()
                        async for chunk in response.aiter_raw():
                            if len(body) + len(chunk) > _PROXY_CHECK_BODY_LIMIT:
                                return None
                            body.extend(chunk)
            payload = json.loads(body)
            if binding.provider == "decodo" and isinstance(payload, dict):
                payload = payload.get("proxy")
            return normalize_proxy_check_ip(payload.get("ip")) if isinstance(payload, dict) else None
    except (httpx.HTTPError, OSError, TimeoutError, ValueError):
        return None


check_oxylabs_ip = check_proxy_ip
