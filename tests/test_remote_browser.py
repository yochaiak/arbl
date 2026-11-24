import asyncio
import base64
import socket
from contextlib import asynccontextmanager, suppress

import httpx
import pytest
import uvicorn
from test_browser import check_mobile_session, check_session_request_settings, never_cancel
from test_browser import chromium as chromium
from test_browser import fixture as fixture
from test_browser import https_asset_site as https_asset_site

from abrl.browser import execute_workflow
from abrl.config import CLOAKBROWSER_GEOIP_ORIGINS, CLOAKBROWSER_LICENSE_ORIGIN
from abrl.network import PROXY_DESTINATION_DENIED, NetworkDenied
from abrl.remote_browser import (
    BrowserService,
    RemoteBroker,
    RuntimeLease,
    capability_token,
    create_browser_app,
    remote_adapter,
    remote_proxy_check_ip,
)


def headers(token="a" * 40, session_id="session-remote", epoch=1):
    return {"Authorization": "Bearer " + token, "X-ABRL-Session": session_id, "X-ABRL-Epoch": str(epoch)}


@asynccontextmanager
async def runtime(origin, *, epoch=1, traffic_budget_bytes=104857600):
    token, session_id = "a" * 40, "session-remote"
    proxy_token = capability_token(token, "proxy-access")
    broker_lease = RuntimeLease(
        capability_token(token, "broker-control"), session_id, epoch, proxy_token=proxy_token
    )
    async with RemoteBroker(
        {"origin": origin, "traffic_budget_bytes": traffic_budget_bytes},
        broker_lease,
        bind_host="127.0.0.1",
        bind_port=0,
    ) as broker:
        watchdog = asyncio.create_task(broker.watchdog())
        browser_lease = RuntimeLease(
            capability_token(token, "browser-control"), session_id, epoch, proxy_token=proxy_token
        )
        app = create_browser_app({"proxy_url": broker.url}, browser_lease)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on", access_log=False))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(500):
                if server.started:
                    break
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
            assert server.started
            yield (
                {
                    "browser_url": f"http://127.0.0.1:{port}",
                    "proxy_url": broker.url,
                    "token": token,
                    "session_id": session_id,
                    "epoch": epoch,
                },
                app.state.runtime,
            )
        finally:
            server.should_exit = True
            await serving
            listener.close()
            watchdog.cancel()
            with suppress(asyncio.CancelledError):
                await watchdog


async def proxy_request(url, request):
    from urllib.parse import urlsplit

    address = urlsplit(url)
    reader, writer = await asyncio.open_connection(address.hostname, address.port)
    writer.write(request)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    await writer.wait_closed()
    return response


def test_remote_broker_retains_latest_destination_denial_when_transport_history_is_trimmed():
    lease = RuntimeLease("a" * 40, "session-remote", 1)
    broker = RemoteBroker({"origin": "https://authorized.example"}, lease)
    latest_denial = {
        "reason": PROXY_DESTINATION_DENIED,
        "status": 403,
        "requested_authority": "asset.example:443",
        "upstream_authority": "93.184.216.34:443",
    }
    previous_denial = {**latest_denial, "requested_authority": "earlier.example:443"}
    later = [
        {"reason": "proxy_tunnel_failed", "status": 502, "sequence": index} for index in range(150)
    ]
    broker.transport_events[:] = [previous_denial, latest_denial, *later]

    assert broker.state()["transport_events"] == [latest_denial, *later[-127:]]
    assert broker.transport_events == [previous_denial, latest_denial, *later]


