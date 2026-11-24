import asyncio
import json
import socket
import struct
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse

from abrl import browser, browser_diagnostics, network, proxy_preflight
from abrl.browser import BrowserResult
from abrl.browser_diagnostics import _configured_public_origin, run_browser_diagnostics
from abrl.diagnostic_fixture import DIAGNOSTIC_PATH, ECHO_PATH, create_app


class LoopbackStun(asyncio.DatagramProtocol):
    header = struct.Struct("!HHI12s")
    mapped_address = struct.Struct("!HHBBHI")
    cookie = 0x2112A442

    def __init__(self, respond: bool):
        self.respond = respond
        self.requests = 0
        self.responses = 0

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, peer):
        if peer[0] != "127.0.0.1" or len(data) < self.header.size:
            return
        kind, length, cookie, transaction_id = self.header.unpack_from(data)
        if kind != 0x0001 or cookie != self.cookie or len(data) != self.header.size + length:
            return
        self.requests += 1
        if self.respond:
            address = int.from_bytes(socket.inet_aton(peer[0]), "big")
            attribute = self.mapped_address.pack(0x0020, 8, 0, 1, peer[1] ^ (cookie >> 16), address ^ cookie)
            self.transport.sendto(self.header.pack(0x0101, len(attribute), cookie, transaction_id) + attribute, peer)
            self.responses += 1


async def test_loopback_stun_response_contains_request_transaction_and_mapped_peer():
    loop = asyncio.get_running_loop()
    transport, server = await loop.create_datagram_endpoint(lambda: LoopbackStun(True), local_addr=("127.0.0.1", 0))
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.bind(("127.0.0.1", 0))
            client.setblocking(False)
            cookie = LoopbackStun.cookie
            transaction_id = b"abrl-test-id"
            await loop.sock_sendto(client, LoopbackStun.header.pack(1, 0, cookie, transaction_id),
                                   transport.get_extra_info("sockname"))
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(client, 100), timeout=2)
            assert LoopbackStun.header.unpack_from(data) == (0x0101, 12, cookie, transaction_id)
            kind, length, reserved, family, port, address = LoopbackStun.mapped_address.unpack_from(data, 20)
            assert (kind, length, reserved, family) == (0x0020, 8, 0, 1)
            assert port ^ (cookie >> 16) == client.getsockname()[1]
            assert (address ^ cookie).to_bytes(4, "big") == socket.inet_aton(client.getsockname()[0])
        assert server.requests == server.responses == 1
    finally:
        transport.close()


