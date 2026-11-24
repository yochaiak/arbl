"""A bounded timeline of browser milestones with no request or input data."""

from dataclasses import dataclass, field
from time import monotonic_ns

LIFECYCLE_VERSION = 1
LIFECYCLE_EVENTS = (
    "preflight_completed",
    "browser_launched",
    "navigation_started",
    "document_committed",
    "dom_content_loaded",
    "first_subresource_completed",
    "last_subresource_completed_before_load",
    "page_loaded",
    "first_interaction",
    "first_post_load_step",
    "workflow_completed",
    "failure_observed",
)
MAX_LIFECYCLE_ELAPSED_MS = 172_800_000
_NAVIGATION_WAIT_STAGES = (
    "navigation_setup", "initial_dom_content_loaded", "post_navigation_challenge_check",
    "post_navigation_delay", "post_navigation_dom_content_loaded", "post_step_challenge_check", "url_condition",
)
_LOAD_TIMEOUT_SOURCES = ("step_override", "proxy_policy", "proxy_default", "direct_default")
_TUNNEL_WAIT_STAGES = ("target_dns", "gateway_dns", "gateway_tls_connect", "proxy_connect")


def public_duration_ms(value: object) -> int | float | None:
    if type(value) in (int, float) and 0 <= value <= MAX_LIFECYCLE_ELAPSED_MS:
        return round(value, 3)
    return None


def public_initial_navigation_timeout(value: object) -> dict | None:
    """Project the configured deadline for a dispatched navigation."""
    if not isinstance(value, dict):
        return None
    duration, source = value.get("load_timeout_ms"), value.get("load_timeout_source")
    if type(duration) is not int or not 100 <= duration <= 120_000:
        return None
    if not isinstance(source, str) or source not in _LOAD_TIMEOUT_SOURCES:
        return None
    return {"load_timeout_ms": duration, "load_timeout_source": source}


def public_navigation_wait(value: object) -> dict | None:
    """Keep the named wait, timeout attribution, and bounded timing values."""
    if not isinstance(value, dict):
        return None
    stage, deadline = value.get("stage"), value.get("deadline")
    if stage not in _NAVIGATION_WAIT_STAGES or deadline not in ("step", "workflow"):
        return None
    result = {"stage": stage, "deadline": deadline}
    for name, maximum in (
        ("stage_elapsed_ms", MAX_LIFECYCLE_ELAPSED_MS),
        ("load_timeout_ms", 120_000),
        ("post_navigation_wait_ms", 60_000),
    ):
        duration = value.get(name)
        if type(duration) is int and 0 <= duration <= maximum:
            result[name] = duration
    if (timeout := public_initial_navigation_timeout(value)) is not None:
        result.update(timeout)
    return result


def public_tunnel_wait(value: object) -> dict | None:
    """Keep only a named broker stage and its measured duration."""
    if not isinstance(value, dict) or value.get("stage") not in _TUNNEL_WAIT_STAGES:
        return None
    elapsed = value.get("stage_elapsed_ms")
    if type(elapsed) is not int or not 0 <= elapsed <= MAX_LIFECYCLE_ELAPSED_MS:
        return None
    return {"stage": value["stage"], "stage_elapsed_ms": elapsed}


def public_lifecycle_timeline(value: object) -> dict | None:
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != LIFECYCLE_VERSION:
        return None
    events = value.get("events")
    if not isinstance(events, list):
        return None
    elapsed = {}
    for row in events[:len(LIFECYCLE_EVENTS)]:
        if not isinstance(row, dict):
            continue
        event, elapsed_ms = row.get("event"), row.get("elapsed_ms")
        if event in LIFECYCLE_EVENTS and type(elapsed_ms) is int and 0 <= elapsed_ms <= MAX_LIFECYCLE_ELAPSED_MS:
            elapsed.setdefault(event, elapsed_ms)
    if not elapsed:
        return None
    navigation_start = elapsed.get("navigation_started")
    return {
        "version": LIFECYCLE_VERSION,
        "events": [
            {
                "event": event,
                "elapsed_ms": elapsed_ms,
                "navigation_delta_ms": None if navigation_start is None else elapsed_ms - navigation_start,
            }
            for event, elapsed_ms in sorted(elapsed.items(), key=lambda item: item[1])
        ],
    }


@dataclass
class LifecycleTimeline:
    """Use one monotonic clock from runner preflight through browser execution."""

    _started_ns: int = field(default_factory=lambda: monotonic_ns(), init=False)
    _marks: dict[str, int] = field(default_factory=dict, init=False)

    def has(self, event: str) -> bool:
        return event in self._marks

    def mark(self, event: str, *, latest: bool = False) -> None:
        if event in LIFECYCLE_EVENTS and (latest or event not in self._marks):
            self._marks[event] = monotonic_ns()

    def view(self) -> dict | None:
        return public_lifecycle_timeline({
            "version": LIFECYCLE_VERSION,
            "events": [
                {"event": event, "elapsed_ms": (recorded_ns - self._started_ns) // 1_000_000}
                for event, recorded_ns in sorted(self._marks.items(), key=lambda item: item[1])
            ],
        })