@pytest.mark.parametrize("provider", ["oxylabs", "decodo"])
async def test_remote_proxy_check_requires_controller_and_stopped_route(monkeypatch, provider):
    monkeypatch.setenv("ABRL_SECRET_PROXY_USER", "customer-test" if provider == "oxylabs" else "testuser01")
    monkeypatch.setenv("ABRL_SECRET_PROXY_PASSWORD", "test-secret")
    proxy = {
        "product": "residential", "host": "pr.oxylabs.io", "port": 7777,
        "username_ref": "PROXY_USER", "password_ref": "PROXY_PASSWORD", "provider_resolved_ack": True,
    }
    if provider == "decodo":
        proxy.update(
            provider="decodo", host="gate.decodo.com", port=7000,
            country="US", session_key="test123", ttl_minutes=10,
        )
    master = "a" * 40
    broker_token = capability_token(master, "broker-control")
    lease = RuntimeLease(broker_token, "session-remote", 1)
    calls = []

    async def check(binding):
        calls.append(binding)
        await asyncio.sleep(0.3)
        return "8.8.4.4"

    monkeypatch.setattr("abrl.remote_browser.check_proxy_ip", check)
    async with RemoteBroker(
        {"origin": "https://authorized.example", "proxy": proxy, "proxy_check": True},
        lease, bind_host="127.0.0.1", bind_port=0,
    ) as broker:
        connection = {"proxy_url": broker.url, "token": master, "session_id": lease.session_id, "epoch": 1}
        assert await remote_proxy_check_ip(connection) is None
        assert calls == []

        async with httpx.AsyncClient(trust_env=False) as client:
            unauthorized = await client.post(broker.url + "/__abrl__/proxy-check", headers=headers())
            assert unauthorized.status_code == 401
            watchdog = asyncio.create_task(broker.watchdog())
            stop = await client.post(broker.url + "/__abrl__/stop", headers=headers(token=broker_token))
            assert stop.json()["stopped"]

        assert await remote_proxy_check_ip(connection) == "8.8.4.4"
        assert await remote_proxy_check_ip(connection) is None
        await watchdog
        assert calls == [broker.upstream]
        assert broker.upstream.provider == provider
        assert broker.transport_summary()["attempts"] == 0


async def test_remote_broker_auth_scope_pause_and_expiry(fixture):
    origin, visited, _ = fixture
    lease = RuntimeLease("a" * 40, "session-remote", 1, seconds=1)
    async with RemoteBroker({"origin": origin}, lease, bind_host="127.0.0.1", bind_port=0) as broker:
        assert (
            await proxy_request(broker.url, f"GET {origin}/lab HTTP/1.1\r\nHost: fixture\r\n\r\n".encode())
        ).startswith(b"HTTP/1.1 407")
        authorization = b"Proxy-Authorization: Basic " + base64.b64encode(b"abrl-session:" + b"a" * 40)
        request = f"GET {origin}/lab HTTP/1.1\r\nHost: fixture\r\n".encode() + authorization + b"\r\n\r\n"
        assert (await proxy_request(broker.url, request)).startswith(b"HTTP/1.1 503")
        async with httpx.AsyncClient(headers=headers(), trust_env=False) as client:
            assert (
                await client.post(
                    broker.url + "/__abrl__/activate", headers={**headers(), "X-ABRL-Epoch": "2"}
                )
            ).status_code == 401
            assert (await client.post(broker.url + "/__abrl__/activate")).status_code == 200
            assert (await proxy_request(broker.url, request)).startswith(b"HTTP/1.1 200")
            forbidden = b"CONNECT outside.example:443 HTTP/1.1\r\n" + authorization + b"\r\n\r\n"
            assert (await proxy_request(broker.url, forbidden)).startswith(b"HTTP/1.1 403")
            lease.deadline = 0
            assert (await client.post(broker.url + "/__abrl__/lease")).status_code == 409
            assert (await proxy_request(broker.url, request)).startswith(b"HTTP/1.1 503")
            assert (await client.post(broker.url + "/__abrl__/activate")).status_code == 409
    assert visited == [("GET", "/lab", b"")]


async def test_proxy_capability_cannot_renew_controller_lease(fixture):
    origin, _, _ = fixture
    master = "a" * 40
    lease = RuntimeLease(
        capability_token(master, "broker-control"),
        "session-remote",
        1,
        proxy_token=capability_token(master, "proxy-access"),
    )
    async with RemoteBroker({"origin": origin}, lease, bind_host="127.0.0.1", bind_port=0) as broker:
        async with httpx.AsyncClient(trust_env=False) as client:
            for purpose in ("proxy-access", "browser-control"):
                response = await client.post(
                    broker.url + "/__abrl__/lease", headers=headers(token=capability_token(master, purpose))
                )
                assert response.status_code == 401
            response = await client.post(
                broker.url + "/__abrl__/lease",
                headers=headers(token=capability_token(master, "broker-control")),
            )
            assert response.status_code == 200


