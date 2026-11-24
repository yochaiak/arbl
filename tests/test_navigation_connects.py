import asyncio
import base64
import hashlib
import json
import socket
import ssl
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from test_browser import chromium as chromium
from test_browser import never_cancel

from abrl import adapters
from abrl.browser import execute_workflow
from abrl.proxy import DECODO_GATEWAY, DECODO_PORT

FIRST_HOST = "first.owned.test"
REDIRECT_HOST = "redirect.owned.test"
FIRST_PATH = "/start?token=fixture-private-start"
REDIRECT_PATH = "/finish?cookie=fixture-private-redirect"
PUBLIC_DNS_ANSWER = "93.184.216.34"


@pytest.fixture
def connect_fixture_tls(tmp_path, monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, FIRST_HOST)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=10))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(host) for host in (FIRST_HOST, REDIRECT_HOST, DECODO_GATEWAY)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "connect.crt", tmp_path / "connect.key"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_file, key_file)
    client = ssl.create_default_context(cafile=cert_file)
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pin = base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")
    monkeypatch.setattr(
        adapters, "NETWORK_ARGS", [*adapters.NETWORK_ARGS, f"--ignore-certificate-errors-spki-list={pin}"],
    )
    return server, client


async def relay(source, sink):
    while payload := await source.read(4096):
        sink.write(payload)
        await sink.drain()


@pytest.mark.integration
@pytest.mark.parametrize(("local_rejection", "source", "reason", "status"), [
    (True, "local_proxy", "local_dns_denied", 403),
    (False, "decodo", "proxy_tunnel_failed", 502),
])
async def test_chromium_attributes_connect_failures_on_owned_https_redirect(
    chromium, connect_fixture_tls, monkeypatch, local_rejection, source, reason, status,
):
    server_tls, client_tls = connect_fixture_tls
    open_connection = asyncio.open_connection
    gateway_destinations = []
    visited_paths = []

    async def first_origin(reader, writer):
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            visited_paths.append(request.split()[1].decode("ascii"))
            writer.write(
                f"HTTP/1.1 302 Found\r\nLocation: https://{REDIRECT_HOST}{REDIRECT_PATH}\r\n"
                "Content-Length: 0\r\nConnection: close\r\n\r\n".encode("ascii")
            )
            await writer.drain()
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async with await asyncio.start_server(first_origin, "127.0.0.1", 0, ssl=server_tls) as origin:
        origin_port = origin.sockets[0].getsockname()[1]

        async def fake_decodo(reader, writer):
            destination = None
            pipes = []
            try:
                request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
                method, authority, _ = request.splitlines()[0].decode("ascii").split()
                assert method == "CONNECT"
                assert b"Proxy-Authorization: Basic " in request
                gateway_destinations.append(authority)
                if authority == f"{REDIRECT_HOST}:443":
                    writer.write(
                        b"HTTP/1.1 502 Bad Gateway\r\n"
                        b"X-Error-Description: fixture-private-provider-detail\r\n"
                        b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                    await writer.drain()
                    return
                assert authority == f"{FIRST_HOST}:443"
                source_reader, destination = await open_connection("127.0.0.1", origin_port)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                pipes = [
                    asyncio.create_task(relay(reader, destination)),
                    asyncio.create_task(relay(source_reader, writer)),
                ]
                await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pipe in pipes:
                    pipe.cancel()
                await asyncio.gather(*pipes, return_exceptions=True)
                if destination is not None:
                    destination.close()
                    with suppress(OSError):
                        await destination.wait_closed()
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()

        async with await asyncio.start_server(fake_decodo, "127.0.0.1", 0, ssl=server_tls) as gateway:
            gateway_port = gateway.sockets[0].getsockname()[1]

            async def dns(host, port, **_kwargs):
                assert (host, port) in {(FIRST_HOST, 443), (REDIRECT_HOST, 443), (DECODO_GATEWAY, DECODO_PORT)}
                ip = "127.0.0.1" if local_rejection and host == REDIRECT_HOST else PUBLIC_DNS_ANSWER
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

            async def dial(host, port, **kwargs):
                assert (host, port, kwargs.get("server_hostname")) == (
                    PUBLIC_DNS_ANSWER, DECODO_PORT, DECODO_GATEWAY,
                )
                assert kwargs["ssl"].check_hostname
                return await open_connection("127.0.0.1", gateway_port, ssl=client_tls, server_hostname=DECODO_GATEWAY)

            monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
            monkeypatch.setattr(asyncio, "open_connection", dial)
            monkeypatch.setenv("ABRL_SECRET_CONNECT_FIXTURE_USER", "fixture_user")
            monkeypatch.setenv("ABRL_SECRET_CONNECT_FIXTURE_PASSWORD", "fixture-private-gateway-password")
            result = await execute_workflow(
                origin=f"https://{FIRST_HOST}",
                navigation_origins=[f"https://{REDIRECT_HOST}"],
                resource_origins=[],
                steps=[{"action": "navigate", "url": FIRST_PATH}],
                cancel=never_cancel,
                timeout_seconds=10,
                proxy={
                    "provider": "decodo", "host": DECODO_GATEWAY, "port": DECODO_PORT,
                    "product": "residential", "country": "US", "provider_resolved_ack": True,
                    "username_ref": "CONNECT_FIXTURE_USER", "password_ref": "CONNECT_FIXTURE_PASSWORD",
                    "max_connections": 4,
                },
            )

    assert result.outcome == "failed", result
    assert visited_paths == [FIRST_PATH]
    assert gateway_destinations[0] == f"{FIRST_HOST}:443"
    assert set(gateway_destinations) == (
        {f"{FIRST_HOST}:443"} if local_rejection else {f"{FIRST_HOST}:443", f"{REDIRECT_HOST}:443"}
    )
    trace = result.metadata["navigation_connects"]
    assert trace["version"] == 1
    first, follow_on = trace["navigations"]
    assert first["navigation"] == 1 and not first["redirected"]
    assert first["outcome"] == "redirected"
    assert first["attribution"] == "observed"
    assert first["connects"] == [{"source": "decodo", "reason": "connect_established", "http_status": 200}]
    assert follow_on["navigation"] == 2 and follow_on["redirected"]
    assert follow_on["step"] == 1 and follow_on["outcome"] == "failed"
    assert follow_on["attribution"] == "observed"
    assert follow_on["connects"] == [{"source": source, "reason": reason, "http_status": status}]
    serialized = json.dumps(trace)
    assert not any(value in serialized for value in (
        FIRST_HOST, REDIRECT_HOST, FIRST_PATH, REDIRECT_PATH, PUBLIC_DNS_ANSWER, "127.0.0.1", DECODO_GATEWAY,
        "fixture-private", "fixture_user", "Proxy-Authorization",
    ))
