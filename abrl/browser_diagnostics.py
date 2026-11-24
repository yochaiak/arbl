"""Collect browser and server observations from the owned diagnostic fixture."""

import argparse
import asyncio
import json
import os
import re
import secrets
from datetime import UTC, datetime
from tempfile import TemporaryDirectory
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict

from .browser import BrowserResult, execute_workflow
from .browser_network_trace import (
    public_http_status_counts,
    public_transport_checks,
    public_transport_summary,
)
from .browser_settings import DEVICE_VIEWPORTS, DevicePreset
from .connect_telemetry import public_navigation_connects
from .diagnostic_bundle_lifecycle import public_lifecycle_failure
from .diagnostic_fixture import DIAGNOSTIC_GROUPS, DIAGNOSTIC_PATH, PROXY_HEADER_NAMES
from .lifecycle import (
    LifecycleTimeline,
    public_duration_ms,
    public_initial_navigation_timeout,
    public_lifecycle_timeline,
)
from .network import FIXTURE_ORIGIN, first_proxy_tunnel_failure, normalize_origin, resolve_public_ipv4
from .proxy import DECODO_GATEWAY, DECODO_PORT, ProxyBinding

_CHROMIUM_VERSION = re.compile(r"\b(?:HeadlessChrome|Chrome|Chromium)/(\d+)")
_CHROMIUM_BRANDS = frozenset({"chromium", "google chrome"})
_STRUCTURED_BRANDS = re.compile(r'(?:^|,)\s*"([^"\\]+)";\s*v="(\d+(?:\.\d+){0,3})"')
_LANGUAGE_TAG = re.compile(r"[a-z]{1,8}(?:-[a-z0-9]{1,8})*\Z", re.IGNORECASE | re.ASCII)
_PLACEHOLDER_SUFFIXES = (".localhost", ".local", ".example", ".test", ".invalid", ".internal", ".onion")
_EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net")
_CAPABILITY_FIELDS = (
    "audio_context", "canvas_2d", "webgl", "font_loading", "performance_observer",
    "network_information", "service_worker",
)
_CAPABILITY_STATES = ("available", "unsupported", "unavailable")
_CONNECTION_METRICS = ("duration", "request_to_first_byte", "connect")
_MAX_CONNECTION_TIMING_MS = 60_000
_CLIENT_HINT_FIELDS = (
    "client_hints_brands", "client_hints_full_versions", "client_hints_mobile", "client_hints_platform",
    "client_hints_platform_version",
)
_OBSERVATION_PRESENCE_FIELDS = {
    "request_header_presence": ("user_agent", "accept_language", *_CLIENT_HINT_FIELDS),
    "navigator_presence": ("user_agent", "language", "languages", *_CLIENT_HINT_FIELDS),
}


class ProbeGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Identity(ProbeGroup):
    server_ua: str | None
    browser_ua: str | None
    platform: str | None
    webdriver: bool | None
    proxy_header_names: list[str]


class Hints(ProbeGroup):
    server_brands: str | None
    server_full_versions: str | None
    server_mobile: str | None
    server_platform: str | None
    server_platform_version: str | None
    browser_brands: list[list[str]]
    browser_full_versions: bool
    browser_mobile: bool | None
    browser_platform: str | None
    browser_platform_version: str | None


class Environment(ProbeGroup):
    accept_language: str | None
    language: str | None
    languages: list[str]
    timezone: str | None
    offset_minutes: int
    before_epoch_ms: int
    after_epoch_ms: int
    server_epoch_ms: int


class WebGL(ProbeGroup):
    vendor: str | None
    renderer: str | None
    unmasked: bool


class Ice(ProbeGroup):
    state: Literal["not_configured", "unavailable", "timeout", "complete"]
    literal: int
    other: int
    masked: int


HttpProtocol = Literal["http/1.0", "http/1.1", "h2", "h3", "unavailable"]


