import asyncio
import json
from contextlib import suppress
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
from test_browser import chromium as chromium
from test_browser import never_cancel

from abrl import browser, network


def test_main_origin_failure_counts_deduplicate_and_forget_request_details():
    execution = browser.Execution("https://fixture.example", set())

    def resource(kind="fetch", error=None, origin=execution.origin, navigation=False):
        return Mock(
            url=f"{origin}/private/path?cookie=secret&ip=192.0.2.25", resource_type=kind,
            failure=error, is_navigation_request=lambda: navigation,
        )

    denied = resource("image", "net::ERR_FAILED")
    execution.record_subresource_failure(denied, blocked=True)
    execution.record_subresource_failure(denied)
    missing = resource("font", "net::ERR_ABORTED")
    execution.record_subresource_failure(missing, status=404)
    execution.record_subresource_failure(missing)
    execution.record_subresource_failure(resource(error="net::ERR_ABORTED"))
    execution.record_subresource_failure(resource("script", "net::ERR_BLOCKED_BY_CLIENT"))
    execution.record_subresource_failure(resource("xhr"), status=502)
    for i in range(150):
        execution.record_subresource_failure(resource(f"private-kind-{i}", "secret raw error address"))
    execution.record_subresource_failure(resource(origin="https://other.example"), status=500)
    execution.record_subresource_failure(resource("document", navigation=True), status=403)
    execution.record_subresource_failure(resource("stylesheet"), status=200)

    assert execution.metadata == {"main_origin_subresource_failures": {
        "font": {"http_4xx": 1}, "image": {"blocked": 1}, "fetch": {"aborted": 1},
        "script": {"blocked": 1}, "xhr": {"http_5xx": 1}, "other": {"network_error": 150},
    }}
    report = json.dumps(execution.metadata)
    assert all(value not in report for value in ("cookie", "192.0.2.25", "secret", "private", "fixture.example"))


@pytest.mark.integration
async def test_chromium_counts_observed_responses_separately_from_subresource_failures(chromium, monkeypatch):
    visited = []
    html = b"""
    <link rel="icon" href="data:,">
    <link rel="stylesheet" href="/lab/form">
    <link rel="preload" as="font" crossorigin href="/lab/repeat?private=font">
    <script src="/lab/next"></script>
    <img id="ok" src="/lab/finish">
    <img src="/disallowed-asset?private=blocked">
    <p id="status">Waiting</p>
    <script>window.onload = () => {
      if (window.scriptLoaded && document.querySelector('#ok').naturalWidth === 2 &&
          getComputedStyle(document.body).getPropertyValue('--asset-loaded').trim() === 'yes')
        document.querySelector('#status').textContent = 'Ready';
    };</script>
    """
    routes = {
        "/lab": ("200 OK", "text/html", html),
        "/lab/form": ("200 OK", "text/css", b"body { --asset-loaded: yes; }"),
        "/lab/next": ("200 OK", "text/javascript", b"window.scriptLoaded = true;"),
        "/lab/finish": ("200 OK", "image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"/>'),
        "/lab/repeat": ("404 Not Found", "text/plain", b"private body"),
    }

    async def serve(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            path = urlsplit(request.split(b" ", 2)[1].decode("ascii")).path
            visited.append(path)
            status, content_type, body = routes[path]
            writer.write((
                f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii") + body)
            await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async with await asyncio.start_server(serve, "127.0.0.1", 0) as fixture:
        port = fixture.sockets[0].getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        monkeypatch.setattr(browser, "FIXTURE_ORIGIN", origin)
        monkeypatch.setattr(network, "FIXTURE_ORIGIN", origin)
        monkeypatch.setattr(network, "FIXTURE_PORT", port)
        result = await browser.execute_workflow(
            origin=origin, resource_origins=[], cancel=never_cancel,
            steps=[{"action": "navigate", "url": "/lab"},
                   {"action": "assert_text", "selector": "#status", "value": "Ready"}],
        )

    assert result.outcome == "completed", result.error
    assert set(visited) == routes.keys()
    assert result.metadata["main_origin_subresource_failures"] == {"font": {"http_4xx": 1}, "image": {"blocked": 1}}
    assert result.metadata["http_status_counts"] == {
        "main_frame": {"200": 1}, "subresource": {"200": 3, "404": 1},
    }
