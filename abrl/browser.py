"""Execute a bounded QA workflow in a stock Chromium session."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import platform
import random
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
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .adapters import AdapterUnavailable, launch_adapter
from .network import (
    FIXTURE_ORIGIN,
    EgressGuard,
    NetworkDenied,
    is_fixture_path,
    normalize_origin,
    request_origin,
)
from .profile_storage import ProfileStorageError
from .proxy import OxylabsBinding, ProxyConfigurationError

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
    last_assertion_step: int | None = None
    metadata: dict = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    extraction_budget: int = MAX_EXTRACTED_CHARACTERS
    phase: str = "workflow"
    navigation_origins: set[str] = field(default_factory=set)
    emit: Callable[[dict], Awaitable[None]] | None = None
    console_messages: list[dict] = field(default_factory=list)
    request_budget: int = 1000
    request_counts: dict[str, int] = field(default_factory=dict)
    request_budget_exhausted: bool = False

    def admit_request(self) -> bool:
        if sum(self.request_counts.values()) >= self.request_budget:
            self.request_budget_exhausted = True
            self.deny("HTTP request budget exhausted")
            return False
        self.request_counts[self.phase] = self.request_counts.get(self.phase, 0) + 1
        return True

    async def event(self, kind: str, **data):
        if self.emit:
            await self.emit({"kind": kind, **data})

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
        if self.phase == "automated_preparation" and method not in {"GET", "HEAD"}:
            return False
        return origin in {self.origin, *self.navigation_origins} or (
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
    context: BrowserContext,
    page: Page,
    session: CDPSession,
    execution: Execution,
    proxy_auth: dict | None = None,
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
        elif not execution.admit_request():
            await route.abort()
        else:
            await route.continue_()

    async def intercept(params: dict) -> None:
        request_id = params["requestId"]
        document = params.get("resourceType") == "Document"
        request = params["request"]
        allowed = execution.permits(request["url"], document=document, method=request["method"])
        # Playwright routes only the first request in a redirect chain. CDP
        # pauses every redirected hop, so account those separately here.
        if allowed and params.get("redirectedRequestId") and not execution.block.is_set():
            allowed = execution.admit_request()
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

    async def authenticate(params: dict) -> None:
        response = {"response": "Default"}
        if proxy_auth and params.get("authChallenge", {}).get("source") == "Proxy":
            response = {
                "response": "ProvideCredentials",
                "username": proxy_auth["username"],
                "password": proxy_auth["password"],
            }
        try:
            await session.send(
                "Fetch.continueWithAuth",
                {"requestId": params["requestId"], "authChallengeResponse": response},
            )
        except PlaywrightError:
            return

    session.on("Fetch.requestPaused", intercept)
    if proxy_auth:
        session.on("Fetch.authRequired", authenticate)
    page.on("response", inspect_response)
    page.on("framenavigated", inspect_navigation)
    page.on("popup", close_popup)
    await context.route("**/*", restrict_context)
    await context.route_web_socket("**/*", close_socket)
    await session.send(
        "Fetch.enable",
        {
            "patterns": [{"urlPattern": "*", "requestStage": "Request"}],
            "handleAuthRequests": bool(proxy_auth),
        },
    )


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
    if action == "checkpoint":
        name = step.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", name):
            raise StepFailed("Checkpoint requires a valid name")
        if execution.last_assertion_step != execution.active_step - 1:
            raise StepFailed("Checkpoint requires an immediately preceding successful assertion")
        checkpoints = execution.metadata.setdefault("checkpoints", [])
        if any(item["name"] == name for item in checkpoints):
            raise StepFailed("Checkpoint names must be unique within a workflow")
        boundary = {
            "name": name,
            "step": execution.active_step,
            "assertion_step": execution.last_assertion_step,
            "non_idempotent_action_started": bool(execution.metadata.get("non_idempotent_action_started")),
            "replay_allowed": False,
        }
        # Checkpoints record progress; they never reset action history or grant retry permission.
        checkpoints.append(boundary)
        await execution.event("checkpoint_reached", **boundary)
        return
    if action == "navigate":
        url = urljoin(execution.origin + "/", step["url"])
        if not execution.permits(url, document=True):
            execution.deny("Navigation outside the registered target scope")
            return
        await execution.event(
            "action_started", action="navigate", step=execution.active_step, non_idempotent=False
        )
        execution.started_action = True
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        if request_origin(url) == FIXTURE_ORIGIN and "/lab/research?" in url:
            await page.wait_for_url(
                lambda current: "/lab/research" not in str(current),
                timeout=40_000,
                wait_until="domcontentloaded",
            )
        if response and response.status >= 400 and not execution.block.is_set():
            raise StepFailed(f"Navigation returned HTTP {response.status}")
        return

    locator = page.locator(step["selector"])
    value = step.get("value")
    if action in {"click", "fill", "select"}:
        await execution.event(
            "action_started", action=action, step=execution.active_step, non_idempotent=True
        )
        execution.metadata["non_idempotent_action_started"] = True
    if action == "click":
        execution.metadata["non_idempotent_action_started"] = True
        await locator.click(timeout=timeout)
    elif action == "fill":
        if step.get("secret_ref"):
            value = _secret_value(step["secret_ref"], execution)
        await locator.fill(value, timeout=timeout)
    elif action == "select":
        await locator.select_option(value=value, timeout=timeout)
    elif action in {"wait_for", "assert_visible", "assert_not_visible"}:
        await locator.wait_for(
            state="hidden" if action == "assert_not_visible" else "visible", timeout=timeout
        )
    elif action in {"assert_text", "extract"}:
        await locator.wait_for(state="visible", timeout=timeout)
        visible_text = await locator.inner_text(timeout=timeout)
        if action == "extract":
            _record_extraction(visible_text, execution)
        elif value not in visible_text:
            raise StepFailed("Expected text was not present")
    else:
        raise StepFailed("Unsupported workflow step")
    if action in {"assert_visible", "assert_not_visible", "assert_text"}:
        execution.last_assertion_step = execution.active_step


def preparation_plan(config: dict) -> dict:
    """Deterministic bounded navigation/dwell/scroll plan; never transactional clicks."""
    seed = config.get("seed", 0)
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise StepFailed("Preparation seed must be an integer")
    low, high = float(config.get("min_seconds", 30)), float(config.get("max_seconds", 90))
    if not math.isfinite(low + high) or not 0 < low <= high <= 300:
        raise StepFailed("Preparation duration must be positive, ordered and at most 300 seconds")
    paths = config.get("paths") or []
    if (
        not isinstance(paths, list)
        or len(paths) > 20
        or any(not isinstance(p, str) or not p.startswith("/") or p.startswith("//") for p in paths)
    ):
        raise StepFailed("Preparation paths must be approved relative paths")
    rng = random.Random(seed)
    duration = rng.uniform(low, high)
    at, actions = 0.0, []
    while at < duration and len(actions) < 300:
        action = {"at_seconds": round(at, 4), "action": "scroll", "delta_y": rng.randint(-200, 500)}
        if paths and rng.random() < 0.2:
            action = {"at_seconds": round(at, 4), "action": "navigate", "url": rng.choice(paths)}
        actions.append(action)
        at += rng.uniform(1, 3)
    return {"seed": seed, "duration_seconds": duration, "actions": actions}


async def _prepare(page: Page, steps: list[dict], execution: Execution, config: dict, control) -> None:
    mode = config.get("mode", "none")
    execution.metadata["preparation_mode"] = mode
    if mode == "none":
        return
    began = monotonic()
    execution.phase = "automated_preparation" if mode == "automated" else "manual_preparation"
    await execution.event("preparation_started", mode=mode)
    # Navigation is still bounded by target scope, even before workflow steps.
    await _execute_step(page, steps[0], execution)
    await _check_challenge(page, execution)
    if execution.block.is_set():
        return
    if mode == "automated":
        plan = preparation_plan(config)
        execution.metadata["preparation"] = {**plan, "ground_truth": "automated", "executed": []}
        schedule_start = monotonic()
        for action in plan["actions"]:
            await asyncio.sleep(max(0, schedule_start + action["at_seconds"] - monotonic()))
            if action["action"] == "navigate":
                await _execute_step(page, action, execution)
            else:
                await page.mouse.wheel(0, action["delta_y"])
            execution.metadata["preparation"]["executed"].append(action)
            await execution.event("preparation_action", **action, ground_truth="automated")
            await _check_challenge(page, execution)
            if execution.block.is_set():
                return
        await asyncio.sleep(max(0, schedule_start + plan["duration_seconds"] - monotonic()))
    elif mode == "manual":
        if control is None:
            raise StepFailed("Manual preparation requires an authenticated operator channel")
        idle_timeout = float(config.get("idle_timeout_seconds", 900))
        lifetime = float(config.get("lifetime_seconds", 3600))
        if not 1 <= idle_timeout <= 3600 or not idle_timeout <= lifetime <= 86400:
            raise StepFailed("Invalid manual idle timeout or lifetime")
        last_input, last_frame = monotonic(), 0.0
        manual_actions = 0
        await execution.event("manual_control_ready", idle_timeout_seconds=idle_timeout)
        while True:
            now = monotonic()
            if now - last_input >= idle_timeout or now - began >= lifetime:
                raise StepFailed("Manual control expired before handoff")
            command = await control()
            if command:
                last_input = now
                kind = command.get("command")
                if kind == "continue":
                    await execution.event("manual_handoff", actions=manual_actions)
                    break
                if kind == "stop":
                    raise StepFailed("Operator stopped the session")
                if kind in {"click", "type", "key"}:
                    await execution.event("action_started", action="manual_" + kind, non_idempotent=True)
                    execution.metadata["non_idempotent_action_started"] = True
                if kind == "click":
                    x, y = float(command["x"]), float(command["y"])
                    if not math.isfinite(x + y) or not 0 <= x <= 10000 or not 0 <= y <= 10000:
                        raise StepFailed("Manual coordinates are invalid")
                    execution.metadata["non_idempotent_action_started"] = True
                    await page.mouse.click(x, y)
                elif kind == "type":
                    text = command.get("text", "")
                    if not isinstance(text, str) or len(text) > 4000:
                        raise StepFailed("Manual input exceeds its size budget")
                    await page.keyboard.insert_text(text)
                elif kind == "key":
                    key = command.get("key", "")
                    if key not in {
                        "Enter",
                        "Tab",
                        "Shift+Tab",
                        "Escape",
                        "Backspace",
                        "ArrowDown",
                        "ArrowUp",
                        "ArrowLeft",
                        "ArrowRight",
                        "Space",
                    }:
                        raise StepFailed("Manual key is not permitted")
                    execution.metadata["non_idempotent_action_started"] = True
                    await page.keyboard.press(key)
                elif kind == "scroll":
                    delta = float(command.get("delta_y", 0))
                    if not math.isfinite(delta) or abs(delta) > 3000:
                        raise StepFailed("Manual scroll exceeds its bounds")
                    await page.mouse.wheel(0, delta)
                elif kind == "navigate":
                    await _execute_step(page, {"action": "navigate", "url": command["url"]}, execution)
                else:
                    raise StepFailed("Unknown manual control command")
                manual_actions += 1
                await execution.event("manual_input", command=kind)  # never store text or key values
                await _check_challenge(page, execution)
                if execution.block.is_set():
                    return
            if execution.emit and now - last_frame >= 1:
                shot = await page.screenshot(type="jpeg", quality=55, timeout=3000)
                viewport = page.viewport_size or {"width": 1280, "height": 720}
                await execution.event(
                    "manual_frame", jpeg_base64=base64.b64encode(shot).decode(), **viewport
                )
                last_frame = now
            await asyncio.sleep(0.1)
        execution.metadata["manual_actions"] = manual_actions
    else:
        raise StepFailed("Unknown preparation mode")
    execution.phase = "workflow"
    execution.metadata["preparation_elapsed_ms"] = round((monotonic() - began) * 1000)
    await execution.event("preparation_completed", elapsed_ms=execution.metadata["preparation_elapsed_ms"])
    await execution.event("preparation_finished", elapsed_ms=execution.metadata["preparation_elapsed_ms"])


async def _capture_artifacts(page: Page, execution: Execution, steps: list[dict], policy: str) -> None:
    failed = execution.block.is_set() or execution.steps_completed != len(steps)
    if policy == "none" or (policy == "on_failure" and not failed):
        return
    if execution.emit is None:
        execution.metadata["artifact_capture_error"] = "No artifact persistence channel is configured"
        return
    try:
        masks = [page.locator("input[type=password], [data-abrl-secret]")]
        masks.extend(
            page.locator(step["selector"])
            for step in steps
            if step.get("secret_ref") and step.get("selector")
        )
        screenshot = await page.screenshot(type="png", full_page=False, mask=masks, timeout=3000)
        if len(screenshot) > 4 * 1024**2:
            raise ValueError("Screenshot exceeds artifact limit")
        await execution.event(
            "artifact",
            media_type="image/png",
            name="final.png",
            data_base64=base64.b64encode(screenshot).decode(),
        )
        messages = []
        for entry in execution.console_messages:
            text = entry["text"]
            for secret in execution.secrets.values():
                text = text.replace(secret, "[redacted]") if secret else text
            text = re.sub(r"([?&](?:abrl_context|context)=)[^&\s]+", r"\1[redacted]", text)
            messages.append({**entry, "text": text})
        await execution.event(
            "artifact",
            media_type="application/json",
            name="console.json",
            data_base64=base64.b64encode(json.dumps(messages).encode()).decode(),
        )
        execution.metadata["artifact_capture"] = "completed"
    except Exception:
        execution.metadata["artifact_capture_error"] = "Bounded screenshot/console capture was unavailable"


async def _drive(
    steps: list[dict],
    execution: Execution,
    *,
    adapter: str,
    profile_dir: str | None,
    preparation: dict,
    proxy: dict | None,
    control,
    expected_compatibility: dict | None,
    remote: dict | None,
    artifact_policy: str,
    traffic_budget_bytes: int,
) -> None:
    upstream = OxylabsBinding.from_config(proxy) if proxy and not remote else None
    if upstream:
        execution.metadata["proxy"] = upstream.public_metadata()
    if proxy:
        execution.metadata["proxy"] = {
            **execution.metadata.get("proxy", {}),
            **{
                key: proxy.get(key)
                for key in (
                    "provider",
                    "product",
                    "country",
                    "cost_per_gb",
                    "cost_currency",
                    "max_connections",
                )
            },
        }
    execution.metadata["egress_enforcement"] = "local_browser_proxy; host OS egress is not sandboxed"
    async with EgressGuard(
        execution.origin,
        list(execution.resources),
        execution.deny,
        upstream=upstream,
        navigation_origins=sorted(execution.navigation_origins),
        traffic_budget_bytes=traffic_budget_bytes,
        max_connections=proxy.get("max_connections", 1) if proxy else 32,
    ) as guard:
        guard.forwarding_enabled = False
        try:
            await execution.event("launching", adapter=adapter)
            async with launch_adapter(
                adapter,
                guard.url,
                profile_dir,
                execution.metadata,
                remote=remote,
                expected_compatibility=expected_compatibility,
            ) as runtime:
                page = None
                try:
                    context = runtime.context
                    # Persistent launches can restore pages. Close them before any
                    # permitted target navigation; all newly opened windows are denied.
                    page = context.pages[0] if context.pages else await context.new_page()
                    for extra in context.pages[1:]:
                        await extra.close()

                    def collect_console(message):
                        if len(execution.console_messages) < 100:
                            execution.console_messages.append(
                                {"type": message.type, "text": message.text[:2000]}
                            )

                    page.on("console", collect_console)
                    session = await context.new_cdp_session(page)
                    await _attach_page_policy(
                        context, page, session, execution, proxy_auth=getattr(runtime, "proxy_auth", None)
                    )
                    actual = await session.send("Browser.getVersion")
                    execution.metadata.update(
                        browser_version=actual.get("product"),
                        browser_revision=actual.get("revision"),
                        protocol_version=actual.get("protocolVersion"),
                        actual_user_agent=actual.get("userAgent"),
                    )
                    execution.metadata["profile_compatible"] = True
                    if expected_compatibility is not None and any(
                        expected_compatibility.get(key) != execution.metadata.get(key)
                        for key in (
                            "adapter",
                            "browser_version",
                            "os",
                            "architecture",
                            "wrapper_version",
                            "engine_version",
                        )
                        if key in expected_compatibility
                    ):
                        execution.metadata["profile_compatible"] = False
                        raise StepFailed("Persistent profile browser/platform compatibility does not match")
                    guard.forwarding_enabled = True
                    if remote:
                        await runtime.activate(traffic_budget_bytes=traffic_budget_bytes)
                    await execution.event(
                        "launched", adapter=adapter, browser_version=actual.get("product")
                    )
                    for step in steps:
                        if step.get("secret_ref"):
                            _secret_value(step["secret_ref"], execution)
                    await _prepare(page, steps, execution, preparation, control)
                    if execution.block.is_set():
                        return
                    workflow_start = monotonic()
                    await execution.event("workflow_started")
                    async with asyncio.timeout(execution.metadata["time_budgets"]["workflow_seconds"]):
                        for index, step in enumerate(steps, start=1):
                            execution.active_step = index
                            if adapter == "manual_chrome" and step.get("action") not in {
                                "assert_text",
                                "assert_visible",
                                "assert_not_visible",
                                "extract",
                                "wait_for",
                                "checkpoint",
                            }:
                                continue  # Human performs the action; automation only measures success.
                            if execution.block.is_set():
                                return
                            await _check_challenge(page, execution)
                            if execution.block.is_set():
                                return
                            await execution.event("workflow_step", step=index, action=step.get("action"))
                            await _execute_step(page, step, execution)
                            await _check_challenge(page, execution)
                            if execution.block.is_set():
                                return
                            execution.steps_completed = index
                    execution.metadata["workflow_outcome"] = "completed"
                    execution.metadata["workflow_elapsed_ms"] = round((monotonic() - workflow_start) * 1000)
                finally:
                    if page is not None:
                        await _capture_artifacts(page, execution, steps, artifact_policy)

        finally:
            if not remote:
                execution.metadata["transport_events"] = guard.transport_events
                execution.metadata["transport_summary"] = guard.transport_summary()
                execution.metadata["transport_usage"] = guard.transport_usage()
                execution.metadata["vendor_control_bytes"] = guard.vendor_control_bytes
            transport_events = execution.metadata.get("transport_events", [])
            if transport_events:
                execution.metadata["failure_kind"] = transport_events[-1]["kind"]
                if execution.metadata.get("non_idempotent_action_started"):
                    for event in transport_events:
                        if event["retryable"]:
                            event.update(retryable=False, action="needs_reconciliation")
            await execution.event(
                "browser_closed",
                profile_saved=execution.metadata.get("profile_saved", False),
                confirmed=execution.metadata.get("browser_closed", False),
            )


async def execute_workflow(
    *,
    origin: str,
    resource_origins: list[str],
    navigation_origins: list[str] | None = None,
    steps: list[dict],
    cancel: Callable[[], Awaitable[bool]],
    correlation: str | None = None,
    timeout_seconds: float = 60,
    adapter: str = "playwright",
    profile_dir: str | None = None,
    preparation: dict | None = None,
    proxy: dict | None = None,
    emit: Callable[[dict], Awaitable[None]] | None = None,
    control: Callable[[], Awaitable[dict | None]] | None = None,
    expected_compatibility: dict | None = None,
    remote: dict | None = None,
    artifact_policy: str = "none",
    request_budget: int = 1000,
    traffic_budget_bytes: int = 104857600,
) -> BrowserResult:
    """Execute one bounded assignment with supported adapter and explicit profile ownership."""
    started = monotonic()
    metadata = {
        "adapter": adapter,
        "playwright_version": version("playwright"),
        "os": platform.system(),
        "architecture": platform.machine(),
        "session": "persistent" if profile_dir else "fresh",
        "profile_saved": False,
        "runtime_stopped": False,
        "ground_truth": "human"
        if adapter == "manual_chrome"
        else "mixed"
        if preparation and preparation.get("mode") == "manual"
        else "automated",
    }
    if correlation is not None:
        metadata["correlation"] = correlation

    outcome: Literal["completed", "failed", "blocked", "cancelled"] = "failed"
    error = None
    execution = Execution(origin, set(resource_origins), metadata=metadata, emit=emit)
    try:
        if type(request_budget) is not int or not 1 <= request_budget <= 100000:
            raise StepFailed("HTTP request budget must be an integer from 1 to 100000")
        if type(traffic_budget_bytes) is not int or not 1024 <= traffic_budget_bytes <= 2 * 1024**3:
            raise StepFailed("Traffic budget must be an integer from 1024 bytes to 2 GiB")
        execution.request_budget = request_budget
        if artifact_policy not in {"none", "on_failure", "all"}:
            raise StepFailed("Invalid artifact retention policy")
        execution.origin = normalize_origin(origin)
        execution.navigation_origins = {normalize_origin(value) for value in navigation_origins or []}
        if any(
            not value.startswith("https://") for value in execution.navigation_origins - {execution.origin}
        ):
            raise StepFailed("Additional navigation origins must use HTTPS")
        execution.resources = {normalize_origin(resource) for resource in resource_origins}
        if not steps or steps[0].get("action") != "navigate":
            raise StepFailed("A workflow must begin with navigation")
        if await cancel():
            return BrowserResult("cancelled", 0, None, metadata, False)
        preparation = dict(preparation or {"mode": "none"})
        if adapter == "manual_chrome":
            preparation["mode"] = "manual"
            if not any(step.get("action", "").startswith("assert_") for step in steps):
                raise StepFailed("Manual Chrome workflows require an explicit success assertion")
        if preparation.get("mode") == "manual" and control is None:
            raise StepFailed("Manual preparation requires an authenticated operator channel")
        if preparation.get("mode") == "automated":
            preparation_plan(preparation)  # Validate before opening the browser.
        preparation_budget = (
            float(preparation.get("max_seconds", 90))
            if preparation.get("mode") == "automated"
            else float(preparation.get("lifetime_seconds", 3600))
            if preparation.get("mode") == "manual"
            else 0
        )
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 86400:
            raise StepFailed("Workflow time budget must be positive and finite")
        metadata["time_budgets"] = {
            "workflow_seconds": timeout_seconds,
            "preparation_seconds": preparation_budget,
        }
        operation = asyncio.create_task(
            _drive(
                steps,
                execution,
                adapter=adapter,
                profile_dir=profile_dir,
                preparation=preparation,
                proxy=proxy,
                control=control,
                expected_compatibility=expected_compatibility,
                remote=remote,
                artifact_policy=artifact_policy,
                traffic_budget_bytes=traffic_budget_bytes,
            )
        )
        cancellation = asyncio.create_task(_await_cancellation(cancel))
        blocked = asyncio.create_task(execution.block.wait())
        tasks = (operation, cancellation, blocked)
        try:
            async with asyncio.timeout(timeout_seconds + preparation_budget):
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
    except AdapterUnavailable as exc:
        error = str(exc)
        metadata["adapter_status"] = exc.status
    except (StepFailed, ProfileStorageError, ProxyConfigurationError) as exc:
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
    except Exception:
        # Preserve confirmed cleanup metadata even when a controller callback or
        # optional vendor driver fails unexpectedly. Never infer browser death.
        error = "Session infrastructure interrupted execution"

    metadata["request_usage"] = {
        "total": sum(execution.request_counts.values()),
        "by_phase": execution.request_counts,
        "budget": execution.request_budget,
        "exhausted": execution.request_budget_exhausted,
        "measurement": "intercepted_http_requests",
    }
    traffic_exhausted = metadata.get("transport_usage", {}).get("budget_exhausted", False)
    if execution.request_budget_exhausted or traffic_exhausted:
        outcome = "blocked"
        error = (
            "HTTP request budget exhausted"
            if execution.request_budget_exhausted
            else "Browser traffic byte budget exhausted"
        )
    metadata.setdefault("workflow_outcome", outcome)
    if metadata.get("non_idempotent_action_started") and outcome not in {"completed"}:
        metadata["needs_reconciliation"] = True
    if metadata.get("profile_compatible") is False:
        metadata["profile_saved"] = False
        metadata["profile_save_failed"] = True
    if outcome == "blocked":
        metadata["failure_kind"] = (
            "budget_exhausted"
            if execution.request_budget_exhausted or traffic_exhausted
            else "site_denial"
            if error and "Site " in error
            else "scope_denied"
        )
    if metadata.get("cleanup_error") and outcome == "completed":
        outcome, error = "failed", metadata["cleanup_error"]
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
