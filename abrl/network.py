"""Origin validation and the Chromium worker's local egress guard."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

from .config import API_ORIGIN

FIXTURE_ORIGIN = API_ORIGIN
FIXTURE_PORT = urlsplit(FIXTURE_ORIGIN).port
FIXTURE_PATHS = frozenset(
    {"/lab", "/lab/", "/lab/interaction", "/lab/form", "/lab/next", "/lab/finish", "/lab/repeat"}
)
_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)


class NetworkDenied(ValueError):
    """A URL, address, or proxy request violated the registered scope."""


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
    ) -> None:
        self.origin = normalize_origin(origin)
        self.resources = {normalize_origin(item) for item in resource_origins}
        if self.origin != FIXTURE_ORIGIN and FIXTURE_ORIGIN in self.resources:
            raise NetworkDenied("A remote target cannot access the local fixture")
        self.allowed = self.resources | {self.origin}
        self.deny_target = deny_target
        self._connections: set[asyncio.Task] = set()
        self._closing = False

    async def __aenter__(self) -> EgressGuard:
        self._server = await asyncio.start_server(self._handle, host="127.0.0.1", port=0, limit=16384)
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
            if method == "CONNECT":
                await self._connect(reader, writer, address)
            else:
                await self._fixture(reader, writer, method, address, headers)
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
        upstream = None
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
        source, sink = upstream
        transfers = []
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            transfers = [
                asyncio.create_task(self._pipe(reader, sink)),
                asyncio.create_task(self._pipe(source, writer)),
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
            if lower_name in {b"content-type", b"cookie", b"user-agent", b"accept"}:
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
        source, sink = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", FIXTURE_PORT), 3)
        try:
            sink.write(b"\r\n".join(lines) + b"\r\n\r\n" + body)
            await sink.drain()
            await self._pipe(source, writer)
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
