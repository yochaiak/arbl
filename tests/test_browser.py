import asyncio
import base64
import hashlib
import json
import socket
import ssl
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from html import escape
from http.cookies import SimpleCookie
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qsl, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from playwright.async_api import async_playwright

from abrl import adapters, browser, network
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


def test_adscore_post_is_limited_to_the_registered_engine_endpoint():
    execution = browser.Execution("https://site.example", {"https://c.adsco.re"})
    endpoint = "https://c.adsco.re/p?context=sample"
    assert execution.permits(endpoint, document=False, method="OPTIONS")
    assert execution.permits(endpoint, document=False, method="POST")
    assert not execution.permits(endpoint, document=True, method="POST")
    assert not execution.permits(endpoint, document=False, method="PUT")
    assert not execution.permits("https://c.adsco.re/other", document=False, method="POST")
    assert not execution.permits("https://adsco.re/p", document=False, method="POST")
    assert not execution.permits("https://162.252.214.4/p", document=False, method="POST")
    execution.resources.add("https://162.252.214.4")
    assert execution.permits("https://162.252.214.4/p", document=False, method="POST")
    assert not execution.permits("https://162.252.214.4/other", document=False, method="POST")
    execution.phase = "automated_preparation"
    assert not execution.permits(endpoint, document=False, method="POST")


def test_controlled_adscore_ping_scope_is_bound_to_the_signed_research_page():
    execution = browser.Execution("https://fixture.example", {"https://c.adsco.re"})
    source = "https://fixture.example/lab/research?context=test-attempt"
    favicon = "https://c.adsco.re/favicon.ico"
    match = execution.controlled_adscore_ping_origin
    assert match(favicon, "POST", "ping", source) is None
    execution.controlled_adscore_ping_context = "test-attempt"
    assert match(favicon, "POST", "Ping", source) == "https://c.adsco.re"
    for family in ("l4", "n4", "s4"):
        origin = f"https://generated.{family}.adsco.re"
        assert match(f"{origin}/", "POST", "ping", source) == origin
        assert match(f"{origin}/other", "POST", "ping", source) is None
    assert match(favicon, "GET", "ping", source) is None
    assert match(favicon, "POST", "fetch", source) is None
    assert match(favicon, "POST", "ping", source.replace("test-attempt", "other-attempt")) is None
    assert match(favicon, "POST", "ping", source.replace("/lab/research", "/lab/entry")) is None
    assert match(favicon, "POST", "ping", source.replace("fixture.example", "other.example")) is None
    assert match("https://generated.l4.adsco.re:2087/", "POST", "ping", source) is None
    assert match("https://generated.l4.adsco.re.elsewhere.example/", "POST", "ping", source) is None
    execution.phase = "automated_preparation"
    assert match(favicon, "POST", "ping", source) is None


def test_explicit_navigation_origin_allows_documents_and_actions_but_resources_do_not():
    execution = browser.Execution(
        "https://site.example", {"https://assets.example"}, navigation_origins={"https://checkout.example"}
    )
    assert execution.permits("https://checkout.example/confirm", document=True, method="GET")
    assert execution.permits("https://checkout.example/submit", document=True, method="POST")
    assert not execution.permits("https://assets.example/submit", document=True, method="POST")
    assert not execution.permits("https://unregistered.example/submit", document=True, method="POST")


