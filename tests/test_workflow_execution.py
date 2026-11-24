import asyncio
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_browser import chromium as chromium
from test_browser import fixture as fixture
from test_browser import never_cancel

from abrl import browser, network
from abrl.schemas import WorkflowInput


def workflow(steps, **settings):
    return WorkflowInput(schema_version=2, name="Owned fixture", steps=steps, **settings).model_dump(
        mode="json"
    )


async def run_workflow(origin, config, **options):
    return await browser.execute_workflow(
        origin=origin,
        resource_origins=[],
        steps=config["steps"],
        workflow=config,
        cancel=never_cancel,
        **options,
    )


@pytest.mark.parametrize(("proxied", "policy_ms", "workflow_ms", "step_ms", "expected"), [
    (False, None, None, None, (5000, "direct_default")),
    (False, 22_000, None, None, (5000, "direct_default")),
    (True, None, None, None, (15_000, "proxy_default")),
    (True, 22_000, None, None, (22_000, "proxy_policy")),
    (True, 22_000, 18_000, None, (18_000, "step_override")),
    (False, None, 18_000, None, (18_000, "step_override")),
    (True, 22_000, 18_000, 5000, (5000, "step_override")),
])
def test_initial_navigation_timeout_precedence(proxied, policy_ms, workflow_ms, step_ms, expected):
    execution = browser.Execution("https://owned.example", set())
    execution.initial_navigation_timeout = browser._initial_navigation_timeout(
        workflow_ms, policy_ms, proxied=proxied,
    )
    wait = browser._step_navigation_wait({"action": "navigate", "timeout_ms": step_ms}, execution)
    assert (wait.load_timeout_ms, wait.load_timeout_source) == expected


@pytest.mark.integration
@pytest.mark.parametrize(("proxied", "policy_ms", "step_ms", "expected_ms", "source", "outcome"), [
    (False, None, None, 700, "direct_default", "failed"),
    (True, None, None, 3000, "proxy_default", "completed"),
    (True, 700, None, 700, "proxy_policy", "failed"),
    (True, 700, 3000, 3000, "step_override", "completed"),
])
async def test_delayed_dom_content_loaded_uses_resolved_deadline(
    chromium, monkeypatch, proxied, policy_ms, step_ms, expected_ms, source, outcome,
):
    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            path = head.split(b" ", 2)[1]
            if path.startswith(b"/lab/form"):
                await asyncio.sleep(1.4)
                content_type, body = "application/javascript", b"void 0;"
            else:
                content_type, body = "text/html", b'<script defer src="/lab/form"></script>'
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    monkeypatch.setattr(browser, "DIRECT_INITIAL_NAVIGATION_TIMEOUT_MS", 700)
    monkeypatch.setattr(browser, "PROXY_INITIAL_NAVIGATION_TIMEOUT_MS", 3000)
    # The browser still uses the real guard and loopback server. Model the
    # assigned provider at the guard boundary to keep this test offline.
    seen_upstreams = []
    binding = SimpleNamespace(public_metadata=lambda: {})
    guard_type = browser.EgressGuard

    def guard(*args, upstream=None, **kwargs):
        seen_upstreams.append(upstream)
        return guard_type(*args, **kwargs)

    monkeypatch.setattr(browser.ProxyBinding, "from_config", lambda _config: binding)
    monkeypatch.setattr(browser, "EgressGuard", guard)
    async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        monkeypatch.setattr(network, "FIXTURE_PORT", port)
        monkeypatch.setattr(network, "FIXTURE_ORIGIN", origin)
        monkeypatch.setattr(browser, "FIXTURE_ORIGIN", origin)
        step = {"action": "navigate", "url": "/lab"}
        if step_ms is not None:
            step["timeout_ms"] = step_ms
        events = []

        async def emit(event):
            events.append(event)

        result = await run_workflow(
            origin, workflow([step]), proxy={"provider": "decodo"} if proxied else None,
            proxy_navigation_timeout_ms=policy_ms, emit=emit,
        )

    assert seen_upstreams == [binding if proxied else None]
    assert result.outcome == outcome, result.error
    expected = {"load_timeout_ms": expected_ms, "load_timeout_source": source}
    assert result.metadata["initial_navigation_timeout"] == expected
    if outcome == "failed":
        failure = result.metadata["causal_failure"]
        assert failure["reason"] == "navigation_timeout"
        assert failure["navigation"]["last_status"] == 200
        wait = failure["navigation_wait"]
        assert wait["stage"] == "initial_dom_content_loaded" and wait["deadline"] == "step"
        assert {key: wait[key] for key in expected} == expected
        assert next(event for event in events if event["kind"] == "workflow_step_failed")["navigation_wait"] == wait


