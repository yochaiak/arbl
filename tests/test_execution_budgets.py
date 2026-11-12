"""Bounded, measured browser requests and opaque transport forwarding."""

import asyncio
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError
from test_browser import chromium as chromium
from test_browser import fixture as fixture
from test_browser import never_cancel, request_through_proxy
from test_remote_browser import runtime

from abrl.browser import Execution, execute_workflow
from abrl.network import EgressGuard, NetworkDenied
from abrl.platform_schemas import BatchInput
from abrl.remote_browser import RemoteBroker, RuntimeLease


def test_batch_budget_validation_and_shared_preparation_counter():
    for field, value in (
        ("request_budget", 0),
        ("request_budget", 100001),
        ("traffic_budget_bytes", 0),
        ("traffic_budget_bytes", 2**32),
    ):
        with pytest.raises(ValidationError):
            BatchInput(
                target_id="target", workflow_id="workflow", idempotency_key="budget-001", **{field: value}
            )
    execution = Execution("https://owned.example", set(), request_budget=2)
    execution.phase = "automated_preparation"
    assert execution.admit_request()
    execution.phase = "workflow"
    assert execution.admit_request()
    assert not execution.admit_request()
    assert execution.request_counts == {"automated_preparation": 1, "workflow": 1}
    assert execution.request_budget_exhausted and execution.block.is_set()


class Sink:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, value):
        self.data.extend(value)

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True


async def test_one_byte_budget_spans_concurrent_opaque_tunnels_and_directions():
    denied = []
    guard = EgressGuard("https://owned.example", [], denied.append, traffic_budget_bytes=1024)
    sources, sinks = [], []
    # Encrypted TLS/WSS data is opaque to the broker; requests are not inferred
    # from these chunks or from the number of CONNECT tunnels.
    for payload in (b"\x16\x03\x03" * 100, b"\x17\x03\x03" * 200, b"\xff" * 300):
        source, sink = asyncio.StreamReader(), Sink()
        source.feed_data(payload)
        source.feed_eof()
        sources.append(source)
        sinks.append(sink)
    await asyncio.gather(
        *(
            guard._metered_pipe(source, sink, upstream=index != 1)
            for index, (source, sink) in enumerate(zip(sources, sinks, strict=True))
        )
    )
    usage = guard.transport_usage()
    assert usage["total_bytes"] == sum(len(sink.data) for sink in sinks) == 900
    assert usage["upstream_bytes"] == 300 and usage["downstream_bytes"] == 600
    assert usage["budget_consumed_bytes"] <= 1024 and usage["budget_exhausted"]
    assert denied == ["Browser traffic byte budget exhausted"]
    assert not guard.forwarding_enabled
    assert not guard._charge_bytes(1, upstream=False)


def test_vendor_control_traffic_consumes_budget_but_not_proxy_cost_quantity():
    broker = RemoteBroker(
        {
            "origin": "https://owned.example",
            "vendor_control_origins": ["https://license.example"],
            "traffic_budget_bytes": 1024,
        },
        RuntimeLease("x" * 40, "session", 1),
    )
    assert broker.is_vendor_control("https://license.example")
    assert broker._charge_bytes(500, upstream=True, vendor_control=True)
    assert broker._charge_bytes(400, upstream=False)
    assert not broker._charge_bytes(200, upstream=False)
    usage = broker.state()["transport_usage"]
    assert usage["total_bytes"] == 400
    assert usage["vendor_control_bytes"] == 500
    assert usage["budget_consumed_bytes"] == 900
    assert usage["budget_exhausted"]


def test_proxy_trials_cannot_silently_use_direct_local_fixture(fixture):
    origin, _, _ = fixture
    with pytest.raises(NetworkDenied, match="authorized HTTPS"):
        EgressGuard(origin, [], lambda _: None, upstream=object())