async def test_dns_rejects_mixed_public_and_private_answers(monkeypatch):
    async def records(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", records)
    with pytest.raises(NetworkDenied, match="non-public"):
        await resolve_public_ipv4("site.example")


MOBILE_TEST_DOCUMENT = (
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<p id="device"></p><script>'
    'document.querySelector("#device").textContent = '
    '[innerWidth, innerHeight, devicePixelRatio, navigator.maxTouchPoints > 0].join(" ");'
    "</script>"
)


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
                (int(line.split(":", 1)[1]) for line in lines if line.lower().startswith("content-length:")), 0
            )
            body = await reader.readexactly(body_size)
            visited.append((method, path, body))
            if path == "/lab?slow":
                request_received.set()
                await reader.read()
                return
            if path == "/lab/form?observation=ready":
                request_received.set()
            status = "200 OK"
            content_type = "text/html"
            headers = ""
            response = (
                '<form action="/lab/form" method="post"><label>Sample<input id="name" name="name"></label>'
                '<select id="topic" name="topic"><option value="test">Test</option></select>'
                '<button id="submit">Save</button></form>'
            )
            if method == "POST":
                response = '<p id="success">Sample saved</p>'
            if method == "POST" and path == "/p":
                referrer = next((line.split(":", 1)[1].strip() for line in lines if line.lower().startswith("referer:")), None)
                response = json.dumps(referrer)
            if path == "/lab?popup":
                response = '<a id="popup" href="/lab/form" target="_blank">Open form</a>'
            if path == "/lab?disabled-action":
                response = response.replace('id="submit"', 'id="private-readiness-control" disabled')
            if path == "/lab?socket":
                response = (
                    '<p id="state">Connecting</p><script>'
                    'const socket = new WebSocket("ws://" + location.host + "/lab/form");'
                    'socket.onclose = () => {document.querySelector("#state").textContent = "Closed";};'
                    "</script>"
                )
            if path.startswith("/lab?profile="):
                marker = path.split("=", 1)[1]
                cookie_header = next((
                    line.partition(":")[2] for line in lines[1:] if line.lower().startswith("cookie:")
                ), "")
                cookie = SimpleCookie(cookie_header).get("abrl_profile")
                cookie_value = cookie.value if cookie else "empty"
                if marker != "read":
                    headers = (
                        f"Set-Cookie: abrl_profile={marker}; Max-Age=300; Path=/lab; HttpOnly; SameSite=Strict\r\n"
                    )
                response = (
                    f'<p id="state"></p><p id="profile-cookie">{escape(cookie_value)}</p><script>'
                    + (f'localStorage.setItem("marker", "{marker}");' if marker != "read" else "")
                    + 'document.querySelector("#state").textContent = localStorage.getItem("marker") || "empty";'
                    + "</script>"
                )
            if path == "/lab?console":
                response += "<script>console.log('private-filled-value');</script>"
            if path == "/lab?mobile":
                response = MOBILE_TEST_DOCUMENT
            if path.startswith("/lab?lifecycle="):
                response += (
                    '<img src="/lab/form?lifecycle-asset=private-asset" alt="Fixture asset">'
                    '<p id="load-ready">Loading</p><script>'
                    'addEventListener("load", () => document.querySelector("#load-ready").textContent = "Ready");'
                    '</script>'
                )
            if path == "/lab/form?lifecycle-asset=private-asset":
                content_type = "image/svg+xml"
                response = '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
            if path == "/lab?private=not-for-the-trace":
                headers = "Referrer-Policy: origin\r\n"
                response += """<script>
                    window.readReferrer = async (referrerPolicy) => {
                        const response = await fetch('/p', {method: 'POST', referrerPolicy});
                        return response.json();
                    };
                </script>"""
            if path == "/lab?adscore-warning":
                response = '<p>ADSCORE_SCRIPT_RULE with private-site-text</p>'
            if path == "/lab?targeting-warning":
                response = (
                    '<section id="warning"><p>OFFER_TARGETING_MISMATCH with private-site-text</p>'
                    '<p hidden>ADSCORE_SCRIPT_RULE</p></section>'
                )
            if path == "/lab?recording":
                response = (
                    '<body style="background:lime"><div data-abrl-secret '
                    'style="position:fixed;inset:20% 50% 20% 10%;background:red">Private label</div>'
                    '<input id="name" style="position:fixed;inset:20% 10% 20% 55%;background:red">'
                )
            if path.startswith("/lab?observe="):
                response += "<script>setTimeout(() => fetch('/lab/form?observation=ready'), 250);</script>"
            if path == "/lab?large":
                response = "<p id='large'>" + "x" * 10000 + "</p>"
            if path == "/lab?request-settings":
                received_headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
                received_headers = {name.lower(): value.strip() for name, value in received_headers.items()}
                received = json.dumps([received_headers.get("user-agent"), received_headers.get("referer")])
                response = (
                    f'<p id="request-settings" data-headers="{escape(received)}"></p><script>'
                    'const probe = document.querySelector("#request-settings");'
                    'const values = JSON.parse(sessionStorage.getItem("requestSettings") || "[]");'
                    "values.push({headers: JSON.parse(probe.dataset.headers), "
                    "browser: [navigator.userAgent, document.referrer]});"
                    'sessionStorage.setItem("requestSettings", JSON.stringify(values));'
                    "probe.textContent = JSON.stringify(values);"
                    "</script>"
                )
            if path == "/lab?redirect":
                status, headers = "302 Found", "Location: /lab/form\r\n"
            if path.startswith("/lab?network-trace&abrl_context="):
                status, headers = "302 Found", "Location: /lab/form?network-trace=private-redirect\r\n"
            if path == "/lab/form?network-trace=private-redirect":
                response = (
                    '<p id="trace">Pending</p><script>'
                    "fetch('https://unregistered.example/private-resource?token=private-query').catch(() => {"
                    "document.querySelector('#trace').textContent = 'Blocked';});</script>"
                )
            if path == "/lab?delayed-redirect":
                response = (
                    '<p id="redirect-state">Redirect pending</p>'
                    "<script>setTimeout(() => location.assign('/lab/form?redirected'), 350);</script>"
                )
            if path == "/lab/form?redirected":
                response = '<p id="redirect-state">Redirect complete</p>'
            if path == "/lab?delayed-empty-response":
                response = "<script>setTimeout(() => location.assign('/lab/form?empty-response'), 350);</script>"
            if path == "/lab/form?empty-response":
                return
            if path.startswith("/lab?post-http-failure="):
                code = path.rsplit("=", 1)[1]
                response = (
                    f'<form method="post" action="/lab/form?http-failure={code}">'
                    '<button id="submit">Save</button></form>'
                )
            if path in {"/lab/form?http-failure=404", "/lab/form?http-failure=503"}:
                status = "404 Not Found" if path.endswith("404") else "503 Service Unavailable"
                response = '<p id="failure-document">Document failed</p>'
            if path == "/lab?challenge-link":
                response = '<a id="verify" href="/lab/form?challenge">Continue</a>'
            if path == "/lab/form?challenge":
                response = "<title>Just a moment...</title><p>Verify you are human</p>"
            if path == "/lab?dialog":
                response = (
                    '<button id="prompt" onclick="prompt(\'private-dialog-text\', \'private-default\')">Open</button>'
                )
            if path == "/lab?subresource-error":
                response += '<img src="/lab/form?http-failure=404" alt="Missing test asset">'
            if path == "/lab?outside":
                status = "302 Found"
                headers = "Location: https://outside.example/private-path?token=private-query\r\n"
            if path == "/lab/finish":
                status, headers = "302 Found", "Location: /lab/manual\r\n"
            if path == "/lab/next":
                status, response = "403 Forbidden", "<p>Access denied</p>"
            content = response.encode()
            writer.write(
                (
                    f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {len(content)}\r\n"
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


async def test_proxy_tunnels_registered_external_origin_for_fixture_target(fixture, monkeypatch):
    origin, visited, _ = fixture
    external_host = "checkout.example"
    public_ip = "93.184.216.34"
    payload = b"opaque tunnel payload"
    open_connection = asyncio.open_connection

    async def echo(reader, writer):
        try:
            writer.write(await reader.readexactly(len(payload)))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(echo, "127.0.0.1", 0) as target:
        target_address = target.sockets[0].getsockname()

        async def resolve(host, port):
            assert (host, port) == (external_host, 443)
            return (public_ip,)

        async def dial(host, port, **kwargs):
            if host == "127.0.0.1":
                return await open_connection(host, port, **kwargs)
            assert (host, port, kwargs) == (public_ip, 443, {"family": socket.AF_INET})
            return await open_connection(*target_address)

        monkeypatch.setattr(network, "resolve_public_ipv4", resolve)
        monkeypatch.setattr(asyncio, "open_connection", dial)
        async with EgressGuard(
            origin, [], lambda _: None, navigation_origins=[f"https://{external_host}"]
        ) as proxy:
            response = await request_through_proxy(
                proxy.url, f"CONNECT {external_host}:443 HTTP/1.1\r\n\r\n" + payload.decode()
            )
    assert response == b"HTTP/1.1 200 Connection Established\r\n\r\n" + payload
    assert visited == []


async def test_kameleo_bootstraps_for_an_external_target(monkeypatch):
    guards = []

    def capture_guard(*args, **kwargs):
        guard = EgressGuard(*args, **kwargs)
        guards.append(guard)
        return guard

    def inspect_startup(adapter, proxy_url, *_args, **_kwargs):
        (guard,) = guards
        assert adapter == "kameleo" and proxy_url == guard.url
        assert guard.forwarding_allowed("CONNECT", "tools.kameleo.io:443")
        assert not guard.forwarding_allowed("CONNECT", "site.example:443")
        assert not guard.forwarding_allowed("CONNECT", "tools-bckp1.kameleo.io:443")
        raise browser.StepFailed("Test intercepted the browser launch")

    monkeypatch.setattr(browser, "EgressGuard", capture_guard)
    monkeypatch.setattr(browser, "launch_adapter", inspect_startup)
    result = await execute_workflow(
        origin="https://site.example",
        resource_origins=["https://tools-bckp1.kameleo.io"],
        adapter="kameleo",
        steps=[{"action": "navigate", "url": "/qa"}],
        cancel=never_cancel,
    )
    assert result.error == "Test intercepted the browser launch"


@pytest.fixture(scope="module")
def chromium():
    readiness = browser.browser_availability()
    if not readiness["available"]:
        pytest.skip(readiness["reason"])


async def never_cancel():
    return False


async def check_mobile_session(origin, *, check_secondary_tab=True, **launch):
    steps = [
        {"action": "navigate", "url": "/lab?mobile"},
        {"action": "assert_text", "selector": "#device", "value": "390 844 3 true"},
    ]
    if check_secondary_tab:
        steps.append({"action": "open_tab", "tab": "secondary", "url": "/lab?mobile"})
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        device_preset="phone",
        workflow={
            "schema_version": 2,
            "success_check": {"kind": "text_contains", "selector": "#device", "text": "390 844 3 true"},
        },
        steps=steps,
        cancel=never_cancel,
        **launch,
    )
    assert result.outcome == "completed", result
    assert result.metadata["device_preset"] == "phone"
    assert result.metadata["device_mode"] == "mobile_emulation"
    assert result.metadata["viewport"] == {"width": 390, "height": 844}
    checked_tabs = 2 if check_secondary_tab else 1
    assert result.metadata["viewport_conformance"] == {"checks": checked_tabs, "matched": checked_tabs}
    assert result.metadata["workflow_readiness"]["success_check"]["ready"] == 1
    assert result.metadata["browser_engine"] == "Chromium"
    assert result.metadata["browser_version"]


@pytest.mark.integration
@pytest.mark.parametrize("persistent", [False, True], ids=["fresh", "persistent"])
async def test_local_chromium_applies_mobile_preset(chromium, fixture, tmp_path, persistent):
    origin, _, _ = fixture
    await check_mobile_session(origin, profile_dir=str(tmp_path / "profile") if persistent else None)


@pytest.mark.parametrize(("adapter", "application", "extra"), [
    ("playwright", "context", {"user_agent": "Conflicting QA override"}),
    ("playwright", "context", {"device_preset": "phone"}),
    ("playwright", "context", {"remote": {}}),
    ("cloakbrowser", "context", {}),
    ("kameleo", "context", {}),
    ("manual_chrome", "context", {}),
    ("playwright", "viewport_only", {}),
    ("manual_chrome", "viewport_only", {"device_preset": "phone"}),
    ("kameleo", "viewport_only", {"remote": {}}),
])
async def test_batch_presets_reject_conflicting_launch_settings(adapter, application, extra, monkeypatch):
    monkeypatch.setattr(adapters, "_check_capability", lambda *_: pytest.fail("Browser launch was reached"))
    assignment = {
        "catalog_version": 1, "application": application, "id": "compact_390",
        "locale": "en-US" if application == "context" else None, "mode": "fixed", "seed": 42,
    }
    with pytest.raises(adapters.AdapterUnavailable, match="browser-default identity"):
        async with adapters.launch_adapter(
            adapter, "http://127.0.0.1:3001", None, {}, batch_preset=assignment, **extra,
        ):
            pytest.fail("Incompatible browser launched")


@pytest.mark.integration
@pytest.mark.parametrize(("preset", "locale", "timezone", "viewport", "persistent"), [
    ("compact_390", "he-IL", "Asia/Jerusalem", [390, 844, 3], False),
    ("desktop_1440", "en-GB", "Europe/London", [1440, 900, 2], True),
])
async def test_local_browser_batch_preset_preserves_native_identity(
    chromium, https_asset_site, tmp_path, preset, locale, timezone, viewport, persistent,
):
    origin = "https://shop.example"
    url = "/lab?responsive-settings"
    navigate = {"action": "navigate", "url": url}
    extract = {"action": "extract", "selector": "#responsive-settings"}
    baseline = await execute_workflow(
        origin=origin, resource_origins=[], steps=[navigate, extract], cancel=never_cancel,
    )
    assert baseline.outcome == "completed", baseline
    native = json.loads(baseline.metadata["extractions"][0]["text"])

    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        workflow={"schema_version": 2},
        steps=[navigate, extract, {"action": "open_tab", "tab": "secondary", "url": url}, extract],
        profile_dir=str(tmp_path / "profile") if persistent else None,
        batch_preset={"catalog_version": 1, "id": preset, "locale": locale, "mode": "round_robin", "seed": 42},
        cancel=never_cancel,
    )
    assert result.outcome == "completed", result
    assert result.metadata["device_mode"] == "responsive_viewport"
    assert result.metadata["viewport"] == {"width": viewport[0], "height": viewport[1]}
    assert "configured_user_agent" not in result.metadata
    observations = [json.loads(item["text"]) for item in result.metadata["extractions"]]
    assert len(observations) == 2
    for observation in observations:
        ua, brands, mobile, platform, language = observation["headers"]
        nav_ua, nav_mobile, nav_platform, touch = observation["identity"]
        assert [ua, brands, mobile, platform] == native["headers"][:4]
        assert observation["identity"] == native["identity"]
        assert ua == nav_ua and brands and mobile == "?0" and nav_mobile is False and touch == 0
        assert platform == f'"{nav_platform}"' and nav_platform
        assert observation["viewport"] == viewport
        assert observation["language"] == locale and language.split(",")[0] == locale
        assert observation["timezone"] == timezone
    visited, _ = https_asset_site
    assert visited == {("shop.example", "GET", url)}


def _viewport_only_assignment():
    return {
        "catalog_version": 1, "application": "viewport_only", "id": "compact_390",
        "locale": None, "mode": "fixed", "seed": 42,
    }


