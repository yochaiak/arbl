"""Execute a bounded QA workflow in a stock Chromium session."""

from __future__ import annotations

import asyncio
import os
import platform
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from time import monotonic
from typing import Literal
from urllib.parse import urljoin

from playwright.async_api import (
    BrowserContext,
    CDPSession,
    Page,
    Response,
    Route,
    WebSocketRoute,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .network import (
    FIXTURE_ORIGIN,
    EgressGuard,
    NetworkDenied,
    is_fixture_path,
    normalize_origin,
    request_origin,
)

DENIAL_STATUSES = frozenset({401, 403, 429})
CHALLENGE_TITLE = re.compile(
    r"^(access denied|attention required[!.]*|just a moment[.]*|security check|verify you are human)$",
    flags=re.IGNORECASE,
)
CHALLENGE_WIDGETS = (
    "iframe[title*='captcha' i], iframe[title*='challenge' i], "
    "input[name='cf-turnstile-response'], [aria-label*='captcha' i]"
)
SECRET_REF = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
DEFAULT_STEP_TIMEOUT_MS = 8_000
MAX_EXTRACTED_CHARACTERS = 4_000
SAFE_OMIT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class StepFailed(ValueError):
    """A known workflow error with safe details for the operator."""


@dataclass(slots=True)
class BrowserResult:
    outcome: Literal["completed", "failed", "blocked", "cancelled"]
    elapsed_ms: int
    error: str | None
    metadata: dict
    started_action: bool


@dataclass
class Execution:
    origin: str
    resources: set[str]
    block: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str | None = None
    blocked_resources: int = 0
    started_action: bool = False
    steps_completed: int = 0
    active_step: int = 0
    metadata: dict = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    extraction_budget: int = MAX_EXTRACTED_CHARACTERS

    def deny(self, reason: str) -> None:
        if not self.block.is_set():
            self.reason = reason
            self.block.set()

    def permits(self, url: str, *, document: bool, method: str = "GET") -> bool:
        try:
            origin = request_origin(url)
        except NetworkDenied:
            return False
        if origin == FIXTURE_ORIGIN and (self.origin != FIXTURE_ORIGIN or not is_fixture_path(url)):
            return False
        return origin == self.origin or (
            not document and method in {"GET", "HEAD"} and origin in self.resources
        )


def browser_availability() -> dict:
    """Check whether the installed Playwright Chromium executable is present."""
    try:
        with sync_playwright() as driver:
            executable = Path(driver.chromium.executable_path)
        available = executable.is_file() and os.access(executable, os.X_OK)
        reason = None if available else "Install Chromium with: uv run playwright install chromium"
    except (OSError, PlaywrightError):
        available = False
        reason = "Playwright could not locate Chromium"
    return {
        "name": "Chromium",
        "adapter": "playwright",
        "available": available,
        "version": version("playwright"),
        "reason": reason,
    }


async def _await_cancellation(cancel: Callable[[], Awaitable[bool]]) -> None:
    while not await cancel():
        await asyncio.sleep(0.15)


async def _attach_page_policy(
    context: BrowserContext, page: Page, session: CDPSession, execution: Execution
) -> None:
    async def restrict_context(route: Route) -> None:
        request = route.request
        document = request.is_navigation_request()
        if document:
            try:
                request_page = request.frame.page
            except PlaywrightError:
                request_page = None  # A popup can request its document before creating a frame.
            if request_page is not page:
                execution.deny("Unexpected browser window opened")
        if not execution.permits(request.url, document=document, method=request.method):
            if document:
                execution.deny("Navigation outside the registered target scope")
            else:
                execution.blocked_resources += 1
            await route.abort()
        elif execution.block.is_set():
            await route.abort()
        else:
            await route.continue_()

    async def intercept(params: dict) -> None:
        request_id = params["requestId"]
        document = params.get("resourceType") == "Document"
        request = params["request"]
        allowed = execution.permits(request["url"], document=document, method=request["method"])
        if not allowed and not execution.block.is_set():
            if document:
                execution.deny("Navigation outside the registered target scope")
            else:
                execution.blocked_resources += 1
        try:
            if allowed and not execution.block.is_set():
                await session.send("Fetch.continueRequest", {"requestId": request_id})
            else:
                await session.send(
                    "Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}
                )
        except PlaywrightError:
            # The request may have been canceled by Chromium during page teardown.
            return

    def inspect_response(response: Response) -> None:
        if response.status in DENIAL_STATUSES:
            execution.deny(f"Site denied or limited access (HTTP {response.status})")

    def inspect_navigation(frame) -> None:
        if frame == page.main_frame and frame.url != "about:blank":
            if not execution.permits(frame.url, document=True):
                execution.deny("Navigation outside the registered target scope")

    async def close_popup(popup: Page) -> None:
        execution.deny("Unexpected browser window opened")
        try:
            await popup.close()
        except PlaywrightError:
            pass

    async def close_socket(socket: WebSocketRoute) -> None:
        execution.blocked_resources += 1
        await socket.close(code=1008, reason="WebSockets are outside the workflow scope")

    session.on("Fetch.requestPaused", intercept)
    page.on("response", inspect_response)
    page.on("framenavigated", inspect_navigation)
    page.on("popup", close_popup)
    await context.route("**/*", restrict_context)
    await context.route_web_socket("**/*", close_socket)
    await session.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})


