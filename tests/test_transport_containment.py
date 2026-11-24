import asyncio
import errno
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from playwright.async_api import Error as PlaywrightError

from abrl import adapters, network


@pytest.mark.parametrize(("arguments", "expected"), [
    (["chromium", "--disable-quic"], "passed"),
    (["chromium", "--disable-quic", "--", "--enable-quic"], "passed"),
    (["chromium", "--disable-quic", "--enable-quic"], "mismatch"),
    (["chromium", "--origin-to-force-quic-on=fixture.test:443", "--disable-quic"], "mismatch"),
    (["chromium", "--", "--disable-quic"], "mismatch"),
    ([], "unavailable"),
])
async def test_quic_launch_verdict_only_uses_active_switches(arguments, expected):
    session = SimpleNamespace(send=AsyncMock(return_value={"arguments": arguments}))
    assert await adapters.quic_launch_check(session) == expected
    session.send.assert_awaited_once_with("Browser.getBrowserCommandLine")


async def test_vendor_command_line_unavailable():
    session = SimpleNamespace(send=AsyncMock(side_effect=PlaywrightError("Restricted command")))
    assert await adapters.quic_launch_check(session) == "unavailable"


@pytest.fixture
def chromium():
    capability = next(item for item in adapters.adapter_capabilities() if item["id"] == "playwright")
    if not capability["available"]:
        pytest.skip(capability["reason"])


@pytest.mark.integration
async def test_chromium_reports_effective_quic_flag_when_inspection_is_enabled(chromium, monkeypatch):
    # This CDP command needs the automation switch on some builds. Keep the switch local to this test.
    monkeypatch.setattr(adapters, "NETWORK_ARGS", [*adapters.NETWORK_ARGS, "--enable-automation"])
    async with network.EgressGuard(network.FIXTURE_ORIGIN, [], lambda _: None) as guard:
        async with adapters.launch_adapter("playwright", guard.url, None, {}) as runtime:
            page = await runtime.context.new_page()
            session = await runtime.context.new_cdp_session(page)
            assert await adapters.quic_launch_check(session) == "passed"


@pytest.mark.parametrize("literal", ["[::1]", "[::ffff:127.0.0.1]", "[2001:db8::1]", "[v1.site.example]"])
async def test_broker_rejects_ip_literals_outside_ipv4_before_resolving(monkeypatch, literal):
    lookup = AsyncMock(side_effect=AssertionError("A blocked authority reached the resolver"))
    monkeypatch.setattr(network, "resolve_public_ipv4", lookup)
    async with network.EgressGuard("https://v1.site.example", [], lambda _: None) as guard:
        with pytest.raises(network.NetworkDenied):
            guard.allow_asset_origin(f"https://{literal}")
        # A raw CONNECT preserves the original authority, including its brackets.
        reader, writer = await asyncio.open_connection("127.0.0.1", httpx.URL(guard.url).port)
        try:
            writer.write(f"CONNECT {literal}:443 HTTP/1.1\r\nHost: {literal}:443\r\n\r\n".encode("ascii"))
            await writer.drain()
            assert (await asyncio.wait_for(reader.readline(), timeout=3)).startswith(b"HTTP/1.1 403")
        finally:
            writer.close()
            await writer.wait_closed()
    lookup.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.parametrize("persistent", [False, True])
async def test_chromium_sends_ipv6_loopback_request_to_guard(chromium, tmp_path, monkeypatch, persistent):
    connections = 0

    async def serve(reader, writer):
        nonlocal connections
        connections += 1
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        server = await asyncio.start_server(serve, "::1", 0, family=socket.AF_INET6)
    except OSError as exc:
        if exc.errno in {errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT}:
            pytest.skip("IPv6 loopback unavailable on this host")
        raise
    async with server:
        url = f"http://[::1]:{server.sockets[0].getsockname()[1]}/"
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(url)).text == "OK"
        assert connections == 1
        profile = str(tmp_path / "profile") if persistent else None
        async with network.EgressGuard(network.FIXTURE_ORIGIN, [], lambda _: None) as guard:
            observed = asyncio.Event()
            authorize = guard.authorize_request

            async def observe(method, address, headers, writer):
                if method == "GET" and address == url:
                    observed.set()
                return await authorize(method, address, headers, writer)

            monkeypatch.setattr(guard, "authorize_request", observe)
            async with adapters.launch_adapter("playwright", guard.url, profile, {}) as runtime:
                page = await runtime.context.new_page()
                try:
                    response = await page.goto(url, wait_until="commit", timeout=5000)
                except PlaywrightError as exc:
                    # Chromium can surface the guard's empty 403 as a main-document failure.
                    assert "net::ERR_HTTP_RESPONSE_CODE_FAILURE" in str(exc)
                else:
                    assert response is not None and response.status == 403
            assert observed.is_set()
        assert connections == 1