@pytest.mark.integration
async def test_workflow_polls_delayed_text_without_fixed_wait(chromium, fixture):
    origin, visited, _ = fixture
    config = workflow([
        {"action": "navigate", "url": "/lab?delayed-redirect"},
        {"action": "assert_text", "selector": "#redirect-state", "value": "Redirect complete"},
    ])
    result = await run_workflow(origin, config)
    assert result.outcome == "completed", result
    assert result.metadata["completion_status"] == "steps_completed"
    assert visited == [("GET", "/lab?delayed-redirect", b""), ("GET", "/lab/form?redirected", b"")]


@pytest.mark.integration
async def test_unexpected_context_exit_fails_before_the_pending_selector_deadline(chromium, fixture, monkeypatch):
    origin, visited, _ = fixture
    launch = browser.launch_adapter
    context = None
    exit_task = None

    @asynccontextmanager
    async def observe_context(*args, **kwargs):
        nonlocal context
        async with launch(*args, **kwargs) as runtime:
            context = runtime.context
            yield runtime

    async def emit(event):
        nonlocal exit_task
        if event["kind"] == "workflow_step" and event["action"] == "assert_text":
            exit_task = asyncio.create_task(context.close())

    monkeypatch.setattr(browser, "launch_adapter", observe_context)
    try:
        async with asyncio.timeout(10):
            result = await run_workflow(origin, workflow([
                {"action": "navigate", "url": "/lab"},
                {"action": "assert_text", "selector": "#still-waiting", "value": "never", "timeout_ms": 20_000},
            ]), emit=emit)
    finally:
        if exit_task is not None:
            await exit_task

    assert result.outcome == "failed" and result.metadata["runtime_stopped"]
    assert result.metadata["profile_saved"] is False
    failure = result.metadata["causal_failure"]
    assert (failure["reason"], failure["phase"], failure["action"], failure["step"]) == (
        "browser_crash", "workflow", "assert_text", 2,
    )
    assert visited == [("GET", "/lab", b"")]


@pytest.mark.integration
@pytest.mark.parametrize(("steps", "action", "progress", "visited_path"), [
    (
        [{"action": "navigate", "url": "/lab?slow", "timeout_ms": 1000}],
        "navigate", {"requests": 1, "responses": 0, "commits": 0, "last_status": None}, "/lab?slow",
    ),
    (
        [
            {"action": "navigate", "url": "/lab?private-fixture-value"},
            {"action": "wait_for_url", "value": "/lab/form?private-destination", "timeout_ms": 200},
        ],
        "wait_for_url", {"requests": 1, "responses": 1, "commits": 1, "last_status": 200},
        "/lab?private-fixture-value",
    ),
])
async def test_navigation_timeout_reports_redacted_progress(chromium, fixture, steps, action, progress, visited_path):
    origin, visited, _ = fixture
    events = []

    async def emit(event):
        events.append(event)

    result = await run_workflow(origin, workflow(steps), emit=emit)

    assert result.outcome == "failed", result
    assert result.metadata["runtime_stopped"]
    failure = result.metadata["causal_failure"]
    assert failure["reason"] == "navigation_timeout"
    assert failure["action"] == action and failure["step"] == len(steps)
    assert failure["navigation"] == progress
    assert failure["navigation_wait"]["deadline"] == "step"
    assert failure["navigation_wait"]["stage"] == (
        "initial_dom_content_loaded" if action == "navigate" else "url_condition"
    )
    failure_event = next(event for event in events if event["kind"] == "workflow_step_failed")
    assert failure_event["navigation"] == progress
    assert failure_event["navigation_wait"] == failure["navigation_wait"]
    assert all(value not in repr((failure, failure_event)) for value in (
        origin, "/lab", "private-fixture-value", "private-destination",
    ))
    assert visited == [("GET", visited_path, b"")]