async def _check_challenge(page: Page, execution: Execution) -> None:
    if not execution.started_action or execution.block.is_set():
        return
    if CHALLENGE_TITLE.fullmatch((await page.title()).strip()):
        execution.deny("Site requested verification or denied access")
        return
    widgets = page.locator(CHALLENGE_WIDGETS)
    for widget in (await widgets.all())[:20]:
        if await widget.is_visible():
            execution.deny("Site requested interactive verification")
            return
    if await page.get_by_text("Verify you are human", exact=True).first.is_visible():
        execution.deny("Site requested interactive verification")


def _secret_value(reference: str, execution: Execution) -> str:
    key = reference.upper()
    if not SECRET_REF.fullmatch(key):
        raise StepFailed("Invalid secret reference")
    if key not in execution.secrets:
        value = os.environ.get(f"ABRL_SECRET_{key}")
        if value is None:
            raise StepFailed("A referenced workflow secret is missing")
        execution.secrets[key] = value
    return execution.secrets[key]


def _record_extraction(text: str, execution: Execution) -> None:
    visible = SAFE_OMIT.sub("", text)
    for secret in execution.secrets.values():
        if secret:
            visible = visible.replace(secret, "[redacted]")
    budget = min(1_000, execution.extraction_budget)
    captured = visible[:budget]
    execution.extraction_budget -= len(captured)
    execution.metadata.setdefault("extractions", []).append(
        {
            "step": execution.active_step,
            "text": captured,
            "truncated": len(visible) > len(captured),
        }
    )


async def _execute_step(page: Page, step: dict, execution: Execution) -> None:
    action = step["action"]
    timeout = step.get("timeout_ms") or DEFAULT_STEP_TIMEOUT_MS
    if action == "navigate":
        url = urljoin(execution.origin + "/", step["url"])
        if not execution.permits(url, document=True):
            execution.deny("Navigation outside the registered target scope")
            return
        execution.started_action = True
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        if response and response.status >= 400 and not execution.block.is_set():
            raise StepFailed(f"Navigation returned HTTP {response.status}")
        return

    locator = page.locator(step["selector"])
    value = step.get("value")
    if action == "click":
        await locator.click(timeout=timeout)
    elif action == "fill":
        if step.get("secret_ref"):
            value = _secret_value(step["secret_ref"], execution)
        await locator.fill(value, timeout=timeout)
    elif action == "select":
        await locator.select_option(value=value, timeout=timeout)
    elif action in {"wait_for", "assert_visible"}:
        await locator.wait_for(state="visible", timeout=timeout)
    elif action in {"assert_text", "extract"}:
        await locator.wait_for(state="visible", timeout=timeout)
        visible_text = await locator.inner_text(timeout=timeout)
        if action == "extract":
            _record_extraction(visible_text, execution)
        elif value not in visible_text:
            raise StepFailed("Expected text was not present")
    else:
        raise StepFailed("Unsupported workflow step")