@pytest.mark.integration
async def test_adapter_viewport_only_keeps_native_display_and_locale(chromium, https_asset_site, monkeypatch):
    @asynccontextmanager
    async def local_runtime(adapter, proxy_url, profile_dir, metadata, **kwargs):
        assert adapter == "kameleo"
        async with async_playwright() as driver:
            launched = await driver.chromium.launch(
                channel="chromium", chromium_sandbox=True, proxy={"server": proxy_url},
                args=[*adapters.NETWORK_ARGS, "--screen-info={3200x2400}", "--force-device-scale-factor=2"],
            )
            try:
                context = await launched.new_context(no_viewport=True, service_workers="block")
                yield adapters.BrowserRuntime(context, metadata)
            finally:
                await launched.close()

    monkeypatch.setattr(browser, "launch_adapter", local_runtime)
    origin = "https://shop.example"
    url = "/lab?responsive-settings"
    navigate = {"action": "navigate", "url": url}
    extract = {"action": "extract", "selector": "#responsive-settings"}
    baseline = await execute_workflow(
        origin=origin, resource_origins=[], steps=[navigate, extract], adapter="kameleo", cancel=never_cancel,
    )
    assert baseline.outcome == "completed", baseline
    native = json.loads(baseline.metadata["extractions"][0]["text"])
    assert native["viewport"] != [390, 844, 2] and native["viewport"][2] == 2

    result = await execute_workflow(
        origin=origin, resource_origins=[], adapter="kameleo", workflow={"schema_version": 2},
        steps=[navigate, extract, {"action": "open_tab", "tab": "secondary", "url": url}, extract],
        batch_preset=_viewport_only_assignment(), cancel=never_cancel,
    )
    assert result.outcome == "completed", result
    assert result.metadata["device_mode"] == "responsive_viewport"
    assert result.metadata["viewport"] == {"width": 390, "height": 844}
    assert result.metadata["viewport_conformance"] == {"checks": 2, "matched": 2}
    observations = [json.loads(item["text"]) for item in result.metadata["extractions"]]
    assert observations == [{**native, "viewport": [390, 844, 2]}] * 2


@pytest.mark.parametrize(("observed", "resized", "error"), [
    ([[500, 600, 800, 600, 2]], False, "exceeds the adapter screen"),
    ([[500, 600, 1600, 1200, 2], [390, 844, 1600, 1200, 1]], True, "changed native display"),
    ([[500, 600, 1600, 1200, 2], [391, 844, 1600, 1200, 2]], True, "did not apply"),
])
async def test_adapter_viewport_only_rejects_incoherent_display(observed, resized, error):
    page = Mock(evaluate=AsyncMock(side_effect=observed), set_viewport_size=AsyncMock())
    session = Mock(send=AsyncMock())
    execution = browser.Execution(network.FIXTURE_ORIGIN, set())
    with pytest.raises(adapters.AdapterUnavailable, match=error) as failure:
        await browser._apply_batch_preset(
            page, session, browser.preset_details(_viewport_only_assignment()), execution,
        )
    assert failure.value.status == "invalid_configuration"
    assert execution.metadata["viewport_conformance"] == {"checks": 1, "matched": 0}
    assert execution.metadata["causal_failure"]["reason"] == "viewport_mismatch"
    if resized:
        session.send.assert_awaited_once_with("Emulation.setVisibleSize", {"width": 390, "height": 844})
    else:
        session.send.assert_not_awaited()
    page.set_viewport_size.assert_not_awaited()


@pytest.mark.parametrize("adapter", ["manual_chrome", "cloakbrowser", "kameleo"])
async def test_other_adapters_use_same_viewport_without_mobile_emulation(adapter):
    page = Mock(set_viewport_size=AsyncMock(), evaluate=AsyncMock(), viewport_size={"width": 390, "height": 844})
    execution = browser.Execution(network.FIXTURE_ORIGIN, set())
    assert await browser._apply_device_preset(page, adapter, "phone", execution) == {
        "device_preset": "phone",
        "device_mode": "responsive_viewport",
        "viewport": {"width": 390, "height": 844},
    }
    page.set_viewport_size.assert_awaited_once_with({"width": 390, "height": 844})
    page.evaluate.assert_not_awaited()
    assert execution.metadata["viewport_conformance"] == {"checks": 1, "matched": 1}


async def test_viewport_mismatch_reports_counts_without_observed_values():
    page = Mock(set_viewport_size=AsyncMock(), viewport_size={"width": 413, "height": 856})
    execution = browser.Execution(network.FIXTURE_ORIGIN, set())
    with pytest.raises(adapters.AdapterUnavailable, match="configured viewport"):
        await browser._apply_device_preset(page, "manual_chrome", "phone", execution)
    assert execution.metadata["viewport_conformance"] == {"checks": 1, "matched": 0}
    assert execution.metadata["causal_failure"]["reason"] == "viewport_mismatch"
    assert "413" not in repr(execution.metadata["viewport_conformance"])


@pytest.fixture
async def https_asset_site(monkeypatch, tmp_path):
    hosts = {"shop.example": "93.184.216.34", "assets.example": "8.8.8.8", "static.example": "1.1.1.1"}
    private_host = "private-assets.example"
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "shop.example")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=10))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host) for host in hosts]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "assets.crt", tmp_path / "assets.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_file, key_file)
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    pin = base64.b64encode(hashlib.sha256(spki).digest()).decode()
    monkeypatch.setattr(
        adapters, "NETWORK_ARGS", [*adapters.NETWORK_ARGS, f"--ignore-certificate-errors-spki-list={pin}"]
    )

    html = b"""
<link rel="stylesheet" href="https://assets.example/style.css">
<p id="style-pending">Style pending</p>
<p id="image">Waiting</p><p id="module">Waiting</p><p id="private">Waiting</p><p id="api">Waiting</p>
<img src="https://assets.example/image.svg" onload="document.querySelector('#image').textContent='Loaded'">
<img src="https://private-assets.example/image.svg"
     onerror="document.querySelector('#private').textContent='Blocked'">
<script type="module" src="https://assets.example/module.js"></script>
<script>
const requests = [
  fetch("https://assets.example/get", {mode: "no-cors"}),
  fetch("https://assets.example/post", {method: "POST", mode: "no-cors"})
];
Promise.allSettled(requests).then(results => {
  if (results.every(result => result.status === "rejected")) {
    document.querySelector("#api").textContent = "Blocked";
  }
});
</script>
"""
    responses = {
        ("shop.example", "/"): ("200 OK", "text/html", "", html),
        ("shop.example", "/lab?mobile"): ("200 OK", "text/html", "", MOBILE_TEST_DOCUMENT.encode()),
        ("assets.example", "/style.css"): ("200 OK", "text/css", "", b"#style-pending{display:none}"),
        ("assets.example", "/image.svg"): (
            "302 Found", "text/plain", "Location: https://static.example/image.svg\r\n", b""
        ),
        ("static.example", "/image.svg"): (
            "200 OK", "image/svg+xml", "", b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
        ),
        ("assets.example", "/module.js"): (
            "200 OK", "text/javascript", "Access-Control-Allow-Origin: *\r\n",
            b"document.querySelector('#module').textContent='Loaded';",
        ),
    }
    visited = set()
    dns_hosts = set()

    async def handle(reader, writer):
        try:
            first, *headers = (await reader.readuntil(b"\r\n\r\n")).decode("ascii").split("\r\n")
            method, path, _ = first.split()
            host = next(header.split(":", 1)[1].strip() for header in headers if header.lower().startswith("host:"))
            visited.add((host, method, path))
            status, content_type, extra, body = responses.get((host, path), ("404 Not Found", "text/plain", "", b""))
            if (host, path) == ("shop.example", "/lab?responsive-settings"):
                received = dict(line.split(":", 1) for line in headers if ":" in line)
                received = {name.lower(): value.strip() for name, value in received.items()}
                values = [received.get(name) for name in (
                    "user-agent", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "accept-language",
                )]
                status, content_type = "200 OK", "text/html"
                body = (
                    f'<p id="responsive-settings" data-headers="{escape(json.dumps(values))}"></p><script>'
                    'const observation = document.querySelector("#responsive-settings");'
                    'const {userAgent, userAgentData, language, maxTouchPoints} = navigator;'
                    'observation.textContent = JSON.stringify({'
                    'headers: JSON.parse(observation.dataset.headers),'
                    'identity: [userAgent, userAgentData.mobile, userAgentData.platform, maxTouchPoints],'
                    'viewport: [innerWidth, innerHeight, devicePixelRatio],'
                    'screen: [screen.width, screen.height],'
                    'language, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone'
                    '});</script>'
                ).encode()
            writer.write(
                (
                    f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n"
                    f"Connection: close\r\n{extra}\r\n"
                ).encode("ascii") + body
            )
            await writer.drain()
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async with await asyncio.start_server(handle, "127.0.0.1", 0, ssl=tls) as target:
        address = target.sockets[0].getsockname()
        loop = asyncio.get_running_loop()
        getaddrinfo, open_connection = loop.getaddrinfo, asyncio.open_connection

        async def local_dns(host, port, *args, **kwargs):
            if port != 443:
                return await getaddrinfo(host, port, *args, **kwargs)
            dns_hosts.add(host)
            ip = "127.0.0.1" if host == private_host else hosts[host]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

        async def dial(host, port, **kwargs):
            if port != 443:
                return await open_connection(host, port, **kwargs)
            assert host in hosts.values(), "Only validated public addresses may reach the TLS fixture"
            return await open_connection(*address, **kwargs)

        monkeypatch.setattr(loop, "getaddrinfo", local_dns)
        monkeypatch.setattr(asyncio, "open_connection", dial)
        yield visited, dns_hosts


