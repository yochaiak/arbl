import asyncio
import hashlib
import io
import json
import random
from collections import Counter
from urllib.parse import urlsplit
from zipfile import ZipFile

import pytest
from playwright.async_api import async_playwright
from test_browser import chromium as chromium

from abrl import javascript_capture
from abrl.javascript_capture import JavaScriptCapture

EXTERNAL = b"globalThis.externalLoads = (globalThis.externalLoads || 0) + 1;"
PRELOAD = b"globalThis.unusedPreloadExecuted = true;"
MODULE = b"export const ready = true;"
GENERIC_MIME_SCRIPT = b"globalThis.fetchedScriptWasExecuted = true;"
WORKER = b"self.onmessage = () => importScripts('/worker-import.js'); self.postMessage('ready');"
WORKER_IMPORT = b"self.postMessage('imported');"
SHARED_WORKER = b"onconnect = ({ports}) => ports[0].postMessage('shared');"


@pytest.fixture
async def capture_site():
    visited = Counter()

    async def serve(reader, writer):
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            location = raw.split()[1].decode()
            visited[location] += 1
            path = urlsplit(location).path
            status, mime_type, body = "200 OK", "text/html", b""
            if path == "/":
                body = f"""
<link rel="preload" href="/preload" as="script">
<script src="/external?token=private-query#private-fragment"></script>
<script src="{cross_origin}/cross-origin"></script>
<script>
    debugger;
    globalThis.inlineWasExecuted = true;
    eval('globalThis.evalWasExecuted = true;');
    new Function('globalThis.constructedWasExecuted = true;')();
    import('/module').then(module => globalThis.moduleWasExecuted = module.ready);
    import('data:text/javascript,export default true')
        .then(module => globalThis.dataModuleWasExecuted = module.default);
    fetch('/download.cjs').then(response => response.text()).then(() => globalThis.genericMimeWasFetched = true);
</script>
<p>Fixture</p>
""".encode()
            elif path == "/nested":
                body = f"""
<iframe id="same" src="/same-frame"></iframe>
<iframe id="cross" src="{cross_origin}/cross-frame"></iframe>
<script>
    globalThis.sharedWorker = new SharedWorker('/shared.js');
    sharedWorker.port.onmessage = ({{data}}) => document.body.dataset.shared = data;
</script>
""".encode()
            elif path == "/same-frame":
                body = b"<script>globalThis.sameFrameWasExecuted = true;</script>"
            elif path == "/cross-frame":
                body = b"""
<body><script>
    globalThis.worker = new Worker('/worker.js');
    worker.onmessage = ({data}) => document.body.dataset.worker = data;
</script></body>
"""
            elif path == "/download.cjs":
                mime_type, body = "application/octet-stream", GENERIC_MIME_SCRIPT
            elif path in {
                "/external",
                "/cross-origin",
                "/preload",
                "/module",
                "/worker.js",
                "/worker-import.js",
                "/shared.js",
            }:
                mime_type = "text/javascript"
                body = {
                    "/external": EXTERNAL,
                    "/cross-origin": EXTERNAL,
                    "/preload": PRELOAD,
                    "/module": MODULE,
                    "/worker.js": WORKER,
                    "/worker-import.js": WORKER_IMPORT,
                    "/shared.js": SHARED_WORKER,
                }[path]
            else:
                status = "404 Not Found"
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: {mime_type}\r\nContent-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        cross_origin = f"http://localhost:{port}"
        yield origin, cross_origin, visited