def test_echo_limits_output_to_allowed_headers_and_excludes_credentials():
    client = TestClient(create_app())
    response = client.get(
        ECHO_PATH,
        headers={
            "User-Agent": "Fixture browser",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-CH-UA-Mobile": "?0",
            "Authorization": "private authorization",
            "Proxy-Authorization": "private proxy authorization",
            "Cookie": "private cookie",
            "X-Forwarded-For": "198.51.100.42",
        },
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["headers"]["user-agent"] == "Fixture browser"
    assert response.json()["headers"]["accept-language"] == "en-US,en;q=0.9"
    assert response.json()["headers"]["sec-ch-ua-mobile"] == "?0"
    assert response.json()["proxy_header_names"] == ["proxy-authorization", "x-forwarded-for"]
    assert response.json()["peer_ip"] is None
    assert "private" not in response.text and "198.51.100.42" not in response.text
    assert client.get("/api/v1/status").status_code == 404


def test_public_probe_only_enables_stun_on_its_own_fixture_host():
    origin = "https://qa.my-company.com"
    fixture = create_app(public_origin=origin, stun_url="stun:qa.my-company.com:3478")
    assert 'data-stun="stun:qa.my-company.com:3478"' in TestClient(fixture, base_url=origin).get(DIAGNOSTIC_PATH).text
    assert 'data-stun=""' in TestClient(fixture, base_url="https://other.my-company.com").get(DIAGNOSTIC_PATH).text
    remote_stun = create_app(public_origin=origin, stun_url="stun:other.my-company.com:3478")
    assert 'data-stun=""' in TestClient(remote_stun, base_url=origin).get(DIAGNOSTIC_PATH).text


@pytest.mark.parametrize("url", [
    "https://example.com/lab/diagnostics", "https://qa.test/lab/diagnostics", "https://10.0.0.6/lab/diagnostics",
    "http://public-owner.com/lab/diagnostics", "https://public-owner.com/bogus", "https://user@public-owner.com/lab/diagnostics",
    "https://public-owner.com/lab/diagnostics?", "<YOUR_URL>",
])
def test_public_configuration_rejects_non_fixture_or_non_public_urls(monkeypatch, url):
    monkeypatch.setenv("ABRL_DIAGNOSTIC_URL", url)
    with pytest.raises(ValueError):
        _configured_public_origin()


@pytest.mark.parametrize("path", ["", "/", DIAGNOSTIC_PATH])
def test_public_configuration_accepts_the_origin_or_its_exact_fixture_path(monkeypatch, path):
    origin = "https://qa.my-company.com"
    monkeypatch.setenv("ABRL_DIAGNOSTIC_URL", origin + path)
    assert _configured_public_origin() == origin


def test_default_cli_uses_loopback_even_with_public_fixture_configured(monkeypatch):
    monkeypatch.setenv("ABRL_DIAGNOSTIC_URL", "https://qa.my-company.com")
    monkeypatch.setattr(sys, "argv", ["browser_diagnostics.py"])
    probe = AsyncMock(return_value={"status": "passed"})
    monkeypatch.setattr(browser_diagnostics, "run_browser_diagnostics", probe)
    assert browser_diagnostics._main() == 0
    assert probe.await_args.kwargs["origin"] == network.FIXTURE_ORIGIN
    assert probe.await_args.kwargs["proxy"] is None


@pytest.mark.parametrize(("status", "exit_code"), [("passed", 0), ("failed", 1), ("partial", 2)])
def test_live_cli_report_status_and_proxy_configuration(monkeypatch, capsys, status, exit_code):
    origin = "https://qa.my-company.com"
    monkeypatch.setenv("ABRL_DIAGNOSTIC_URL", origin + DIAGNOSTIC_PATH)
    monkeypatch.setenv("ABRL_SECRET_DECODO_USERNAME", "fixture_proxy_user")
    monkeypatch.setenv("ABRL_SECRET_DECODO_PASSWORD", "fixture-proxy-password")
    monkeypatch.setattr(sys, "argv", ["browser_diagnostics.py", "--adapter", "playwright", "--decodo"])
    report = {"status": status, "preflight": {"status": status}}
    probe = AsyncMock(return_value=report)
    monkeypatch.setattr(browser_diagnostics, "run_browser_diagnostics", probe)

    assert browser_diagnostics._main() == exit_code
    assert probe.await_args.kwargs["origin"] == origin
    proxy = probe.await_args.kwargs["proxy"]
    assert proxy["max_connections"] == 2
    binding = browser_diagnostics.ProxyBinding.from_config(proxy)
    assert binding.provider == "decodo" and binding.continuity == "sticky"
    assert binding.username.startswith("user-fixture_proxy_user-country-us-session-")
    assert binding.password == "fixture-proxy-password"
    output = capsys.readouterr()
    decoded = json.loads(output.out)
    assert datetime.fromisoformat(decoded.pop("observed_at")).tzinfo == UTC
    assert decoded == report and output.err == ""
    assert "fixture_proxy_user" not in output.out and "fixture-proxy-password" not in output.out


def test_live_cli_configuration_error_has_usage_and_no_report(monkeypatch, capsys):
    monkeypatch.delenv("ABRL_DIAGNOSTIC_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["browser_diagnostics.py", "--decodo"])
    probe = AsyncMock()
    monkeypatch.setattr(browser_diagnostics, "run_browser_diagnostics", probe)

    with pytest.raises(SystemExit) as error:
        browser_diagnostics._main()

    assert error.value.code == 2
    output = capsys.readouterr()
    assert "usage:" in output.err and "ABRL_DIAGNOSTIC_URL" in output.err
    assert output.out == ""
    probe.assert_not_awaited()


def _diagnostic_extractions(ice: dict) -> list[dict]:
    epoch_ms = 1_798_000_000_000
    groups = [
        {"server_ua": "browser", "browser_ua": "browser", "platform": None, "webdriver": True, "proxy_header_names": []},
        {"server_brands": None, "server_full_versions": None, "server_mobile": None, "server_platform": None,
         "server_platform_version": None, "browser_brands": [], "browser_full_versions": False, "browser_mobile": None,
         "browser_platform": None, "browser_platform_version": None},
        {"accept_language": "en-US,en;q=0.9", "language": "en-US", "languages": ["en-US", "en"],
         "timezone": "UTC", "offset_minutes": 0, "before_epoch_ms": epoch_ms, "after_epoch_ms": epoch_ms + 2,
         "server_epoch_ms": epoch_ms + 1},
        {"inner": [640, 480], "screen": [640, 480], "dpr": 1, "webgl": None, "ice": ice,
         "document_protocol": "h2", "echo_protocol": "h2"},
    ]
    return [{"text": json.dumps(group), "truncated": False} for group in groups]


def test_live_cli_reports_browser_observations_without_maxmind_files(monkeypatch, capsys, tmp_path):
    origin = "https://qa.my-company.com"
    address = "192.0.2.202"
    password = "fixture-proxy-password"
    username = "fixture_proxy_user"
    monkeypatch.setenv("ABRL_DIAGNOSTIC_URL", origin + DIAGNOSTIC_PATH)
    monkeypatch.setenv("ABRL_SECRET_DECODO_USERNAME", username)
    monkeypatch.setenv("ABRL_SECRET_DECODO_PASSWORD", password)
    monkeypatch.setattr(sys, "argv", ["browser_diagnostics.py", "--adapter", "playwright", "--decodo", "--redacted"])
    monkeypatch.setattr(browser_diagnostics, "resolve_public_ipv4", AsyncMock(return_value=(address,)))
    for dataset in ("CITY", "ASN", "ANONYMOUS"):
        monkeypatch.setenv(f"ABRL_GEOIP_{dataset}_DB", str(tmp_path / f"{dataset}.mmdb"))
    lookup = proxy_preflight._Probe(
        ip=address, metadata={"country": "US", "asn": 64501}, timings={"tunnel_establish": 423},
    )
    probe = AsyncMock(side_effect=(lookup, lookup, proxy_preflight._Probe(timings={"tunnel_establish": 425})))
    monkeypatch.setattr(proxy_preflight, "_probe", probe)
    ice = {"state": "not_configured", "literal": 0, "other": 0, "masked": 0}

    async def observe_browser(**options):
        timeline = options["lifecycle_timeline"]
        timeline.mark("browser_launched")
        return BrowserResult("completed", 5100, None, {
            "extractions": _diagnostic_extractions(ice),
            "quic_launch_flag": "passed",
            "runtime_stopped": True,
            "lifecycle_timeline": timeline.view(),
        }, False)

    browser_run = AsyncMock(side_effect=observe_browser)
    monkeypatch.setattr(browser_diagnostics, "execute_workflow", browser_run)

    assert browser_diagnostics._main() == 2

    output = capsys.readouterr()
    report = json.loads(output.out)
    assert output.err == ""
    assert report["status"] == report["preflight"]["status"] == "partial"
    assert report["preflight"]["blocked"] is False
    checks = report["preflight"]["checks"]
    assert checks["country"]["confirmed"] is False and checks["asn"]["confirmed"] is False
    assert checks["network_type"]["status"] == "unavailable"
    assert all(checks[name]["dataset_status"] == "missing_or_not_file" for name in ("country", "asn", "network_type"))
    assert checks["sticky"]["status"] == checks["target_tls"]["status"] == "passed"
    assert report["observation_status"] == "complete" and report["runtime_stopped"] is True
    assert report["fixture_http_protocols"] == {"document": "h2", "echo": "h2"}
    assert report["lifecycle_timeline"]["events"][0]["event"] == "preflight_completed"
    assert "observations" not in report and report["recorded_observations"]["environment"]["timezone_present"] is True
    assert all(value not in output.out for value in (address, username, password, str(tmp_path)))
    assert probe.await_count == 3
    browser_run.assert_awaited_once()


@pytest.mark.parametrize(("ice", "verdict"), [
    ({"state": "complete", "literal": 1, "other": 0, "masked": 0}, "passed"),
    ({"state": "complete", "literal": 1, "other": 1, "masked": 0}, "mismatch"),
    ({"state": "complete", "literal": 0, "other": 0, "masked": 1}, "inconclusive"),
])
async def test_ice_comparison_surfaces_only_the_verdict(monkeypatch, ice, verdict):
    result = BrowserResult("completed", 40, None, {"extractions": _diagnostic_extractions(ice)}, True)
    monkeypatch.setattr(browser_diagnostics, "execute_workflow", AsyncMock(return_value=result))
    report = await run_browser_diagnostics(origin=network.FIXTURE_ORIGIN)
    assert report["checks"]["ice"] == verdict
    assert "ice" not in report["observations"]["rendering"]
    assert "capabilities" not in report and "connection_timings_ms" not in report
    assert all(key not in json.dumps(report) for key in ("peer_ip", "candidate", "literal", "other", "masked"))


async def test_failed_decodo_preflight_prevents_browser_launch(monkeypatch):
    origin = "https://qa.my-company.com"
    monkeypatch.setenv("ABRL_DIAGNOSTIC_URL", origin + DIAGNOSTIC_PATH)
    monkeypatch.setattr(browser_diagnostics, "resolve_public_ipv4", AsyncMock(return_value=("8.8.8.8",)))
    monkeypatch.setenv("ABRL_SECRET_TEST_PROXY_USER", "test_user")
    monkeypatch.setenv("ABRL_SECRET_TEST_PROXY_PASSWORD", "test-password")
    preflight = AsyncMock(return_value={"status": "blocked", "blocked": True, "reason": "authentication"})
    browser_run = AsyncMock()
    monkeypatch.setattr(proxy_preflight, "run_decodo_preflight", preflight)
    monkeypatch.setattr(browser_diagnostics, "execute_workflow", browser_run)
    report = await run_browser_diagnostics(
        origin=origin,
        proxy={
            "provider": "decodo", "product": "residential", "host": "gate.decodo.com", "port": 7000,
            "country": "US", "username_ref": "TEST_PROXY_USER", "password_ref": "TEST_PROXY_PASSWORD",
            "provider_resolved_ack": True,
        },
    )
    assert report["status"] == "failed"
    assert report["reason"] == "authentication"
    assert report["observation_status"] == "unavailable"
    preflight_event, = report["lifecycle_timeline"]["events"]
    assert preflight_event["event"] == "preflight_completed"
    assert preflight_event["elapsed_ms"] >= 0 and preflight_event["navigation_delta_ms"] is None
    preflight.assert_awaited_once()
    browser_run.assert_not_awaited()


async def test_standalone_diagnostic_uses_broker_cause_and_omits_browser_error(monkeypatch):
    monkeypatch.setattr(browser_diagnostics, "execute_workflow", AsyncMock(return_value=BrowserResult(
        "failed", 40, "private error body 198.51.100.99",
        {
            "runtime_stopped": True,
            "initial_navigation_timeout": {
                "load_timeout_ms": 15_000, "load_timeout_source": "proxy_default", "url": "198.51.100.99",
            },
            "navigation_connects": {"version": 1, "truncated": False, "navigations": [{
                "navigation": 1, "redirected": False, "outcome": "failed", "attribution": "observed",
                "connects": [{"source": "upstream_connection", "reason": "proxy_tunnel_timeout",
                              "host": "198.51.100.99"}],
            }]},
            "causal_failure": {
                "phase": "workflow", "reason": "navigation_timeout",
                "navigation_wait": {"stage": "initial_dom_content_loaded", "deadline": "step", "stage_elapsed_ms": 20},
            },
            "transport_summary": {"attempts": 1, "successes": 0, "secret": "private-tunnel-token"},
            "http_status_counts": {"main_frame": {}, "subresource": {"198.51.100.99": 2}},
            "transport_events": [{
                "kind": "transport", "reason": "proxy_tunnel_timeout",
                "tunnel_wait": {"stage": "proxy_connect", "stage_elapsed_ms": 15, "url": "198.51.100.99"},
            }],
        }, True,
    )))
    report = await run_browser_diagnostics(origin=network.FIXTURE_ORIGIN)
    assert report["status"] == "failed" and report["reason"] == "proxy_tunnel_timeout"
    assert report["transport_summary"] == {"attempts": 1, "successes": 0}
    assert report["initial_navigation_timeout"] == {"load_timeout_ms": 15_000, "load_timeout_source": "proxy_default"}
    assert report["navigation_connects"] == {"version": 1, "truncated": False, "navigations": [{
        "navigation": 1, "redirected": False, "outcome": "failed", "attribution": "observed",
        "connects": [{"source": "upstream_connection", "reason": "proxy_tunnel_timeout"}],
    }]}
    assert report["http_status_counts"] == {"main_frame": {}, "subresource": {}}
    failure = report["first_causal_failure"]
    assert failure["phase"] == "proxy" and failure["reason"] == "proxy_tunnel_timeout"
    assert failure["tunnel_wait"] == {"stage": "proxy_connect", "stage_elapsed_ms": 15}
    assert failure["navigation_wait"]["stage"] == "initial_dom_content_loaded"
    assert "private error" not in json.dumps(report) and "198.51.100.99" not in json.dumps(report)


@pytest.fixture(scope="module")
def chromium():
    availability = browser.browser_availability()
    if not availability["available"]:
        pytest.skip(availability["reason"])


@pytest.mark.integration
@pytest.mark.parametrize(("device_preset", "stun_mode"), [
    (None, None), ("phone", None), (None, "no_response"), (None, "respond"),
])
async def test_real_chromium_reports_fixture_observations(chromium, monkeypatch, device_preset, stun_mode, record_property):
    stun_transport = None
    stun_url = None
    if stun_mode:
        stun_transport, stun = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: LoopbackStun(stun_mode == "respond"), local_addr=("127.0.0.1", 0),
        )
        stun_url = f"stun:127.0.0.1:{stun_transport.get_extra_info('sockname')[1]}"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        monkeypatch.setattr(network, "FIXTURE_ORIGIN", origin)
        monkeypatch.setattr(network, "FIXTURE_PORT", port)
        monkeypatch.setattr(browser, "FIXTURE_ORIGIN", origin)
        monkeypatch.setattr(browser_diagnostics, "FIXTURE_ORIGIN", origin)
        server = uvicorn.Server(uvicorn.Config(
            create_app(stun_url=stun_url), log_level="error", access_log=False, proxy_headers=False,
        ))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            report = await run_browser_diagnostics(origin=origin, device_preset=device_preset)
        finally:
            server.should_exit = True
            await task
            if stun_transport is not None:
                stun_transport.close()
    assert report["runtime_stopped"] is True
    assert report["fixture_http_protocols"] == {"document": "http/1.1", "echo": "http/1.1"}
    capabilities = report["capabilities"]
    assert set(capabilities) == {
        "audio_context", "canvas_2d", "webgl", "font_loading", "performance_observer",
        "network_information", "service_worker",
    }
    for field in ("audio_context", "canvas_2d", "font_loading", "performance_observer"):
        assert capabilities[field] == "available"
    if report["observations"]["rendering"]["webgl"]:
        assert capabilities["webgl"] == "available"
    else:
        assert capabilities["webgl"] in {"unavailable", "unsupported"}
    for field in ("network_information", "service_worker"):
        assert capabilities[field] in {"available", "unsupported"}
    assert set(report["connection_timings_ms"]) == {"document", "echo"}
    for entry in report["connection_timings_ms"].values():
        assert entry["status"] == "observed"
        assert set(entry) == {"status", "duration", "request_to_first_byte", "connect"}
        assert 0 <= entry["duration"] <= 60_000
        assert all(
            sample is None or type(sample) is int and 0 <= sample <= 60_000
            for name, sample in entry.items() if name != "status"
        )
    assert not {"capabilities", "connection_timings_ms"} & report["observations"]["rendering"].keys()
    events = {item["event"]: item["navigation_delta_ms"] for item in report["lifecycle_timeline"]["events"]}
    assert events["browser_launched"] <= events["navigation_started"] == 0
    assert 0 <= events["document_committed"] <= events["dom_content_loaded"] <= events["workflow_completed"]
    assert report["checks"]["fixture_http_transport"] == "passed"
    flag = report["checks"]["quic_launch_flag"]
    assert flag in {"passed", "unavailable"}
    expected_status = "passed" if flag == "passed" else "partial"
    for name in ("user_agent", "chromium_major", "unexpected_proxy_headers", "locale", "timezone_offset", "browser_clock",
                 "viewport", "device_pixel_ratio"):
        assert report["checks"][name] == "passed", json.dumps(report, indent=2)
    assert report["checks"]["browser_automation"] == "observed"
    if stun_mode:
        ice = report["checks"]["ice"]
        assert ice in {"passed", "inconclusive", "timeout", "mismatch"}
        assert report["status"] == {"passed": expected_status, "mismatch": "failed"}.get(ice, "partial")
        assert stun.responses == (stun.requests if stun_mode == "respond" else 0)
        record_property("ice_verdict", ice)
        record_property("stun_binding_responses", stun.responses)
    else:
        assert report["status"] == expected_status, json.dumps(report, indent=2)
        assert report["checks"]["ice"] == "not_configured"
    assert "ice" not in report["observations"]["rendering"]
    assert "127.0.0.1" not in json.dumps(report)
    if device_preset == "phone":
        assert report["observations"]["rendering"]["inner"] == [390, 844]
        assert report["observations"]["rendering"]["dpr"] == 3.0


def test_live_ice_ignores_private_ingress_peer_and_untrusted_forwarded_header():
    origin = "https://qa.my-company.com"
    fixture = create_app(public_origin=origin, stun_url="stun:qa.my-company.com:3478")
    client = TestClient(fixture, base_url=origin, client=("127.0.0.1", 4222))
    response = client.get(ECHO_PATH, headers={"X-Forwarded-For": "8.8.4.4"})
    assert response.json()["peer_ip"] is None


@pytest.mark.parametrize(("owned", "path"), [(False, DIAGNOSTIC_PATH), (True, "/lab/unrelated")])
async def test_ready_snapshot_does_not_read_outside_an_owned_fixture(owned, path):
    origin = "https://owned.qa-host.org"
    execution = browser.Execution(origin, set(), owned_diagnostic=owned)
    execution.lifecycle_timeline.mark("document_committed")
    page = SimpleNamespace(url=origin + path, evaluate=AsyncMock())

    await browser._preserve_ready_diagnostics(page, execution)

    page.evaluate.assert_not_called()
    assert "extractions" not in execution.metadata


@pytest.mark.integration
@pytest.mark.parametrize(("deadline", "reason"), [("step", "selector_timeout"), ("workflow", "timeout")])
async def test_owned_fixture_preserves_published_observations_when_post_load_step_fails(
    chromium, monkeypatch, deadline, reason,
):
    monkeypatch.setattr(browser_diagnostics, "diagnostic_steps", lambda: [
        {"action": "navigate", "url": DIAGNOSTIC_PATH},
        {"action": "assert_text", "selector": "#abrl-probe-status", "value": "Diagnostics complete"},
        {"action": "assert_text", "selector": "#private-test-selector", "value": "Not present",
         "timeout_ms": 100 if deadline == "step" else 30_000},
    ])
    if deadline == "workflow":
        async def run_with_short_workflow_budget(**options):
            return await browser.execute_workflow(**{**options, "timeout_seconds": 5})

        monkeypatch.setattr(browser_diagnostics, "execute_workflow", run_with_short_workflow_budget)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        monkeypatch.setattr(network, "FIXTURE_ORIGIN", origin)
        monkeypatch.setattr(network, "FIXTURE_PORT", port)
        monkeypatch.setattr(browser, "FIXTURE_ORIGIN", origin)
        monkeypatch.setattr(browser_diagnostics, "FIXTURE_ORIGIN", origin)
        server = uvicorn.Server(uvicorn.Config(
            create_app(), log_level="error", access_log=False, proxy_headers=False,
        ))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            report = await run_browser_diagnostics(origin=origin)
        finally:
            server.should_exit = True
            await task

    assert report["status"] == "failed" and report["reason"] == reason
    assert report["first_causal_failure"]["reason"] == reason
    assert report["first_causal_failure"]["step"] == 3
    assert report["observation_status"] == "complete" and report["runtime_stopped"] is True
    assert report["checks"]["locale"] == "passed"
    assert report["recorded_observations"]["request_header_presence"]["user_agent"] is True
    assert report["capabilities"]["canvas_2d"] == "available"
    assert report["minimum_clock_skew_ms"] == 0
    events = {mark["event"]: mark["navigation_delta_ms"] for mark in report["lifecycle_timeline"]["events"]}
    assert 0 <= events["page_loaded"] <= events["failure_observed"]
    assert "observations" not in report
    assert "private-test-selector" not in json.dumps(report) and "127.0.0.1" not in json.dumps(report)


@pytest.mark.integration
@pytest.mark.parametrize(("echo_status", "reason"), [
    (503, "diagnostic_echo_http_error"), (200, "diagnostic_fixture_error"),
])
async def test_owned_echo_error_is_not_a_selector_timeout(chromium, monkeypatch, echo_status, reason):
    fixture = create_app()

    @fixture.middleware("http")
    async def fail_echo(request, call_next):
        if request.url.path == ECHO_PATH:
            return PlainTextResponse("private fixture error body", status_code=echo_status)
        return await call_next(request)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        monkeypatch.setattr(network, "FIXTURE_ORIGIN", origin)
        monkeypatch.setattr(network, "FIXTURE_PORT", port)
        monkeypatch.setattr(browser, "FIXTURE_ORIGIN", origin)
        monkeypatch.setattr(browser_diagnostics, "FIXTURE_ORIGIN", origin)
        server = uvicorn.Server(uvicorn.Config(fixture, log_level="error", access_log=False, proxy_headers=False))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            report = await run_browser_diagnostics(origin=origin)
        finally:
            server.should_exit = True
            await task
    assert report["status"] == "failed"
    assert report["reason"] == reason
    assert report["runtime_stopped"]
    assert report["first_causal_failure"].get("probe_failure") == (
        "echo_invalid_json" if echo_status == 200 else None
    )
    assert "private fixture error body" not in json.dumps(report)


def test_only_measured_forwarding_headers_on_the_owned_public_fixture_can_be_exempt(monkeypatch):
    origin = "https://owned.qa-host.org"
    monkeypatch.setenv("ABRL_DIAGNOSTIC_URL", origin)
    monkeypatch.setenv("ABRL_DIAGNOSTIC_EDGE_HEADERS", "X-Forwarded-For, Proxy-Authorization, Proxy-Connection")
    assert browser_diagnostics._expected_edge_headers(origin) == {"x-forwarded-for"}
    assert not browser_diagnostics._expected_edge_headers(network.FIXTURE_ORIGIN)
    assert not browser_diagnostics._expected_edge_headers("https://another.qa-host.org")