async def test_remote_asset_origin_requires_active_controller_lease_and_public_dns(monkeypatch):
    master = "a" * 40
    asset = "https://assets.example"
    broker_token = capability_token(master, "broker-control")
    proxy_token = capability_token(master, "proxy-access")
    lease = RuntimeLease(broker_token, "session-remote", 1, proxy_token=proxy_token)
    resolved = []

    async def reject_resolution(host, port):
        resolved.append((host, port))
        raise NetworkDenied("Non-public DNS answer")

    monkeypatch.setattr("abrl.network.resolve_public_ipv4", reject_resolution)
    async with RemoteBroker(
        {"origin": "https://site.example"}, lease, bind_host="127.0.0.1", bind_port=0
    ) as broker:
        async with httpx.AsyncClient(trust_env=False) as client:
            async def register(origin=asset, token=broker_token, epoch=1):
                response = await client.post(
                    broker.url + "/__abrl__/resource-origin",
                    headers={**headers(token=token, epoch=epoch), "X-ABRL-Resource-Origin": origin},
                )
                return response.status_code

            assert await register(token=proxy_token) == 401
            assert await register(epoch=2) == 401
            assert await register() == 409
            response = await client.post(broker.url + "/__abrl__/activate", headers=headers(token=broker_token))
            assert response.status_code == 200
            assert await register("https://127.0.0.1") == 400
            assert await register(asset + "/asset.png") == 400

            def connect(host):
                authorization = base64.b64encode(f"abrl-session:{proxy_token}".encode())
                return (
                    f"CONNECT {host}:443 HTTP/1.1\r\n".encode()
                    + b"Proxy-Authorization: Basic " + authorization + b"\r\n\r\n"
                )

            assert (await proxy_request(broker.url, connect("assets.example"))).startswith(b"HTTP/1.1 403")
            assert resolved == []
            deadline = lease.deadline
            assert await register() == 200
            assert await register() == 200
            assert lease.deadline == deadline
            assert (await proxy_request(broker.url, connect("assets.example"))).startswith(b"HTTP/1.1 403")
            assert (await proxy_request(broker.url, connect("other.example"))).startswith(b"HTTP/1.1 403")
            assert resolved == [("assets.example", 443)]
            lease.deadline = 0
            assert await register("https://late.example") == 409
            assert (await proxy_request(broker.url, connect("assets.example"))).startswith(b"HTTP/1.1 503")


@pytest.mark.integration
async def test_remote_adapter_can_admit_asset_origin_after_activation(chromium, fixture):
    origin, _, _ = fixture
    async with runtime(origin) as (connection, _):
        async with remote_adapter(connection, None, {}, None) as running:
            assert not await running.allow_asset_origin("https://assets.example")
            await running.activate()
            assert await running.allow_asset_origin("https://assets.example")
            assert not await running.allow_asset_origin("https://assets.example/image.png")


@pytest.mark.integration
async def test_remote_chromium_loads_unlisted_page_assets(chromium, https_asset_site):
    visited, dns_hosts = https_asset_site
    origin = "https://shop.example"
    async with runtime(origin) as (connection, service):
        result = await execute_workflow(
            origin=origin,
            resource_origins=[],
            cancel=never_cancel,
            remote=connection,
            steps=[
                {"action": "navigate", "url": "/"},
                {"action": "assert_not_visible", "selector": "#style-pending"},
                {"action": "assert_text", "selector": "#image", "value": "Loaded"},
                {"action": "assert_text", "selector": "#module", "value": "Loaded"},
                {"action": "assert_text", "selector": "#private", "value": "Blocked"},
                {"action": "assert_text", "selector": "#api", "value": "Blocked"},
            ],
        )
        assert result.outcome == "completed", result
        assert result.metadata["runtime_stopped"] and service.metadata["runtime_stopped"]
    assert {
        ("assets.example", "GET", "/style.css"),
        ("assets.example", "GET", "/image.svg"),
        ("static.example", "GET", "/image.svg"),
        ("assets.example", "GET", "/module.js"),
    } <= visited
    assert {"private-assets.example", "static.example"} <= dns_hosts
    assert not {"/post", "/get"} & {path for _, _, path in visited}