def _read_capture(collector):
    payload, summary = collector.build_archive({"attempt_number": 1, "adapter": "playwright"})
    with ZipFile(io.BytesIO(payload)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        files = {name: archive.read(name) for name in archive.namelist() if name != "manifest.json"}
    assert summary == manifest["capture"]
    return manifest, files


@pytest.mark.integration
async def test_delivered_and_executed_javascript_use_original_bodies(chromium, capture_site):
    origin, cross_origin, visited = capture_site
    collector = JavaScriptCapture()
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        page = await browser.new_page(service_workers="block")
        try:
            await collector.start(page.context, page, permits_url=lambda url: url.startswith(origin + "/"))
            await page.goto(origin + "/")
            await page.wait_for_function(
                "externalLoads === 2 && inlineWasExecuted && evalWasExecuted && constructedWasExecuted"
                " && moduleWasExecuted && dataModuleWasExecuted && genericMimeWasFetched"
            )
        finally:
            await collector.stop()
            await browser.close()

    manifest, files = _read_capture(collector)
    assert manifest["capture"]["status"] == "ready", manifest["capture"]
    response_records = {record["url"]: record for record in manifest["responses"]}
    digest = hashlib.sha256(EXTERNAL).hexdigest()
    source = f"sources/{digest}.js"
    assert response_records[origin + "/external"]["source"] == source
    assert response_records[cross_origin + "/cross-origin"]["source"] == source
    assert files[source] == EXTERNAL
    assert files[response_records[origin + "/preload"]["source"]] == PRELOAD
    assert files[response_records[origin + "/module"]["source"]] == MODULE
    assert files[response_records[origin + "/download.cjs"]["source"]] == GENERIC_MIME_SCRIPT
    assert not any(
        record["url"] in {origin + "/preload", origin + "/download.cjs"}
        for record in manifest["runtime_scripts"]
    )
    for content in (EXTERNAL, MODULE, b"globalThis.evalWasExecuted = true;"):
        assert any(
            record["execution"] == "executed" and files.get(record.get("source")) == content
            for record in manifest["runtime_scripts"]
        )
    assert any(
        record["kind"] == "inline" and record["execution"] == "executed"
        for record in manifest["runtime_scripts"]
    )
    assert any(
        record["url"] == "data:"
        and record["execution"] == "executed"
        and files.get(record.get("source")) == b"export default true"
        for record in manifest["runtime_scripts"]
    )
    assert any(
        record["kind"] == "generated"
        and record["execution"] == "executed"
        and b"globalThis.constructedWasExecuted = true;" in files.get(record.get("source"), b"")
        for record in manifest["runtime_scripts"]
    )
    assert "private-query" not in json.dumps(manifest)
    assert "private-fragment" not in json.dumps(manifest)
    assert visited["/external?token=private-query"] == 1
    assert visited["/cross-origin"] == visited["/module"] == visited["/preload"] == 1


@pytest.mark.integration
async def test_coverage_for_same_process_frame_and_nested_worker_targets(chromium, capture_site):
    origin, cross_origin, _ = capture_site
    collector = JavaScriptCapture()
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(args=["--site-per-process"])
        page = await browser.new_page(service_workers="block")
        try:
            await collector.start(
                page.context,
                page,
                permits_url=lambda url: url.startswith((origin + "/", cross_origin + "/")),
            )
            await page.goto(origin + "/nested")
            await page.locator("body[data-shared=shared]").wait_for()
            frame = page.frame_locator("#cross")
            await frame.locator("body[data-worker=ready]").wait_for()
            await frame.locator("body").evaluate("() => worker.postMessage('import')")
            await frame.locator("body[data-worker=imported]").wait_for()
        finally:
            await collector.stop()
            await browser.close()

    manifest, files = _read_capture(collector)
    scripts = {
        (record["target_type"], files.get(record.get("source"))): record["execution"]
        for record in manifest["runtime_scripts"]
    }
    assert scripts["page", b"globalThis.sameFrameWasExecuted = true;"] == "executed", manifest
    assert scripts["worker", WORKER_IMPORT] == "executed", manifest
    assert scripts["worker", WORKER] == "executed", manifest
    assert scripts["shared_worker", SHARED_WORKER] == "executed", manifest
    assert any(kind == "iframe" and state == "executed" for (kind, _), state in scripts.items()), manifest
    responses = {record["url"]: files.get(record.get("source")) for record in manifest["responses"]}
    assert responses[cross_origin + "/worker.js"] == WORKER, manifest
    assert responses[cross_origin + "/worker-import.js"] == WORKER_IMPORT, manifest
    assert b"() => worker.postMessage('import')" not in files.values()
    assert manifest["capture"]["status"] == "partial"
    assert manifest["capture"]["gaps"]["related_target_attached_after_start"] >= 1


def test_archive_omits_whole_files_when_beyond_size_limit(monkeypatch):
    collector = JavaScriptCapture()
    collector.started = True
    monkeypatch.setattr(javascript_capture, "MAX_ARCHIVE_BYTES", 2048)
    large = random.Random(1).randbytes(2048)
    small = b"a = 1;"
    for body in (large, small):
        record = {}
        collector.delivered.append(record)
        collector._store(record, body)

    manifest, files = _read_capture(collector)
    assert manifest["capture"]["status"] == "partial"
    assert manifest["capture"]["omitted_count"] == 1
    assert manifest["responses"][0] == {"omission": "archive_size_limit"}
    assert list(files.values()) == [small]


def test_archive_describes_an_unavailable_capture():
    manifest, files = _read_capture(JavaScriptCapture())
    assert manifest["capture"]["status"] == "unavailable"
    assert not files