@pytest.mark.integration
async def test_chromium_loads_unlisted_assets_without_granting_navigation(chromium, https_asset_site):
    visited, dns_hosts = https_asset_site
    result = await execute_workflow(
        origin="https://shop.example",
        resource_origins=[],
        cancel=never_cancel,
        steps=[
            {"action": "navigate", "url": "/"},
            {"action": "assert_not_visible", "selector": "#style-pending"},
            {"action": "assert_text", "selector": "#image", "value": "Loaded"},
            {"action": "assert_text", "selector": "#module", "value": "Loaded"},
            {"action": "assert_text", "selector": "#private", "value": "Blocked"},
            {"action": "assert_text", "selector": "#api", "value": "Blocked"},
            {"action": "navigate", "url": "https://assets.example/document"},
        ],
    )
    assert result.outcome == "blocked", result
    assert result.error == "Navigation outside the registered target scope (https://assets.example)"
    assert result.metadata["steps_completed"] == 6
    assert {
        ("assets.example", "GET", "/style.css"),
        ("assets.example", "GET", "/image.svg"),
        ("static.example", "GET", "/image.svg"),
        ("assets.example", "GET", "/module.js"),
    } <= visited
    assert {"private-assets.example", "static.example"} <= dns_hosts
    assert not {"/post", "/get", "/document"} & {path for _, _, path in visited}


@pytest.mark.integration
async def test_chromium_automatic_assets_use_the_request_budget(chromium, https_asset_site):
    visited, _ = https_asset_site
    result = await execute_workflow(
        origin="https://shop.example",
        resource_origins=[],
        cancel=never_cancel,
        request_budget=1,
        steps=[{"action": "navigate", "url": "/"}],
    )
    assert result.outcome == "blocked", result
    assert result.metadata["request_usage"]["exhausted"]
    assert visited == {("shop.example", "GET", "/")}


async def check_session_request_settings(origin, *, profile_dir=None, remote=None, prepare=False):
    user_agent = "ABRL Compatibility Probe/1.0"
    referrer = origin + "/lab?source=compatibility-probe"

    async def handoff():
        return {"command": "continue"}

    navigate = {"action": "navigate", "url": "/lab?request-settings"}
    steps = [navigate, {"action": "extract", "selector": "#request-settings"}]
    if not prepare:
        steps.insert(0, navigate)
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        user_agent=user_agent,
        referrer=referrer,
        preparation={"mode": "manual"} if prepare else None,
        control=handoff if prepare else None,
        profile_dir=profile_dir,
        remote=remote,
        steps=steps,
        cancel=never_cancel,
    )
    assert result.outcome == "completed", result
    assert json.loads(result.metadata["extractions"][0]["text"]) == [
        {"headers": [user_agent, referrer], "browser": [user_agent, referrer]},
        {"headers": [user_agent, None], "browser": [user_agent, ""]},
    ]
    assert result.metadata["configured_user_agent"] == user_agent
    assert result.metadata["actual_user_agent"] and result.metadata["actual_user_agent"] != user_agent
    assert result.metadata["profile_saved"] is (profile_dir is not None)
    assert result.metadata["runtime_stopped"]


@pytest.mark.integration
@pytest.mark.parametrize("prepare", [False, True], ids=["fresh-workflow", "persistent-preparation"])
async def test_chromium_applies_session_request_settings(chromium, fixture, tmp_path, prepare):
    origin, _, _ = fixture
    await check_session_request_settings(
        origin, profile_dir=str(tmp_path / "profile") if prepare else None, prepare=prepare
    )


@pytest.mark.integration
@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
async def test_local_fixture_receives_encoded_workflow_parameters(chromium, fixture, absolute):
    origin, visited, _ = fixture
    prefix = origin if absolute else ""
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        steps=[
            {
                "action": "navigate",
                "url": f"{prefix}/lab?term=[QUERY]&pub_sub_id=[SUBID]&marker=local",
                "fixture_params": {"QUERY": "בדיקה & QA=1", "SUBID": "test/#1"},
            },
        ],
        cancel=never_cancel,
    )
    assert result.outcome == "completed", result
    assert result.metadata["runtime_stopped"]
    assert len(visited) == 1
    _, requested, _ = visited[0]
    assert urlsplit(requested).path == "/lab"
    assert parse_qsl(urlsplit(requested).query) == [
        ("term", "בדיקה & QA=1"),
        ("pub_sub_id", "test/#1"),
        ("marker", "local"),
    ]


@pytest.mark.integration
async def test_single_navigation_keeps_browser_open_for_site_verification(chromium, fixture):
    origin, visited, observation = fixture

    async def wait_for_observation():
        await asyncio.wait_for(observation.wait(), timeout=5)
        return True

    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        steps=[
            {
                "action": "navigate",
                "url": "/lab?observe=[QUERY]&id=[SUBID]",
                "fixture_params": {"QUERY": "QA & test", "SUBID": "test-1"},
            }
        ],
        cancel=never_cancel,
        observation_context="sandbox context",
        wait_for_observation=wait_for_observation,
    )
    assert result.outcome == "completed", result
    assert result.metadata["observation_received"]
    assert result.metadata["runtime_stopped"]
    assert visited == [
        ("GET", "/lab?observe=QA+%26+test&id=test-1&abrl_context=sandbox+context", b""),
        ("GET", "/lab/form?observation=ready", b""),
    ]


async def test_policy_cdp_session_never_sends_proxy_credentials_to_server_challenges():
    from types import SimpleNamespace

    handlers, sent = {}, []

    async def ignore(*args, **kwargs):
        return None

    async def send(method, payload):
        sent.append((method, payload))

    session = SimpleNamespace(on=lambda event, callback: handlers.update({event: callback}), send=send)
    context = SimpleNamespace(route=ignore, route_web_socket=ignore)
    page = SimpleNamespace(on=lambda *_args: None)
    execution = browser.Execution("https://site.example", set())
    await browser._attach_page_policy(
        context, page, session, execution, proxy_auth={"username": "test-session", "password": "test-lease"}
    )
    assert sent[-1][1]["handleAuthRequests"] is True
    await handlers["Fetch.authRequired"]({"requestId": "server", "authChallenge": {"source": "Server"}})
    assert sent[-1][1]["authChallengeResponse"] == {"response": "Default"}
    await handlers["Fetch.authRequired"]({"requestId": "proxy", "authChallenge": {"source": "Proxy"}})
    assert sent[-1][1]["authChallengeResponse"] == {
        "response": "ProvideCredentials",
        "username": "test-session",
        "password": "test-lease",
    }
    assert "test-lease" not in str(execution.metadata)


async def test_same_document_navigation_keeps_pending_document_failure_tracking():
    listeners = {}
    origin = "https://site.example"
    frame = Mock(url=f"{origin}/old#section")
    page = Mock(main_frame=frame, on=listeners.__setitem__)
    session = Mock(send=AsyncMock(), on=Mock())
    context = Mock(route=AsyncMock(), route_web_socket=AsyncMock())
    execution = browser.Execution(origin, set())
    await browser._attach_page_policy(context, page, session, execution)
    request = Mock(
        frame=frame, url=f"{origin}/next", method="GET", resource_type="document",
        is_navigation_request=lambda: True, failure="net::ERR_CONNECTION_RESET",
    )

    listeners["request"](request)
    listeners["framenavigated"](frame)
    listeners["requestfailed"](request)

    assert execution.metadata["causal_failure"]["reason"] == "navigation_failed"
    assert execution.metadata["navigation_failure"] == {"origin": origin, "error": "net::ERR_CONNECTION_RESET"}
    assert execution.navigation_activity.requests == 1
    assert execution.navigation_activity.commits == 0


async def test_adscore_request_failures_distinguish_scope_browser_errors_and_http_status():
    from types import SimpleNamespace
    from unittest.mock import Mock

    page_events, cdp_events, routes, replies = {}, {}, {}, []

    async def send(method, payload):
        replies.append((method, payload))

    async def register(pattern, callback):
        routes[pattern] = callback

    async def ignore(*_args, **_kwargs):
        pass

    context = SimpleNamespace(route=register, route_web_socket=ignore)
    page = SimpleNamespace(on=lambda event, callback: page_events.setdefault(event, []).append(callback))
    session = SimpleNamespace(on=lambda event, callback: cdp_events.update({event: callback}), send=send)
    execution = browser.Execution("https://site.example", {"https://c.adsco.re", "https://adsco.re"}, active_step=1)
    await browser._attach_page_policy(context, page, session, execution)

    def emit(event, value):
        for callback in page_events[event]:
            callback(value)

    def request(url, error=None, *, method="GET"):
        return Mock(
            url=url, method=method, resource_type="fetch", is_navigation_request=lambda: False,
            frame=None, failure=error, service_worker=None,
        )

    engine_post = request("https://adsco.re/p?context=private-query", method="POST")
    continued = []

    async def continue_():
        continued.append(True)

    await routes["**/*"](SimpleNamespace(request=engine_post, continue_=continue_))
    await cdp_events["Fetch.requestPaused"]({
        "requestId": "engine", "resourceType": "Fetch", "request": {"method": "POST", "url": engine_post.url},
    })
    assert continued == [True]
    assert replies[-1] == ("Fetch.continueRequest", {"requestId": "engine"})

    refused = request("https://162.252.214.4/p?token=private-query#private-fragment", "net::ERR_FAILED")
    called = []

    async def abort():
        called.append(True)

    await routes["**/*"](SimpleNamespace(request=refused, abort=abort))
    emit("requestfailed", refused)
    await cdp_events["Fetch.requestPaused"]({
        "requestId": "literal", "resourceType": "Fetch", "request": {"method": "GET", "url": refused.url},
    })
    assert called == [True]
    assert replies[-1] == ("Fetch.failRequest", {"requestId": "literal", "errorReason": "BlockedByClient"})
    emit("requestfailed", request(
        "https://c.adsco.re/engine?key=private-query", "net::ERR_TUNNEL_CONNECTION_FAILED"
    ))
    http_error = request("https://adsco.re/p?context=private-query")
    emit("response", SimpleNamespace(url=http_error.url, request=http_error, status=503))
    emit("requestfailed", request("https://c.adsco.re.elsewhere.example/p", "net::ERR_FAILED"))

    common = {"method": "GET", "resource_type": "fetch", "phase": "workflow", "step": 1}
    assert execution.metadata["adscore_request_failures"] == [
        {**common, "url": "https://162.252.214.4/p", "kind": "scope_blocked"},
        {
            **common, "url": "https://c.adsco.re/engine", "kind": "network_error",
            "error": "net::ERR_TUNNEL_CONNECTION_FAILED",
        },
        {**common, "url": "https://adsco.re/p", "kind": "http_error", "status": 503},
    ]
    assert not execution.block.is_set()


