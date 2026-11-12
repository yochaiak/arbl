import asyncio
import socket
from urllib.parse import urlsplit

import pytest

from abrl import browser, network
from abrl.browser import execute_workflow
from abrl.network import EgressGuard, NetworkDenied, normalize_origin, resolve_public_ipv4


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1",
        "https://192.168.1.1",
        "https://169.254.169.254",
        "https://0x7f000001",
        "https://user@site.example",
        "https://site.example:9443",
        "https://site.example/resource",
        "https://site.example/?tracking=1",
        "http://site.example",
        "http://localhost:8000",
    ],
)
def test_unsafe_origin_is_rejected(value):
    with pytest.raises(NetworkDenied):
        normalize_origin(value)


def test_origin_canonicalization():
    assert normalize_origin("HTTPS://SITE.EXAMPLE:443/") == "https://site.example"
    assert normalize_origin(network.FIXTURE_ORIGIN) == network.FIXTURE_ORIGIN


def test_resource_origin_is_read_only_and_cannot_host_a_document():
    execution = browser.Execution("https://site.example", {"https://assets.example"})
    resource = "https://assets.example/shared"
    assert execution.permits(resource, document=False, method="GET")
    assert not execution.permits(resource, document=True, method="GET")
    assert not execution.permits(resource, document=False, method="POST")
    assert execution.permits("https://site.example/form", document=True, method="POST")


