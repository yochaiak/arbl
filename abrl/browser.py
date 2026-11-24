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
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from importlib.metadata import version
from inspect import isawaitable
from pathlib import Path
from time import monotonic
from typing import Literal
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlsplit, urlunsplit
from weakref import WeakSet

from playwright.async_api import (
    BrowserContext,
    CDPSession,
    Dialog,
    Locator,
    Page,
    Request,
    Response,
    Route,
    WebSocketRoute,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .adapters import AdapterUnavailable, launch_adapter, quic_launch_check
from .batch_presets import PresetDetails, preset_details
from .browser_network_trace import (
    PAGE_ASSET_TYPES,
    SUBRESOURCE_TYPES,
    AdScoreInitiatorTrace,
    InstrumentedNetworkTrace,
    NavigationSourceTrace,
)
from .browser_settings import (
    DevicePreset,
    device_viewport,
    operator_controlled,
    operator_reserved_step,
    session_capabilities,
)
from .config import (
    ADSCORE_ENGINE_ORIGINS,
    ADSCORE_PING_HOST,
    CLOAKBROWSER_GEOIP_ORIGINS,
    CLOAKBROWSER_LICENSE_ORIGIN,
)
from .connect_telemetry import NavigationConnectTrace
from .diagnostic_fixture import DIAGNOSTIC_GROUPS, DIAGNOSTIC_PATH, DIAGNOSTIC_PROBE_FAILURES, ECHO_PATH
from .javascript_capture import JavaScriptCapture
from .lifecycle import LifecycleTimeline
from .network import (
    FIXTURE_ORIGIN,
    EgressGuard,
    NetworkDenied,
    is_fixture_path,
    normalize_origin,
    request_origin,
)
from .platform_schemas import validate_referrer
from .profile_storage import ProfileStorageError
from .proxy import ProxyBinding, ProxyConfigurationError
from .recording import RecordingSegment, SessionRecorder
from .schemas import (
    ASSERTIONS,
    CONTROL_ACTIONS,
    MAX_NAVIGATION_TIMEOUT_MS,
    NAVIGATION_ACTIONS,
    resolve_navigation_parameters,
    resolve_workflow_inputs,
    validate_navigation_workflow_scope,
    validate_workflow_page_paths,
    walk_workflow_steps,
    workflow_execution_limit,
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
DIRECT_INITIAL_NAVIGATION_TIMEOUT_MS = 5_000
PROXY_INITIAL_NAVIGATION_TIMEOUT_MS = 15_000
MAX_EXTRACTED_CHARACTERS = 4_000
MAX_SINGLE_EXTRACTION_CHARACTERS = 1_000
SAFE_OMIT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
KAMELEO_CONTROL_ORIGINS = {"https://tools.kameleo.io", "https://tools-bckp1.kameleo.io"}
MAX_ADSCORE_REQUEST_FAILURES = 32
_DIAGNOSTIC_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._~-]{1,48}\Z")
_CHROMIUM_NETWORK_ERROR = re.compile(r"net::ERR_[A-Z0-9_]{1,60}\Z")
_CHROMIUM_ERROR_PAGE = "chrome-error://chromewebdata/"
_GENERIC_ABORT_ERRORS = {"net::ERR_ABORTED", "net::ERR_BLOCKED_BY_CLIENT", "net::ERR_FAILED"}
_REFERRER_POLICIES = frozenset({
    "no-referrer", "no-referrer-when-downgrade", "origin", "origin-when-cross-origin",
    "same-origin", "strict-origin", "strict-origin-when-cross-origin", "unsafe-url",
})
_WORKFLOW_WARNING_CODES = ("ADSCORE_SCRIPT_RULE", "OFFER_TARGETING_MISMATCH")
_NAVIGATION_CHECKS = NAVIGATION_ACTIONS | {"wait_for_url", "assert_url"}
_INPUT_ACTIONS = frozenset({"click", "fill", "select", "check", "uncheck", "press", "hover"})
_SELECTOR_ACTIONS = frozenset({
    "click", "fill", "select", "extract", "check", "uncheck", "press", "hover", "wait_for",
    "wait_for_hidden", "wait_for_text", "assert_visible", "assert_not_visible", "assert_text",
})
_ADSCORE_ERROR_BODY_LIMIT = 2048
_ADSCORE_ERROR_BASE64_LIMIT = (_ADSCORE_ERROR_BODY_LIMIT + 2) // 3 * 4
_ADSCORE_ERROR_MEDIA_TYPES = {"text/plain", "text/html", "application/json", "application/problem+json"}
_ADSCORE_ERROR_CODE_FIELDS = ("error_code", "status_code", "code", "error", "status", "e")
_ADSCORE_ERROR_TERMS = frozenset("""
    access account active allowed api balance billing blacklist blocked credit credits daily deactivated denied disabled
    domain eligible empty enough exhausted expired failed forbidden funds inappropriate inactive invalid key limit locked
    low method missing money negative no not origin paid payment prepaid quota referrer rejected required restriction
    signature suspended trial unknown unpaid unsupported zero zone
""".split())
_ADSCORE_REFERRER_PROSE = _ADSCORE_ERROR_TERMS | frozenset("""
    a absent accepted allow allowlist an any approved are as be because by can cannot check configured contains
    detected did do does enabled entry excluded expected for found from has have header http in is list listed
    match matched matches matching meet meets must of on only or passed pattern permitted please policy present
    provide provided received recognised recognized referer registered regex regular rules satisfies sent set specified
    that the to unable valid validation value was were whitelist whitelisted will with without you your
""".split())
_ADSCORE_ERROR_REASONS = {
    "insufficient_balance": (
        r"(?:insufficient|not enough|out of|no) (?:available )?(?:funds?|credits?|balance)"
        r"|(?:funds?|credits?|balance) (?:is |are |has been )?(?:exhausted|too low|negative|empty|depleted|zero)"
    ),
    "request_signature_required": r"(?:missing|provide a) request signature|request signature (?:is )?required",
    "invalid_request_signature": r"invalid request signature|request signature (?:is )?invalid",
    "invalid_zone_key": r"invalid (?:zone |api )?key|unknown zone|zone not found",
    "inactive_zone": r"(?:disabled|inactive) zone|zone (?:is )?(?:disabled|inactive)",
    "inactive_account": r"(?:disabled|inactive|suspended|deactivated) account|account (?:is )?(?:disabled|inactive|suspended|deactivated)",
    "domain_rejected": r"(?:domain|origin|referrer) (?:is )?(?:not allowed|denied|blocked|rejected)",
    "payment_required": r"payment required",
}


class StepFailed(ValueError):
    """A known workflow error with safe details for the operator."""

    def __init__(self, message: str, *, reason: str = "step_failed"):
        super().__init__(message)
        self.reason = reason


def _navigation_scope_error(url: str, *, popup: bool = False) -> str:
    action = "Expected popup attempted navigation" if popup else "Navigation"
    message = f"{action} outside the registered target scope"
    try:
        origin = request_origin(url)
    except NetworkDenied:
        return message
    return f"{message} ({origin})"


@dataclass(slots=True)
class BrowserResult:
    outcome: Literal["completed", "failed", "blocked", "cancelled"]
    elapsed_ms: int
    error: str | None
    metadata: dict
    started_action: bool


@dataclass
class ExpectedPopup:
    opener: Page
    initial_url: str | None = None
    observed: Page | None = None


@dataclass
class NavigationActivity:
    requests: int = 0
    responses: int = 0
    commits: int = 0
    last_status: int | None = None


@dataclass(slots=True)
class NavigationWait:
    load_timeout_ms: int
    post_navigation_wait_ms: int
    load_timeout_source: str | None = None
    stage: str = "navigation_setup"
    started_at: float = field(default_factory=monotonic)

    def begin(self, stage: str) -> None:
        self.stage = stage
        self.started_at = monotonic()

    def details(self, deadline: str) -> dict:
        return {
            "stage": self.stage,
            "deadline": deadline,
            "stage_elapsed_ms": round((monotonic() - self.started_at) * 1000),
            "load_timeout_ms": self.load_timeout_ms,
            "post_navigation_wait_ms": self.post_navigation_wait_ms,
            **({"load_timeout_source": self.load_timeout_source} if self.load_timeout_source else {}),
        }

    @property
    def total_seconds(self) -> float:
        return (self.load_timeout_ms + self.post_navigation_wait_ms) / 1000


@dataclass
class Execution:
    origin: str
    resources: set[str]
    block: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str | None = None
    terminal_failed: bool = False
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
    initial_referrer: str | None = None
    wait_for_observation: Callable[[], Awaitable[bool]] | None = None
    recording: SessionRecorder | None = None
    recording_wait_seconds: float = 0.0
    javascript_capture: JavaScriptCapture | None = None
    javascript_wait_seconds: float = 0.0
    network_trace: InstrumentedNetworkTrace | None = None
    counted_subresource_failures: WeakSet[Request] = field(default_factory=WeakSet, repr=False)
    controlled_adscore_ping_context: str | None = field(default=None, repr=False)
    adscore_referrer_reads: set[asyncio.Task[None]] = field(default_factory=set, repr=False)
    workflow_version: int = 1
    success_check: dict | None = None
    values: dict = field(default_factory=dict, repr=False)
    pages: dict[str, Page] = field(default_factory=dict)
    navigation_activity: NavigationActivity = field(default_factory=NavigationActivity, init=False)
    connect_trace: NavigationConnectTrace | None = field(default=None, init=False, repr=False)
    navigation_wait: NavigationWait | None = field(default=None, init=False)
    lifecycle_timeline: LifecycleTimeline = field(default_factory=LifecycleTimeline)
    lifecycle_page: Page | None = field(default=None, init=False, repr=False)
    interrupted_navigation: dict | None = field(default=None, init=False)
    active_tab: str = "main"
    attach_page: Callable[[Page], Awaitable[None]] | None = None
    expected_popup: ExpectedPopup | None = None
    owned_diagnostic: bool = False
    initial_navigation_timeout: tuple[int, str] = (DIRECT_INITIAL_NAVIGATION_TIMEOUT_MS, "direct_default")
    observation_context: str | None = field(default=None, repr=False)
    step_path: list[str | int] = field(default_factory=list)
    active_action: str | None = None
    active_step_started: float | None = None
    started_at: float = field(default_factory=monotonic)
    execution_step_limit: int = 5000

    def admit_request(self) -> bool:
        if sum(self.request_counts.values()) >= self.request_budget:
            self.request_budget_exhausted = True
            self.deny("HTTP request budget exhausted", code="budget_exhausted")
            return False
        self.request_counts[self.phase] = self.request_counts.get(self.phase, 0) + 1
        return True

    def record_subresource_failure(self, request: Request, *, status: int | None = None, blocked: bool = False) -> None:
        if request.is_navigation_request() or request.resource_type == "document":
            return
        try:
            if request_origin(request.url) != self.origin:
                return
        except NetworkDenied:
            return
        # A route abort or HTTP error can also produce a requestfailed event.
        if request in self.counted_subresource_failures:
            return
        if status is not None:
            if not 400 <= status < 600:
                return
            reason = "http_4xx" if status < 500 else "http_5xx"
        elif blocked or (request.failure or "").startswith("net::ERR_BLOCKED_"):
            reason = "blocked"
        elif request.failure == "net::ERR_ABORTED":
            reason = "aborted"
        else:
            reason = "network_error"
        self.counted_subresource_failures.add(request)
        resource_type = request.resource_type if request.resource_type in SUBRESOURCE_TYPES else "other"
        counts = self.metadata.setdefault("main_origin_subresource_failures", {}).setdefault(resource_type, {})
        counts[reason] = counts.get(reason, 0) + 1

    async def event(self, kind: str, **data):
        if self.emit:
            await self.emit({"kind": kind, **data})

    def note_post_load_step(self) -> None:
        if self.lifecycle_timeline.has("page_loaded"):
            self.lifecycle_timeline.mark("first_post_load_step")

    @contextmanager
    def measure_readiness(self, action: str) -> Iterator[None]:
        """Measure readiness without retaining selectors or input values."""
        timing = self.metadata.setdefault("workflow_readiness", {}).setdefault(
            action, {"checks": 0, "ready": 0, "elapsed_ms": 0},
        )
        timing["checks"] += 1
        started = monotonic()
        try:
            yield
            timing["ready"] += 1
        finally:
            timing["elapsed_ms"] += round((monotonic() - started) * 1000)

    def is_navigation_check(self, action: str) -> bool:
        return action in _NAVIGATION_CHECKS or (
            action == "success_check" and (self.success_check or {}).get("kind") == "url_contains"
        )

    def navigation_timeout_details(self, action: str, deadline: str) -> dict | None:
        if not self.is_navigation_check(action):
            return None
        elapsed = round((monotonic() - (self.active_step_started or self.started_at)) * 1000)
        wait = self.navigation_wait
        return {
            "navigation": asdict(self.navigation_activity),
            "elapsed_ms": elapsed,
            "navigation_wait": wait.details(deadline) if wait else {
                "stage": "url_condition" if action in {"wait_for_url", "assert_url", "success_check"} else "navigation_setup",
                "deadline": deadline,
                "stage_elapsed_ms": elapsed,
            },
        }

    def note_interrupted_navigation(self, action: str) -> None:
        if (detail := self.navigation_timeout_details(action, "workflow")) is not None:
            self.interrupted_navigation = detail

    def record_failure(self, code: str, **detail) -> None:
        if "causal_failure" in self.metadata:
            return
        self.lifecycle_timeline.mark("failure_observed")
        failure = {
            "phase": self.phase if self.started_action or self.active_step else "startup",
            "reason": code,
            "elapsed_ms": round((monotonic() - (self.active_step_started or self.started_at)) * 1000),
        }
        if self.active_step:
            failure.update(step=self.active_step, step_path=list(self.step_path))
        if self.active_action:
            failure["action"] = self.active_action
        self.metadata["causal_failure"] = {**failure, **detail}

    def deny(self, reason: str, *, code: str = "scope_denied", failed: bool = False, **detail) -> None:
        if not self.block.is_set():
            self.reason = reason
            self.terminal_failed = failed
            if reason == "Browser traffic byte budget exhausted":
                code = "budget_exhausted"
            self.record_failure(code, **detail)
            self.block.set()

    def fail_navigation(self, request: Request | None) -> None:
        if self.block.is_set():
            return
        detail = {}
        if request is not None:
            with suppress(NetworkDenied):
                detail["origin"] = request_origin(request.url)
            error = request.failure or ""
            if error != "net::ERR_ABORTED" and _CHROMIUM_NETWORK_ERROR.fullmatch(error):
                detail["error"] = error
        message = f"Chromium could not load {detail.get('origin', 'the page')}"
        if "error" in detail:
            message += f" ({detail['error']})"
        self.metadata["navigation_failure"] = detail
        self.metadata.setdefault("failure_kind", "navigation_failed")
        self.deny(message, code="navigation_failed", failed=True, **detail)

    def fail_http_navigation(self, response: Response) -> None:
        detail = {"status": response.status}
        with suppress(NetworkDenied):
            detail["origin"] = request_origin(response.url)
        self.deny(
            f"Navigation returned HTTP {response.status}", code="main_document_http_error", failed=True, **detail
        )

    def permits(self, url: str, *, document: bool, method: str = "GET") -> bool:
        try:
            origin = request_origin(url)
        except NetworkDenied:
            return False
        if origin == FIXTURE_ORIGIN and (self.origin != FIXTURE_ORIGIN or not is_fixture_path(url)):
            return False
        if self.phase == "automated_preparation" and method not in {"GET", "HEAD"}:
            return False
        if origin == self.origin or origin in self.navigation_origins:
            return True
        if document or origin not in self.resources:
            return False
        # The AdScore engine submits its measurement to an explicitly registered resource.
        return method in {"GET", "HEAD"} or (
            origin in ADSCORE_ENGINE_ORIGINS and method in {"POST", "OPTIONS"} and urlsplit(url).path == "/p"
        )

    def is_controlled_research_page(self, url: str) -> bool:
        context = self.controlled_adscore_ping_context
        if not context or self.phase != "workflow":
            return False
        try:
            if request_origin(url) != self.origin:
                return False
        except NetworkDenied:
            return False
        source = urlsplit(url)
        if source.path != "/lab/research":
            return False
        return [value for key, value in parse_qsl(source.query) if key == "context"] == [context]

    def controlled_adscore_ping_origin(
        self, url: str, method: str, resource_type: str, source_url: str
    ) -> str | None:
        if method != "POST" or resource_type.lower() != "ping" or not self.is_controlled_research_page(source_url):
            return None
        try:
            origin = request_origin(url)
        except NetworkDenied:
            return None
        parts = urlsplit(url)
        if (origin == "https://c.adsco.re" and parts.path == "/favicon.ico") or (
            parts.path == "/" and ADSCORE_PING_HOST.fullmatch(parts.hostname or "")
        ):
            return origin
        return None

    def record_adscore_request_failure(
        self,
        url: str,
        method: str,
        resource_type: str,
        kind: Literal["scope_blocked", "network_error", "http_error"],
        *,
        status: int | None = None,
        error: str | None = None,
    ) -> None:
        try:
            origin = request_origin(url)
        except NetworkDenied:
            return
        if origin not in ADSCORE_ENGINE_ORIGINS:
            return
        # Retain only the endpoint shape. Query parameters and opaque path
        # segments can contain signatures or click identifiers.
        parts = urlsplit(url).path.split("/", maxsplit=8)
        path = "/".join(
            part
            if not part
            or (_DIAGNOSTIC_PATH_SEGMENT.fullmatch(part)
                and not any(secret and secret in part for secret in self.secrets.values()))
            else "[redacted]"
            for part in parts
        )
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
            method = "OTHER"
        if not re.fullmatch(r"[A-Za-z]{1,24}", resource_type):
            resource_type = "other"
        request = {
            "url": origin + (path or "/"),
            "method": method,
            "resource_type": resource_type.lower(),
            "phase": self.phase,
            "step": self.active_step,
        }
        failures = self.metadata.setdefault("adscore_request_failures", [])
        if kind == "network_error" and error in _GENERIC_ABORT_ERRORS and any(
            item["kind"] == "scope_blocked" and all(item[key] == value for key, value in request.items())
            for item in failures
        ):
            # The policy already recorded the reason for this endpoint abort.
            return
        failure = {**request, "kind": kind}
        if status is not None:
            failure["status"] = status
        if kind == "network_error":
            failure["error"] = error if error and _CHROMIUM_NETWORK_ERROR.fullmatch(error) else "request_failed"
        if failure in failures:
            return
        if len(failures) >= MAX_ADSCORE_REQUEST_FAILURES:
            key = "adscore_request_failures_omitted"
            self.metadata[key] = self.metadata.get(key, 0) + 1
            return
        failures.append(failure)


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


def _safe_adscore_referrer_message(value: object, depth: int = 0) -> str | None:
    if isinstance(value, str):
        words = [word.strip(".,!?()[]'`\":;").lower() for word in value.split()]
        if "referrer" not in words and "referer" not in words:
            return None
        public_words: list[str] = []
        for word in words[:32]:
            safe = word if word in _ADSCORE_REFERRER_PROSE else "[redacted]"
            if safe != "[redacted]" or not public_words or public_words[-1] != safe:
                public_words.append(safe)
        return " ".join(public_words)
    if depth < 3 and isinstance(value, (dict, list)):
        items = value.values() if isinstance(value, dict) else value
        for item in items:
            if message := _safe_adscore_referrer_message(item, depth + 1):
                return message
    return None


def _summarize_adscore_error_body(content_type: str, body: bytes) -> dict:
    """Retain only fixed diagnostics from a small provider error response."""
    summary = {"media_type": content_type if content_type in _ADSCORE_ERROR_MEDIA_TYPES else "other"}
    if not body:
        summary["body"] = "empty"
    elif len(body) > _ADSCORE_ERROR_BODY_LIMIT:
        summary["body"] = "skipped"
    else:
        summary["body"] = "read"
        try:
            text = body.decode("utf-8").strip()
        except UnicodeDecodeError:
            summary["body"] = "opaque"
            return summary
        if re.fullmatch(r"[1-5][0-9]{2,3}", text):
            summary["code"] = int(text)
        elif content_type in {"application/json", "application/problem+json"}:
            with suppress(ValueError, RecursionError):
                value = json.loads(text)
                if message := _safe_adscore_referrer_message(value):
                    summary["referrer_message"] = message
                if isinstance(value, dict):
                    error = value.get("error")
                    fields = (value, error) if isinstance(error, dict) else (value,)
                    for item in fields:
                        for key in _ADSCORE_ERROR_CODE_FIELDS:
                            code = item.get(key)
                            if isinstance(code, str) and re.fullmatch(r"[1-5][0-9]{2,3}", code):
                                code = int(code)
                            if type(code) is int and 100 <= code <= 5999:
                                summary["code"] = code
                                break
                        if "code" in summary:
                            break
        normalized = text.replace("_", " ").replace("-", " ")
        for reason, pattern in _ADSCORE_ERROR_REASONS.items():
            if re.search(pattern, normalized, flags=re.IGNORECASE):
                summary["reason"] = reason
                break
        else:
            terms = set(re.findall(r"[a-z]+", normalized.lower())) & _ADSCORE_ERROR_TERMS
            if terms:
                summary["reason_terms"] = sorted(terms)
    return summary


async def _read_adscore_402_probe(session: CDPSession, params: dict) -> dict:
    headers = {header["name"].lower(): header["value"] for header in params.get("responseHeaders", ())}
    mime = headers.get("content-type", "").partition(";")[0].strip().lower()
    media_type = mime if mime in _ADSCORE_ERROR_MEDIA_TYPES else "other"
    summary = {"media_type": media_type, "body": "unavailable"}
    length = headers.get("content-length", "")
    if not re.fullmatch(r"[0-9]{1,5}", length):
        return summary
    if int(length) > _ADSCORE_ERROR_BODY_LIMIT:
        return {"media_type": media_type, "body": "skipped"}
    try:
        async with asyncio.timeout(1):
            response = await session.send("Fetch.getResponseBody", {"requestId": params["requestId"]})
        data = response["body"]
        if len(data) > _ADSCORE_ERROR_BASE64_LIMIT:
            return {"media_type": media_type, "body": "skipped"}
        body = base64.b64decode(data, validate=True) if response.get("base64Encoded") else data.encode()
    except (PlaywrightError, TimeoutError, KeyError, ValueError):
        return summary
    return {**_summarize_adscore_error_body(media_type, body), "decoded_body_bytes": len(body)}


def _adscore_endpoint(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    origin = f"{parts.scheme}://{parts.hostname}"
    if origin not in ADSCORE_ENGINE_ORIGINS or parts.path != "/p":
        return None
    return f"{origin}:{port}/p" if port not in {None, 443} else f"{origin}/p"


def _trace_adscore_requests(page: Page, execution: Execution) -> None:
    """Record the outcome of each engine request without its payload or query."""
    pending: dict[Request, dict] = {}
    trace: list[dict] = []
    started_at = None

    def elapsed() -> int:
        assert started_at is not None
        return round((monotonic() - started_at) * 1000)

    def requested(request: Request) -> None:
        nonlocal started_at
        if request.method not in {"POST", "OPTIONS"} or len(trace) >= MAX_ADSCORE_REQUEST_FAILURES:
            return
        if (endpoint := _adscore_endpoint(request.url)) is None:
            return
        if started_at is None:
            started_at = monotonic()
            execution.metadata["adscore_requests"] = trace
        entry = {"url": endpoint, "method": request.method, "started_ms": elapsed()}
        trace.append(entry)
        pending[request] = entry

    def responded(response: Response) -> None:
        request = response.request
        entry = pending.get(request)
        if entry is not None:
            entry.update(status=response.status, response_ms=elapsed())
            if entry["method"] == "POST":
                task = asyncio.create_task(read_referrer(request, entry))
                execution.adscore_referrer_reads.add(task)
                task.add_done_callback(execution.adscore_referrer_reads.discard)

    async def read_referrer(request: Request, entry: dict) -> None:
        try:
            async with asyncio.timeout(0.5):
                referrer = await request.header_value("referer")
            entry["referrer_origin"] = request_origin(referrer) if referrer is not None else None
        except (PlaywrightError, TimeoutError, NetworkDenied):
            pass

    def finished(request: Request, *, failed: bool = False) -> None:
        entry = pending.pop(request, None)
        if entry is None:
            return
        entry.update(outcome="failed" if failed else "finished", finished_ms=elapsed())
        if failed:
            error = request.failure or ""
            entry["error"] = error if _CHROMIUM_NETWORK_ERROR.fullmatch(error) else "request_failed"

    page.on("request", requested)
    page.on("response", responded)
    page.on("requestfinished", finished)
    page.on("requestfailed", lambda request: finished(request, failed=True))


async def _install_attached_context_policy(context: BrowserContext, page_session: CDPSession) -> CDPSession:
    """Apply download denial and registration blocking to the attached profile context."""
    try:
        async with asyncio.timeout(5):
            browser = context.browser
            context_id = (await page_session.send("Target.getTargetInfo")).get("targetInfo", {}).get("browserContextId")
            if browser is None or not isinstance(context_id, str) or not context_id:
                raise AdapterUnavailable("The attached browser context could not be identified", "containment_unavailable")
            session = await browser.new_browser_cdp_session()
            await session.send("Browser.setDownloadBehavior", {"behavior": "deny", "browserContextId": context_id})
            # CDP attachment cannot set Playwright's context creation option.
            # Apply the same API restriction before any target documents load.
            await context.add_init_script("""if (self.ServiceWorkerContainer) {
                Object.defineProperty(ServiceWorkerContainer.prototype, 'register', {
                    value: async () => undefined, writable: false, configurable: false,
                });
            }""")
            return session
    except (PlaywrightError, TimeoutError):
        raise AdapterUnavailable("The attached browser could not enforce containment", "containment_unavailable") from None


async def _bypass_service_workers(session: CDPSession) -> None:
    try:
        async with asyncio.timeout(5):
            await session.send("Network.setBypassServiceWorker", {"bypass": True})
    except (PlaywrightError, TimeoutError):
        raise AdapterUnavailable("The attached page could not bypass service workers", "containment_unavailable") from None


def _extension_service_worker(worker) -> bool:
    # Chrome starts browser extension workers on fresh profiles. Web documents
    # cannot register these targets, and the request guard still denies worker HTTP traffic.
    url = getattr(worker, "url", None)
    return isinstance(url, str) and url.startswith("chrome-extension://")


async def _attach_page_policy(
    context: BrowserContext,
    page: Page,
    session: CDPSession,
    execution: Execution,
    proxy_auth: dict | None = None,
    *,
    allow_asset_origin: Callable[[str], bool | Awaitable[bool]] | None = None,
    register_context_routes: bool = True,
) -> None:
    asset_origins: set[str] = set()
    execution.metadata.setdefault("main_origin_subresource_failures", {})
    http_status_counts = execution.metadata.setdefault("http_status_counts", {"main_frame": {}, "subresource": {}})
    admitted_controlled_pings: set[Request] = set()
    asset_admission = asyncio.Lock()
    network_trace = execution.network_trace
    adscore_diagnostics = bool(ADSCORE_ENGINE_ORIGINS & execution.resources)
    document_url = None
    document_referrer_policy = None
    main_navigation_request: Request | None = None
    initial_document_active = False
    initial_document_subresources: WeakSet[Request] = WeakSet()
    main_frame_id = None
    if allow_asset_origin is not None:
        frame_tree = await session.send("Page.getFrameTree")
        main_frame_id = frame_tree["frameTree"]["frame"]["id"]

    initial_protocol_sampled = False

    def observe_initial_protocol(event: dict) -> None:
        nonlocal initial_protocol_sampled
        if (
            initial_protocol_sampled or page is not execution.lifecycle_page
            or event.get("frameId") != main_frame_id or event.get("type") != "Document"
        ):
            return
        response = event.get("response", {})
        url, status = response.get("url"), response.get("status")
        if (
            not isinstance(url, str) or not execution.permits(url, document=True)
            or type(status) is not int or not 200 <= status <= 599 or status in (301, 302, 303, 307, 308)
        ):
            return
        initial_protocol_sampled = True
        # A cached response is evidence of a prior connection rather than negotiation in this run.
        if any(response.get(key) for key in ("fromDiskCache", "fromServiceWorker", "fromPrefetchCache")):
            return
        if (protocol := response.get("protocol")) in ("http/1.0", "http/1.1", "h2", "h3"):
            execution.metadata["main_document_http_protocol"] = protocol

    navigation_sources = NavigationSourceTrace()
    initiators = None
    try:
        await session.send("Network.enable", {"maxPostDataSize": 0})
    except PlaywrightError:
        pass
    else:
        session.on("Network.requestWillBeSent", navigation_sources.requested)
        if main_frame_id is not None:
            session.on("Network.responseReceived", observe_initial_protocol)
        if adscore_diagnostics:
            initiators = AdScoreInitiatorTrace(
                execution.metadata, execution.resources & ADSCORE_ENGINE_ORIGINS, _adscore_endpoint, main_frame_id
            )
            session.on("Network.requestWillBeSent", initiators.requested)
            session.on("Network.requestWillBeSentExtraInfo", initiators.network_headers)
            session.on("Network.responseReceived", initiators.responded)
    probe_requested = False

    def deny_document(url: str, *, source_url=None, network_id=None, popup=False, redirected=False) -> None:
        if execution.block.is_set():
            return
        detail = navigation_sources.blocked(
            url, network_id=network_id, source_url=source_url, main_frame_id=main_frame_id,
        )
        if detail:
            execution.metadata["blocked_navigation"] = detail
        is_redirect = redirected or (detail and detail.get("trigger") == "http_redirect")
        code = "unexpected_redirect" if is_redirect else "scope_denied"
        execution.deny(_navigation_scope_error(url, popup=popup), code=code)

    def record_adscore_referrer_policy(request: dict, main_frame: bool) -> None:
        if request["method"] != "POST" or (endpoint := _adscore_endpoint(request["url"])) is None:
            return
        row = {"url": endpoint}
        policy = request.get("referrerPolicy")
        if policy in _REFERRER_POLICIES:
            row["request_policy"] = policy
        if main_frame and document_url == page.main_frame.url:
            row["document_policy"] = document_referrer_policy
        if len(row) > 1:
            evidence = execution.metadata.setdefault("adscore_referrer_policies", [])
            if len(evidence) < MAX_ADSCORE_REQUEST_FAILURES and row not in evidence:
                evidence.append(row)

    def asset_origin(url: str, method: str, resource_type: str, source_url: str | None) -> str | None:
        if (
            allow_asset_origin is None
            or source_url is None
            or method not in {"GET", "HEAD"}
            or resource_type.lower() not in PAGE_ASSET_TYPES
            or not execution.permits(source_url, document=True)
        ):
            return None
        try:
            origin = request_origin(url)
        except NetworkDenied:
            return None
        return origin if origin.startswith("https://") else None

    async def admit_asset(origin: str) -> bool:
        if origin in asset_origins:
            return True
        assert allow_asset_origin is not None
        async with asset_admission:
            if origin in asset_origins:
                return True
            if execution.block.is_set():
                return False
            admitted = allow_asset_origin(origin)
            if isawaitable(admitted):
                admitted = await admitted
            if admitted:
                asset_origins.add(origin)
            return admitted

    async def abort_route(route: Route) -> None:
        execution.record_subresource_failure(route.request, blocked=True)
        await route.abort()

    async def restrict_context(route: Route) -> None:
        request = route.request
        if getattr(request, "service_worker", None) is not None:
            execution.blocked_resources += 1
            await abort_route(route)
            return
        document = request.is_navigation_request()
        try:
            frame = request.frame
        except PlaywrightError:
            frame = None  # A popup can request its document before creating a frame.
        current_page = (
            page if frame is not None and frame is getattr(page, "main_frame", None)
            else frame.page if frame is not None else None
        )
        tracked = current_page is page or current_page in execution.pages.values()
        if not tracked and document:
            expected = execution.expected_popup
            if (
                expected is not None and expected.initial_url is None
                and request.method == "GET" and not execution.block.is_set()
                and execution.permits(request.url, document=True)
            ):
                # Chromium may not expose the new page until its initial URL
                # replies. Hold that origin request behind an inert document.
                # The opener is checked and CDP interception installed before
                # navigating the claimed page to its actual destination.
                expected.initial_url = request.url
                await route.fulfill(
                    status=200,
                    headers={
                        "Content-Type": "text/html; charset=utf-8",
                        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
                        "Cache-Control": "no-store",
                    },
                    body="<!doctype html>",
                )
                return
            if network_trace:
                network_trace.blocked(request.url, request.method, request.resource_type, "unexpected_window")
            if expected is None:
                execution.deny("Unexpected browser window opened", code="unexpected_popup")
            else:
                deny_document(request.url, source_url=expected.opener.url, popup=True)
        elif not tracked and execution.pages:
            execution.blocked_resources += 1
            await abort_route(route)
            return
        allowed = execution.permits(request.url, document=document, method=request.method)
        ping_origin = (
            execution.controlled_adscore_ping_origin(request.url, request.method, request.resource_type, frame.url)
            if (
                execution.controlled_adscore_ping_context
                and not allowed and not document and frame is not None and frame is page.main_frame
                and request.redirected_from is None
            )
            else None
        )
        allowed = allowed or ping_origin == "https://c.adsco.re"
        origin = (
            ping_origin or asset_origin(request.url, request.method, request.resource_type, frame.url)
            if not allowed and not document and current_page is not None and frame is current_page.main_frame
            else None
        )
        if not allowed and origin is None:
            if network_trace:
                network_trace.blocked(request.url, request.method, request.resource_type, "target_scope")
            execution.record_adscore_request_failure(
                request.url, request.method, request.resource_type, "scope_blocked"
            )
            if document:
                source_frame = (frame.parent_frame or frame) if frame is not None else None
                deny_document(request.url, source_url=source_frame.url if source_frame else None)
            else:
                execution.blocked_resources += 1
            await abort_route(route)
        elif execution.block.is_set():
            await abort_route(route)
        elif not execution.admit_request():
            if network_trace:
                network_trace.blocked(request.url, request.method, request.resource_type, "request_budget")
            await abort_route(route)
        elif origin is not None and (not await admit_asset(origin) or execution.block.is_set()):
            if not execution.block.is_set():
                if network_trace:
                    network_trace.blocked(request.url, request.method, request.resource_type, "asset_admission_denied")
                execution.record_adscore_request_failure(
                    request.url, request.method, request.resource_type, "scope_blocked"
                )
            execution.blocked_resources += 1
            await abort_route(route)
        else:
            if ping_origin:
                admitted_controlled_pings.add(request)
            await route.continue_()

    async def intercept(params: dict) -> None:
        nonlocal probe_requested
        request_id = params["requestId"]
        if "responseStatusCode" in params or "responseErrorReason" in params:
            try:
                if probe_requested and params.get("responseStatusCode") == 402:
                    execution.metadata["adscore_402_response"] = await _read_adscore_402_probe(session, params)
            finally:
                with suppress(PlaywrightError):
                    await session.send("Fetch.continueResponse", {"requestId": request_id})
            return
        resource_type = params.get("resourceType", "")
        document = resource_type == "Document"
        request = params["request"]
        allowed = execution.permits(request["url"], document=document, method=request["method"])
        main_frame = main_frame_id is not None and params.get("frameId") == main_frame_id
        ping_origin = (
            execution.controlled_adscore_ping_origin(
                request["url"], request["method"], resource_type, page.main_frame.url
            )
            if execution.controlled_adscore_ping_context and not allowed and main_frame and not params.get("redirectedRequestId")
            else None
        )
        allowed = allowed or ping_origin == "https://c.adsco.re"
        origin = (
            ping_origin or asset_origin(request["url"], request["method"], resource_type, page.main_frame.url)
            if not allowed and main_frame
            else None
        )
        # Playwright routes only the first request in a redirect chain. CDP
        # pauses every redirected hop, so account those separately here.
        if (allowed or origin is not None) and params.get("redirectedRequestId") and not execution.block.is_set():
            if not execution.admit_request():
                if network_trace:
                    network_trace.blocked(request["url"], request["method"], resource_type, "request_budget")
                allowed = False
                origin = None
        if origin is not None and not execution.block.is_set():
            allowed = await admit_asset(origin)
        if not allowed and not execution.block.is_set():
            if network_trace:
                reason = "asset_admission_denied" if origin is not None else "target_scope"
                network_trace.blocked(request["url"], request["method"], resource_type, reason)
            execution.record_adscore_request_failure(
                request["url"], request["method"], resource_type, "scope_blocked"
            )
            if document:
                deny_document(
                    request["url"], source_url=page.main_frame.url, network_id=params.get("networkId"),
                    redirected=bool(params.get("redirectedRequestId")),
                )
            else:
                execution.blocked_resources += 1
        try:
            if allowed and not execution.block.is_set():
                if adscore_diagnostics:
                    record_adscore_referrer_policy(request, main_frame)
                if initiators is not None:
                    initiators.paused(params)
                options = {"requestId": request_id}
                if (
                    not probe_requested and main_frame and not params.get("redirectedRequestId")
                    and request["method"] == "POST"
                    and execution.is_controlled_research_page(page.main_frame.url)
                    and request_origin(request["url"]) == "https://c.adsco.re"
                    and urlsplit(request["url"]).path == "/p"
                ):
                    options["interceptResponse"] = True
                    probe_requested = True
                    referrer = next(
                        (value for header, value in request.get("headers", {}).items() if header.lower() == "referer"),
                        "",
                    )
                    execution.metadata["adscore_measurement_referrer_origin"] = None
                    with suppress(NetworkDenied):
                        execution.metadata["adscore_measurement_referrer_origin"] = request_origin(referrer)
                await session.send("Fetch.continueRequest", options)
            else:
                await session.send(
                    "Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}
                )
        except PlaywrightError:
            # The request may have been canceled by Chromium during page teardown.
            return

    def is_main_navigation(request: Request) -> bool:
        if request.is_navigation_request():
            with suppress(PlaywrightError):
                return request.frame == page.main_frame
        return False

    def is_owned_echo(request: Request) -> bool:
        return execution.owned_diagnostic and request.method == "GET" and request.url == execution.origin + ECHO_PATH

    def count_http_status(group: str, status: int) -> None:
        if type(status) is int and 100 <= status <= 599:
            counts = http_status_counts[group]
            key = str(status)
            counts[key] = counts.get(key, 0) + 1

    def inspect_response(response: Response) -> None:
        nonlocal document_url, document_referrer_policy
        request = response.request
        main_navigation = is_main_navigation(request)
        controlled_ping = request in admitted_controlled_pings
        if main_navigation and execution.permits(response.url, document=True, method=request.method):
            count_http_status("main_frame", response.status)
            execution.navigation_activity.responses += 1
            execution.navigation_activity.last_status = response.status
            if execution.connect_trace is not None and response.status >= 400:
                execution.connect_trace.finish(request, "failed")
            if execution.started_action and (200 <= response.status < 300 or response.status == 304):
                execution.metadata["authorized_document_observed"] = True
        elif not request.is_navigation_request() and request.resource_type != "document":
            allowed = controlled_ping or execution.permits(response.url, document=False, method=request.method)
            if not allowed and request.method in {"GET", "HEAD"}:
                with suppress(NetworkDenied):
                    allowed = request_origin(response.url) in asset_origins
            if allowed:
                count_http_status("subresource", response.status)
        if adscore_diagnostics and request.is_navigation_request() is True and response.status not in {301, 302, 303, 307, 308}:
            with suppress(PlaywrightError):
                if request.frame is page.main_frame:
                    document_url = response.url
                    tokens = reversed(response.headers.get("referrer-policy", "").lower().split(","))
                    document_referrer_policy = next(
                        (token for raw in tokens if (token := raw.strip()) in _REFERRER_POLICIES), None
                    )
        admitted_controlled_pings.discard(request)
        if response.status >= 400:
            execution.record_subresource_failure(request, status=response.status)
            execution.record_adscore_request_failure(
                response.url, request.method, request.resource_type, "http_error", status=response.status
            )
            if is_owned_echo(request):
                execution.deny(
                    "The owned diagnostic echo returned an HTTP error", code="diagnostic_echo_http_error",
                    phase="diagnostic", failed=True, status=response.status,
                )
                return
        if response.status in DENIAL_STATUSES and not controlled_ping:
            try:
                if request_origin(response.url) in asset_origins:
                    return
            except NetworkDenied:
                pass
            execution.deny(
                f"Site denied or limited access (HTTP {response.status})", code="site_denial", status=response.status
            )
        elif response.status >= 400 and main_navigation:
            execution.fail_http_navigation(response)

    def inspect_failed_request(request: Request) -> None:
        if execution.connect_trace is not None:
            execution.connect_trace.finish(request, "failed")
        admitted_controlled_pings.discard(request)
        execution.record_subresource_failure(request)
        execution.record_adscore_request_failure(
            request.url, request.method, request.resource_type, "network_error", error=request.failure
        )
        if is_owned_echo(request) and request.failure != "net::ERR_ABORTED":
            error = request.failure or ""
            execution.deny(
                "The owned diagnostic echo could not be reached", code="diagnostic_echo_network_error",
                phase="diagnostic", failed=True,
                error=error if _CHROMIUM_NETWORK_ERROR.fullmatch(error) else "request_failed",
            )
        if request is main_navigation_request and request.failure != "net::ERR_ABORTED":
            execution.fail_navigation(request)

    def remember_navigation(request: Request) -> None:
        nonlocal main_navigation_request
        main_navigation = request.url != _CHROMIUM_ERROR_PAGE and is_main_navigation(request)
        if execution.connect_trace is not None:
            origin = None
            with suppress(NetworkDenied):
                origin = request_origin(request.url)
            execution.connect_trace.request(
                request, origin, navigation=main_navigation, step=execution.active_step,
                redirected_from=request.redirected_from,
            )
        if main_navigation:
            main_navigation_request = request
            if execution.permits(request.url, document=True, method=request.method):
                execution.navigation_activity.requests += 1
        elif initial_document_active and request.resource_type != "document":
            with suppress(PlaywrightError):
                if request.frame == page.main_frame:
                    initial_document_subresources.add(request)

    def note_subresource_completion(request: Request) -> None:
        if execution.connect_trace is not None:
            execution.connect_trace.finish(request)
        if initial_document_active and request in initial_document_subresources:
            execution.lifecycle_timeline.mark("first_subresource_completed")
            if not execution.lifecycle_timeline.has("page_loaded"):
                execution.lifecycle_timeline.mark("last_subresource_completed_before_load", latest=True)

    def inspect_navigation(frame) -> None:
        nonlocal main_navigation_request, initial_document_active
        if frame != page.main_frame:
            return
        if frame.url == _CHROMIUM_ERROR_PAGE:
            if execution.connect_trace is not None and main_navigation_request is not None:
                execution.connect_trace.finish(main_navigation_request, "failed")
            execution.fail_navigation(main_navigation_request)
            main_navigation_request = None
        elif frame.url != "about:blank" and not execution.permits(frame.url, document=True):
            execution.deny(_navigation_scope_error(frame.url))
        elif main_navigation_request is not None and urldefrag(frame.url).url == main_navigation_request.url:
            # Same-document navigation can fire while a new document request is in flight.
            execution.navigation_activity.commits += 1
            if execution.connect_trace is not None:
                execution.connect_trace.finish(main_navigation_request, "committed")
            main_navigation_request = None
            if page is execution.lifecycle_page:
                initial_document_active = not execution.lifecycle_timeline.has("document_committed")
                if initial_document_active:
                    execution.lifecycle_timeline.mark("document_committed")

    def note_document_milestone(event: str) -> None:
        if initial_document_active:
            execution.lifecycle_timeline.mark(event)

    async def close_popup(popup: Page) -> None:
        expected = execution.expected_popup
        if expected is not None and expected.opener is page:
            if expected.observed is None or expected.observed is popup:
                expected.observed = popup
                return
        execution.deny("Unexpected browser window opened", code="unexpected_popup")
        try:
            await popup.close()
        except PlaywrightError:
            pass

    async def dismiss_dialog(dialog: Dialog) -> None:
        execution.deny(
            "Unexpected browser dialog opened", code="unexpected_dialog", failed=True, dialog_type=dialog.type
        )
        with suppress(PlaywrightError):
            await dialog.dismiss()

    async def inspect_document(_page: Page) -> None:
        with suppress(PlaywrightError):
            await _check_challenge(page, execution)

    async def close_socket(socket: WebSocketRoute) -> None:
        if network_trace:
            network_trace.blocked(socket.url, "GET", "websocket", "websocket_scope")
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
    page.on("request", remember_navigation)
    page.on("response", inspect_response)
    page.on("requestfinished", note_subresource_completion)
    page.on("requestfailed", inspect_failed_request)
    if network_trace:
        network_trace.attach(page)
    if adscore_diagnostics:
        _trace_adscore_requests(page, execution)
    page.on("framenavigated", inspect_navigation)
    page.on("domcontentloaded", lambda _: note_document_milestone("dom_content_loaded"))
    page.on("domcontentloaded", inspect_document)
    page.on("load", lambda _: note_document_milestone("page_loaded"))
    page.on("popup", close_popup)
    page.on("dialog", dismiss_dialog)
    if register_context_routes:
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
        execution.deny("Site requested verification or denied access", code="site_challenge")
        return
    widgets = page.locator(CHALLENGE_WIDGETS)
    for widget in (await widgets.all())[:20]:
        if await widget.is_visible():
            execution.deny("Site requested interactive verification", code="site_challenge")
            return
    if await page.get_by_text("Verify you are human", exact=True).first.is_visible():
        execution.deny("Site requested interactive verification", code="site_challenge")


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
    budget = min(MAX_SINGLE_EXTRACTION_CHARACTERS, execution.extraction_budget)
    captured = visible[:budget]
    execution.extraction_budget -= len(captured)
    execution.metadata.setdefault("extractions", []).append(
        {
            "step": execution.active_step,
            "text": captured,
            "truncated": len(visible) > len(captured),
        }
    )


async def _preserve_ready_diagnostics(page: Page, execution: Execution) -> None:
    if not execution.owned_diagnostic or not execution.lifecycle_timeline.has("document_committed"):
        return
    rows = execution.metadata.get("extractions", [])
    if len(rows) == len(DIAGNOSTIC_GROUPS) and not any(row["truncated"] for row in rows):
        return
    fixture_url = execution.origin + DIAGNOSTIC_PATH
    if page.url != fixture_url:
        return
    # Preserve only evidence that the owned fixture has already published. This
    # read does not wait for the fixture's probes or extend the workflow deadline.
    try:
        async with asyncio.timeout(0.25):
            texts = await page.evaluate("""({url, groups, limit}) => {
                if (location.href !== url ||
                    document.querySelector('#abrl-probe-status')?.textContent !== 'Diagnostics complete') return null;
                const fields = groups.map(group => document.getElementById('abrl-' + group)?.textContent);
                return fields.every(text => typeof text === 'string' && text.length <= limit) ? fields : null;
            }""", {"url": fixture_url, "groups": DIAGNOSTIC_GROUPS, "limit": MAX_SINGLE_EXTRACTION_CHARACTERS})
    except (PlaywrightError, TimeoutError):
        return
    if not isinstance(texts, list) or len(texts) != len(DIAGNOSTIC_GROUPS):
        return
    if not all(isinstance(text, str) and len(text) <= MAX_SINGLE_EXTRACTION_CHARACTERS for text in texts):
        return
    execution.metadata["extractions"] = []
    execution.extraction_budget = MAX_EXTRACTED_CHARACTERS
    for text in texts:
        _record_extraction(text, execution)


async def _observe_assertion_warning(page: Page, locator: Locator, selector: str, execution: Execution) -> None:
    expected = [code for code in _WORKFLOW_WARNING_CODES if code in selector]
    if not expected:
        return
    visible = locator.filter(visible=True)
    codes = []
    try:
        async with asyncio.timeout(0.4):
            for code in expected:
                text = re.compile(rf"\b{code}\b")
                warning = visible.and_(page.get_by_text(text)).or_(visible.get_by_text(text)).filter(visible=True)
                if await warning.first.is_visible():
                    codes.append(code)
    except (PlaywrightError, TimeoutError):
        pass
    if codes:
        execution.metadata["visible_assertion_warning"] = {"step": execution.active_step, "codes": codes}


def _locator_root(page: Page, step: dict):
    root = page
    for selector in step.get("frame") or ():
        root = root.frame_locator(selector)
    return root


def _referenced_value(execution: Execution, name: str):
    if name not in execution.values:
        raise StepFailed(f"Workflow value {name} is unavailable on this path")
    return execution.values[name]


def _step_value(step: dict, execution: Execution) -> str | None:
    if name := step.get("value_ref"):
        value = _referenced_value(execution, name)
        if isinstance(value, bool):
            return str(value).lower()
        return str(value)
    if reference := step.get("secret_ref"):
        return _secret_value(reference, execution)
    return step.get("value")


async def _wait_until(predicate, deadline: float) -> None:
    while True:
        if await predicate():
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise PlaywrightTimeout("Workflow condition timed out")
        await asyncio.sleep(min(0.05, remaining))


async def _visible_text(locator, deadline: float) -> str | None:
    if await locator.is_visible():
        return await locator.inner_text(timeout=max(1, int((deadline - monotonic()) * 1000)))
    return None


async def _condition_matches(page: Page, root, condition: dict, execution: Execution, deadline: float) -> bool:
    kind = condition["kind"]
    if kind == "value_equals":
        current = _referenced_value(execution, condition["value_ref"])
        expected = condition["value"]
        if isinstance(current, bool) != isinstance(expected, bool):
            return False
        return current == expected
    if execution.workflow_version == 2 and page.url == "about:blank":
        raise StepFailed("Open a page before evaluating a page condition")
    if kind == "url_contains":
        return condition["text"] in page.url
    locator = root.locator(condition["selector"])
    if kind == "visible":
        return await locator.is_visible()
    if kind == "hidden":
        return not await locator.is_visible()
    visible_text = await _visible_text(locator, deadline)
    if (
        execution.owned_diagnostic and condition["selector"] == "#abrl-probe-status"
        and page.url == execution.origin + DIAGNOSTIC_PATH and visible_text == "Diagnostics unavailable"
    ):
        probe_failure = None
        with suppress(PlaywrightError, TimeoutError):
            probe_failure = await locator.get_attribute("data-failure", timeout=_remaining_step_ms(deadline))
        detail = {"probe_failure": probe_failure} if probe_failure in DIAGNOSTIC_PROBE_FAILURES else {}
        execution.record_failure("diagnostic_fixture_error", phase="diagnostic", **detail)
        raise StepFailed("The owned diagnostic fixture reported a probe failure")
    return visible_text is not None and condition["text"] in visible_text


async def _mark_action(execution: Execution, action: str, *, non_idempotent: bool) -> None:
    await execution.event(
        "action_started", action=action, step=execution.active_step, non_idempotent=non_idempotent
    )
    execution.started_action = True
    if non_idempotent:
        execution.metadata["non_idempotent_action_started"] = True


def _remaining_step_ms(deadline: float) -> int:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise PlaywrightTimeout("Workflow step exceeded its deadline")
    return max(1, int(remaining * 1000))


async def _wait_for_actionable(execution: Execution, locator: Locator, action: str, deadline: float) -> None:
    trial = "click" if action == "expect_popup" else action
    with execution.measure_readiness(action):
        timeout = _remaining_step_ms(deadline)
        if trial in {"click", "check", "uncheck", "hover"}:
            # Let Playwright verify actionability before the durable dispatch marker.
            await getattr(locator, trial)(trial=True, timeout=timeout)
        else:
            await locator.wait_for(state="visible", timeout=timeout)


def _checked_navigation_timeout(value: int) -> int:
    if type(value) is not int or not 100 <= value <= MAX_NAVIGATION_TIMEOUT_MS:
        raise StepFailed(
            f"Initial navigation timeout must be an integer from 100 to {MAX_NAVIGATION_TIMEOUT_MS} milliseconds"
        )
    return value


def _initial_navigation_timeout(
    workflow_override: int | None, policy_override: int | None, *, proxied: bool,
) -> tuple[int, str]:
    if workflow_override is not None:
        return _checked_navigation_timeout(workflow_override), "step_override"
    if proxied:
        if policy_override is not None:
            return _checked_navigation_timeout(policy_override), "proxy_policy"
        return PROXY_INITIAL_NAVIGATION_TIMEOUT_MS, "proxy_default"
    return DIRECT_INITIAL_NAVIGATION_TIMEOUT_MS, "direct_default"


def _step_navigation_wait(step: dict, execution: Execution) -> NavigationWait | None:
    if step["action"] not in NAVIGATION_ACTIONS:
        return None
    load_timeout, source = execution.initial_navigation_timeout
    if (override := step.get("timeout_ms")) is not None:
        load_timeout, source = _checked_navigation_timeout(override), "step_override"
    return NavigationWait(load_timeout, step.get("post_navigation_wait_ms") or 0, source)


async def _navigate(page: Page, step: dict, execution: Execution, wait: NavigationWait, deadline: float) -> None:
    execution.navigation_wait = wait
    url = urljoin(execution.origin + "/", step["url"])
    if not execution.permits(url, document=True):
        execution.deny(_navigation_scope_error(url))
        return
    if execution.observation_context is not None:
        url = _instrumented_url(url, execution.observation_context)
        execution.observation_context = None
    await _mark_action(execution, step["action"], non_idempotent=False)
    referrer = execution.initial_referrer
    execution.initial_referrer = None
    if execution.lifecycle_page is None:
        execution.lifecycle_page = page
        execution.lifecycle_timeline.mark("navigation_started")
    if execution.phase == "workflow" and wait.load_timeout_source is not None:
        execution.metadata.setdefault("initial_navigation_timeout", {
            "load_timeout_ms": wait.load_timeout_ms, "load_timeout_source": wait.load_timeout_source,
        })
    wait.begin("initial_dom_content_loaded")
    response = await page.goto(
        url, referer=referrer, wait_until="domcontentloaded",
        timeout=min(wait.load_timeout_ms, _remaining_step_ms(deadline)),
    )
    if response and response.status >= 400 and not execution.block.is_set():
        execution.fail_http_navigation(response)
    if not wait.post_navigation_wait_ms or execution.block.is_set():
        return
    wait.begin("post_navigation_challenge_check")
    async with asyncio.timeout(_remaining_step_ms(deadline) / 1000):
        await _check_challenge(page, execution)
        if execution.block.is_set():
            return
        wait.begin("post_navigation_delay")
        await asyncio.sleep(wait.post_navigation_wait_ms / 1000)
        if not execution.block.is_set():
            wait.begin("post_navigation_dom_content_loaded")
            await page.wait_for_load_state("domcontentloaded", timeout=_remaining_step_ms(deadline))


def _select_workflow_tab(execution: Execution, name: str) -> None:
    execution.active_tab = name
    if execution.recording:
        execution.recording.select_page(execution.pages[name])


async def _browser_tab_action(
    page: Page, root, step: dict, execution: Execution, deadline: float, navigation_wait: NavigationWait | None,
) -> None:
    action = step["action"]
    name = step["tab"]
    if action in {"switch_tab", "close_tab"}:
        selected = execution.pages.get(name)
        if selected is None or selected.is_closed():
            raise StepFailed(f"Browser tab {name} is unavailable")
        if action == "switch_tab":
            _select_workflow_tab(execution, name)
            return
        if name == "main":
            raise StepFailed("The main tab cannot be closed explicitly")
        await _mark_action(execution, action, non_idempotent=True)
        await selected.close()
        del execution.pages[name]
        if execution.active_tab == name:
            _select_workflow_tab(execution, "main")
        return
    if name in execution.pages:
        raise StepFailed(f"Browser tab {name} is already open")
    if action == "open_tab":
        assert navigation_wait is not None
        created = await page.context.new_page()
        execution.pages[name] = created
        await execution.attach_page(created)
        await _navigate(created, step, execution, navigation_wait, deadline)
    else:
        locator = root.locator(step["selector"])
        await _wait_for_actionable(execution, locator, action, deadline)
        if execution.block.is_set():
            return
        await _mark_action(execution, action, non_idempotent=True)
        expected = ExpectedPopup(page)
        execution.expected_popup = expected
        try:
            async with page.expect_popup(timeout=_remaining_step_ms(deadline)) as popup:
                timeout = _remaining_step_ms(deadline)
                execution.lifecycle_timeline.mark("first_interaction")
                await locator.click(timeout=timeout)
            created = await popup.value
            if await created.opener() is not page or (
                expected.observed is not None and expected.observed is not created
            ):
                await created.close()
                raise StepFailed("Popup opener did not match the active tab")
            if expected.initial_url is not None:
                await created.wait_for_url(
                    lambda url: url == expected.initial_url, wait_until="commit",
                    timeout=_remaining_step_ms(deadline),
                )
        finally:
            execution.expected_popup = None
        execution.pages[name] = created
        await execution.attach_page(created)
        if expected.initial_url is not None:
            await _navigate(
                created, {"action": "navigate", "url": expected.initial_url}, execution,
                NavigationWait(_remaining_step_ms(deadline), 0), deadline,
            )
        elif created.url != "about:blank" and not execution.permits(created.url, document=True):
            execution.deny(_navigation_scope_error(created.url, popup=True))
            return
    _select_workflow_tab(execution, name)


async def _execute_step(
    page: Page, step: dict, execution: Execution, navigation_wait: NavigationWait | None = None,
) -> None:
    action = step["action"]
    navigation_wait = navigation_wait or _step_navigation_wait(step, execution)
    timeout = navigation_wait.load_timeout_ms if navigation_wait else step.get("timeout_ms") or DEFAULT_STEP_TIMEOUT_MS
    deadline = monotonic() + (navigation_wait.total_seconds if navigation_wait else timeout / 1000)
    if action == "checkpoint":
        name = step.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", name):
            raise StepFailed("Checkpoint requires a valid name")
        if execution.workflow_version == 1 and execution.last_assertion_step != execution.active_step - 1:
            raise StepFailed("Checkpoint requires an immediately preceding successful assertion")
        checkpoints = execution.metadata.setdefault("checkpoints", [])
        if execution.workflow_version == 1 and any(item["name"] == name for item in checkpoints):
            raise StepFailed("Checkpoint names must be unique within a workflow")
        boundary = {
            "name": name,
            "step": execution.active_step,
            "assertion_step": execution.last_assertion_step,
            "non_idempotent_action_started": bool(execution.metadata.get("non_idempotent_action_started")),
            "replay_allowed": False,
        }
        if execution.workflow_version == 2:
            boundary["step_path"] = execution.step_path
        checkpoints.append(boundary)
        await execution.event("checkpoint_reached", **boundary)
        return
    if action == "navigate" and navigation_wait is not None:
        await _navigate(page, step, execution, navigation_wait, deadline)
        return

    root = _locator_root(page, step)
    if action in {"open_tab", "switch_tab", "close_tab", "expect_popup"}:
        await _browser_tab_action(page, root, step, execution, deadline, navigation_wait)
        return
    value = _step_value(step, execution)
    if action in {"wait_for_url", "assert_url"}:
        with execution.measure_readiness(action):
            await _wait_until(
                lambda: _condition_matches(page, root, {"kind": "url_contains", "text": value}, execution, deadline),
                deadline,
            )
    else:
        locator = root.locator(step["selector"])
        if action in _INPUT_ACTIONS:
            await _wait_for_actionable(execution, locator, action, deadline)
            if execution.block.is_set():
                return
            await _mark_action(execution, action, non_idempotent=True)
            timeout = _remaining_step_ms(deadline)
            execution.lifecycle_timeline.mark("first_interaction")
            if action == "fill":
                await locator.fill(value, timeout=timeout)
            elif action == "select":
                await locator.select_option(value=value, timeout=timeout)
            elif action == "press":
                await locator.press(value, timeout=timeout)
            else:
                await getattr(locator, action)(timeout=timeout)
        elif action in {"wait_for", "wait_for_hidden", "assert_visible", "assert_not_visible"}:
            with execution.measure_readiness(action):
                await locator.wait_for(
                    state="hidden" if action in {"wait_for_hidden", "assert_not_visible"} else "visible",
                    timeout=max(1, int((deadline - monotonic()) * 1000)),
                )
        elif action in {"assert_text", "wait_for_text"}:
            with execution.measure_readiness(action):
                await _wait_until(
                    lambda: _condition_matches(
                        page, root, {"kind": "text_contains", "selector": step["selector"], "text": value},
                        execution, deadline,
                    ),
                    deadline,
                )
        elif action == "extract":
            await locator.wait_for(state="visible", timeout=timeout)
            visible_text = await locator.inner_text(timeout=max(1, int((deadline - monotonic()) * 1000)))
            if capture_name := step.get("save_as"):
                execution.values[capture_name] = SAFE_OMIT.sub("", visible_text)[:MAX_EXTRACTED_CHARACTERS]
            else:
                _record_extraction(visible_text, execution)
        else:
            raise StepFailed("Unsupported workflow step")
    if action in ASSERTIONS:
        execution.last_assertion_step = execution.active_step


async def _record_step_failure(
    execution: Execution, action: str, started: float, exc: Exception, *, navigation_detail: dict | None = None,
) -> None:
    if "step_failure" in execution.metadata:
        return
    reason = (
        "timeout" if isinstance(exc, (TimeoutError, PlaywrightTimeout))
        else "browser_error" if isinstance(exc, PlaywrightError)
        else exc.reason if isinstance(exc, StepFailed)
        else "execution_interrupted"
    )
    causal_reason = reason
    detail = {}
    if reason == "timeout":
        if execution.is_navigation_check(action):
            causal_reason = "navigation_timeout"
            detail = navigation_detail or execution.navigation_timeout_details(action, "step") or {}
        elif action in _SELECTOR_ACTIONS:
            causal_reason = "selector_timeout"
    execution.record_failure(causal_reason, action=action, **detail)
    failure = {
        "step": execution.active_step,
        "step_path": execution.step_path,
        "action": action,
        "phase": execution.phase,
        "elapsed_ms": round((monotonic() - started) * 1000),
        "reason": reason,
        **detail,
    }
    execution.metadata["step_failure"] = failure
    await execution.event("workflow_step_failed", **failure)


async def _execute_steps(steps: list[dict], execution: Execution, adapter: str, prefix=()) -> None:
    for index, step in enumerate(steps, 1):
        if execution.block.is_set():
            return
        action = step["action"]
        execution.active_step += 1
        execution.step_path = [*prefix, index]
        page = execution.pages[execution.active_tab]
        start = monotonic()
        execution.active_action = action
        execution.active_step_started = start
        execution.navigation_wait = None
        try:
            navigation_wait = _step_navigation_wait(step, execution)
            execution.navigation_wait = navigation_wait
            timeout = navigation_wait.total_seconds if navigation_wait else (
                (step.get("timeout_ms") or DEFAULT_STEP_TIMEOUT_MS) / 1000
            )
            if execution.active_step > execution.execution_step_limit:
                raise StepFailed("Workflow exceeded its executed step budget")
            if operator_reserved_step(adapter, action):
                execution.metadata["operator_reserved_steps"] = execution.metadata.get("operator_reserved_steps", 0) + 1
                await execution.event("operator_step_reserved", action=action, step=execution.active_step,
                                      step_path=execution.step_path)
                continue
            if (execution.workflow_version == 2 and page.url == "about:blank"
                    and action not in NAVIGATION_ACTIONS | CONTROL_ACTIONS | {"checkpoint", "switch_tab"}):
                raise StepFailed("Open a page before running a browser action")
            await _check_challenge(page, execution)
            if execution.block.is_set():
                return
            execution.note_post_load_step()
            details = {"step": execution.active_step, "action": action}
            if execution.workflow_version == 2:
                details["step_path"] = execution.step_path
            await execution.event("workflow_step", **details)
            if action == "if":
                async with asyncio.timeout(timeout):
                    matches = await _condition_matches(
                        page, _locator_root(page, step), step["condition"], execution, monotonic() + timeout
                    )
                child_key, label = ("steps", "then") if matches else ("else_steps", "else")
                await _execute_steps(step.get(child_key) or [], execution, adapter, (*prefix, index, label))
            elif action == "repeat":
                for iteration in range(1, step["max_iterations"] + 1):
                    await _execute_steps(step["steps"], execution, adapter, (*prefix, index, "repeat", iteration))
                    if execution.block.is_set():
                        return
                    if condition := step.get("condition"):
                        selected = execution.pages[execution.active_tab]
                        async with asyncio.timeout(timeout):
                            if await _condition_matches(
                                selected, _locator_root(selected, step), condition, execution, monotonic() + timeout
                            ):
                                break
            elif action == "use_block":
                await _execute_steps(step["steps"], execution, adapter, (*prefix, index, "block"))
            else:
                async with asyncio.timeout(timeout):
                    await _execute_step(page, step, execution, navigation_wait)
            if execution.navigation_wait is not None:
                execution.navigation_wait.begin("post_step_challenge_check")
            await _check_challenge(execution.pages[execution.active_tab], execution)
            if execution.block.is_set():
                return
            execution.steps_completed = execution.active_step
        except asyncio.CancelledError:
            execution.note_interrupted_navigation(action)
            raise
        except (TimeoutError, PlaywrightTimeout) as exc:
            navigation_detail = execution.navigation_timeout_details(action, "step")
            with suppress(PlaywrightError, TimeoutError):
                async with asyncio.timeout(0.5):
                    await _check_challenge(page, execution)
            if execution.block.is_set():
                return
            if action == "assert_not_visible":
                locator = _locator_root(page, step).locator(step["selector"])
                await _observe_assertion_warning(page, locator, step["selector"], execution)
            await _record_step_failure(execution, action, start, exc, navigation_detail=navigation_detail)
            raise PlaywrightTimeout("Workflow step exceeded its deadline") from exc
        except Exception as exc:
            await _record_step_failure(execution, action, start, exc)
            raise

def effective_preparation(adapter: str, config: dict | None) -> dict:
    preparation = {"mode": "none", **(config or {})}
    if operator_controlled(adapter):
        preparation["mode"] = "manual"
    return preparation


def preparation_duration_seconds(config: dict) -> float:
    if config["mode"] == "manual":
        return float(config.get("lifetime_seconds", 3600))
    if config["mode"] == "automated":
        return float(config.get("max_seconds", 90))
    return 0.0


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
    if steps[0]["action"] == "navigate":
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
                execution.lifecycle_timeline.mark("first_interaction")
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
                    execution.lifecycle_timeline.mark("first_interaction")
                    await page.mouse.click(x, y)
                elif kind == "type":
                    text = command.get("text", "")
                    if not isinstance(text, str) or len(text) > 4000:
                        raise StepFailed("Manual input exceeds its size budget")
                    execution.lifecycle_timeline.mark("first_interaction")
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
                    execution.lifecycle_timeline.mark("first_interaction")
                    await page.keyboard.press(key)
                elif kind == "scroll":
                    delta = float(command.get("delta_y", 0))
                    if not math.isfinite(delta) or abs(delta) > 3000:
                        raise StepFailed("Manual scroll exceeds its bounds")
                    execution.lifecycle_timeline.mark("first_interaction")
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
                viewport = execution.metadata.get("viewport") or page.viewport_size or {"width": 1280, "height": 720}
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
    failed = (
        execution.block.is_set() or execution.metadata.get("workflow_outcome") != "completed"
        if execution.workflow_version == 2
        else execution.block.is_set() or execution.steps_completed != len(steps)
    )
    if policy == "none" or (policy == "on_failure" and not failed):
        return
    if execution.emit is None:
        execution.metadata["artifact_capture_error"] = "No artifact persistence channel is configured"
        return
    failed_artifacts = []
    try:
        masks = [page.locator("input[type=password], [data-abrl-secret]")]
        masks.extend(
            _locator_root(page, step).locator(step["selector"])
            for step in walk_workflow_steps(steps)
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
    except Exception:
        failed_artifacts.append("screenshot")
    try:
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
    except Exception:
        failed_artifacts.append("console")
    if failed_artifacts:
        execution.metadata["artifact_capture"] = "partial" if len(failed_artifacts) == 1 else "unavailable"
        execution.metadata["artifact_capture_error"] = f"Capture unavailable for {', '.join(failed_artifacts)}"
    else:
        execution.metadata["artifact_capture"] = "completed"


async def _apply_device_preset(
    page: Page, adapter: str, preset: DevicePreset | None, execution: Execution,
) -> dict:
    device = device_viewport(preset)
    if device is None:
        return {"device_preset": None, "device_mode": "browser_default", "viewport": page.viewport_size}
    conformance = execution.metadata.setdefault("viewport_conformance", {"checks": 0, "matched": 0})
    conformance["checks"] += 1
    if "mobile_emulation" in session_capabilities(adapter):
        actual = await page.evaluate(
            """() => ({
                width: screen.width, height: screen.height,
                scale: devicePixelRatio, touch: navigator.maxTouchPoints > 0
            })"""
        )
        if actual != {**device.size, "scale": device.scale, "touch": True}:
            execution.record_failure("viewport_mismatch")
            raise AdapterUnavailable("Chromium did not apply mobile emulation", "invalid_configuration")
        mode = "mobile_emulation"
    else:
        await page.set_viewport_size(device.size)
        mode = "responsive_viewport"
    # A CDP-attached Playwright client can omit the cached viewport even when
    # the browser reports the configured mobile screen, scale, and touch state.
    viewport = page.viewport_size
    if (viewport is not None or mode == "responsive_viewport") and viewport != device.size:
        execution.record_failure("viewport_mismatch")
        raise AdapterUnavailable("Browser did not apply the configured viewport", "invalid_configuration")
    conformance["matched"] += 1
    return {"device_preset": preset, "device_mode": mode, "viewport": device.size}


async def _apply_batch_preset(
    page: Page, session: CDPSession, preset: PresetDetails, execution: Execution,
) -> dict:
    expected = preset["viewport"]
    conformance = execution.metadata.setdefault("viewport_conformance", {"checks": 0, "matched": 0})
    conformance["checks"] += 1
    if preset["application"] == "viewport_only":
        # Page.set_viewport_size also overrides screen dimensions and DPR on CDP-attached browsers.
        dimensions = "() => [innerWidth, innerHeight, screen.width, screen.height, devicePixelRatio]"
        try:
            before = await page.evaluate(dimensions)
            if expected["width"] > before[2] or expected["height"] > before[3]:
                execution.record_failure("viewport_mismatch")
                raise AdapterUnavailable("The configured viewport exceeds the adapter screen", "invalid_configuration")
            await session.send("Emulation.setVisibleSize", expected)
            width, height, *display = await page.evaluate(dimensions)
        except PlaywrightError:
            execution.record_failure("viewport_mismatch")
            raise AdapterUnavailable("The adapter could not resize the viewport", "invalid_configuration") from None
        if display != before[2:]:
            execution.record_failure("viewport_mismatch")
            raise AdapterUnavailable("Viewport resizing changed native display settings", "invalid_configuration")
        actual = {"width": width, "height": height}
    else:
        actual = await page.evaluate("() => ({width: innerWidth, height: innerHeight})")
    if actual != expected:
        execution.record_failure("viewport_mismatch")
        raise AdapterUnavailable("Browser did not apply the configured viewport", "invalid_configuration")
    conformance["matched"] += 1
    return {"device_preset": None, "device_mode": "responsive_viewport", "viewport": expected}


async def _wait_for_first_recording_frame(execution: Execution, assignment_timeout: asyncio.Timeout) -> None:
    recorder = execution.recording
    if recorder is None or not recorder.waiting_for_first_frame or execution.block.is_set():
        return
    loop = asyncio.get_running_loop()
    deadline = assignment_timeout.when()
    started = loop.time()
    if deadline is None or deadline <= started:
        return
    # Steps and site verification have finished. A quick navigation can still
    # need one paint before capture. Exclude only this bounded wait from the
    # assignment timer, then resume the original time allowance for cleanup.
    assignment_timeout.reschedule(None)
    try:
        await recorder.wait_first_frame()
    finally:
        waited = loop.time() - started
        execution.recording_wait_seconds += waited
        # Cancellation or a network block can end the parent timeout first.
        with suppress(RuntimeError):
            assignment_timeout.reschedule(deadline + waited)


@contextmanager
def _javascript_capture_wait(execution: Execution, assignment_timeout: asyncio.Timeout):
    # The collector bounds its protocol calls separately from the browser assignment.
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = assignment_timeout.when()
    if deadline is not None and deadline > started and not assignment_timeout.expired():
        assignment_timeout.reschedule(None)
    else:
        deadline = None
    try:
        yield
    finally:
        waited = loop.time() - started
        execution.javascript_wait_seconds += waited
        if deadline is not None:
            with suppress(RuntimeError):
                assignment_timeout.reschedule(deadline + waited)


async def _drive(
    steps: list[dict],
    execution: Execution,
    *,
    adapter: str,
    user_agent: str | None,
    device_preset: DevicePreset | None,
    batch_preset: dict | None,
    profile_dir: str | None,
    preparation: dict,
    proxy: dict | None,
    control,
    expected_compatibility: dict | None,
    remote: dict | None,
    artifact_policy: str,
    traffic_budget_bytes: int,
    assignment_timeout: asyncio.Timeout,
) -> None:
    responsive = preset_details(batch_preset)
    if batch_preset is not None and responsive is None:
        raise AdapterUnavailable("Invalid batch preset", "invalid_configuration")
    upstream = ProxyBinding.from_config(proxy) if proxy and not remote else None
    connect_trace = None if remote else NavigationConnectTrace()
    execution.connect_trace = connect_trace
    vendor_control_origins = None
    if remote is None and adapter in {"cloakbrowser", "kameleo"}:
        target_origins = {execution.origin} | execution.navigation_origins | execution.resources
        controls = {CLOAKBROWSER_LICENSE_ORIGIN} if adapter == "cloakbrowser" else KAMELEO_CONTROL_ORIGINS
        vendor_control_origins = sorted(controls - target_origins)
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
        vendor_control_origins=vendor_control_origins,
        allow_automatic_assets=not execution.owned_diagnostic and not (proxy or {}).get("target_origin"),
        startup_origins=CLOAKBROWSER_GEOIP_ORIGINS if remote is None and adapter == "cloakbrowser" else (),
        observe_connect=connect_trace.observe_connect if connect_trace is not None else None,
    ) as guard:
        guard.forwarding_enabled = False
        runtime_lost = False
        containment_failed = False
        try:
            await execution.event("launching", adapter=adapter)
            async with launch_adapter(
                adapter,
                guard.url,
                profile_dir,
                execution.metadata,
                remote=remote,
                expected_compatibility=expected_compatibility,
                user_agent=user_agent,
                device_preset=device_preset,
                **({"batch_preset": batch_preset} if batch_preset is not None else {}),
            ) as runtime:
                execution.lifecycle_timeline.mark("browser_launched")
                page = None
                context = runtime.context
                watching_runtime = True
                attached_context = remote is None and adapter in {"manual_chrome", "kameleo"}

                def worker_started(_worker):
                    nonlocal containment_failed
                    if watching_runtime and not _extension_service_worker(_worker):
                        containment_failed = True
                        guard.stop_forwarding()
                        execution.deny("Service worker started in an attached browser", code="service_worker_not_allowed")

                def runtime_exited(_source):
                    nonlocal runtime_lost
                    if watching_runtime:
                        runtime_lost = True
                        execution.deny("Browser runtime exited unexpectedly", code="browser_crash", failed=True)

                context.on("close", runtime_exited)
                if attached_context:
                    context.on("serviceworker", worker_started)
                try:
                    # Persistent launches can restore pages. Close them before any
                    # permitted target navigation; all newly opened windows are denied.
                    if attached_context and any(not _extension_service_worker(worker) for worker in context.service_workers):
                        raise AdapterUnavailable("The attached browser has an active service worker", "containment_unavailable")
                    page = context.pages[0] if context.pages else await context.new_page()
                    if attached_context and page.url != "about:blank":
                        page = await context.new_page()
                    page.on("crash", runtime_exited)
                    for extra in context.pages:
                        if extra is not page:
                            await extra.close()

                    def collect_console(message):
                        if len(execution.console_messages) < 100:
                            execution.console_messages.append(
                                {"type": message.type, "text": message.text[:2000]}
                            )

                    page.on("console", collect_console)
                    session = await context.new_cdp_session(page)
                    if attached_context:
                        # A session-scoped download override must survive until browser shutdown.
                        _containment_session = await _install_attached_context_policy(context, session)
                        await _bypass_service_workers(session)
                    if execution.owned_diagnostic:
                        execution.metadata["quic_launch_flag"] = await quic_launch_check(session)
                    execution.pages["main"] = page
                    allow_asset = None
                    if not execution.owned_diagnostic:
                        allow_asset = getattr(runtime, "allow_asset_origin", None) if remote else guard.allow_asset_origin
                    await _attach_page_policy(
                        context,
                        page,
                        session,
                        execution,
                        proxy_auth=getattr(runtime, "proxy_auth", None),
                        allow_asset_origin=allow_asset,
                    )

                    async def apply_viewport(tab: Page, tab_session: CDPSession) -> dict:
                        if responsive is not None:
                            return await _apply_batch_preset(tab, tab_session, responsive, execution)
                        return await _apply_device_preset(tab, adapter, device_preset, execution)

                    async def attach_tab(tab: Page) -> None:
                        tab.on("crash", runtime_exited)
                        tab_session = await context.new_cdp_session(tab)
                        if attached_context:
                            await _bypass_service_workers(tab_session)
                        await _attach_page_policy(
                            context, tab, tab_session, execution,
                            proxy_auth=getattr(runtime, "proxy_auth", None),
                            allow_asset_origin=allow_asset,
                            register_context_routes=False,
                        )
                        tab.on("console", collect_console)
                        await apply_viewport(tab, tab_session)

                    execution.attach_page = attach_tab
                    actual = await session.send("Browser.getVersion")
                    device_metadata = await apply_viewport(page, session)
                    execution.metadata.update(
                        device_metadata,
                        browser_engine="Chromium",
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
                        execution.record_failure("profile_incompatible", phase="profile")
                        raise StepFailed("Persistent profile browser/platform compatibility does not match")
                    if (
                        expected_compatibility is not None
                        and execution.metadata.get("profile_launch_state") != "restored"
                    ):
                        execution.metadata["profile_compatible"] = False
                        raise AdapterUnavailable("The saved browser profile was not loaded", "profile_restore_failed")
                    if capture := execution.javascript_capture:
                        try:
                            with _javascript_capture_wait(execution, assignment_timeout):
                                await capture.start(
                                    context, page, permits_url=lambda url: execution.permits(url, document=True)
                                )
                        except Exception:
                            capture.note_gap("setup_failed")
                    if execution.block.is_set():
                        return
                    guard.activate_forwarding()
                    if remote:
                        await runtime.activate(traffic_budget_bytes=traffic_budget_bytes)
                    if execution.recording:
                        execution.recording.start(
                            page,
                            [
                                (step.get("frame") or [step["selector"]])[0]
                                for step in walk_workflow_steps(steps)
                                if step.get("secret_ref") and step.get("selector")
                            ],
                            lambda url: not execution.block.is_set() and execution.permits(url, document=True),
                        )
                    await execution.event(
                        "launched",
                        adapter=adapter,
                        browser_engine="Chromium",
                        browser_version=actual.get("product"),
                        **device_metadata,
                        **({"viewport_conformance": execution.metadata["viewport_conformance"]}
                           if responsive is not None else {}),
                    )
                    await _prepare(page, steps, execution, preparation, control)
                    if execution.block.is_set():
                        return
                    workflow_start = monotonic()
                    await execution.event("workflow_started")
                    async with asyncio.timeout(execution.metadata["time_budgets"]["workflow_seconds"]):
                        await _execute_steps(steps, execution, adapter)
                        if execution.block.is_set():
                            return
                        if execution.success_check is not None:
                            execution.note_post_load_step()
                            execution.step_path = ["success_check"]
                            execution.active_step += 1
                            check_started = monotonic()
                            execution.active_action = "success_check"
                            execution.active_step_started = check_started
                            execution.navigation_wait = None
                            check_deadline = check_started + DEFAULT_STEP_TIMEOUT_MS / 1000
                            selected = execution.pages[execution.active_tab]
                            try:
                                with execution.measure_readiness("success_check"):
                                    async with asyncio.timeout(DEFAULT_STEP_TIMEOUT_MS / 1000):
                                        await _wait_until(
                                            lambda: _condition_matches(
                                                selected, selected, execution.success_check, execution, check_deadline
                                            ),
                                            check_deadline,
                                        )
                            except asyncio.CancelledError:
                                execution.note_interrupted_navigation("success_check")
                                raise
                            except (TimeoutError, PlaywrightTimeout) as exc:
                                await _record_step_failure(execution, "success_check", check_started, exc)
                                execution.metadata["completion_status"] = "check_failed"
                                raise StepFailed("The final workflow check did not pass") from exc
                        execution.metadata["completion_status"] = (
                            "verified"
                            if execution.success_check is not None
                            or (execution.workflow_version == 1 and steps[-1]["action"] in ASSERTIONS)
                            else "steps_completed"
                        )
                        execution.metadata["workflow_outcome"] = "completed"
                        execution.lifecycle_timeline.mark("workflow_completed")
                        execution.metadata["workflow_elapsed_ms"] = round(
                            (monotonic() - workflow_start) * 1000
                        )
                        if execution.wait_for_observation is not None:
                            received = await execution.wait_for_observation()
                            execution.metadata["observation_received"] = received
                            if not received:
                                raise StepFailed("No site verification callback was recorded")
                    await _wait_for_first_recording_frame(execution, assignment_timeout)
                finally:
                    try:
                        if execution.recording:
                            execution.recording.stop()
                        if capture := execution.javascript_capture:
                            try:
                                with _javascript_capture_wait(execution, assignment_timeout):
                                    await capture.stop()
                            except asyncio.CancelledError:
                                capture.note_gap("cleanup_interrupted")
                                raise
                            except Exception:
                                capture.note_gap("cleanup_failed")
                        if page is not None and not runtime_lost:
                            selected = execution.pages.get(execution.active_tab, page)
                            await _preserve_ready_diagnostics(selected, execution)
                            await _capture_artifacts(selected, execution, steps, artifact_policy)
                        if execution.adscore_referrer_reads:
                            await asyncio.gather(*execution.adscore_referrer_reads)
                    finally:
                        watching_runtime = False  # Adapter teardown closes the context intentionally.
                        context.remove_listener("close", runtime_exited)
                        if attached_context:
                            guard.stop_forwarding()
                            context.remove_listener("serviceworker", worker_started)

        except AdapterUnavailable as exc:
            if exc.status == "containment_unavailable":
                containment_failed = True
            raise
        finally:
            if runtime_lost or containment_failed:
                execution.metadata["profile_saved"] = False
            if not remote:
                execution.metadata["transport_events"] = guard.transport_events
                execution.metadata["transport_summary"] = guard.transport_summary()
                execution.metadata["transport_usage"] = guard.transport_usage()
                execution.metadata["vendor_control_bytes"] = guard.vendor_control_bytes
            if connect_trace is not None and (trace := connect_trace.export()) is not None:
                execution.metadata["navigation_connects"] = trace
            transport_events = execution.metadata.get("transport_events", [])
            if transport_events:
                upstream_failure = next(
                    (event for event in transport_events if event["kind"] in {"authentication", "quota"}),
                    transport_events[-1],
                )
                execution.metadata["failure_kind"] = upstream_failure["kind"]
                if execution.metadata.get("non_idempotent_action_started"):
                    for event in transport_events:
                        if event["retryable"]:
                            event.update(retryable=False, action="needs_reconciliation")
            await execution.event(
                "browser_closed",
                profile_saved=execution.metadata.get("profile_saved", False),
                confirmed=execution.metadata.get("browser_closed", False),
            )


def _resolved_steps(steps: list[dict]) -> list[dict]:
    resolved = []
    for step in steps:
        node = dict(step)
        if (params := step.get("fixture_params")) is not None:
            node["url"] = resolve_navigation_parameters(step["url"], params)
        for key in ("steps", "else_steps"):
            if children := step.get(key):
                node[key] = _resolved_steps(children)
        resolved.append(node)
    return resolved


def _instrumented_url(url: str, context: str) -> str:
    parts = urlsplit(url)
    if any(key == "abrl_context" for key, _ in parse_qsl(parts.query, keep_blank_values=True)):
        raise StepFailed("The instrumented navigation URL must not set abrl_context")
    context_query = urlencode({"abrl_context": context})
    query = f"{parts.query}&{context_query}" if parts.query else context_query
    return urlunsplit(parts._replace(query=query))


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
    proxy_navigation_timeout_ms: int | None = None,
    emit: Callable[[dict], Awaitable[None]] | None = None,
    control: Callable[[], Awaitable[dict | None]] | None = None,
    expected_compatibility: dict | None = None,
    remote: dict | None = None,
    artifact_policy: str = "none",
    request_budget: int = 1000,
    traffic_budget_bytes: int = 104857600,
    user_agent: str | None = None,
    device_preset: DevicePreset | None = None,
    batch_preset: dict | None = None,
    referrer: str | None = None,
    observation_context: str | None = None,
    wait_for_observation: Callable[[], Awaitable[bool]] | None = None,
    recording_directory: Path | None = None,
    store_recording_segment: Callable[[RecordingSegment], Awaitable[None]] | None = None,
    recording_started: Callable[[], Awaitable[None]] | None = None,
    javascript_capture: JavaScriptCapture | None = None,
    owned_diagnostic: bool = False,
    workflow: dict | None = None,
    workflow_inputs: dict | None = None,
    lifecycle_timeline: LifecycleTimeline | None = None,
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
        "runtime_stopped": True,
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
    execution = Execution(
        origin, set(resource_origins), metadata=metadata, emit=emit, javascript_capture=javascript_capture,
        started_at=started, owned_diagnostic=owned_diagnostic,
        lifecycle_timeline=lifecycle_timeline if lifecycle_timeline is not None else LifecycleTimeline(),
    )
    if recording_directory is not None and store_recording_segment is not None:
        execution.recording = SessionRecorder(recording_directory, store_recording_segment, recording_started)
        metadata["recording_status"] = "unavailable"
    try:
        execution.initial_navigation_timeout = _initial_navigation_timeout(
            (workflow or {}).get("initial_navigation_timeout_ms"), proxy_navigation_timeout_ms, proxied=bool(proxy),
        )
        if expected_compatibility is not None and not profile_dir:
            raise AdapterUnavailable("Saved profile restoration requires its profile directory", "profile_restore_failed")
        if type(request_budget) is not int or not 1 <= request_budget <= 100000:
            raise StepFailed("HTTP request budget must be an integer from 1 to 100000")
        if type(traffic_budget_bytes) is not int or not 1024 <= traffic_budget_bytes <= 2 * 1024**3:
            raise StepFailed("Traffic budget must be an integer from 1024 bytes to 2 GiB")
        execution.request_budget = request_budget
        if artifact_policy not in {"none", "on_failure", "all"}:
            raise StepFailed("Invalid artifact retention policy")
        try:
            execution.initial_referrer = validate_referrer(referrer)
        except ValueError as exc:
            raise StepFailed(str(exc)) from exc
        execution.origin = normalize_origin(origin)
        execution.navigation_origins = {normalize_origin(value) for value in navigation_origins or []}
        if any(
            not value.startswith("https://") for value in execution.navigation_origins - {execution.origin}
        ):
            raise StepFailed("Additional navigation origins must use HTTPS")
        execution.resources = {normalize_origin(resource) for resource in resource_origins}
        if workflow and workflow.get("schema_version") == 2:
            execution.workflow_version = 2
            execution.execution_step_limit = workflow_execution_limit()
            execution.success_check = workflow.get("success_check")
            execution.values = resolve_workflow_inputs(workflow, workflow_inputs)
        elif workflow_inputs:
            raise StepFailed("Workflow inputs require a version 2 workflow")
        if not steps or (execution.workflow_version == 1 and steps[0].get("action") != "navigate"):
            raise StepFailed("A workflow must begin with navigation")
        try:
            if execution.workflow_version == 2:
                validate_workflow_page_paths(steps)
            validate_navigation_workflow_scope(
                steps,
                origin=execution.origin,
                resource_origins=execution.resources,
                navigation_origins=execution.navigation_origins,
                fixture_origin=FIXTURE_ORIGIN,
                remote=remote is not None,
                constrain_navigation_only=False,
                all_navigation=execution.workflow_version == 2,
            )
            steps = _resolved_steps(steps)
        except ValueError as exc:
            raise StepFailed(str(exc)) from exc
        if observation_context is not None:
            execution.network_trace = InstrumentedNetworkTrace(
                execution.origin, lambda: (execution.phase, execution.active_step)
            )
            metadata["instrumented_network_trace"] = execution.network_trace.data
            if execution.workflow_version == 2:
                for node in walk_workflow_steps(steps):
                    if node["action"] in NAVIGATION_ACTIONS:
                        _instrumented_url(node["url"], observation_context)
                execution.observation_context = observation_context
            else:
                first_url = urljoin(execution.origin + "/", steps[0]["url"])
                if not execution.permits(first_url, document=True):
                    raise NetworkDenied(_navigation_scope_error(first_url))
                steps = [{**steps[0], "url": _instrumented_url(first_url, observation_context)}, *steps[1:]]
            if len(steps) == 1 and steps[0]["action"] == "navigate":
                execution.wait_for_observation = wait_for_observation
        if await cancel():
            return BrowserResult("cancelled", 0, None, metadata, False)
        preparation = effective_preparation(adapter, preparation)
        if adapter == "manual_chrome":
            has_success_check = (
                execution.success_check is not None if execution.workflow_version == 2
                else any(step.get("action", "").startswith("assert_") for step in steps)
            )
            if not has_success_check:
                raise StepFailed("Manual Chrome workflows require an explicit success assertion")
        if preparation.get("mode") == "manual" and control is None:
            raise StepFailed("Manual preparation requires an authenticated operator channel")
        if preparation.get("mode") == "automated":
            preparation_plan(preparation)  # Validate before opening the browser.
        for step in walk_workflow_steps(steps):
            if step.get("secret_ref"):
                _secret_value(step["secret_ref"], execution)
        preparation_budget = preparation_duration_seconds(preparation)
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 86400:
            raise StepFailed("Workflow time budget must be positive and finite")
        metadata["time_budgets"] = {
            "workflow_seconds": timeout_seconds,
            "preparation_seconds": preparation_budget,
        }
        assignment_timeout = asyncio.timeout(timeout_seconds + preparation_budget)
        operation = asyncio.create_task(
            _drive(
                steps,
                execution,
                adapter=adapter,
                user_agent=user_agent,
                device_preset=device_preset,
                batch_preset=batch_preset,
                profile_dir=profile_dir,
                preparation=preparation,
                proxy=proxy,
                control=control,
                expected_compatibility=expected_compatibility,
                remote=remote,
                artifact_policy=artifact_policy,
                traffic_budget_bytes=traffic_budget_bytes,
                assignment_timeout=assignment_timeout,
            )
        )
        cancellation = asyncio.create_task(_await_cancellation(cancel))
        blocked = asyncio.create_task(execution.block.wait())
        tasks = (operation, cancellation, blocked)
        try:
            async with assignment_timeout:
                finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if cancellation in finished:
                    cancellation.result()
                    outcome = "cancelled"
                elif execution.block.is_set():
                    outcome = "failed" if execution.terminal_failed else "blocked"
                    error = execution.reason
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
        phase = "profile" if exc.status in {"profile_restore_failed", "profile_incompatible"} else "startup"
        execution.record_failure(exc.status, phase=phase)
        if exc.status in {
            "launch_timeout", "capacity_unverified", "capacity_exhausted", "engine_unavailable",
        } and not execution.started_action:
            metadata.setdefault("failure_kind", "startup_transient")
    except (StepFailed, ProfileStorageError, ProxyConfigurationError) as exc:
        error = str(exc)
    except PlaywrightTimeout:
        if not execution.lifecycle_timeline.has("browser_launched"):
            execution.record_failure("launch_timeout", phase="startup")
            metadata.setdefault("failure_kind", "startup_transient")
        error = (
            f"Step {execution.active_step} timed out"
            if execution.started_action
            else "Chromium startup timed out"
        )
    except TimeoutError:
        if not execution.lifecycle_timeline.has("browser_launched"):
            execution.record_failure("launch_timeout", phase="startup")
            metadata.setdefault("failure_kind", "startup_transient")
        elif execution.interrupted_navigation is None:
            execution.record_failure("timeout")
        else:
            execution.record_failure("navigation_timeout", **execution.interrupted_navigation)
        if execution.wait_for_observation and metadata.get("workflow_outcome") == "completed":
            metadata["observation_received"] = False
            error = "Site verification exceeded the workflow time budget"
        elif execution.lifecycle_timeline.has("browser_launched"):
            error = "Workflow exceeded its time budget"
        else:
            error = "Browser startup exceeded its time budget"
    except PlaywrightError:
        if not execution.lifecycle_timeline.has("browser_launched"):
            execution.record_failure("launch_failed", phase="startup")
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
    finally:
        finished_at = monotonic()
        if execution.recording:
            # Screenshots stop before the browser closes. Encoding and storage
            # finish outside the workflow deadline and timing measurement.
            metadata["recording_status"] = await execution.recording.finish()

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
    if (timeline := execution.lifecycle_timeline.view()) is not None:
        metadata["lifecycle_timeline"] = timeline
    return BrowserResult(
        outcome,
        round(
            (finished_at - started - execution.recording_wait_seconds - execution.javascript_wait_seconds)
            * 1000
        ),
        error,
        metadata,
        execution.started_action,
    )