@pytest.mark.integration
async def test_remote_chromium_real_fresh_workflow_and_cleanup(chromium, fixture):
    origin, visited, _ = fixture
    async with runtime(origin) as (connection, service):
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(connection["browser_url"] + "/json/version")).status_code == 401
        result = await execute_workflow(
            origin=origin,
            resource_origins=[],
            cancel=never_cancel,
            remote=connection,
            steps=[
                {"action": "navigate", "url": "/lab/form"},
                {"action": "fill", "selector": "#name", "value": "remote"},
                {"action": "click", "selector": "#submit"},
                {"action": "assert_text", "selector": "#success", "value": "Sample saved"},
            ],
        )
        assert result.outcome == "completed", result
        assert result.metadata["runtime_stopped"]
        assert service.metadata["runtime_stopped"]
        assert not result.metadata["profile_saved"]
        assert any(method == "POST" for method, _, _ in visited)


@pytest.mark.integration
@pytest.mark.parametrize("persistent", [False, True], ids=["fresh", "persistent"])
async def test_remote_chromium_applies_session_request_settings(chromium, fixture, tmp_path, persistent):
    origin, _, _ = fixture
    profile_dir = tmp_path / "profile" if persistent else None
    if profile_dir:
        profile_dir.mkdir()
    async with runtime(origin) as (connection, service):
        await check_session_request_settings(
            origin, profile_dir=str(profile_dir) if profile_dir else None, remote=connection
        )
        assert service.metadata["runtime_stopped"]


@pytest.mark.integration
async def test_remote_chromium_applies_mobile_preset(chromium, https_asset_site):
    visited, _ = https_asset_site
    origin = "https://shop.example"
    async with runtime(origin) as (connection, service):
        # A CDP-created tab may not inherit the host Playwright context options.
        await check_mobile_session(origin, remote=connection, check_secondary_tab=False)
        assert service.metadata["configured_device_preset"] == "phone"
        assert service.metadata["runtime_stopped"]
    assert visited == {("shop.example", "GET", "/lab?mobile")}


@pytest.mark.integration
async def test_remote_chromium_requires_user_agent_confirmation_before_navigation(
    chromium, fixture, monkeypatch
):
    origin, visited, _ = fixture
    launch = BrowserService.launch
    broker_state = RemoteBroker.state
    forwarding = []

    async def launch_without_user_agent(service, persistent, compatibility, **_options):
        return await launch(service, persistent, compatibility)

    def observe_forwarding(broker):
        state = broker_state(broker)
        forwarding.append(state["forwarding"])
        return state

    monkeypatch.setattr(BrowserService, "launch", launch_without_user_agent)
    monkeypatch.setattr(RemoteBroker, "state", observe_forwarding)
    async with runtime(origin) as (connection, service):
        result = await execute_workflow(
            origin=origin,
            resource_origins=[],
            cancel=never_cancel,
            remote=connection,
            user_agent="ABRL Compatibility Probe/1.0",
            steps=[{"action": "navigate", "url": "/lab"}],
        )
        assert result.outcome == "failed", result
        assert "User-Agent" in result.error
        assert not result.started_action
        assert service.metadata["runtime_stopped"]
    assert forwarding and not any(forwarding)
    assert not visited


@pytest.mark.integration
async def test_remote_persistent_encrypted_transfer_roundtrip(chromium, fixture, tmp_path):
    origin, _, _ = fixture
    directory = tmp_path / "restored-profile"
    directory.mkdir()
    compatibility = None
    for epoch, marker in ((1, "saved"), (2, "read")):
        steps = [
            {"action": "navigate", "url": f"/lab?profile={marker}"},
            {"action": "assert_text", "selector": "#state", "value": "saved"},
        ]
        if marker == "read":
            steps.append({"action": "assert_text", "selector": "#profile-cookie", "value": "saved"})
        async with runtime(origin, epoch=epoch) as (connection, service):
            result = await execute_workflow(
                origin=origin,
                resource_origins=[],
                cancel=never_cancel,
                remote=connection,
                profile_dir=str(directory),
                expected_compatibility=compatibility,
                steps=steps,
            )
            assert result.outcome == "completed", result
            assert result.metadata["runtime_stopped"] and result.metadata["profile_saved"], result
            assert result.metadata["profile_launch_state"] == ("initialized" if epoch == 1 else "restored")
            compatibility = {
                k: result.metadata[k] for k in ("adapter", "browser_version", "os", "architecture")
            }
            archive = service.transfer.root / connection["session_id"] / "output.abrl"
            assert archive.exists() and b"saved" not in archive.read_bytes()
            async with httpx.AsyncClient(
                trust_env=False,
                headers=headers(
                    token=capability_token(connection["token"], "browser-control"), epoch=epoch
                ),
            ) as client:
                assert (await client.post(connection["browser_url"] + "/v1/lease")).status_code == 409