class Rendering(ProbeGroup):
    inner: list[int]
    screen: list[int]
    dpr: float
    webgl: WebGL | None
    ice: Ice
    document_protocol: HttpProtocol = "unavailable"
    echo_protocol: HttpProtocol = "unavailable"
    capabilities: object = None
    connection_timings_ms: object = None


def public_capabilities(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    return {
        name: state for name in _CAPABILITY_FIELDS if (state := value.get(name)) in _CAPABILITY_STATES
    } or None


def public_connection_timings(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = {}
    for name in ("document", "echo"):
        entry = value.get(name)
        if not isinstance(entry, dict) or entry.get("status") not in ("observed", "unsupported", "unavailable"):
            continue
        status = entry["status"]
        timings = dict.fromkeys(_CONNECTION_METRICS)
        if status == "observed":
            for metric in _CONNECTION_METRICS:
                sample = entry.get(metric)
                if type(sample) is int and 0 <= sample <= _MAX_CONNECTION_TIMING_MS:
                    timings[metric] = sample
            if all(sample is None for sample in timings.values()):
                status = "unavailable"
        result[name] = {"status": status, **timings}
    return result or None


def public_recorded_observations(value: object) -> dict | None:
    """Retain fixed presence checks and geometry without header contents or identity strings."""
    if not isinstance(value, dict):
        return None
    result = {}
    for group, fields in _OBSERVATION_PRESENCE_FIELDS.items():
        row = value.get(group)
        if isinstance(row, dict):
            values = {name: row[name] for name in fields if type(row.get(name)) is bool}
            if values:
                result[group] = values

    environment = value.get("environment")
    if isinstance(environment, dict):
        safe = {}
        if type(present := environment.get("timezone_present")) is bool:
            safe["timezone_present"] = present
        if type(offset := environment.get("timezone_offset_minutes")) is int and -1440 <= offset <= 1440:
            safe["timezone_offset_minutes"] = offset
        if safe:
            result["environment"] = safe

    rendering = value.get("rendering")
    if isinstance(rendering, dict):
        safe = {}
        if type(available := rendering.get("webgl_available")) is bool:
            safe["webgl_available"] = available
        for name in ("viewport_px", "screen_px"):
            dimensions = rendering.get(name)
            if (
                isinstance(dimensions, list) and len(dimensions) == 2
                and all(type(dimension) is int and 1 <= dimension <= 16384 for dimension in dimensions)
            ):
                safe[name] = list(dimensions)
        if safe:
            result["rendering"] = safe
    return result or None


def _recorded_observations(identity: Identity, hints: Hints, environment: Environment, rendering: Rendering) -> dict:
    return {
        "request_header_presence": {
            "user_agent": bool(identity.server_ua),
            "accept_language": bool(environment.accept_language),
            "client_hints_brands": bool(hints.server_brands),
            "client_hints_full_versions": bool(hints.server_full_versions),
            "client_hints_mobile": bool(hints.server_mobile),
            "client_hints_platform": bool(hints.server_platform),
            "client_hints_platform_version": bool(hints.server_platform_version),
        },
        "navigator_presence": {
            "user_agent": bool(identity.browser_ua),
            "language": bool(environment.language),
            "languages": bool(environment.languages),
            "client_hints_brands": bool(hints.browser_brands),
            "client_hints_full_versions": hints.browser_full_versions,
            "client_hints_mobile": hints.browser_mobile is not None,
            "client_hints_platform": bool(hints.browser_platform),
            "client_hints_platform_version": bool(hints.browser_platform_version),
        },
        "environment": {
            "timezone_present": bool(environment.timezone),
            "timezone_offset_minutes": environment.offset_minutes,
        },
        "rendering": {
            "viewport_px": rendering.inner,
            "screen_px": rendering.screen,
            "webgl_available": rendering.webgl is not None,
        },
    }


def _consistent(*values: object) -> str:
    observed = [value for value in values if value is not None]
    if len(observed) < 2:
        return "unavailable"
    return "passed" if all(value == observed[0] for value in observed[1:]) else "mismatch"


def _chromium_brands(pairs: list) -> dict[str, str]:
    return {
        pair[0].lower(): pair[1]
        for pair in pairs
        if len(pair) == 2 and pair[0].lower() in _CHROMIUM_BRANDS and re.fullmatch(r"\d+(?:\.\d+){0,3}", pair[1])
    }


def _ua_platform(value: str | None) -> str | None:
    value = (value or "").lower()
    for token, platform in (
        ("android", "android"), ("cros", "chrome os"), ("windows", "windows"), ("win32", "windows"),
        ("iphone", "ios"), ("ipad", "ios"), ("mac", "macos"), ("linux", "linux"),
    ):
        if token in value:
            return platform
    return None


def _language_tags(value: str | None) -> list[str]:
    tags = []
    for part in (value or "").split(","):
        tag, _, weight = part.partition(";")
        tag = tag.strip().lower()
        if _LANGUAGE_TAG.fullmatch(tag) and not re.fullmatch(r"\s*q=0(?:\.0{1,3})?\s*", weight):
            tags.append(tag)
    return tags


def _locale_consistency(language: str | None, browser: list[str], http: list[str]) -> str:
    if not language or not browser or not http:
        return "unavailable"
    # Chromium can insert base-language fallbacks between the configured preferences.
    remaining = iter(http)
    consistent = language == browser[0] == http[0] and all(tag in remaining for tag in browser)
    return "passed" if consistent else "mismatch"


def _configured_public_origin() -> str | None:
    url = os.environ.get("ABRL_DIAGNOSTIC_URL")
    if not url:
        return None
    parts = urlsplit(url)
    if parts.path not in {"", "/", DIAGNOSTIC_PATH} or "?" in url or "#" in url:
        raise ValueError("ABRL_DIAGNOSTIC_URL must be the owned HTTPS origin or its /lab/diagnostics URL")
    origin = normalize_origin(url.removesuffix(DIAGNOSTIC_PATH))
    hostname = parts.hostname or ""
    if (
        not origin.startswith("https://") or hostname.endswith(_PLACEHOLDER_SUFFIXES)
        or any(hostname == name or hostname.endswith("." + name) for name in _EXAMPLE_DOMAINS)
    ):
        raise ValueError("ABRL_DIAGNOSTIC_URL must identify a public, owned HTTPS fixture")
    return origin


def _offset_at(timezone: str | None, milliseconds: int) -> int | None:
    if timezone is not None:
        try:
            offset = datetime.fromtimestamp(milliseconds / 1000, tz=ZoneInfo(timezone)).utcoffset()
            if offset is not None:
                return round(offset.total_seconds() / 60)
        except (ZoneInfoNotFoundError, ValueError, OverflowError, OSError):
            pass
    return None


def _expected_edge_headers(origin: str | None) -> set[str]:
    configured = os.getenv("ABRL_DIAGNOSTIC_EDGE_HEADERS")
    if not configured or not origin or origin == FIXTURE_ORIGIN or origin != _configured_public_origin():
        return set()
    # Only forwarding metadata independently seen on an unproxied request to this fixture.
    names = {value.strip().lower() for value in configured.split(",")}
    return names & {"forwarded", "x-forwarded-for", "x-real-ip", "via"}


def compare_observations(
    identity: Identity,
    hints: Hints,
    environment: Environment,
    rendering: Rendering,
    *,
    expected_timezone: str | None = None,
    expected_language: str | None = None,
    expected_dpr: int | None = None,
    fixture_origin: str | None = None,
) -> dict:
    server_brands = _chromium_brands(_STRUCTURED_BRANDS.findall(hints.server_brands or ""))
    server_full = _chromium_brands(_STRUCTURED_BRANDS.findall(hints.server_full_versions or ""))
    browser_brands = _chromium_brands(hints.browser_brands)
    ua_major = _CHROMIUM_VERSION.search(identity.browser_ua or "")
    ua_platform = _ua_platform(identity.browser_ua)
    nav_platform = _ua_platform(identity.platform)
    if ua_platform == "android" and nav_platform == "linux":
        nav_platform = "android"
    language = environment.language.lower() if environment.language else None
    http_languages = _language_tags(environment.accept_language)
    browser_languages = list(map(str.lower, environment.languages))
    zone = expected_timezone or environment.timezone
    observed_timezone = environment.timezone
    utc_labels = {"UTC", "Etc/UTC", "GMT", "Etc/GMT"}
    if expected_timezone in utc_labels and observed_timezone in utc_labels:
        observed_timezone = expected_timezone
    skew_ms = (
        max(environment.before_epoch_ms - environment.server_epoch_ms,
            environment.server_epoch_ms - environment.after_epoch_ms, 0)
        if 0 < environment.before_epoch_ms <= environment.after_epoch_ms and environment.server_epoch_ms > 0
        else None
    )
    valid_dimensions = (
        len(rendering.inner) == len(rendering.screen) == 2
        and all(0 < inner <= screen for inner, screen in zip(rendering.inner, rendering.screen, strict=True))
    )
    ice = rendering.ice
    protocols = (rendering.document_protocol, rendering.echo_protocol)
    expected_headers = _expected_edge_headers(fixture_origin)
    proxy_headers = [name for name in PROXY_HEADER_NAMES if name in identity.proxy_header_names and name not in expected_headers]
    checks = {
        "user_agent": _consistent(identity.server_ua, identity.browser_ua),
        "chromium_major": _consistent(
            ua_major.group(1) if ua_major else None,
            *(version.split(".")[0] for version in server_brands.values()),
            *(version.split(".")[0] for version in server_full.values()),
            *(version.split(".")[0] for version in browser_brands.values()),
        ) if ua_major and server_brands and browser_brands else "unavailable",
        "client_hints_full_versions": (
            _consistent(server_full, browser_brands)
            if server_full and browser_brands and hints.browser_full_versions else "unavailable"
        ),
        "user_agent_platform": _consistent(ua_platform, nav_platform, (hints.browser_platform or "").lower() or None),
        "client_hints_platform": _consistent(
            (hints.server_platform or "").strip('"').lower() or None,
            (hints.browser_platform or "").lower() or None,
        ),
        "client_hints_platform_version": _consistent(
            (hints.server_platform_version or "").strip('"') or None, hints.browser_platform_version,
        ),
        "client_hints_mobile": _consistent({"?0": False, "?1": True}.get(hints.server_mobile), hints.browser_mobile),
        "unexpected_proxy_headers": "mismatch" if proxy_headers else "passed",
        "browser_automation": "observed" if identity.webdriver is not None else "unavailable",
        "locale": _locale_consistency(language, browser_languages, http_languages),
        "expected_language": (
            _consistent(language, expected_language.lower()) if expected_language else "not_configured"
        ),
        "timezone": _consistent(expected_timezone, observed_timezone) if expected_timezone else "not_configured",
        "timezone_offset": _consistent(_offset_at(zone, environment.after_epoch_ms), environment.offset_minutes),
        "browser_clock": "unavailable" if skew_ms is None else ("passed" if skew_ms <= 5000 else "mismatch"),
        "viewport": "passed" if valid_dimensions else "mismatch",
        "device_pixel_ratio": (
            "passed" if rendering.dpr > 0 and (expected_dpr is None or rendering.dpr == expected_dpr) else "mismatch"
        ),
        "webgl": "observed" if rendering.webgl is not None else "unavailable",
        "fixture_http_transport": (
            "mismatch" if "h3" in protocols else "unavailable" if "unavailable" in protocols else "passed"
        ),
        "ice": (
            "mismatch" if ice.other > 0 else
            "passed" if ice.state == "complete" and ice.literal > 0 else
            "inconclusive" if ice.state == "complete" else ice.state
        ),
    }
    return {"checks": checks, "minimum_clock_skew_ms": skew_ms}


async def _never_cancel() -> bool:
    return False


def diagnostic_steps() -> list[dict]:
    return [
        {"action": "navigate", "url": DIAGNOSTIC_PATH},
        {"action": "assert_text", "selector": "#abrl-probe-status", "value": "Diagnostics complete"},
        *({"action": "extract", "selector": f"#abrl-{group}"} for group in DIAGNOSTIC_GROUPS),
    ]


def is_managed_diagnostic(origin: str, workflow: dict) -> bool:
    steps = workflow.get("steps", [])
    return bool(
        len(steps) == 1
        and steps[0].get("action") == "navigate"
        and steps[0].get("url") in {DIAGNOSTIC_PATH, origin + DIAGNOSTIC_PATH}
        and _configured_public_origin() == origin
    )


def confirmed_sticky_timezone(preflight: dict | None) -> str | None:
    checks = (preflight or {}).get("checks", {})
    timezone = checks.get("timezone", {})
    if checks.get("sticky", {}).get("status") == "passed" and timezone.get("confirmed") is True:
        return timezone.get("observed")
    return None


async def run_browser_diagnostics(
    *,
    origin: str,
    adapter: str = "playwright",
    proxy: dict | None = None,
    preflight_policy: dict | None = None,
    profile_dir: str | None = None,
    device_preset: DevicePreset | None = None,
    expected_timezone: str | None = None,
    expected_language: str | None = None,
    retain_observations: bool = True,
) -> dict:
    origin = normalize_origin(origin)
    if origin != FIXTURE_ORIGIN:
        if _configured_public_origin() != origin:
            raise ValueError("Configure the owned HTTPS fixture in ABRL_DIAGNOSTIC_URL")
        await resolve_public_ipv4(urlsplit(origin).hostname)
    if adapter not in {"playwright", "cloakbrowser", "kameleo"}:
        raise ValueError("Select an existing automated browser adapter")
    if expected_timezone:
        ZoneInfo(expected_timezone)
    if expected_language and not _LANGUAGE_TAG.fullmatch(expected_language):
        raise ValueError("Expected language must be a language tag")
    preflight = None
    timeline = LifecycleTimeline()
    timezone_source = "operator" if expected_timezone else None
    if proxy is not None:
        if origin == FIXTURE_ORIGIN:
            raise ValueError("A provider proxy requires the owned HTTPS fixture")
        from .proxy_preflight import run_decodo_preflight

        binding = ProxyBinding.from_config(proxy)
        if binding.provider != "decodo":
            raise ValueError("The diagnostic provider probe supports Decodo")
        preflight = await run_decodo_preflight(binding, origin, preflight_policy or {}, _never_cancel)
        timeline.mark("preflight_completed")
        if preflight.get("blocked") or preflight.get("status") == "cancelled":
            return {
                "status": "failed", "reason": preflight.get("reason"), "preflight": preflight,
                "observation_status": "unavailable",
                "lifecycle_timeline": timeline.view(),
            }
        observed_timezone = confirmed_sticky_timezone(preflight)
        if not expected_timezone and binding.continuity == "sticky" and observed_timezone:
            expected_timezone = observed_timezone
            timezone_source = "sticky_preflight"

    result = await execute_workflow(
        origin=origin, resource_origins=[], adapter=adapter, profile_dir=profile_dir, proxy=proxy,
        proxy_navigation_timeout_ms=(preflight_policy or {}).get("initial_navigation_timeout_ms"),
        device_preset=device_preset, artifact_policy="none", request_budget=20, traffic_budget_bytes=1_048_576,
        steps=diagnostic_steps(), owned_diagnostic=True,
        cancel=_never_cancel, timeout_seconds=30, lifecycle_timeline=timeline,
    )
    return diagnostic_report(
        result, adapter=adapter, device_preset=device_preset, preflight=preflight, fixture_origin=origin,
        expected_timezone=expected_timezone, timezone_source=timezone_source, expected_language=expected_language,
        retain_observations=retain_observations,
    )


def diagnostic_report(
    result: BrowserResult,
    *,
    fixture_origin: str | None = None,
    adapter: str,
    device_preset: DevicePreset | None = None,
    preflight: dict | None = None,
    expected_timezone: str | None = None,
    timezone_source: str | None = None,
    expected_language: str | None = None,
    expected_dpr: int | None = None,
    retain_observations: bool = True,
) -> dict:
    report = {
        "adapter": adapter,
        "browser_version": result.metadata.get("browser_version"),
        "device_mode": result.metadata.get("device_mode"),
        "runtime_stopped": result.metadata.get("runtime_stopped"),
        "preflight": preflight,
        "expected_timezone_source": timezone_source,
        "elapsed_ms": public_duration_ms(result.elapsed_ms),
    }
    if (workflow_elapsed := public_duration_ms(result.metadata.get("workflow_elapsed_ms"))) is not None:
        report["workflow_elapsed_ms"] = workflow_elapsed
    if (counts := public_http_status_counts(result.metadata.get("http_status_counts"))) is not None:
        report["http_status_counts"] = counts
    transport_summary = public_transport_summary(result.metadata.get("transport_summary"))
    if transport_summary is not None:
        report["transport_summary"] = transport_summary
    if (timeline := public_lifecycle_timeline(result.metadata.get("lifecycle_timeline"))) is not None:
        report["lifecycle_timeline"] = timeline
    if (navigation_timeout := public_initial_navigation_timeout(result.metadata.get("initial_navigation_timeout"))) is not None:
        report["initial_navigation_timeout"] = navigation_timeout
    if (navigation_connects := public_navigation_connects(result.metadata.get("navigation_connects"))) is not None:
        report["navigation_connects"] = navigation_connects
    transport = public_transport_checks(result.metadata)
    launch_check = transport.pop("quic_launch_flag", None)
    report.update(transport)
    workflow_failure = None
    if result.outcome != "completed":
        if launch_check is not None:
            report["quic_launch_flag"] = launch_check
        failure = result.metadata.get("causal_failure") or result.metadata.get("step_failure")
        failure = first_proxy_tunnel_failure(result.metadata, failure, result.elapsed_ms) or failure or {}
        if (public_failure := public_lifecycle_failure(failure)) is not None:
            report["first_causal_failure"] = public_failure
        rejections = (transport_summary or {}).get("capacity_rejections", 0)
        if rejections > 0:
            report["connection_capacity_rejections"] = rejections
        workflow_failure = {
            "status": "failed",
            "reason": failure.get("reason") or result.metadata.get("failure_kind")
            or result.metadata.get("adapter_status") or "fixture_failed",
        }
    try:
        rows = result.metadata.get("extractions", [])
        if len(rows) != len(DIAGNOSTIC_GROUPS) or any(row["truncated"] for row in rows):
            raise ValueError("Incomplete observations")
        identity, hints, environment, rendering = (
            model.model_validate_json(row["text"])
            for model, row in zip((Identity, Hints, Environment, Rendering), rows, strict=True)
        )
    except (ValueError, KeyError, TypeError):
        return {
            **report, "observation_status": "unavailable",
            **(workflow_failure or {"status": "failed", "reason": "invalid_observations"}),
        }
    if expected_dpr is None and device_preset and report["device_mode"] == "mobile_emulation":
        expected_dpr = DEVICE_VIEWPORTS[device_preset].scale
    comparison = compare_observations(
        identity, hints, environment, rendering,
        expected_timezone=expected_timezone, expected_language=expected_language, expected_dpr=expected_dpr,
        fixture_origin=fixture_origin,
    )
    checks = comparison["checks"]
    checks["quic_launch_flag"] = launch_check or "unavailable"
    report["fixture_http_protocols"] = {"document": rendering.document_protocol, "echo": rendering.echo_protocol}
    if (capabilities := public_capabilities(rendering.capabilities)) is not None:
        report["capabilities"] = capabilities
    if (connection_timings := public_connection_timings(rendering.connection_timings_ms)) is not None:
        report["connection_timings_ms"] = connection_timings
    observations = public_recorded_observations(_recorded_observations(identity, hints, environment, rendering))
    if observations is not None:
        report["recorded_observations"] = observations
    required = ["user_agent", "locale", "timezone_offset", "browser_clock", "fixture_http_transport", "quic_launch_flag"]
    if expected_timezone:
        required.append("timezone")
    if expected_language:
        required.append("expected_language")
    partial = (
        (preflight is not None and preflight.get("status") != "passed")
        or any(checks[name] == "unavailable" for name in required)
        or checks["ice"] in {"unavailable", "timeout", "inconclusive"}
    )
    first_mismatch = next((name for name, status in checks.items() if status == "mismatch"), None)
    report = {**report, **comparison, "status": "partial" if partial else "passed", "observation_status": "complete"}
    if first_mismatch is not None:
        report.update(status="failed", reason="check_mismatch", failed_check=first_mismatch)
    if workflow_failure is not None:
        report.update(workflow_failure)
    elif retain_observations:
        report["observations"] = {
            "identity": {**identity.model_dump(), "proxy_header_names": [
                name for name in PROXY_HEADER_NAMES if name in identity.proxy_header_names
            ]},
            "hints": hints.model_dump(),
            "environment": environment.model_dump(),
            "rendering": rendering.model_dump(exclude={
                "ice", "document_protocol", "echo_protocol", "capabilities", "connection_timings_ms",
            }),
        }
    return report


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", choices=("playwright", "cloakbrowser", "kameleo"), default="playwright")
    parser.add_argument("--persistent", action="store_true", help="Use an isolated temporary profile directory")
    parser.add_argument("--device-preset", choices=DEVICE_VIEWPORTS)
    parser.add_argument("--expected-timezone")
    parser.add_argument("--expected-language")
    parser.add_argument("--decodo", action="store_true", help="Use the configured owned HTTPS fixture through Decodo")
    parser.add_argument("--redacted", action="store_true", help="Omit detailed browser and server observations")
    parser.add_argument("--rotating", action="store_true", help="Use rotating Decodo mode instead of sticky")
    parser.add_argument("--allowed-asn", type=int, action="append", default=[])
    parser.add_argument("--max-latency-ms", type=int)
    args = parser.parse_args()
    if not args.decodo and (args.rotating or args.allowed_asn or args.max_latency_ms is not None):
        parser.error("Proxy settings require --decodo")
    if len(args.allowed_asn) > 256 or any(not 1 <= asn <= 4294967295 for asn in args.allowed_asn):
        parser.error("Supply up to 256 ASNs between 1 and 4294967295")
    if args.max_latency_ms is not None and args.max_latency_ms < 1:
        parser.error("Maximum latency must be positive")
    proxy = None
    if args.decodo:
        proxy = {
            "provider": "decodo", "product": "residential", "host": DECODO_GATEWAY, "port": DECODO_PORT,
            "country": "US", "username_ref": "DECODO_USERNAME", "password_ref": "DECODO_PASSWORD",
            # Chromium may keep the document tunnel open while fetching the credential-free echo.
            "provider_resolved_ack": True, "max_connections": 2,
        }
        if not args.rotating:
            proxy.update(session_key=secrets.token_hex(8), ttl_minutes=5)
    try:
        public_origin = _configured_public_origin() if args.decodo else None
        if args.decodo and not public_origin:
            parser.error("Decodo diagnostics require an owned HTTPS fixture in ABRL_DIAGNOSTIC_URL")
        with TemporaryDirectory(prefix="abrl-diagnostics-") as work:
            report = asyncio.run(run_browser_diagnostics(
                origin=public_origin or FIXTURE_ORIGIN,
                adapter=args.adapter, proxy=proxy, profile_dir=work if args.persistent else None,
                device_preset=args.device_preset, expected_timezone=args.expected_timezone,
                expected_language=args.expected_language,
                retain_observations=not args.redacted,
                preflight_policy={"allowed_asns": args.allowed_asn, "max_latency_ms": args.max_latency_ms},
            ))
    except ValueError as exc:
        parser.error(str(exc))
    except ZoneInfoNotFoundError:
        parser.error("Expected timezone must be an installed IANA timezone")
    print(json.dumps({"observed_at": datetime.now(UTC).isoformat(), **report}, indent=2))
    return {"passed": 0, "failed": 1, "partial": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(_main())
