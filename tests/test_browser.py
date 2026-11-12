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
            if path.startswith("/lab?profile="):
                marker = path.split("=", 1)[1]
                response = (
                    '<p id="state"></p><script>'
                    + (f'localStorage.setItem("marker", "{marker}");' if marker != "read" else "")
                    + 'document.querySelector("#state").textContent = localStorage.getItem("marker") || "empty";'
                    + "</script>"
                )
            if path == "/lab?console":
                response += "<script>console.log('private-filled-value');</script>"
            if path == "/lab?large":
                response = "<p id='large'>" + "x" * 10000 + "</p>"
            if path == "/lab?redirect":
                status, headers = "302 Found", "Location: /lab/form\r\n"
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


async def test_checkpoint_without_completed_assertion_is_not_recorded():
    execution = browser.Execution("https://site.example", set(), active_step=2)
    with pytest.raises(browser.StepFailed, match="successful assertion"):
        await browser._execute_step(None, {"action": "checkpoint", "name": "unconfirmed"}, execution)
    assert not execution.metadata.get("checkpoints")


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
    assert "outside" in result.error
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


@pytest.mark.integration
async def test_persistent_profile_relaunch_and_independent_sessions(chromium, fixture, tmp_path):
    origin, _, _ = fixture

    async def run(path, marker, expected):
        return await execute_workflow(
            origin=origin,
            resource_origins=[],
            cancel=never_cancel,
            profile_dir=str(path) if path else None,
            steps=[
                {"action": "navigate", "url": f"/lab?profile={marker}"},
                {"action": "assert_text", "selector": "#state", "value": expected},
            ],
        )

    # Simultaneous assignments share the workflow but never profile storage.
    first, second = await asyncio.gather(
        run(tmp_path / "first", "alpha", "alpha"), run(tmp_path / "second", "beta", "beta")
    )
    assert first.outcome == second.outcome == "completed", (first, second)
    assert first.metadata["profile_saved"] and second.metadata["profile_saved"]
    assert first.metadata["runtime_stopped"] and second.metadata["runtime_stopped"]
    for folder, expected in (("first", "alpha"), ("second", "beta")):
        restored = await run(tmp_path / folder, "read", expected)
        assert restored.outcome == "completed", restored
        assert restored.metadata["profile_saved"]
        assert restored.metadata["browser_version"]
    fresh = await run(None, "read", "empty")
    assert fresh.outcome == "completed", fresh
    assert not fresh.metadata["profile_saved"]
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
async def test_manual_preparation_pauses_automation_until_operator_handoff(chromium, fixture):
    origin, visited, _ = fixture
    commands = [{"command": "type", "text": "operator private text"}, {"command": "continue"}]
    events = []

    async def control():
        assert visited == [("GET", "/lab", b"")]  # Workflow has not navigated yet.
        return commands.pop(0)

    async def emit(event):
        events.append(event)

    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        cancel=never_cancel,
        preparation={"mode": "manual"},
        control=control,
        emit=emit,
        steps=[{"action": "navigate", "url": "/lab"}, {"action": "assert_visible", "selector": "#name"}],
    )
    assert result.outcome == "completed", result
    assert result.metadata["ground_truth"] == "mixed"
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
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        cancel=never_cancel,
        profile_dir=str(tmp_path / "profile"),
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
