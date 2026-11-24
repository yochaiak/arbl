import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from abrl import browser, network
from abrl.adapters import AdapterUnavailable, adapter_capabilities
from abrl.browser import _bypass_service_workers, _install_attached_context_policy


async def test_attached_containment_identifies_and_protects_the_target_context():
    page = SimpleNamespace(send=AsyncMock(return_value={"targetInfo": {"browserContextId": "fixture-context"}}))
    session = SimpleNamespace(send=AsyncMock())
    context = SimpleNamespace(
        browser=SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=session)),
        add_init_script=AsyncMock(),
    )
    assert await _install_attached_context_policy(context, page) is session
    page.send.assert_awaited_once_with("Target.getTargetInfo")
    session.send.assert_awaited_once_with(
        "Browser.setDownloadBehavior", {"behavior": "deny", "browserContextId": "fixture-context"},
    )
    context.add_init_script.assert_awaited_once()
    await _bypass_service_workers(page)
    page.send.assert_awaited_with("Network.setBypassServiceWorker", {"bypass": True})


@pytest.mark.parametrize("failed_operation", ["context_identity", "download_denial", "registration_block", "bypass"])
async def test_attached_containment_rejects_unsupported_policy_without_exposing_details(failed_operation):
    failure = PlaywrightError("private CDP endpoint and token")
    page = SimpleNamespace(send=AsyncMock(
        return_value={} if failed_operation == "context_identity" else {"targetInfo": {"browserContextId": "test"}},
        side_effect=failure if failed_operation == "bypass" else None,
    ))
    session = SimpleNamespace(send=AsyncMock(side_effect=failure if failed_operation == "download_denial" else None))
    context = SimpleNamespace(
        browser=SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=session)),
        add_init_script=AsyncMock(side_effect=failure if failed_operation == "registration_block" else None),
    )
    with pytest.raises(AdapterUnavailable) as caught:
        if failed_operation == "bypass":
            await _bypass_service_workers(page)
        else:
            await _install_attached_context_policy(context, page)
    assert caught.value.status == "containment_unavailable"
    assert "private CDP" not in str(caught.value)


@pytest.mark.parametrize("failure", ["existing_worker", "unsupported_policy", "worker_started"])
async def test_attached_workflow_keeps_target_traffic_disabled_on_containment_failure(monkeypatch, failure):
    page = SimpleNamespace(url="about:blank", on=Mock())
    listeners = {}
    context = SimpleNamespace(
        pages=[page], service_workers=[object()] if failure == "existing_worker" else [],
        on=lambda event, callback: listeners.update({event: callback}),
        remove_listener=Mock(), new_cdp_session=AsyncMock(),
    )

    @asynccontextmanager
    async def launch(_adapter, _broker, _profile, metadata, **_options):
        try:
            yield SimpleNamespace(context=context)
        finally:
            metadata["profile_saved"] = True

    async def install(_context, _session):
        if failure == "unsupported_policy":
            raise AdapterUnavailable("Fixture containment policy unavailable", "containment_unavailable")
        assert failure == "worker_started"
        listeners["serviceworker"](object())

    monkeypatch.setattr(browser, "launch_adapter", launch)
    monkeypatch.setattr(browser, "_install_attached_context_policy", install)
    monkeypatch.setattr(browser, "_bypass_service_workers", AsyncMock())
    monkeypatch.setattr(browser, "_attach_page_policy", AsyncMock())
    activate = Mock(side_effect=AssertionError("Target forwarding was activated without containment"))
    monkeypatch.setattr(network.EgressGuard, "activate_forwarding", activate)
    result = await browser.execute_workflow(
        origin=network.FIXTURE_ORIGIN, resource_origins=[], steps=[{"action": "navigate", "url": "/lab"}],
        adapter="kameleo", cancel=AsyncMock(return_value=False),
    )
    status = "service_worker_not_allowed" if failure == "worker_started" else "containment_unavailable"
    assert result.metadata["causal_failure"]["reason"] == status
    assert result.outcome in {"blocked", "failed"}
    assert result.metadata["profile_saved"] is False
    activate.assert_not_called()


@pytest.mark.integration
async def test_owned_attached_chromium_denies_downloads_and_service_worker_registration(tmp_path):
    chromium = next(row for row in adapter_capabilities() if row["id"] == "playwright")
    if not chromium["available"]:
        pytest.skip(chromium["reason"])
    visited = []

    async def serve(reader, writer):
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            path = request.split(b" ", 2)[1]
            visited.append(path)
            headers = b"Content-Type: text/html\r\n"
            body = b"<a id=download href=/asset>Download fixture</a>"
            if path == b"/asset":
                headers = b"Content-Type: application/octet-stream\r\nContent-Disposition: attachment; filename=fixture.txt\r\n"
                body = b"fixture asset"
            if path == b"/worker.js":
                headers = b"Content-Type: application/javascript\r\n"
                body = b"self.addEventListener('install', () => {});"
            writer.write(
                b"HTTP/1.0 200 OK\r\n" + headers + b"Content-Length: "
                + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            return
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    profile = tmp_path / "profile"
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    async with server, async_playwright() as driver:
        launched = await driver.chromium.launch_persistent_context(
            profile, channel="chromium", chromium_sandbox=True,
            args=["--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0"], downloads_path=downloads,
        )
        try:
            port = (profile / "DevToolsActivePort").read_text().splitlines()[0]
            browser = await driver.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            try:
                context, = browser.contexts
                page = context.pages[0]
                session = await context.new_cdp_session(page)
                policy = await _install_attached_context_policy(context, session)
                await _bypass_service_workers(session)
                await page.goto(origin)
                async with page.expect_download(timeout=5000) as pending:
                    await page.locator("#download").click()
                assert await (await pending.value).failure() == "canceled"
                assert not any(downloads.iterdir())

                second = await context.new_page()
                await _bypass_service_workers(await context.new_cdp_session(second))
                await second.goto(origin)
                for target in (page, second):
                    assert await target.evaluate(
                        "async () => await navigator.serviceWorker.register('/worker.js') === undefined"
                    )
                assert not context.service_workers
                assert b"/worker.js" not in visited and b"/asset" in visited
                await policy.detach()
            finally:
                await browser.close()
        finally:
            await launched.close()