def test_adscore_request_failure_metadata_is_bounded_and_redacted():
    execution = browser.Execution("https://site.example", set(), secrets={"TEST": "private-key"})
    execution.record_adscore_request_failure(
        "https://adsco.re/private-key/" + "q" * 80, "POST", "xhr", "network_error", error="private message"
    )
    for index in range(browser.MAX_ADSCORE_REQUEST_FAILURES + 2):
        execution.record_adscore_request_failure(f"https://adsco.re/p/{index}", "GET", "fetch", "http_error", status=400)
    failures = execution.metadata["adscore_request_failures"]
    assert failures[0]["url"] == "https://adsco.re/[redacted]/[redacted]"
    assert failures[0]["error"] == "request_failed"
    assert len(failures) == browser.MAX_ADSCORE_REQUEST_FAILURES
    assert execution.metadata["adscore_request_failures_omitted"] == 3


async def test_adscore_trace_keeps_success_and_cancellation_without_request_data(monkeypatch):
    from unittest.mock import AsyncMock, Mock

    handlers = {}
    times = iter((5.0, 5.0, 5.1, 5.2, 5.21, 5.3))
    monkeypatch.setattr(browser, "monotonic", lambda: next(times))
    page = Mock()
    page.on.side_effect = lambda name, callback: handlers.update({name: callback})
    execution = browser.Execution("https://site.example", {"https://adsco.re"})
    browser._trace_adscore_requests(page, execution)

    first = Mock(
        method="POST", url="https://adsco.re/p?opaque=secret", post_data="secret",
        header_value=AsyncMock(return_value="https://site.example/private?secret=value"),
    )
    second = Mock(method="POST", url="https://c.adsco.re/p?opaque=secret", failure="net::ERR_ABORTED")
    handlers["request"](first)
    handlers["request"](second)
    handlers["response"](Mock(request=first, status=200))
    handlers["requestfailed"](second)
    handlers["requestfinished"](first)
    await asyncio.gather(*execution.adscore_referrer_reads)

    assert execution.metadata["adscore_requests"] == [
        {"url": "https://adsco.re/p", "method": "POST", "started_ms": 0, "status": 200,
         "response_ms": 200, "outcome": "finished", "finished_ms": 300, "referrer_origin": "https://site.example"},
        {"url": "https://c.adsco.re/p", "method": "POST", "started_ms": 100, "outcome": "failed",
         "finished_ms": 210, "error": "net::ERR_ABORTED"},
    ]


@pytest.mark.integration
async def test_routed_chromium_adscore_trace_matches_the_loopback_servers_referrer(chromium, fixture, monkeypatch):
    origin, _, _ = fixture
    monkeypatch.setattr(browser, "ADSCORE_ENGINE_ORIGINS", {"http://127.0.0.1", origin})
    monkeypatch.setattr(network, "FIXTURE_PATHS", network.FIXTURE_PATHS | {"/p"})
    execution = browser.Execution(origin, {origin})
    entry_url = f"{origin}/lab?private=not-for-the-trace"
    async with async_playwright() as playwright:
        runtime = await playwright.chromium.launch()
        try:
            page = await runtime.new_page()
            session = await page.context.new_cdp_session(page)
            await browser._attach_page_policy(page.context, page, session, execution, allow_asset_origin=lambda _: True)
            await page.goto(entry_url)
            received = await page.evaluate("""async () => {
                return [await window.readReferrer('same-origin'), await window.readReferrer('no-referrer')];
            }""")
            await asyncio.gather(*execution.adscore_referrer_reads)
        finally:
            await runtime.close()
    assert received == [entry_url, None]
    trace = execution.metadata["adscore_requests"]
    assert [(row["status"], row["referrer_origin"]) for row in trace] == [(200, origin), (200, None)]
    assert "not-for-the-trace" not in json.dumps(trace)
    assert execution.metadata["adscore_referrer_policies"] == [
        {"url": f"{origin}/p", "request_policy": policy, "document_policy": "origin"}
        for policy in ("same-origin", "no-referrer")
    ]
    initiators = execution.metadata["adscore_request_initiators"]
    assert [(row["initiator_type"], row["initiator_origin"], row["context"]) for row in initiators] == [
        ("script", origin, "main_frame"), ("script", origin, "main_frame"),
    ]
    assert [(row["paused_referrer_present"], row["network_referrer_present"]) for row in initiators] == [
        (True, True), (False, False),
    ]
    assert "not-for-the-trace" not in json.dumps(initiators)


def test_adscore_initiator_trace_keeps_header_stages_scoped_to_the_request():
    metadata = {}
    trace = browser.AdScoreInitiatorTrace(metadata, {"https://c.adsco.re"}, browser._adscore_endpoint, "main")
    request = {"url": "https://c.adsco.re/p?private=value", "method": "POST", "headers": {"Referer": "discard-me"}}
    trace.network_headers({"requestId": "first", "headers": {}})
    trace.paused({"networkId": "first", "request": {**request, "headers": {}}})
    event = {
        "requestId": "first", "type": "Fetch", "request": request,
        "frameId": "main", "loaderId": "document", "documentURL": "https://site.example/private?value=abcd",
        "initiator": {"type": "script", "stack": {"callFrames": [
            {"url": "https://c.adsco.re/private.js?value=abcd", "functionName": "privateName", "lineNumber": 42},
        ]}},
    }
    trace.requested(event)
    trace.responded({"requestId": "first", "response": {"status": 200}})
    trace.requested({**event, "requestId": "second", "initiator": {"type": "other"}, "frameId": "nested"})
    trace.network_headers({"requestId": "second", "headers": {"referer": "discard-me"}})
    first, second = metadata["adscore_request_initiators"]
    assert first == {
        "url": "https://c.adsco.re/p", "initiator_type": "script", "initiator_origin": "https://c.adsco.re",
        "document_origin": "https://site.example", "context": "main_frame", "resource_type": "fetch",
        "initial_referrer_present": True, "paused_referrer_present": False, "network_referrer_present": False,
        "status": 200,
    }
    assert second["context"] == "subframe" and second["network_referrer_present"] is True
    assert "initiator_origin" not in second and "paused_referrer_present" not in second
    trace.requested({**event, "requestId": "second", "redirectResponse": {"status": 307}})
    trace.network_headers({"requestId": "second", "headers": {"referer": "discard-me"}})
    assert second["redirected"] and "network_referrer_present" not in second
    assert all(secret not in json.dumps(metadata) for secret in ("private", "discard-me", "abcd"))


@pytest.mark.integration
@pytest.mark.parametrize(("path", "selector", "code"), [
    ("/lab?adscore-warning", "text=/OFFER_TARGETING_MISMATCH|ADSCORE_SCRIPT_RULE/", "ADSCORE_SCRIPT_RULE"),
    ("/lab?targeting-warning", "#warning, #OFFER_TARGETING_MISMATCH, #ADSCORE_SCRIPT_RULE", "OFFER_TARGETING_MISMATCH"),
])
async def test_timeout_records_only_the_visible_warning_in_the_assertion(chromium, fixture, path, selector, code):
    origin, _, _ = fixture
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        steps=[
            {"action": "navigate", "url": path},
            {"action": "assert_not_visible", "selector": selector, "timeout_ms": 100},
        ],
        cancel=never_cancel,
    )
    assert result.error == "Step 2 timed out"
    assert result.metadata["visible_assertion_warning"] == {"step": 2, "codes": [code]}
    assert "private-site-text" not in json.dumps(result.metadata)