async def test_dns_rejects_mixed_public_and_private_answers(monkeypatch):
    async def records(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", records)
    with pytest.raises(NetworkDenied, match="non-public"):
        await resolve_public_ipv4("site.example")


@pytest.fixture
async def fixture(monkeypatch):
    visited = []
    request_received = asyncio.Event()

    async def handle(reader, writer):
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode().split("\r\n")
            method, path, _ = lines[0].split()
            body_size = next(
                int(line.split(":", 1)[1]) for line in lines if line.lower().startswith("content-length:")
            )
            body = await reader.readexactly(body_size)
            visited.append((method, path, body))
            if path == "/lab?slow":
                request_received.set()
                await reader.read()
                return
            status = "200 OK"
            headers = ""
            response = (
                '<form action="/lab/form" method="post"><label>Sample<input id="name" name="name"></label>'
                '<select id="topic" name="topic"><option value="test">Test</option></select>'
                '<button id="submit">Save</button></form>'
            )
            if method == "POST":
                response = '<p id="success">Sample saved</p>'
            if path == "/lab?popup":
                response = '<a id="popup" href="/lab/form" target="_blank">Open form</a>'
            if path == "/lab?socket":
                response = (
                    '<p id="state">Connecting</p><script>'
                    'const socket = new WebSocket("ws://" + location.host + "/lab/form");'
                    'socket.onclose = () => {document.querySelector("#state").textContent = "Closed";};'
                    "</script>"
                )
            if path == "/lab/finish":
                status, headers = "302 Found", "Location: /lab/manual\r\n"
            if path == "/lab/next":
                status, response = "403 Forbidden", "<p>Access denied</p>"
            content = response.encode()
            writer.write(
                (
                    f"HTTP/1.1 {status}\r\nContent-Type: text/html\r\nContent-Length: {len(content)}\r\n"
                    f"Connection: close\r\n{headers}\r\n"
                ).encode()
                + content
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(network, "FIXTURE_PORT", port)
    monkeypatch.setattr(network, "FIXTURE_ORIGIN", origin)
    monkeypatch.setattr(browser, "FIXTURE_ORIGIN", origin)
    async with server:
        yield origin, visited, request_received


async def request_through_proxy(proxy_url, request):
    parsed = urlsplit(proxy_url)
    reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
    try:
        writer.write(request.encode())
        await writer.drain()
        return await asyncio.wait_for(reader.read(), timeout=5)
    finally:
        writer.close()
        await writer.wait_closed()


async def test_proxy_cannot_forward_manual_or_api_traffic(fixture):
    origin, visited, _ = fixture
    async with EgressGuard(origin, [], lambda _reason: None) as proxy:
        for path in ("/lab/manual", "/api/v1/runs", "/lab/%2e%2e/api/v1/runs"):
            response = await request_through_proxy(
                proxy.url, f"GET {origin}{path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
            )
            assert response.startswith(b"HTTP/1.1 403")
        response = await request_through_proxy(
            proxy.url, f"GET {origin}/lab HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        )
        assert response.startswith(b"HTTP/1.1 200")
    assert visited == [("GET", "/lab", b"")]


async def test_proxy_denies_unregistered_connect_before_dns(monkeypatch):
    async def unexpected_lookup(*_args, **_kwargs):
        pytest.fail("An unregistered hostname reached DNS")

    monkeypatch.setattr(network, "resolve_public_ipv4", unexpected_lookup)
    async with EgressGuard("https://site.example", [], lambda _reason: None) as proxy:
        response = await request_through_proxy(
            proxy.url, "CONNECT outside.example:443 HTTP/1.1\r\nHost: outside.example:443\r\n\r\n"
        )
        assert response.startswith(b"HTTP/1.1 403")


@pytest.fixture(scope="module")
def chromium():
    readiness = browser.browser_availability()
    if not readiness["available"]:
        pytest.skip(readiness["reason"])


async def never_cancel():
    return False


@pytest.mark.integration
async def test_chromium_submits_form_and_extracts_visible_success(chromium, fixture, monkeypatch):
    origin, visited, _ = fixture
    monkeypatch.setenv("ABRL_SECRET_SAMPLE", "sample-only")
    steps = [
        {"action": "navigate", "url": "/lab/form"},
        {"action": "fill", "selector": "#name", "secret_ref": "sample"},
        {"action": "select", "selector": "#topic", "value": "test"},
        {"action": "click", "selector": "#submit"},
        {"action": "wait_for", "selector": "#success"},
        {"action": "extract", "selector": "#success"},
        {"action": "assert_text", "selector": "#success", "value": "Sample saved"},
    ]
    result = await execute_workflow(origin=origin, resource_origins=[], steps=steps, cancel=never_cancel)
    assert result.outcome == "completed", result
    assert result.started_action
    assert result.metadata["steps_completed"] == len(steps)
    assert result.metadata["extractions"] == [{"step": 6, "text": "Sample saved", "truncated": False}]
    assert ("POST", "/lab/form", b"name=sample-only&topic=test") in visited


@pytest.mark.integration
@pytest.mark.parametrize(("path", "error"), [("/lab/finish", "outside"), ("/lab/next", "HTTP 403")])
async def test_chromium_stops_on_denial_or_out_of_scope_redirect(chromium, fixture, path, error):
    origin, visited, _ = fixture
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        steps=[{"action": "navigate", "url": path}, {"action": "click", "selector": "#submit"}],
        cancel=never_cancel,
    )
    assert result.outcome == "blocked", result
    assert error in result.error
    assert visited == [("GET", path, b"")]


@pytest.mark.integration
async def test_chromium_interrupts_active_navigation(chromium, fixture):
    origin, _, request_received = fixture

    async def cancel():
        return request_received.is_set()

    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        steps=[{"action": "navigate", "url": "/lab?slow"}],
        cancel=cancel,
        timeout_seconds=10,
    )
    assert result.outcome == "cancelled", result
    assert result.started_action


@pytest.mark.integration
async def test_chromium_blocks_popup_before_document_request(chromium, fixture):
    origin, visited, _ = fixture
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        steps=[{"action": "navigate", "url": "/lab?popup"}, {"action": "click", "selector": "#popup"}],
        cancel=never_cancel,
    )
    assert result.outcome == "blocked", result
    assert "window" in result.error
    assert visited == [("GET", "/lab?popup", b"")]


@pytest.mark.integration
async def test_chromium_closes_socket_without_forwarding_a_handshake(chromium, fixture):
    origin, visited, _ = fixture
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        steps=[
            {"action": "navigate", "url": "/lab?socket"},
            {"action": "wait_for", "selector": "#state:text-is('Closed')"},
        ],
        cancel=never_cancel,
    )
    assert result.outcome == "completed", result
    assert result.metadata["blocked_resources"] >= 1
    assert visited == [("GET", "/lab?socket", b"")]