@pytest.mark.integration
async def test_workflow_deadline_preserves_active_navigation_check_context(chromium, fixture):
    origin, _, _ = fixture

    async def handoff():
        return {"command": "continue"}

    # Manual preparation opens the fixture before the workflow clock starts.
    result = await run_workflow(origin, workflow([
        {"action": "navigate", "url": "/lab"},
        {"action": "assert_url", "value": "private-destination", "timeout_ms": 10_000},
    ]), timeout_seconds=1, control=handoff, preparation={
        "mode": "manual", "idle_timeout_seconds": 5, "lifetime_seconds": 30,
    })

    assert result.outcome == "failed" and result.error == "Workflow exceeded its time budget"
    assert result.metadata["runtime_stopped"]
    failure = result.metadata["causal_failure"]
    assert failure["reason"] == "navigation_timeout"
    assert failure["step"] == 2 and failure["action"] == "assert_url"
    assert failure["navigation"] == {"requests": 2, "responses": 2, "commits": 2, "last_status": 200}
    assert failure["navigation_wait"]["stage"] == "url_condition"
    assert failure["navigation_wait"]["deadline"] == "workflow"
    assert "private-destination" not in repr(failure) and origin not in repr(failure)


@pytest.mark.integration
async def test_post_navigation_hold_is_not_part_of_initial_load_timeout(chromium, fixture):
    origin, _, _ = fixture
    result = await run_workflow(origin, workflow([{
        "action": "navigate", "url": "/lab", "timeout_ms": 700, "post_navigation_wait_ms": 1000,
    }]))
    assert result.outcome == "completed", result.error
    events = {item["event"]: item["navigation_delta_ms"] for item in result.metadata["lifecycle_timeline"]["events"]}
    assert events["workflow_completed"] >= 1000


async def test_post_navigation_readiness_uses_remaining_deadline(monkeypatch):
    started = browser.monotonic()
    elapsed = 0.0

    async def goto(*args, **kwargs):
        nonlocal elapsed
        elapsed = 0.75

    page = SimpleNamespace(goto=AsyncMock(side_effect=goto), wait_for_load_state=AsyncMock())
    monkeypatch.setattr(browser, "monotonic", lambda: started + elapsed)
    monkeypatch.setattr(browser, "_check_challenge", AsyncMock())
    execution = browser.Execution("https://owned.example", set())
    await browser._navigate(
        page, {"action": "navigate", "url": "/"}, execution, browser.NavigationWait(1000, 1), started + 1.001,
    )
    assert page.goto.await_args.kwargs["timeout"] <= 1000
    assert 0 < page.wait_for_load_state.await_args.kwargs["timeout"] <= 300


@pytest.mark.integration
async def test_late_page_load_does_not_obscure_initial_navigation_timeout(chromium, monkeypatch):
    script_requested = asyncio.Event()
    release_script = asyncio.Event()

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            path = head.split(b" ", 2)[1]
            is_script = path.startswith(b"/lab/form")
            if is_script:
                script_requested.set()
                await release_script.wait()
                content_type, body = "application/javascript", b"void 0;"
            else:
                content_type = "text/html"
                body = b'<script src="/lab/form?private-script"></script>'
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    check_challenge = browser._check_challenge

    async def finish_loading_during_failure_check(page, execution):
        if script_requested.is_set() and not release_script.is_set():
            release_script.set()
            await page.wait_for_load_state("load", timeout=400)
        await check_challenge(page, execution)

    monkeypatch.setattr(browser, "_check_challenge", finish_loading_during_failure_check)
    async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        monkeypatch.setattr(network, "FIXTURE_PORT", port)
        monkeypatch.setattr(network, "FIXTURE_ORIGIN", origin)
        monkeypatch.setattr(browser, "FIXTURE_ORIGIN", origin)
        try:
            result = await browser.execute_workflow(
                origin=origin, resource_origins=[], cancel=never_cancel,
                steps=[{
                    "action": "navigate", "url": "/lab?private-document",
                    "timeout_ms": 600, "post_navigation_wait_ms": 1200,
                }],
            )
        finally:
            release_script.set()

    assert result.outcome == "failed", result.error
    failure = result.metadata["causal_failure"]
    assert failure["reason"] == "navigation_timeout" and failure["action"] == "navigate"
    assert failure["navigation"] == {"requests": 1, "responses": 1, "commits": 1, "last_status": 200}
    wait = failure["navigation_wait"].copy()
    assert wait.pop("stage_elapsed_ms") >= 500
    assert wait == {
        "stage": "initial_dom_content_loaded", "deadline": "step",
        "load_timeout_ms": 600, "post_navigation_wait_ms": 1200, "load_timeout_source": "step_override",
    }
    events = [item["event"] for item in result.metadata["lifecycle_timeline"]["events"]]
    assert events.index("page_loaded") < events.index("failure_observed")
    assert all(value not in repr(failure) for value in (origin, "private-document", "private-script"))