async def test_adscore_402_probe_resumes_original_response_and_retains_only_classification():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock, call

    body = '{"error":4021,"message":"Insufficient balance","detail":"private-value"}'
    listeners = {}

    async def send(method, params=None):
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "top-frame"}}}
        if method == "Fetch.getResponseBody":
            return {"body": base64.b64encode(body.encode()).decode(), "base64Encoded": True}
        return {}

    session = SimpleNamespace(on=Mock(side_effect=listeners.__setitem__), send=AsyncMock(side_effect=send))
    page = SimpleNamespace(main_frame=Mock(url="https://fixture.example/lab/research?context=test-attempt"), on=Mock())
    context = SimpleNamespace(route=AsyncMock(), route_web_socket=AsyncMock())
    execution = browser.Execution(
        "https://fixture.example", {"https://c.adsco.re"}, controlled_adscore_ping_context="test-attempt"
    )
    await browser._attach_page_policy(context, page, session, execution, allow_asset_origin=lambda _: True)
    payload = {
        "requestId": "primary",
        "frameId": "top-frame",
        "resourceType": "Fetch",
        "request": {
            "method": "POST", "url": "https://c.adsco.re/p?token=private-value",
            "headers": {"Referer": "https://fixture.example/"},
        },
    }
    await listeners["Fetch.requestPaused"](payload)
    await listeners["Fetch.requestPaused"]({
        **payload,
        "responseStatusCode": 402,
        "responseHeaders": [
            {"name": "content-type", "value": "application/json; charset=utf-8"},
            {"name": "content-length", "value": str(len(body))},
        ],
    })
    assert session.send.await_args_list[-3:] == [
        call("Fetch.continueRequest", {"requestId": "primary", "interceptResponse": True}),
        call("Fetch.getResponseBody", {"requestId": "primary"}),
        call("Fetch.continueResponse", {"requestId": "primary"}),
    ]
    assert execution.metadata["adscore_402_response"] == {
        "body": "read", "media_type": "application/json", "code": 4021,
        "reason": "insufficient_balance", "decoded_body_bytes": len(body),
    }
    assert execution.metadata["adscore_measurement_referrer_origin"] == "https://fixture.example"
    assert browser._summarize_adscore_error_body("text/plain", b"x" * (browser._ADSCORE_ERROR_BODY_LIMIT + 1)) == {
        "body": "skipped", "media_type": "text/plain",
    }
    referrer_error = browser._summarize_adscore_error_body(
        "application/json",
        b'{"error":{"message":"Referrer does not match the configured pattern https://fixture.example/?token=secret-value"}}',
    )
    assert referrer_error["referrer_message"] == "referrer does not match the configured pattern [redacted]"
    assert "secret-value" not in str(referrer_error)


async def test_allowed_controlled_ping_403_is_recorded_without_stopping_fixture():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    events = {}
    routes = {}

    async def register(pattern, callback):
        routes[pattern] = callback

    frame = Mock(url="https://fixture.example/lab/research?context=test-attempt")
    page = SimpleNamespace(main_frame=frame, on=lambda event, callback: events.setdefault(event, []).append(callback))
    context = SimpleNamespace(route=register, route_web_socket=AsyncMock())
    session = SimpleNamespace(on=Mock(), send=AsyncMock())
    execution = browser.Execution(
        "https://fixture.example", {"https://c.adsco.re"}, controlled_adscore_ping_context="test-attempt"
    )
    await browser._attach_page_policy(context, page, session, execution)
    ping = Mock(
        url="https://c.adsco.re/favicon.ico", method="POST", resource_type="ping",
        is_navigation_request=lambda: False, frame=frame, redirected_from=None, service_worker=None,
    )
    route = SimpleNamespace(request=ping, continue_=AsyncMock(), abort=AsyncMock())
    await routes["**/*"](route)
    route.continue_.assert_awaited_once()
    for callback in events["response"]:
        callback(SimpleNamespace(request=ping, url=ping.url, status=403))
    assert not execution.block.is_set()
    assert execution.metadata["adscore_request_failures"][0]["status"] == 403
    measurement = Mock(url="https://c.adsco.re/p", method="POST", resource_type="fetch")
    for callback in events["response"]:
        callback(SimpleNamespace(request=measurement, url=measurement.url, status=403))
    assert execution.block.is_set()


async def test_checkpoint_without_completed_assertion_is_not_recorded():
    execution = browser.Execution("https://site.example", set(), active_step=2)
    with pytest.raises(browser.StepFailed, match="successful assertion"):
        await browser._execute_step(None, {"action": "checkpoint", "name": "unconfirmed"}, execution)
    assert not execution.metadata.get("checkpoints")


async def test_failure_before_browser_launch_confirms_no_runtime(monkeypatch):
    def unexpected_launch(*args, **kwargs):
        pytest.fail("Browser launch was attempted")

    async def emit(payload):
        if payload["kind"] == "launching":
            raise RuntimeError("Activity recording failed")

    monkeypatch.setattr(browser, "launch_adapter", unexpected_launch)
    result = await execute_workflow(
        origin=network.FIXTURE_ORIGIN,
        resource_origins=[],
        cancel=never_cancel,
        emit=emit,
        steps=[{"action": "navigate", "url": "/lab"}],
    )
    assert result.outcome == "failed"
    assert result.metadata["runtime_stopped"] is True
    assert result.metadata["steps_completed"] == 0


async def test_adapter_rejection_does_not_reserve_runtime_capacity(monkeypatch):
    monkeypatch.setattr(adapters, "adapter_capabilities", lambda: [])
    result = await execute_workflow(
        origin=network.FIXTURE_ORIGIN,
        resource_origins=[],
        cancel=never_cancel,
        steps=[{"action": "navigate", "url": "/lab"}],
    )
    assert result.outcome == "failed"
    assert result.metadata["runtime_stopped"] is True
    assert result.metadata["adapter_status"] == "unsupported_adapter"


@pytest.mark.integration
async def test_absence_failure_guard_stops_before_submitting(chromium, fixture):
    origin, visited, _ = fixture
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        cancel=never_cancel,
        steps=[
            {"action": "navigate", "url": "/lab/form"},
            {"action": "assert_not_visible", "selector": "#submit", "timeout_ms": 100},
            {"action": "click", "selector": "#submit"},
        ],
    )
    assert result.outcome == "failed"
    assert not any(method == "POST" for method, _, _ in visited)
    assert result.metadata["steps_completed"] == 1


@pytest.mark.integration
async def test_unregistered_absolute_navigation_stops_without_issuing_a_request(chromium, fixture):
    origin, visited, _ = fixture
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        navigation_origins=["https://checkout.example"],
        cancel=never_cancel,
        steps=[
            {"action": "navigate", "url": "https://unregistered.example/form"},
            {"action": "assert_visible", "selector": "#success"},
        ],
    )
    assert result.outcome == "blocked"
    assert result.error == "Navigation outside the registered target scope (https://unregistered.example)"
    assert not visited


@pytest.mark.integration
async def test_checkpoint_records_confirmed_progress_without_allowing_action_replay(chromium, fixture):
    origin, visited, _ = fixture
    events = []

    async def record(payload):
        events.append(payload)

    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        cancel=never_cancel,
        emit=record,
        steps=[
            {"action": "navigate", "url": "/lab/form"},
            {"action": "assert_not_visible", "selector": "#failure"},
            {"action": "fill", "selector": "#name", "value": "checkpoint"},
            {"action": "click", "selector": "#submit"},
            {"action": "assert_text", "selector": "#success", "value": "Sample saved"},
            {"action": "checkpoint", "name": "submission-confirmed"},
            {"action": "assert_not_visible", "selector": "#failure"},
        ],
    )
    assert result.outcome == "completed", result
    assert result.started_action
    assert result.metadata["non_idempotent_action_started"] is True
    checkpoint = result.metadata["checkpoints"][0]
    assert checkpoint == {
        "name": "submission-confirmed",
        "step": 6,
        "assertion_step": 5,
        "non_idempotent_action_started": True,
        "replay_allowed": False,
    }
    assert [event for event in events if event["kind"] == "checkpoint_reached"] == [
        {"kind": "checkpoint_reached", **checkpoint}
    ]
    assert len([request for request in visited if request[0] == "POST"]) == 1


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
    readiness = result.metadata["workflow_readiness"]
    for action in ("fill", "select", "click", "wait_for", "assert_text"):
        assert readiness[action]["checks"] == readiness[action]["ready"] == 1
        assert readiness[action]["elapsed_ms"] >= 0
    assert "#name" not in repr(readiness) and "sample-only" not in repr(readiness)


@pytest.mark.integration
async def test_click_waits_for_actionability_before_recording_dispatch(chromium, fixture):
    origin, visited, _ = fixture
    events = []

    async def emit(event):
        events.append(event)

    result = await execute_workflow(
        origin=origin, resource_origins=[], cancel=never_cancel, emit=emit,
        steps=[
            {"action": "navigate", "url": "/lab?disabled-action"},
            {"action": "click", "selector": "#private-readiness-control", "timeout_ms": 300},
        ],
    )
    assert result.outcome == "failed", result
    assert result.metadata["causal_failure"]["reason"] == "selector_timeout"
    timing = result.metadata["workflow_readiness"]["click"]
    assert timing["checks"] == 1 and timing["ready"] == 0 and timing["elapsed_ms"] > 0
    assert "non_idempotent_action_started" not in result.metadata
    assert "private-readiness-control" not in repr(result.metadata)
    assert all(event.get("non_idempotent") is not True for event in events)
    assert visited == [("GET", "/lab?disabled-action", b"")]