@pytest.mark.integration
async def test_real_browser_counts_each_http_request_once_and_stops_next_navigation(chromium, fixture):
    origin, visited, _ = fixture
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        cancel=never_cancel,
        request_budget=1,
        steps=[{"action": "navigate", "url": "/lab"}, {"action": "navigate", "url": "/lab/form"}],
    )
    assert result.outcome == "blocked", result
    assert result.metadata["failure_kind"] == "budget_exhausted", result.metadata
    assert result.metadata["request_usage"] == {
        "total": 1,
        "by_phase": {"workflow": 1},
        "budget": 1,
        "exhausted": True,
        "measurement": "intercepted_http_requests",
    }
    assert [(method, path) for method, path, _ in visited] == [("GET", "/lab")]
    assert result.metadata["runtime_stopped"]
    assert result.metadata["transport_usage"]["upstream_bytes"] > 0
    assert result.metadata["transport_usage"]["downstream_bytes"] > 0


@pytest.mark.integration
@pytest.mark.parametrize("remote", [False, True])
async def test_real_browser_transport_budget_closes_large_response(chromium, fixture, remote):
    origin, _, _ = fixture

    async def run(connection=None):
        return await execute_workflow(
            origin=origin,
            resource_origins=[],
            cancel=never_cancel,
            remote=connection,
            traffic_budget_bytes=1024,
            steps=[
                {"action": "navigate", "url": "/lab?large"},
                {"action": "assert_visible", "selector": "#large"},
            ],
        )

    if remote:
        async with runtime(origin, traffic_budget_bytes=1024) as (connection, _):
            result = await run(connection)
    else:
        result = await run()
    assert result.outcome == "blocked", result
    assert result.metadata["failure_kind"] == "budget_exhausted", result.metadata
    assert result.metadata["transport_usage"]["budget_exhausted"]
    assert result.metadata["transport_usage"]["budget_consumed_bytes"] <= 1024
    assert result.metadata["runtime_stopped"]


@pytest.mark.integration
async def test_redirect_hops_count_against_http_budget(chromium, fixture):
    origin, visited, _ = fixture
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        cancel=never_cancel,
        request_budget=1,
        steps=[
            {"action": "navigate", "url": "/lab?redirect"},
            {"action": "assert_visible", "selector": "#name"},
        ],
    )
    assert result.outcome == "blocked", result
    assert result.metadata["request_usage"]["exhausted"]
    assert [(method, path) for method, path, _ in visited] == [("GET", "/lab?redirect")]


async def test_reserved_connection_load_rejects_excess_without_waiting_for_idle_tunnel(fixture):
    origin, visited, received = fixture
    async with EgressGuard(origin, [], lambda _: None, max_connections=1) as guard:
        location = urlsplit(guard.url)
        reader, writer = await asyncio.open_connection(location.hostname, location.port)
        writer.write(f"GET {origin}/lab?slow HTTP/1.1\r\nHost: fixture\r\n\r\n".encode())
        await writer.drain()
        await asyncio.wait_for(received.wait(), 2)
        result = await asyncio.wait_for(
            request_through_proxy(guard.url, f"GET {origin}/lab HTTP/1.1\r\nHost: fixture\r\n\r\n"), 1
        )
        assert result.startswith(b"HTTP/1.1 503")
        assert len(visited) == 1
        assert guard.transport_summary()["peak_connections"] == 1
        assert guard.transport_summary()["capacity_rejections"] == 1
        assert guard.transport_events[-1]["retryable"] is False
        writer.close()
        await writer.wait_closed()
    assert guard.transport_summary()["active_connections"] == 0


@pytest.mark.integration
async def test_budget_stop_never_clears_ambiguous_action_history(chromium, fixture):
    origin, visited, _ = fixture
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        cancel=never_cancel,
        request_budget=1,
        steps=[
            {"action": "navigate", "url": "/lab"},
            {"action": "fill", "selector": "#name", "value": "private"},
            {"action": "click", "selector": "#submit"},
            {"action": "assert_visible", "selector": "#success"},
        ],
    )
    assert result.outcome == "blocked"
    assert result.metadata["needs_reconciliation"]
    assert result.metadata["non_idempotent_action_started"]
    assert result.metadata["failure_kind"] == "budget_exhausted"
    assert not any(method == "POST" for method, _, _ in visited)
