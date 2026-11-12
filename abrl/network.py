"""Origin validation and the Chromium worker's local egress guard."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from time import monotonic
from urllib.parse import urlsplit

from .config import API_ORIGIN
from .proxy import OxylabsBinding, classify_proxy_failure

FIXTURE_ORIGIN = API_ORIGIN
FIXTURE_PORT = urlsplit(FIXTURE_ORIGIN).port
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
    }
)
_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)


class NetworkDenied(ValueError):
    """A URL, address, or proxy request violated the registered scope."""


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
    if not (_DOMAIN.fullmatch(host) or _public_ipv4(host)):
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
    except (OSError, TimeoutError) as exc:
        raise NetworkDenied("No usable IPv4 DNS answer for the registered origin") from exc
    ips = tuple(dict.fromkeys(entry[4][0] for entry in addresses))
    if not ips or any(not _public_ipv4(ip) for ip in ips):
        raise NetworkDenied("Registered origin resolves to a non-public address")
    return ips


class EgressGuard:
    """Forward only registered HTTPS destinations and the exact fixture paths.

    HTTPS remains encrypted between Chromium and the target. DNS validation and
    connection use the same literal address. The proxy preserves the host IP.
    """

    def __init__(
        self,
        origin: str,
        resource_origins: list[str],
        deny_target: Callable[[str], None],
        upstream: OxylabsBinding | None = None,
        bind_host: str = "127.0.0.1",
        bind_port: int = 0,
        navigation_origins: list[str] | None = None,
        traffic_budget_bytes: int = 104857600,
        max_connections: int = 32,
    ) -> None:
        self.origin = normalize_origin(origin)
        self.resources = {normalize_origin(item) for item in resource_origins}
        self.navigation_origins = {self.origin} | {
            normalize_origin(item) for item in navigation_origins or []
        }
        if self.origin != FIXTURE_ORIGIN and FIXTURE_ORIGIN in self.resources | self.navigation_origins:
            raise NetworkDenied("A remote target cannot access the local fixture")
        self.allowed = self.resources | self.navigation_origins
        self.deny_target = deny_target
        self.upstream = upstream
        if upstream is not None and self.origin == FIXTURE_ORIGIN:
            raise NetworkDenied(
                "Provider proxy trials require an authorized HTTPS target, not the local HTTP fixture"
            )
        self.transport_events: list[dict] = []
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
        if type(max_connections) is not int or not 1 <= max_connections <= 32:
            raise ValueError("Target connection limit must be an integer from 1 to 32")
        self.max_connections = max_connections
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
        try:
            if self._closing:
                return
            if len(self._connections) > 32:
                await self._reply(writer, b"503 Service Unavailable")
                return
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            first, *headers = head[:-4].split(b"\r\n")
            method, address, version = first.decode("ascii").split()
            if version not in {"HTTP/1.0", "HTTP/1.1"}:
                raise NetworkDenied("Unsupported HTTP version")
            if not await self.authorize_request(method, address, headers, writer):
                return
            if self.budget_exhausted or not self.forwarding_allowed(method, address):
                await self._reply(writer, b"503 Service Unavailable")
                return
            origin = (
                normalize_origin("https://" + address) if method == "CONNECT" else request_origin(address)
            )
            with self._connection_slot(vendor_control=self.is_vendor_control(origin)):
                if method == "CONNECT":
                    await self._connect(reader, writer, address)
                else:
                    await self._fixture(reader, writer, method, address, headers)
        except ConnectionCapacityExceeded:
            await self._reply(writer, b"503 Service Unavailable")
        except NetworkDenied:
            await self._reply(writer, b"403 Forbidden")
        except (UnicodeError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
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
        return self.forwarding_enabled

    def uses_upstream(self, origin):
        return self.upstream is not None

    def is_vendor_control(self, origin):
        return False

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
    ) -> None:
        url = f"https://{address}"
        parts, host, port = _url_parts(url)
        origin = normalize_origin(url)
        if parts.path or port != 443 or origin not in self.allowed:
            raise NetworkDenied("Destination outside the registered origins")
        try:
            ips = await resolve_public_ipv4(host, port)
        except NetworkDenied as exc:
            if origin == self.origin:
                self.deny_target(str(exc))
            raise
        started = monotonic()
        use_upstream = self.uses_upstream(origin)
        measured = not self.is_vendor_control(origin)
        if measured:
            self.transport_attempts += 1
        upstream = await self._upstream_connect(address) if use_upstream else None
        if not use_upstream:
            for ip in ips[:3]:
                try:
                    upstream = await asyncio.wait_for(
                        asyncio.open_connection(ip, port, family=socket.AF_INET), timeout=4
                    )
                    break
                except (OSError, TimeoutError):
                    continue
        if upstream is None:
            await self._reply(writer, b"502 Bad Gateway")
            return
        if measured:
            self.transport_successes += 1
            self.transport_latency_ms += (monotonic() - started) * 1000
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
            sink.close()
            try:
                await sink.wait_closed()
            except OSError:
                pass

    async def _upstream_connect(self, address: str):
        """Authenticate to the provider over TLS; the browser never sees its credentials.

        Target DNS was checked locally, but the provider resolves the hostname
        again. The explicitly acknowledged boundary is reported in metadata.
        """
        binding = self.upstream
        assert binding is not None
        sink = None
        try:
            gateway_ips = await resolve_public_ipv4(binding.host, binding.port)
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
            sink.write(request)
            await sink.drain()
            head = await asyncio.wait_for(source.readuntil(b"\r\n\r\n"), 15)
            if len(head) > 16384:
                raise ValueError("Proxy response exceeded header budget")
            lines = head.split(b"\r\n")
            status = int(lines[0].split()[1])
            if status == 200:
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
            self.transport_events.append({**event, "status": status})
        except (OSError, TimeoutError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            self.transport_events.append({**classify_proxy_failure(None), "status": None})
        except BaseException:
            if sink is not None:
                sink.close()
            raise
        if sink is not None:
            sink.close()
            try:
                await sink.wait_closed()
            except OSError:
                pass
        return None

    async def _fixture(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        address: str,
        headers: list[bytes],
    ) -> None:
        if (
            self.origin != FIXTURE_ORIGIN
            or request_origin(address) != FIXTURE_ORIGIN
            or method not in {"GET", "HEAD", "POST"}
            or not is_fixture_path(address)
        ):
            raise NetworkDenied("Local connection outside the lab fixture")
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
            if lower_name in {b"content-type", b"cookie", b"user-agent", b"accept", b"origin"}:
                forwarded.append(header)
        body = await asyncio.wait_for(reader.readexactly(content_length or 0), timeout=5)
        parts = urlsplit(address)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        lines = [
            f"{method} {path} HTTP/1.1".encode("ascii"),
            f"Host: 127.0.0.1:{FIXTURE_PORT}".encode(),
            b"Connection: close",
            f"Content-Length: {len(body)}".encode(),
            *forwarded,
        ]
        payload = b"\r\n".join(lines) + b"\r\n\r\n" + body
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