@pytest.mark.integration
async def test_remote_kameleo_cannot_create_when_saved_archive_is_missing(fixture, tmp_path, monkeypatch):
    from abrl import adapters

    monkeypatch.setattr(adapters, "adapter_capabilities", lambda: [{"id": "kameleo", "available": True}])
    origin, visited, _ = fixture
    empty = tmp_path / "restored-without-vendor-archive"
    empty.mkdir()
    async with runtime(origin) as (connection, service):
        service.metadata["adapter"] = "kameleo"
        result = await execute_workflow(
            origin=origin, resource_origins=[], cancel=never_cancel, remote=connection,
            adapter="kameleo", profile_dir=str(empty), expected_compatibility={},
            steps=[{"action": "navigate", "url": "/lab"}],
        )
    assert result.outcome == "failed", result
    assert result.metadata["runtime_stopped"] and not result.metadata["profile_saved"]
    assert result.metadata["causal_failure"]["reason"] == "profile_restore_failed"
    assert not visited and not service.launched


async def test_vendor_bootstrap_scope_is_separate_from_target_traffic(monkeypatch):
    token = "a" * 40
    config = {
        "origin": "https://target.example",
        "vendor_control_origins": [CLOAKBROWSER_LICENSE_ORIGIN],
        "startup_origins": CLOAKBROWSER_GEOIP_ORIGINS,
    }
    async with RemoteBroker(
        config, RuntimeLease(token, "session-remote", 1), bind_host="127.0.0.1", bind_port=0
    ) as broker:
        broker.upstream = object()
        upstream_calls = []

        async def connect_upstream(authority, *, record_failure=True):
            upstream_calls.append((authority, record_failure))
            return None

        async def resolve(host, port):
            return ("93.184.216.34",)

        monkeypatch.setattr(broker, "_upstream_connect", connect_upstream)
        monkeypatch.setattr("abrl.network.resolve_public_ipv4", resolve)
        authorization = b"Proxy-Authorization: Basic " + base64.b64encode(f"abrl-session:{token}".encode())
        lookup = b"CONNECT api.ipify.org:443 HTTP/1.1\r\n"
        target = b"CONNECT target.example:443 HTTP/1.1\r\n"

        assert (await proxy_request(broker.url, lookup + b"\r\n")).startswith(b"HTTP/1.1 407")
        assert (await proxy_request(broker.url, target + authorization + b"\r\n\r\n")).startswith(b"HTTP/1.1 503")
        assert (await proxy_request(broker.url, lookup + authorization + b"\r\n\r\n")).startswith(b"HTTP/1.1 502")
        assert upstream_calls == [("api.ipify.org:443", False)]
        assert broker.transport_summary()["attempts"] == 0
        assert broker.forwarding_allowed("CONNECT", "cloakbrowser.dev:443")
        assert not broker.uses_upstream(CLOAKBROWSER_LICENSE_ORIGIN)

        async with httpx.AsyncClient(headers=headers(token=token), trust_env=False) as client:
            response = await client.post(broker.url + "/__abrl__/activate")
            assert response.status_code == 200
        assert (await proxy_request(broker.url, lookup + authorization + b"\r\n\r\n")).startswith(b"HTTP/1.1 403")
        assert (await proxy_request(broker.url, target + authorization + b"\r\n\r\n")).startswith(b"HTTP/1.1 502")
        assert upstream_calls == [("api.ipify.org:443", False), ("target.example:443", True)]
        assert broker.transport_summary()["attempts"] == 1


def test_runtime_lifetime_caps_renewals(monkeypatch):
    from abrl import remote_browser

    now = [10.0]
    monkeypatch.setattr(remote_browser, "monotonic", lambda: now[0])
    lease = RuntimeLease("a" * 40, "session", 1, seconds=30, lifetime_seconds=40)
    assert lease.deadline == 50
    now[0] = 35
    lease.renew()
    assert lease.deadline == 50
    now[0] = 50
    with pytest.raises(ValueError, match="cannot be resurrected"):
        lease.renew()