@pytest.mark.parametrize("action", ["click", "expect_popup"])
async def test_readiness_and_dispatch_share_the_step_deadline(monkeypatch, action):
    clock = Mock(return_value=100.0)
    monkeypatch.setattr(browser, "monotonic", clock)
    calls = []

    async def click(*, timeout, trial=False):
        calls.append((trial, timeout))
        if trial:
            clock.return_value += 0.25

    page = Mock(locator=Mock(return_value=Mock(click=click)))

    async def popup_page():
        return Mock(opener=AsyncMock(return_value=page), url="about:blank")

    @asynccontextmanager
    async def expect_popup(*, timeout):
        assert timeout == 750
        yield Mock(value=popup_page())

    page.expect_popup = expect_popup
    execution = browser.Execution(network.FIXTURE_ORIGIN, set(), attach_page=AsyncMock())
    await browser._execute_step(
        page, {"action": action, "selector": "#open", "tab": "form", "timeout_ms": 1000}, execution,
    )

    assert calls == [(True, 1000), (False, 750)]
    assert execution.metadata["workflow_readiness"][action] == {"checks": 1, "ready": 1, "elapsed_ms": 250}


@pytest.mark.integration
async def test_navigate_waits_for_delayed_client_redirect_before_next_step(chromium, fixture):
    origin, visited, _ = fixture
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        steps=[
            {"action": "navigate", "url": "/lab?delayed-redirect", "post_navigation_wait_ms": 1000},
            {"action": "assert_text", "selector": "#redirect-state", "value": "Redirect complete"},
        ],
        cancel=never_cancel,
    )
    assert result.outcome == "completed", result
    assert result.metadata["steps_completed"] == 2
    assert visited == [("GET", "/lab?delayed-redirect", b""), ("GET", "/lab/form?redirected", b"")]


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
    assert result.metadata["causal_failure"]["reason"] == (
        "unexpected_redirect" if error == "outside" else "site_denial"
    )
    assert visited == [("GET", path, b"")]


@pytest.mark.integration
async def test_chromium_reports_allowed_navigation_failure_without_running_next_step(chromium, fixture):
    origin, visited, _ = fixture
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        steps=[
            {"action": "navigate", "url": "/lab?delayed-empty-response", "post_navigation_wait_ms": 1000},
            {"action": "assert_not_visible", "selector": "#any-marker"},
        ],
        cancel=never_cancel,
    )
    assert result.outcome == "failed", result
    assert result.error == f"Chromium could not load {origin} (net::ERR_EMPTY_RESPONSE)"
    assert result.metadata["navigation_failure"] == {"origin": origin, "error": "net::ERR_EMPTY_RESPONSE"}
    assert result.metadata["failure_kind"] == "navigation_failed"
    assert result.metadata["causal_failure"]["reason"] == "navigation_failed"
    assert result.metadata["steps_completed"] == 0
    assert ("GET", "/lab/form?empty-response", b"") in visited


@pytest.mark.integration
@pytest.mark.parametrize("status", [404, 503])
async def test_unsuccessful_main_document_after_submit_stops_for_reconciliation(chromium, fixture, status):
    origin, visited, _ = fixture
    result = await execute_workflow(
        origin=origin, resource_origins=[], cancel=never_cancel,
        steps=[
            {"action": "navigate", "url": f"/lab?post-http-failure={status}"},
            {"action": "click", "selector": "#submit"},
            {"action": "navigate", "url": "/lab/finish"},
        ],
    )
    assert result.outcome == "failed"
    assert result.metadata["needs_reconciliation"]
    failure = result.metadata["causal_failure"]
    assert failure["reason"] == "main_document_http_error"
    assert (failure["status"], failure["origin"], failure["action"], failure["step"]) == (status, origin, "click", 2)
    assert visited == [
        ("GET", f"/lab?post-http-failure={status}", b""),
        ("POST", f"/lab/form?http-failure={status}", b""),
    ]


@pytest.mark.integration
async def test_subresource_http_error_does_not_stop_workflow(chromium, fixture):
    origin, visited, _ = fixture
    result = await execute_workflow(
        origin=origin, resource_origins=[], cancel=never_cancel,
        steps=[
            {"action": "navigate", "url": "/lab?subresource-error"},
            {"action": "assert_visible", "selector": "#submit"},
        ],
    )
    assert result.outcome == "completed", result
    assert "causal_failure" not in result.metadata
    assert ("GET", "/lab/form?http-failure=404", b"") in visited


@pytest.mark.integration
@pytest.mark.parametrize(
    ("path", "action", "selector", "expected_outcome", "reason", "visited_paths"),
    [
        (
            "challenge-link", "click", "#verify", "blocked", "site_challenge",
            ["/lab?challenge-link", "/lab/form?challenge"],
        ),
        ("dialog", "click", "#prompt", "failed", "unexpected_dialog", ["/lab?dialog"]),
        ("normal", "wait_for", "#absent", "failed", "selector_timeout", ["/lab?normal"]),
    ],
)
async def test_browser_failures_keep_first_cause_and_stop_followup(
    chromium, fixture, path, action, selector, expected_outcome, reason, visited_paths,
):
    origin, visited, _ = fixture
    result = await execute_workflow(
        origin=origin, resource_origins=[], cancel=never_cancel,
        steps=[
            {"action": "navigate", "url": f"/lab?{path}"},
            {"action": action, "selector": selector, "timeout_ms": 1000},
            {"action": "navigate", "url": "/lab/finish"},
        ],
    )
    assert result.outcome == expected_outcome, result
    failure = result.metadata["causal_failure"]
    assert (failure["reason"], failure["phase"], failure["action"], failure["step"]) == (
        reason, "workflow", action, 2,
    )
    assert [url for _, url, _ in visited] == visited_paths
    assert "private-dialog-text" not in str(result.metadata) and "private-default" not in str(result.metadata)
    if reason == "unexpected_dialog":
        assert failure["dialog_type"] == "prompt"
    if reason == "site_challenge":
        assert result.metadata["failure_kind"] == "site_denial"
        assert not result.metadata["transport_events"]


@pytest.mark.integration
async def test_page_crash_interrupts_selector_wait(chromium, fixture, monkeypatch):
    origin, visited, _ = fixture
    launch = browser.launch_adapter
    active_runtime = None
    crash_task = None

    @asynccontextmanager
    async def remember_runtime(*args, **kwargs):
        nonlocal active_runtime
        async with launch(*args, **kwargs) as runtime:
            active_runtime = runtime
            yield runtime

    async def emit(event):
        nonlocal crash_task
        if event["kind"] == "workflow_step" and event["action"] == "wait_for":
            context = active_runtime.context
            session = await context.new_cdp_session(context.pages[0])
            crash_task = asyncio.create_task(session.send("Page.crash"))

    monkeypatch.setattr(browser, "launch_adapter", remember_runtime)
    try:
        result = await execute_workflow(
            origin=origin, resource_origins=[], cancel=never_cancel, emit=emit,
            steps=[
                {"action": "navigate", "url": "/lab"},
                {"action": "wait_for", "selector": "#absent"},
                {"action": "navigate", "url": "/lab/form"},
            ],
        )
    finally:
        if crash_task is not None:
            crash_task.cancel()
            await asyncio.gather(crash_task, return_exceptions=True)
    assert result.outcome == "failed", result
    assert result.metadata["causal_failure"]["reason"] == "browser_crash"
    assert result.metadata["causal_failure"]["step"] == 2
    assert result.metadata["runtime_stopped"]
    assert visited == [("GET", "/lab", b"")]


@pytest.mark.integration
async def test_chromium_request_settings_do_not_grant_redirect_scope(chromium, fixture, monkeypatch):
    origin, visited, _ = fixture

    async def unexpected_lookup(*_args, **_kwargs):
        pytest.fail("An unregistered hostname reached DNS")

    monkeypatch.setattr(network, "resolve_public_ipv4", unexpected_lookup)
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        user_agent="ABRL Compatibility Probe/1.0",
        referrer=origin + "/lab?source=compatibility-probe",
        steps=[{"action": "navigate", "url": "/lab?outside"}],
        cancel=never_cancel,
    )
    assert result.outcome == "blocked", result
    assert result.error == "Navigation outside the registered target scope (https://outside.example)"
    assert result.metadata["blocked_navigation"] == {
        "frame": "top_level",
        "origin": "https://outside.example",
        "redirect_status": 302,
        "source_origin": origin,
        "trigger": "http_redirect",
    }
    assert visited == [("GET", "/lab?outside", b"")]


@pytest.mark.integration
async def test_instrumented_trace_records_http_redirect_and_local_resource_block(chromium, fixture, monkeypatch):
    origin, _, _ = fixture

    async def unexpected_lookup(*_args, **_kwargs):
        pytest.fail("An unregistered hostname reached DNS")

    monkeypatch.setattr(network, "resolve_public_ipv4", unexpected_lookup)
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        steps=[
            {"action": "navigate", "url": "/lab?network-trace"},
            {"action": "assert_text", "selector": "#trace", "value": "Blocked"},
        ],
        cancel=never_cancel,
        observation_context="private-context",
    )
    assert result.outcome == "completed", result
    trace = result.metadata["instrumented_network_trace"]
    first, second = [entry for entry in trace["requests"] if entry.get("top_level")]
    assert (first["url"], first["status"]) == (f"{origin}/lab", 302)
    assert (second["url"], second["status"], second["redirected_from"]) == (f"{origin}/lab/form", 200, first["id"])
    blocked_url = "https://unregistered.example/[redacted]"
    assert any(
        entry["url"] == blocked_url and entry["resource_type"] == "fetch" and "error" in entry and "status" not in entry
        for entry in trace["requests"]
    )
    assert any(entry["url"] == blocked_url and entry["reason"] == "target_scope" for entry in trace["policy_blocks"])
    assert "private-" not in json.dumps(trace)


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