@pytest.mark.integration
async def test_workflow_can_wait_for_one_expected_popup_and_switch_tabs(chromium, fixture):
    origin, visited, _ = fixture
    config = workflow([
        {"action": "navigate", "url": "/lab?popup"},
        {"action": "expect_popup", "selector": "#popup", "tab": "form"},
        {"action": "assert_url", "value": "/lab/form"},
        {"action": "assert_visible", "selector": "#name"},
        {"action": "switch_tab", "tab": "main"},
        {"action": "close_tab", "tab": "form"},
        {"action": "open_tab", "tab": "another_form", "url": "/lab/form?named-tab"},
        {"action": "assert_visible", "selector": "#submit"},
    ], success_check={"kind": "url_contains", "text": "named-tab"})
    result = await run_workflow(origin, config)
    assert result.outcome == "completed", result
    assert result.metadata["completion_status"] == "verified"
    assert visited == [
        ("GET", "/lab?popup", b""), ("GET", "/lab/form", b""), ("GET", "/lab/form?named-tab", b""),
    ]


@pytest.mark.integration
async def test_nested_control_flow_uses_capture_without_persisting_value(chromium, fixture):
    origin, visited, _ = fixture
    config = workflow(
        [{
            "action": "if",
            "condition": {"kind": "value_equals", "value_ref": "route", "value": True},
            "steps": [
                {"action": "navigate", "url": "/lab?profile=private-capture"},
                {"action": "repeat", "max_iterations": 3, "steps": [
                    {"action": "extract", "selector": "#state", "save_as": "marker"},
                ], "condition": {"kind": "value_equals", "value_ref": "marker", "value": "private-capture"}},
            ],
            "else_steps": [{"action": "navigate", "url": "/lab/form?wrong-branch"}],
        }],
        inputs={"route": {"type": "boolean", "default": False}},
        success_check={"kind": "value_equals", "value_ref": "marker", "value": "private-capture"},
    )
    events = []

    async def emit(event):
        events.append(event)

    result = await run_workflow(origin, config, workflow_inputs={"route": True}, emit=emit)
    assert result.outcome == "completed", result
    assert result.metadata["completion_status"] == "verified"
    assert "private-capture" not in repr(result.metadata)
    assert visited == [("GET", "/lab?profile=private-capture", b"")]
    assert any(event.get("step_path") == [1, "then", 2, "repeat", 1, 1] for event in events)