async def test_stop_cancels_inflight_vendor_launch_and_waits_for_cleanup(monkeypatch):
    from abrl import remote_browser

    entering, stopped = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def slow_adapter(adapter, proxy_url, directory, metadata, **kwargs):
        try:
            entering.set()
            await asyncio.Event().wait()
            yield  # pragma: no cover - deliberately cancelled while entering
        finally:
            metadata["runtime_stopped"] = True
            stopped.set()

    monkeypatch.setattr(remote_browser, "launch_adapter", slow_adapter)
    service = remote_browser.BrowserService(
        {"proxy_url": "http://127.0.0.1:3128", "adapter": "kameleo"},
        RuntimeLease("a" * 40, "session", 1),
    )
    task = asyncio.create_task(service.launch(False, {}))
    await entering.wait()
    await asyncio.wait_for(service.stop(), 2)
    assert task.cancelled() and stopped.is_set()
    assert service.metadata["runtime_stopped"]
    assert not service.metadata["profile_saved"]


@pytest.mark.parametrize("selected_adapter", ["kameleo", "manual_chrome"])
async def test_remote_vendor_or_native_context_preserves_clean_export_contract(
    monkeypatch, selected_adapter
):

    from abrl import remote_browser
    from abrl.adapters import BrowserRuntime

    events = []

    class Page:
        url = "about:blank"

        async def goto(self, url):
            self.url = url

    page = Page()

    class Context:
        pages = [page]
        browser = object()

        async def new_cdp_session(self, _):
            return self

        async def send(self, command):
            assert command == "Browser.getVersion"
            return {"product": "Chrome/140.0.0"}

    context = Context()

    @asynccontextmanager
    async def vendor_adapter(adapter, proxy_url, directory, metadata, **kwargs):
        assert adapter == selected_adapter
        assert kwargs["proxy_auth"] is None
        events.append("launch")
        metadata.update(engine_version="5.4.0", cdp_endpoint="ws://localhost:5050/playwright/profile")
        try:
            yield BrowserRuntime(context, metadata)
        finally:
            events.extend(["stop", "export"])
            metadata.update(runtime_stopped=True, profile_saved=True)

    monkeypatch.setattr(remote_browser, "launch_adapter", vendor_adapter)
    service = remote_browser.BrowserService(
        {"proxy_url": "http://127.0.0.1:3128", "adapter": selected_adapter},
        RuntimeLease("a" * 40, "session", 1),
    )
    state = await service.launch(True, {})
    assert service.context is context
    assert page.url == state["metadata"]["context_marker"] == "about:blank#abrl-session"
    assert service.backend_ws.endswith("/playwright/profile")
    assert state["metadata"]["browser_version"] == "Chrome/140.0.0"
    await service.stop()
    assert events == ["launch", "stop", "export"]
    assert service.metadata["profile_saved"] and service.metadata["runtime_stopped"]


@pytest.mark.integration
async def test_native_proxy_relay_survives_independent_workflow_scope_interception(chromium, fixture):
    """Credential-free native/vendor browser proxy works with CDP scope policy."""
    from playwright.async_api import async_playwright

    from abrl.adapters import NETWORK_ARGS
    from abrl.browser import Execution, _attach_page_policy
    from abrl.remote_browser import BrokerRelay

    origin, visited, _ = fixture
    lease = RuntimeLease("a" * 40, "session-remote", 1, proxy_token="proxy-capability")
    async with RemoteBroker({"origin": origin}, lease, bind_host="127.0.0.1", bind_port=0) as broker:
        async with httpx.AsyncClient(headers=headers(), trust_env=False) as client:
            assert (await client.post(broker.url + "/__abrl__/activate")).status_code == 200
        relay = await BrokerRelay(broker.url, "proxy-capability").start()
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(
                headless=True, proxy={"server": relay.url}, args=NETWORK_ARGS
            )
            try:
                context = await browser.new_context()
                page = await context.new_page()
                execution = Execution(origin, set())
                session = await context.new_cdp_session(page)
                await _attach_page_policy(
                    context,
                    page,
                    session,
                    execution,
                )
                response = await page.goto(origin + "/lab", timeout=5000)
                assert await page.locator("#name").is_visible(), (
                    response.status,
                    await page.content(),
                    execution.reason,
                )
                assert not execution.block.is_set()
            finally:
                await browser.close()
                await relay.close()
    assert visited == [("GET", "/lab", b"")]