@pytest.mark.integration
async def test_persistent_profile_relaunch_and_independent_sessions(chromium, fixture, tmp_path):
    origin, _, _ = fixture

    async def run(path, marker, expected):
        steps = [
            {"action": "navigate", "url": f"/lab?profile={marker}"},
            {"action": "assert_text", "selector": "#state", "value": expected},
        ]
        if marker == "read":
            steps.append({"action": "assert_text", "selector": "#profile-cookie", "value": expected})
        return await execute_workflow(
            origin=origin,
            resource_origins=[],
            cancel=never_cancel,
            profile_dir=str(path) if path else None,
            device_preset="phone",
            steps=steps,
        )

    # Simultaneous assignments share the workflow but never profile storage.
    first, second = await asyncio.gather(
        run(tmp_path / "first", "alpha", "alpha"), run(tmp_path / "second", "beta", "beta")
    )
    assert first.outcome == second.outcome == "completed", (first, second)
    assert first.metadata["profile_saved"] and second.metadata["profile_saved"]
    assert first.metadata["profile_launch_state"] == second.metadata["profile_launch_state"] == "initialized"
    expected_viewport = {"checks": 1, "matched": 1}
    assert first.metadata["viewport_conformance"] == second.metadata["viewport_conformance"] == expected_viewport
    assert first.metadata["runtime_stopped"] and second.metadata["runtime_stopped"]
    for folder, expected in (("first", "alpha"), ("second", "beta")):
        restored = await run(tmp_path / folder, "read", expected)
        assert restored.outcome == "completed", restored
        assert restored.metadata["profile_saved"]
        assert restored.metadata["profile_launch_state"] == "restored"
        assert restored.metadata["browser_version"]
        assert restored.metadata["viewport_conformance"] == expected_viewport
    fresh = await run(None, "read", "empty")
    assert fresh.outcome == "completed", fresh
    assert not fresh.metadata["profile_saved"]
    assert fresh.metadata["profile_launch_state"] == "fresh"
    assert fresh.metadata["runtime_stopped"]


@pytest.mark.integration
async def test_automated_preparation_is_seeded_bounded_and_labelled(chromium, fixture):
    origin, _, _ = fixture
    events = []

    async def emit(event):
        events.append(event)

    config = {"mode": "automated", "seed": 27, "min_seconds": 0.01, "max_seconds": 0.01}
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        cancel=never_cancel,
        preparation=config,
        emit=emit,
        steps=[{"action": "navigate", "url": "/lab"}, {"action": "assert_visible", "selector": "#name"}],
    )
    assert result.outcome == "completed", result
    assert result.metadata["ground_truth"] == "automated"
    preparation = result.metadata["preparation"]
    assert preparation["executed"] == browser.preparation_plan(config)["actions"]
    assert preparation["ground_truth"] == "automated"
    assert any(event["kind"] == "preparation_action" for event in events)
    assert events[-1]["kind"] == "browser_closed"


@pytest.mark.integration
@pytest.mark.parametrize("adapter", ["playwright", "manual_chrome"])
async def test_manual_preparation_pauses_automation_until_operator_handoff(
    chromium, fixture, monkeypatch, adapter
):
    origin, visited, _ = fixture
    commands = [{"command": "type", "text": "operator private text"}, {"command": "continue"}]
    events = []

    async def control():
        assert visited == [("GET", "/lab", b"")]  # Workflow has not navigated yet.
        return commands.pop(0)

    async def emit(event):
        events.append(event)

    launch = browser.launch_adapter

    def use_test_browser(_adapter, proxy_url, profile_dir, metadata, **options):
        return launch("playwright", proxy_url, profile_dir, metadata, **options)

    if adapter == "manual_chrome":
        monkeypatch.setattr(browser, "launch_adapter", use_test_browser)
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        cancel=never_cancel,
        adapter=adapter,
        preparation={"mode": "manual"} if adapter == "playwright" else None,
        control=control,
        emit=emit,
        steps=[{"action": "navigate", "url": "/lab"}, {"action": "assert_visible", "selector": "#name"}],
    )
    assert result.outcome == "completed", result
    assert result.metadata["ground_truth"] == ("mixed" if adapter == "playwright" else "human")
    assert result.metadata["time_budgets"]["preparation_seconds"] == 3600
    assert result.metadata["manual_actions"] == 1
    assert any(event["kind"] == "manual_frame" for event in events)
    assert "operator private text" not in str([e for e in events if e["kind"] != "manual_frame"])
    kinds = [event["kind"] for event in events]
    assert kinds.index("manual_handoff") < kinds.index("workflow_step")


async def test_manual_requires_operator_before_launch():
    result = await execute_workflow(
        origin="https://site.example",
        resource_origins=[],
        cancel=never_cancel,
        preparation={"mode": "manual"},
        steps=[{"action": "navigate", "url": "/"}],
    )
    assert result.outcome == "failed"
    assert "operator" in result.error
    assert not result.started_action


@pytest.mark.integration
async def test_action_boundary_is_emitted_before_form_submission(chromium, fixture):
    origin, visited, _ = fixture
    events = []

    async def emit(event):
        events.append(event)
        if event["kind"] == "action_started" and event.get("action") == "click":
            assert not any(method == "POST" for method, _, _ in visited)

    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        cancel=never_cancel,
        emit=emit,
        steps=[
            {"action": "navigate", "url": "/lab/form"},
            {"action": "click", "selector": "#submit"},
            {"action": "assert_text", "selector": "#success", "value": "not present"},
        ],
    )
    assert result.outcome == "failed"
    assert result.metadata["needs_reconciliation"]
    assert result.metadata["runtime_stopped"]
    assert any(e["kind"] == "action_started" and e.get("non_idempotent") for e in events)


@pytest.mark.integration
async def test_profile_compatibility_failure_never_navigates_or_publishes(chromium, fixture, tmp_path):
    origin, visited, _ = fixture
    profile = str(tmp_path / "profile")
    initialized = await execute_workflow(
        origin=origin, resource_origins=[], cancel=never_cancel, profile_dir=profile,
        steps=[{"action": "navigate", "url": "/lab"}],
    )
    assert initialized.outcome == "completed" and initialized.metadata["profile_saved"]
    visited.clear()
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        cancel=never_cancel,
        profile_dir=profile,
        expected_compatibility={
            "adapter": "playwright",
            "browser_version": "Chrome/1.0",
            "os": browser.platform.system(),
            "architecture": browser.platform.machine(),
        },
        steps=[{"action": "navigate", "url": "/lab"}],
    )
    assert result.outcome == "failed"
    assert "compatibility" in result.error
    assert not result.started_action and not visited
    assert result.metadata["runtime_stopped"]
    assert not result.metadata["profile_saved"]


@pytest.mark.parametrize(
    ("failed_capture", "saved"), [("screenshot", "console.json"), ("console", "final.png")]
)
async def test_screenshot_and_console_artifacts_survive_the_other_failure(failed_capture, saved):
    artifacts = []

    async def emit(event):
        if failed_capture == "console" and event["name"] == "console.json":
            raise RuntimeError("Unable to store console")
        artifacts.append(event)

    execution = browser.Execution("https://site.example", set(), emit=emit)
    page = Mock(screenshot=AsyncMock(return_value=b"PNG"))
    if failed_capture == "screenshot":
        page.screenshot.side_effect = RuntimeError("Unable to capture screenshot")
    await browser._capture_artifacts(page, execution, [], "all")
    assert [artifact["name"] for artifact in artifacts] == [saved]
    assert execution.metadata["artifact_capture"] == "partial"
    assert failed_capture in execution.metadata["artifact_capture_error"]


@pytest.mark.integration
async def test_opt_in_artifacts_capture_and_redact_secrets(chromium, fixture, monkeypatch):
    import base64
    import json

    origin, _, _ = fixture
    monkeypatch.setenv("ABRL_SECRET_PRIVATE", "private-filled-value")
    artifacts = []

    async def emit(event):
        if event["kind"] == "artifact":
            artifacts.append(event)

    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        cancel=never_cancel,
        emit=emit,
        artifact_policy="all",
        steps=[
            {"action": "navigate", "url": "/lab?console"},
            {"action": "fill", "selector": "#name", "secret_ref": "PRIVATE"},
            {"action": "assert_visible", "selector": "#name"},
        ],
    )
    assert result.outcome == "completed", result
    assert result.metadata["artifact_capture"] == "completed"
    assert {item["name"] for item in artifacts} == {"console.json", "final.png"}
    screenshot = next(item for item in artifacts if item["name"] == "final.png")
    assert base64.b64decode(screenshot["data_base64"]).startswith(b"\x89PNG")
    console = next(item for item in artifacts if item["name"] == "console.json")
    messages = json.loads(base64.b64decode(console["data_base64"]))
    assert "private-filled-value" not in str(messages)
    assert "[redacted]" in str(messages)


@pytest.mark.integration
async def test_workflow_timeout_does_not_borrow_unused_preparation_budget(chromium, fixture):
    origin, _, _ = fixture

    async def handoff():
        return {"command": "continue"}

    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        cancel=never_cancel,
        preparation={"mode": "manual", "idle_timeout_seconds": 5, "lifetime_seconds": 30},
        control=handoff,
        timeout_seconds=0.1,
        steps=[
            {"action": "navigate", "url": "/lab"},
            {"action": "wait_for", "selector": "#never-present", "timeout_ms": 10000},
        ],
    )
    assert result.outcome == "failed" and result.error == "Workflow exceeded its time budget"
    assert result.elapsed_ms < 5000
    assert result.metadata["runtime_stopped"]