@pytest.fixture
async def frame_site(monkeypatch):
    documents = {
        "/lab": b'<iframe id="editor" src="/lab/form"></iframe>',
        "/lab/form": (
            b'<input id="toggle" type="checkbox"><input id="entry" '
            b'onkeydown="if(event.key===\'Enter\')document.querySelector(\'#result\').textContent=this.value">'
            b'<p id="result">Pending</p>'
        ),
        "/lab/next": b'<a id="popup" href="https://unregistered.example/" target="_blank">External</a>',
    }
    requested = []

    async def handle(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            path = headers.split(b" ", 2)[1].decode()
            requested.append(path)
            body = documents.get(path, b"")
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
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
        yield origin, requested


@pytest.mark.integration
async def test_checkbox_and_keyboard_actions_in_authorized_frame(chromium, frame_site):
    origin, requested = frame_site
    config = workflow([
        {"action": "navigate", "url": "/lab"},
        {"action": "check", "frame": ["#editor"], "selector": "#toggle"},
        {"action": "uncheck", "frame": ["#editor"], "selector": "#toggle"},
        {"action": "fill", "frame": ["#editor"], "selector": "#entry", "value_ref": "message"},
        {"action": "press", "frame": ["#editor"], "selector": "#entry", "value": "Enter"},
        {"action": "assert_text", "frame": ["#editor"], "selector": "#result", "value_ref": "message"},
    ], inputs={"message": {"type": "string", "default": "Window ready"}})
    result = await run_workflow(origin, config)
    assert result.outcome == "completed", result
    assert requested == ["/lab", "/lab/form"]


@pytest.mark.integration
async def test_expected_popup_cannot_reach_an_unapproved_origin(chromium, frame_site, monkeypatch):
    origin, requested = frame_site
    async def unexpected_lookup(*args, **kwargs):
        pytest.fail("A non-approved popup attempted DNS")

    monkeypatch.setattr(network, "resolve_public_ipv4", unexpected_lookup)
    config = workflow([
        {"action": "navigate", "url": "/lab/next"},
        {"action": "expect_popup", "selector": "#popup", "tab": "external"},
    ])
    result = await run_workflow(origin, config)
    assert result.outcome == "blocked", result
    assert requested == ["/lab/next"]


@pytest.mark.integration
@pytest.mark.parametrize("adapter", ["playwright", "cloakbrowser", "kameleo", "manual_chrome"])
async def test_real_adapter_workflow_contract_when_installed(chromium, fixture, tmp_path, adapter):
    from abrl.adapters import adapter_capabilities

    readiness = next(row for row in adapter_capabilities() if row["id"] == adapter)
    if not readiness["available"]:
        pytest.skip(readiness["reason"])
    origin, visited, _ = fixture
    config = workflow([
        {"action": "navigate", "url": "/lab?delayed-redirect"},
        {"action": "wait_for_url", "value": "/lab/form?redirected"},
        {"action": "assert_text", "selector": "#redirect-state", "value": "Redirect complete"},
    ], success_check={"kind": "text_contains", "selector": "#redirect-state", "text": "Redirect complete"})

    async def handoff():
        return {"command": "continue"}

    profile = tmp_path / "profile" if adapter != "kameleo" else None
    result = await run_workflow(
        origin, config, adapter=adapter, control=handoff,
        profile_dir=str(profile) if profile else None, artifact_policy="all",
    )
    assert result.outcome == "completed", result
    assert result.metadata["completion_status"] == "verified"
    assert result.metadata["runtime_stopped"]
    assert result.metadata["profile_saved"] == (profile is not None)
    assert visited == [("GET", "/lab?delayed-redirect", b""), ("GET", "/lab/form?redirected", b"")]


@pytest.mark.integration
async def test_workflow_repeat_expansion_shares_execution_budget(chromium, fixture, monkeypatch):
    origin, visited, _ = fixture
    monkeypatch.setenv("ABRL_MAX_WORKFLOW_EXECUTED_STEPS", "4")
    config = workflow([
        {"action": "navigate", "url": "/lab"},
        {"action": "repeat", "max_iterations": 5, "steps": [
            {"action": "assert_visible", "selector": "#name"},
        ]},
    ])
    result = await run_workflow(origin, config)
    assert result.outcome == "failed", result
    assert "executed step budget" in result.error
    assert result.metadata["step_failure"]["step_path"] == [2, "repeat", 3, 1]
    assert visited == [("GET", "/lab", b"")]


@pytest.mark.integration
async def test_workflow_recording_starts_on_selected_tab(chromium, fixture, tmp_path):
    import shutil

    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg is unavailable")
    origin, visited, _ = fixture
    config = workflow([
        {"action": "open_tab", "tab": "recorded", "url": "/lab?recording"},
        {"action": "assert_visible", "selector": "#name"},
    ])
    clips = []

    async def store_segment(segment):
        clips.append(segment.path.read_bytes())

    result = await run_workflow(
        origin, config, recording_directory=tmp_path, store_recording_segment=store_segment,
    )
    assert result.outcome == "completed", result
    assert result.metadata["recording_status"] == "ready"
    assert clips and b"ftyp" in clips[0][:16]
    assert visited == [("GET", "/lab?recording", b"")]