async def _drive(steps: list[dict], execution: Execution) -> None:
    async with EgressGuard(execution.origin, list(execution.resources), execution.deny) as guard:
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(
                channel="chromium",
                chromium_sandbox=True,
                proxy={"server": guard.url},
                args=[
                    "--disable-quic",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                    "--proxy-bypass-list=<-loopback>",
                ],
            )
            try:
                context = await browser.new_context(accept_downloads=False, service_workers="block")
                try:
                    page = await context.new_page()
                    session = await context.new_cdp_session(page)
                    await _attach_page_policy(context, page, session, execution)
                    execution.metadata["browser_version"] = browser.version
                    # Resolve references before sending any request.
                    for step in steps:
                        if step.get("secret_ref"):
                            _secret_value(step["secret_ref"], execution)
                    for index, step in enumerate(steps, start=1):
                        execution.active_step = index
                        if execution.block.is_set():
                            return
                        await _check_challenge(page, execution)
                        if execution.block.is_set():
                            return
                        await _execute_step(page, step, execution)
                        await _check_challenge(page, execution)
                        if execution.block.is_set():
                            return
                        execution.steps_completed = index
                finally:
                    await context.close()
            finally:
                await browser.close()


async def execute_workflow(
    *,
    origin: str,
    resource_origins: list[str],
    steps: list[dict],
    cancel: Callable[[], Awaitable[bool]],
    correlation: str | None = None,
    timeout_seconds: float = 60,
) -> BrowserResult:
    """Execute one trial; no workflow code or persistent browser state is accepted."""
    started = monotonic()
    metadata = {
        "adapter": "playwright",
        "playwright_version": version("playwright"),
        "os": platform.system(),
        "architecture": platform.machine(),
        "session": "isolated",
    }
    if correlation is not None:
        metadata["correlation"] = correlation

    outcome: Literal["completed", "failed", "blocked", "cancelled"] = "failed"
    error = None
    execution = Execution(origin, set(resource_origins), metadata=metadata)
    try:
        execution.origin = normalize_origin(origin)
        execution.resources = {normalize_origin(resource) for resource in resource_origins}
        if not steps or steps[0].get("action") != "navigate":
            raise StepFailed("A workflow must begin with navigation")
        if await cancel():
            return BrowserResult("cancelled", 0, None, metadata, False)
        operation = asyncio.create_task(_drive(steps, execution))
        cancellation = asyncio.create_task(_await_cancellation(cancel))
        blocked = asyncio.create_task(execution.block.wait())
        tasks = (operation, cancellation, blocked)
        try:
            async with asyncio.timeout(timeout_seconds):
                finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if cancellation in finished:
                    cancellation.result()
                    outcome = "cancelled"
                elif execution.block.is_set():
                    outcome, error = "blocked", execution.reason
                else:
                    operation.result()
                    outcome = "completed"
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except NetworkDenied as exc:
        outcome, error = "blocked", str(exc)
    except StepFailed as exc:
        error = str(exc)
    except PlaywrightTimeout:
        error = (
            f"Step {execution.active_step} timed out"
            if execution.started_action
            else "Chromium startup timed out"
        )
    except TimeoutError:
        error = "Workflow exceeded its time budget"
    except PlaywrightError:
        error = (
            f"Chromium could not complete step {execution.active_step}"
            if execution.started_action
            else "Chromium could not start or establish an isolated session"
        )
    except (KeyError, TypeError, ValueError):
        error = f"Step {execution.active_step} is invalid"

    metadata.update(
        {"steps_completed": execution.steps_completed, "blocked_resources": execution.blocked_resources}
    )
    return BrowserResult(
        outcome,
        round((monotonic() - started) * 1000),
        error,
        metadata,
        execution.started_action,
    )
