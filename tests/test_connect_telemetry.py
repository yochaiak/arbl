import json

from abrl import connect_telemetry
from abrl.connect_telemetry import (
    MAX_CONNECT_RESULTS,
    MAX_NAVIGATIONS,
    NavigationConnectTrace,
    public_navigation_connects,
)


class Request:
    pass


def test_navigation_windows_keep_redirect_and_provider_results_separate():
    trace = NavigationConnectTrace()
    first, redirected = Request(), Request()
    trace.request(first, "https://first.owned.test", navigation=True, step=1)
    first_connect = trace.observe_connect("https://first.owned.test")
    trace.request(
        redirected, "https://redirect.owned.test", navigation=True, step=1, redirected_from=first,
    )
    second_connect = trace.observe_connect("https://redirect.owned.test")
    trace.finish(redirected, "failed")
    first_connect({"source": "decodo", "reason": "connect_established", "http_status": 200})
    second_connect({"source": "local_proxy", "reason": "local_dns_denied", "http_status": 403})
    assert trace.export() == {"version": 1, "truncated": False, "navigations": [
        {"navigation": 1, "step": 1, "redirected": False, "outcome": "redirected", "attribution": "observed",
         "connects": [{"source": "decodo", "reason": "connect_established", "http_status": 200}]},
        {"navigation": 2, "step": 1, "redirected": True, "outcome": "failed", "attribution": "observed",
         "connects": [{"source": "local_proxy", "reason": "local_dns_denied", "http_status": 403}]},
    ]}


def test_shared_or_reused_connection_does_not_produce_definite_attribution():
    trace = NavigationConnectTrace()
    origin = "https://first.owned.test"
    page, image, next_page = Request(), Request(), Request()
    trace.request(page, origin, navigation=True, step=1)
    trace.request(image, origin, navigation=False, step=1)
    trace.observe_connect(origin)({"source": "decodo", "reason": "proxy_tunnel_failed", "http_status": 502})
    trace.finish(page, "failed")
    trace.finish(image)
    trace.request(next_page, origin, navigation=True, step=2)
    trace.finish(next_page, "committed")

    first, second = trace.export()["navigations"]
    assert first["attribution"] == "ambiguous"
    assert second["attribution"] == "unobserved" and second["connects"] == []


def test_connect_before_browser_request_attaches_only_within_the_lookback(monkeypatch):
    now = 0.0
    monkeypatch.setattr(connect_telemetry, "monotonic", lambda: now)
    trace = NavigationConnectTrace()
    origin = "https://fixture.owned.test"
    first, second = Request(), Request()
    result = {"source": "decodo", "reason": "proxy_tunnel_failed", "http_status": 502}

    early = trace.observe_connect(origin)
    now = 0.1
    trace.request(first, origin, navigation=True, step=1)
    early(result)
    trace.finish(first, "failed")
    trace.observe_connect(origin)(result)
    now = 1.0
    trace.request(second, origin, navigation=True, step=2)

    first_row, second_row = trace.export()["navigations"]
    assert first_row["connects"] == [result] and first_row["attribution"] == "observed"
    assert second_row["connects"] == [] and second_row["attribution"] == "unobserved"


def test_navigation_connect_projection_drops_addresses_tokens_and_unrecognized_fields():
    secret = "fixture-private-signed-token"
    origin = "https://198.51.100.5"
    record = {"version": 1, "truncated": False, "navigations": [{
        "navigation": 1, "step": 2, "redirected": True, "outcome": "failed", "attribution": "observed",
        "origin": origin, "connects": [
            {"source": "decodo", "reason": secret, "http_status": 502, "url": origin},
            {"source": "decodo", "reason": "proxy_tunnel_failed", "http_status": secret},
            {"source": "upstream_connection", "reason": "proxy_response_invalid", "http_status": 502,
             "authorization": secret, "ip": "198.51.100.5"},
        ],
    }]}
    result = public_navigation_connects(record)
    assert result["navigations"][0]["connects"] == [
        {"source": "upstream_connection", "reason": "proxy_response_invalid"},
    ]
    assert secret not in json.dumps(result)
    assert "198.51.100.5" not in json.dumps(result)


def test_navigation_connect_trace_bounds_navigation_and_connection_counts():
    trace = NavigationConnectTrace()
    origin = "https://fixture.owned.test"
    for _ in range(MAX_NAVIGATIONS + 1):
        request = Request()
        trace.request(request, origin, navigation=True, step=2)
        if report := trace.observe_connect(origin):
            for _ in range(MAX_CONNECT_RESULTS + 1):
                report({"source": "local_proxy", "reason": "connect_established", "http_status": 200})
        trace.finish(request, "committed")
    result = trace.export()
    assert result["truncated"]
    assert len(result["navigations"]) == MAX_NAVIGATIONS
    assert all(
        len(row["connects"]) == MAX_CONNECT_RESULTS and row["attribution"] == "ambiguous"
        for row in result["navigations"]
    )
