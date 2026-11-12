import asyncio
import base64
import socket
from contextlib import asynccontextmanager, suppress

import httpx
import pytest
import uvicorn
from test_browser import chromium as chromium
from test_browser import fixture as fixture
from test_browser import never_cancel

from abrl.browser import execute_workflow
from abrl.remote_browser import RemoteBroker, RuntimeLease, capability_token, create_browser_app


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
async def test_remote_persistent_encrypted_transfer_roundtrip(chromium, fixture, tmp_path):
    origin, _, _ = fixture
    directory = tmp_path / "restored-profile"
    directory.mkdir()
    compatibility = None
    for epoch, marker in ((1, "saved"), (2, "read")):
        async with runtime(origin, epoch=epoch) as (connection, service):
            result = await execute_workflow(
                origin=origin,
                resource_origins=[],
                cancel=never_cancel,
                remote=connection,
                profile_dir=str(directory),
                expected_compatibility=compatibility,
                steps=[
                    {"action": "navigate", "url": f"/lab?profile={marker}"},
                    {"action": "assert_text", "selector": "#state", "value": "saved"},
                ],
            )
            assert result.outcome == "completed", result
            assert result.metadata["runtime_stopped"] and result.metadata["profile_saved"], result
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


def test_vendor_bootstrap_scope_is_separate_from_target_traffic(fixture):
    origin, _, _ = fixture
    broker = RemoteBroker(
        {"origin": origin, "vendor_control_origins": ["https://cloakbrowser.dev"]},
        RuntimeLease("a" * 40, "vendor-session", 1),
    )
    broker.upstream = object()  # Routing policy needs no provider credentials/network.
    assert broker.forwarding_allowed("CONNECT", "cloakbrowser.dev:443")
    assert not broker.forwarding_allowed("CONNECT", "other.example:443")
    assert not broker.forwarding_allowed("GET", origin + "/lab")
    assert not broker.forwarding_allowed("GET", "https://cloakbrowser.dev/")
    assert not broker.uses_upstream("https://cloakbrowser.dev")
    assert broker.uses_upstream("https://target.example")
    assert broker.transport_summary()["attempts"] == 0


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
