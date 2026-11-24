"""Bounded, measured browser requests and opaque transport forwarding."""

import asyncio
import socket
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError
from test_browser import chromium as chromium
from test_browser import fixture as fixture
from test_browser import never_cancel, request_through_proxy
from test_remote_browser import runtime

from abrl import network
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


async def test_provider_tunnel_pins_automatic_asset_address(monkeypatch):
    opened = []

    async def resolve(host, port):
        assert (host, port) == ("cdn.example", 443)
        return ("8.8.4.4",)

    async def upstream_connect(authority):
        opened.append(authority)
        return None

    monkeypatch.setattr("abrl.network.resolve_public_ipv4", resolve)
    guard = EgressGuard("https://owned.example", [], lambda _: None, upstream=object())
    monkeypatch.setattr(guard, "_upstream_connect", upstream_connect)
    assert guard.allow_asset_origin("https://cdn.example")
    async with guard:
        response = await request_through_proxy(guard.url, "CONNECT cdn.example:443 HTTP/1.1\r\n\r\n")
    assert opened == ["8.8.4.4:443"]
    assert response.startswith(b"HTTP/1.1 502")


@pytest.mark.parametrize(
    "resolver_error, response_code, transient",
    [(socket.EAI_AGAIN, b"502", True), (socket.EAI_NONAME, b"403", False)],
)
async def test_broker_distinguishes_transient_and_permanent_target_dns(monkeypatch, resolver_error, response_code, transient):
    async def resolve(*_args, **_kwargs):
        raise socket.gaierror(resolver_error, "private resolver detail")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    denied = []
    async with EgressGuard("https://owned.example", [], denied.append) as guard:
        response = await request_through_proxy(guard.url, "CONNECT owned.example:443 HTTP/1.1\r\n\r\n")

    assert response.startswith(b"HTTP/1.1 " + response_code)
    assert guard.transport_summary()["active_connections"] == 0
    assert guard.transport_summary()["attempts"] == guard.transport_summary()["failures"] == int(transient)
    if transient:
        assert not denied
        [issue] = guard.transport_events
        assert issue["reason"] == "target_dns_unavailable" and issue["retryable"]
        assert issue["tunnel_wait"]["stage"] == "target_dns"
    else:
        assert denied == ["No usable IPv4 DNS answer for the registered origin"]
        assert not guard.transport_events


@pytest.mark.parametrize(
    ("failure", "reply", "evidence", "attempts"),
    [
        ("local_dns", b"403", {"source": "local_proxy", "reason": "local_dns_denied", "http_status": 403}, 0),
        ("provider_http", b"502", {"source": "decodo", "reason": "proxy_tunnel_failed", "http_status": 522}, 1),
        ("provider_connection", b"502", {"source": "upstream_connection", "reason": "proxy_tunnel_failed"}, 1),
    ],
)
async def test_connect_observer_distinguishes_local_and_provider_failures(monkeypatch, failure, reply, evidence, attempts):
    secondary = "https://secondary.example"
    origins, reports = [], []

    def observe(origin):
        origins.append(origin)
        return reports.append

    def observe_denial(_):
        pytest.fail("The secondary origin should not stop the primary assignment.")

    async def resolve(host, port):
        if host == "secondary.example" and failure == "local_dns":
            raise NetworkDenied("private resolver error")
        return ("192.0.2.7",)

    original_connect = asyncio.open_connection

    class ProviderWriter(Sink):
        async def wait_closed(self):
            pass

    async def connect(host, port, **kwargs):
        if host == "127.0.0.1":
            return await original_connect(host, port, **kwargs)
        assert (host, port) == ("192.0.2.7", 7000)
        if failure == "provider_connection":
            raise OSError("private provider connection error")
        source = asyncio.StreamReader()
        source.feed_data(b"HTTP/1.1 522 Connection timed out\r\nX-Error-Description: private provider text\r\n\r\n")
        return source, ProviderWriter()

    monkeypatch.setattr(network, "resolve_public_ipv4", resolve)
    monkeypatch.setattr(network.asyncio, "open_connection", connect)
    fake_provider = SimpleNamespace(
        provider="decodo", host="provider.example", port=7000, authorization=lambda: b"Basic fake",
    )
    async with EgressGuard(
        "https://owned.example", [], observe_denial, upstream=fake_provider,
        navigation_origins=[secondary], observe_connect=observe,
    ) as guard:
        response = await request_through_proxy(guard.url, "CONNECT secondary.example:443 HTTP/1.1\r\n\r\n")

    assert response.startswith(b"HTTP/1.1 " + reply)
    assert origins == [secondary]
    assert reports == [evidence]
    assert guard.transport_summary()["attempts"] == attempts
    assert guard.transport_summary()["failures"] == attempts


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


@pytest.mark.parametrize("limit", [1, 100])
async def test_reserved_connection_load_rejects_excess_without_waiting_for_idle_tunnel(fixture, limit):
    origin, visited, _ = fixture

    async def wait_for_open_connections():
        while len(visited) < limit:
            await asyncio.sleep(0.01)

    async with EgressGuard(origin, [], lambda _: None, max_connections=limit) as guard:
        location = urlsplit(guard.url)
        writers = []
        try:
            for _ in range(limit):
                _, writer = await asyncio.open_connection(location.hostname, location.port)
                writers.append(writer)
                writer.write(f"GET {origin}/lab?slow HTTP/1.1\r\nHost: fixture\r\n\r\n".encode())
                await writer.drain()
            await asyncio.wait_for(wait_for_open_connections(), 5)
            result = await request_through_proxy(guard.url, f"GET {origin}/lab HTTP/1.1\r\nHost: fixture\r\n\r\n")
            assert result.startswith(b"HTTP/1.1 503")
            assert len(visited) == limit
            assert guard.transport_summary()["peak_connections"] == limit
            assert guard.transport_summary()["capacity_rejections"] == 1
            assert guard.transport_events[-1]["retryable"] is False
        finally:
            for writer in writers:
                writer.close()
            await asyncio.gather(*(writer.wait_closed() for writer in writers))
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
