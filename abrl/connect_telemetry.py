"""Correlate redacted CONNECT results with main-frame navigation windows."""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from weakref import WeakKeyDictionary

from .network import PROXY_TRANSPORT_REASONS

MAX_NAVIGATIONS = 64
MAX_CONNECT_RESULTS = 8
_MAX_PENDING_REQUESTS = 256
_MAX_EARLY_CONNECTS = 64
_EARLY_CONNECT_SECONDS = 0.5
_OUTCOMES = ("committed", "failed", "redirected", "incomplete")
_LOCAL_REASONS = frozenset({
    "connect_established", "local_scope_denied", "local_dns_denied", "target_dns_unavailable",
    "local_capacity", "local_forwarding_disabled", "local_budget_exhausted", "local_invalid_request",
    "local_direct_connect_failed",
})
_UPSTREAM_REASONS = frozenset((*PROXY_TRANSPORT_REASONS, "authentication", "quota"))
_PROVIDER_REASONS = _UPSTREAM_REASONS | {"connect_established"}


def _public_connect(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    source, reason, status = (value.get(key) for key in ("source", "reason", "http_status"))
    if not isinstance(source, str) or not isinstance(reason, str):
        return None
    status = status if type(status) is int and 100 <= status <= 599 else None
    if reason == "connect_established" and status != 200:
        return None
    if source == "local_proxy":
        allowed = reason in _LOCAL_REASONS
    elif source == "upstream_connection":
        allowed = reason in _UPSTREAM_REASONS
        status = None
    else:
        allowed = source in ("decodo", "upstream_proxy") and status is not None and reason in _PROVIDER_REASONS
    if not allowed:
        return None
    return {"source": source, "reason": reason, **({"http_status": status} if status is not None else {})}


def public_navigation_connects(value: object) -> dict | None:
    """Keep only fixed codes and bounded numeric evidence from a navigation trace."""
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        return None
    rows = value.get("navigations")
    if not isinstance(rows, list):
        return None
    navigations = []
    seen = set()
    truncated = value.get("truncated") is True or len(rows) > MAX_NAVIGATIONS
    for row in rows[:MAX_NAVIGATIONS]:
        if not isinstance(row, dict):
            continue
        number, step, outcome = (row.get(key) for key in ("navigation", "step", "outcome"))
        if type(number) is not int or not 1 <= number <= MAX_NAVIGATIONS or number in seen:
            continue
        if outcome not in _OUTCOMES or type(row.get("redirected")) is not bool:
            continue
        connects = row.get("connects")
        if not isinstance(connects, list):
            continue
        overflow = len(connects) > MAX_CONNECT_RESULTS
        truncated |= overflow
        connects = [
            result for event in connects[:MAX_CONNECT_RESULTS] if (result := _public_connect(event)) is not None
        ]
        seen.add(number)
        attribution = (
            "unobserved" if not connects else
            "ambiguous" if overflow or row.get("attribution") == "ambiguous" or len(connects) > 1 else "observed"
        )
        navigations.append({
            "navigation": number,
            **({"step": step} if type(step) is int and 1 <= step <= 100_000 else {}),
            "redirected": row["redirected"],
            "outcome": outcome,
            "attribution": attribution,
            "connects": connects,
        })
    return {"version": 1, "navigations": navigations, "truncated": truncated}


@dataclass
class _Navigation:
    number: int
    step: int | None
    redirected: bool
    outcome: str = "incomplete"
    ambiguous: bool = False
    connects: list[dict] = field(default_factory=list)


@dataclass
class _EarlyConnect:
    origin: str
    started: float
    ambiguous: bool
    navigation: _Navigation | None = None
    result: dict | None = None


class NavigationConnectTrace:
    """Join by origin and overlap. Chromium can share a CONNECT among several requests."""

    def __init__(self) -> None:
        self._active: WeakKeyDictionary = WeakKeyDictionary()
        self._by_request: WeakKeyDictionary = WeakKeyDictionary()
        self._navigations: list[_Navigation] = []
        self._early: deque[_EarlyConnect] = deque(maxlen=_MAX_EARLY_CONNECTS)
        self._truncated = False
        self._pending_limit_reached = False

    def request(
        self, request: object, origin: str | None, *, navigation: bool, step: int,
        redirected_from: object | None = None,
    ) -> None:
        if redirected_from is not None:
            self.finish(redirected_from, "redirected")
        overlapping = self._active_at(origin) if origin is not None else []
        for pending in overlapping:
            if (other := self._by_request.get(pending)) is not None:
                other.ambiguous = True
        if len(self._active) >= _MAX_PENDING_REQUESTS:
            self._pending_limit_reached = self._truncated = True
        elif origin is not None:
            self._active[request] = origin
        if not navigation:
            for early in self._early:
                if early.origin == origin:
                    early.ambiguous = True
            return
        if len(self._navigations) >= MAX_NAVIGATIONS:
            self._truncated = True
            return
        row = _Navigation(
            len(self._navigations) + 1, step if step > 0 else None, redirected_from is not None,
            ambiguous=bool(overlapping) or self._pending_limit_reached,
        )
        self._navigations.append(row)
        self._by_request[request] = row
        self._expire_early(monotonic())
        for early in tuple(self._early):
            if early.origin != origin:
                continue
            self._early.remove(early)
            early.navigation = row
            row.ambiguous |= early.ambiguous
            if early.result is not None:
                self._append_result(row, early.result)

    def finish(self, request: object, outcome: str | None = None) -> None:
        self._active.pop(request, None)
        if outcome is not None and (row := self._by_request.get(request)) is not None:
            if row.outcome == "incomplete":
                row.outcome = outcome

    def _active_at(self, origin: str) -> list[object]:
        return [request for request, active_origin in self._active.items() if active_origin == origin]

    def _expire_early(self, now: float) -> None:
        while self._early and now - self._early[0].started > _EARLY_CONNECT_SECONDS:
            self._early.popleft()

    def _append_result(self, row: _Navigation, result: dict) -> None:
        if len(row.connects) < MAX_CONNECT_RESULTS:
            row.connects.append(result)
        else:
            row.ambiguous = self._truncated = True

    def observe_connect(self, origin: str) -> Callable[[dict], None]:
        same_origin = self._active_at(origin)
        candidates = [self._by_request[request] for request in same_origin if request in self._by_request]
        if not candidates:
            # Chromium can open a tunnel before Playwright delivers its request event.
            now = monotonic()
            self._expire_early(now)
            if len(self._early) == _MAX_EARLY_CONNECTS:
                self._truncated = True
            early = _EarlyConnect(origin, now, bool(same_origin) or self._pending_limit_reached)
            self._early.append(early)

            def completed_early(event: dict) -> None:
                early.result = _public_connect(event)
                if early.navigation is not None and early.result is not None:
                    self._append_result(early.navigation, early.result)

            return completed_early
        ambiguous = len(same_origin) != 1 or self._pending_limit_reached

        def completed(event: dict) -> None:
            if (result := _public_connect(event)) is None:
                return
            for row in candidates:
                row.ambiguous |= ambiguous
                self._append_result(row, result)

        return completed

    def export(self) -> dict | None:
        if not self._navigations:
            return None
        return public_navigation_connects({
            "version": 1,
            "truncated": self._truncated,
            "navigations": [
                {
                    "navigation": row.number, "step": row.step, "redirected": row.redirected,
                    "outcome": row.outcome, "connects": row.connects,
                    "attribution": "ambiguous" if row.ambiguous else "unobserved",
                }
                for row in self._navigations
            ],
        })
