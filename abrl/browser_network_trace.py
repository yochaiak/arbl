"""Bounded browser network diagnostics."""

import re
from collections.abc import Callable
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Request, Response

from .config import ADSCORE_ENGINE_ORIGINS
from .lifecycle import public_duration_ms
from .network import FIXTURE_PATHS, NetworkDenied, request_origin

MAX_REQUESTS = 128
MAX_POLICY_BLOCKS = 32
PAGE_ASSET_TYPES = frozenset({"stylesheet", "script", "image", "font", "media", "manifest", "texttrack"})
SUBRESOURCE_TYPES = PAGE_ASSET_TYPES | {"fetch", "xhr", "eventsource", "ping", "preflight", "other"}
SUBRESOURCE_FAILURE_REASONS = frozenset({"aborted", "blocked", "http_4xx", "http_5xx", "network_error"})
_MAX_SAFE_INTEGER = 2**53 - 1  # Session Details reads these counts as JavaScript numbers.
_MAX_HTTP_STATUS_COUNT = 100_000
_HTTP_STATUS_GROUPS = ("main_frame", "subresource")
TUNNEL_COUNT_FIELDS = ("attempts", "successes", "failures", "capacity_rejections")
_MAX_ADSCORE_CONTEXTS = 32
_MAX_EARLY_HEADER_EVENTS = 128
_INITIATOR_TYPES = frozenset({"parser", "script", "preload", "SignedExchange", "preflight", "FedCM", "EmailVerification", "other"})
_MEASUREMENT_RESOURCE_TYPES = frozenset({"Fetch", "XHR", "Ping", "Other"})
_FIXTURE_PATHS = FIXTURE_PATHS | {"/lab/entry"}
_ADSCORE_HOSTS = {urlsplit(origin).hostname for origin in ADSCORE_ENGINE_ORIGINS}
_ADSCORE_PING_HOST = re.compile(r"(?:[^.]+\.)+(l4|n4|s4)\.adsco\.re\Z")
_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}


def public_http_status_counts(value: object) -> dict | None:
    """Keep bounded response counts and distinguish missing evidence from an empty observation."""
    if not isinstance(value, dict) or not all(isinstance(value.get(group), dict) for group in _HTTP_STATUS_GROUPS):
        return None
    return {
        group: {
            status: count for status in map(str, range(100, 600))
            if type(count := value[group].get(status)) is int and 0 < count <= _MAX_HTTP_STATUS_COUNT
        }
        for group in _HTTP_STATUS_GROUPS
    }


def public_transport_summary(value: object) -> dict | None:
    """Keep measured tunnel counters and latency without any destination or connection details."""
    if not isinstance(value, dict):
        return None
    summary = {
        key: count for key in TUNNEL_COUNT_FIELDS
        if type(count := value.get(key)) is int and 0 <= count <= _MAX_SAFE_INTEGER
    }
    if (latency := public_duration_ms(value.get("latency_ms"))) is not None:
        summary["latency_ms"] = latency
    return summary or None


def public_transport_checks(metadata: dict) -> dict:
    """Project transport observations onto the fields allowed in reports and session storage."""
    result = {}
    if "quic_launch_flag" in metadata:
        flag = metadata["quic_launch_flag"]
        result["quic_launch_flag"] = flag if flag in ("passed", "mismatch") else "unavailable"
    if "main_origin_subresource_failures" not in metadata:
        return result
    counts = metadata["main_origin_subresource_failures"]
    if not isinstance(counts, dict):
        result["main_origin_subresource_failures"] = None
        return result
    safe = {}
    for kind in SUBRESOURCE_TYPES:
        row = counts.get(kind)
        if not isinstance(row, dict):
            continue
        totals = {}
        for reason in SUBRESOURCE_FAILURE_REASONS:
            count = row.get(reason)
            if type(count) is int and 0 < count <= _MAX_SAFE_INTEGER:
                totals[reason] = count
        if totals:
            safe[kind] = totals
    result["main_origin_subresource_failures"] = safe
    return result


def _observed_origin(value: object) -> str | None:
    if isinstance(value, str):
        try:
            return request_origin(value)
        except NetworkDenied:
            pass
    return None


class AdScoreInitiatorTrace:
    """Correlate reduced CDP evidence by request ID without retaining headers or stacks."""

    def __init__(
        self, metadata: dict, registered: set[str], endpoint: Callable[[str], str | None], main_frame_id: str | None
    ) -> None:
        self.metadata = metadata
        self.registered = registered
        self.endpoint = endpoint
        self.main_frame_id = main_frame_id
        self.rows: dict[str, dict] = {}
        self.early: dict[str, dict[str, bool]] = {}

    def _registered_endpoint(self, request: dict) -> str | None:
        url = request.get("url")
        if request.get("method") == "POST" and isinstance(url, str) and _observed_origin(url) in self.registered:
            return self.endpoint(url)
        return None

    def requested(self, event: dict) -> None:
        request_id = event["requestId"]
        early = self.early.pop(request_id, {})
        if "redirectResponse" in event:
            # Network IDs span redirect hops. Later headers may already refer to a different URL.
            if (row := self.rows.get(request_id)) is not None:
                row["redirected"] = True
                row.pop("paused_referrer_present", None)
                row.pop("network_referrer_present", None)
                row.pop("status", None)
            return
        request = event["request"]
        endpoint = self._registered_endpoint(request)
        if endpoint is None or request_id in self.rows or len(self.rows) >= _MAX_ADSCORE_CONTEXTS:
            return
        row = {"url": endpoint, **early}
        if isinstance(request.get("headers"), dict):
            row["initial_referrer_present"] = any(name.lower() == "referer" for name in request["headers"])
        if event.get("type") in _MEASUREMENT_RESOURCE_TYPES:
            row["resource_type"] = event["type"].lower()
        if document_origin := _observed_origin(event.get("documentURL")):
            row["document_origin"] = document_origin
        if event.get("loaderId") == "":
            row["context"] = "worker"
        elif event.get("frameId") and self.main_frame_id:
            row["context"] = "main_frame" if event["frameId"] == self.main_frame_id else "subframe"
        initiator = event.get("initiator", {})
        if initiator.get("type") in _INITIATOR_TYPES:
            row["initiator_type"] = initiator["type"]
        if initiator.get("type") == "script":
            frames = (initiator.get("stack") or {}).get("callFrames", ())
            initiator_url = frames[0].get("url") if frames else initiator.get("url")
        else:
            initiator_url = initiator.get("url")
        if initiator_origin := _observed_origin(initiator_url):
            row["initiator_origin"] = initiator_origin
        self.rows[request_id] = row
        self.metadata.setdefault("adscore_request_initiators", []).append(row)

    def _header_stage(self, request_id: str, headers: object, field: str) -> None:
        if not isinstance(headers, dict):
            return
        present = any(name.lower() == "referer" for name in headers)
        if (row := self.rows.get(request_id)) is not None:
            if not row.get("redirected"):
                row[field] = present
            return
        if request_id not in self.early and len(self.early) >= _MAX_EARLY_HEADER_EVENTS:
            self.early.pop(next(iter(self.early)))
        self.early.setdefault(request_id, {})[field] = present

    def paused(self, event: dict) -> None:
        request = event["request"]
        if event.get("networkId") and not event.get("redirectedRequestId") and self._registered_endpoint(request):
            self._header_stage(event["networkId"], request.get("headers"), "paused_referrer_present")

    def network_headers(self, event: dict) -> None:
        self._header_stage(event["requestId"], event.get("headers"), "network_referrer_present")

    def responded(self, event: dict) -> None:
        if (row := self.rows.get(event["requestId"])) is not None and not row.get("redirected"):
            status = event["response"].get("status")
            if type(status) is int and 100 <= status < 600:
                row["status"] = status


class NavigationSourceTrace:
    """Retain enough origin-level evidence to explain a denied document request."""

    def __init__(self) -> None:
        self.requests: dict[tuple[str, str], dict] = {}

    def requested(self, event: dict) -> None:
        if event.get("type") != "Document" or not (origin := _observed_origin(event["request"].get("url"))):
            return
        detail = {"origin": origin, "frame_id": event.get("frameId")}
        redirect = event.get("redirectResponse")
        if redirect and (source := _observed_origin(redirect.get("url"))):
            detail.update(source_origin=source, trigger="http_redirect")
            if type(redirect.get("status")) is int:
                detail["redirect_status"] = redirect["status"]
        else:
            initiator = event.get("initiator", {})
            if initiator.get("type") in {"script", "parser"}:
                detail["trigger"] = initiator["type"]
                frames = (initiator.get("stack") or {}).get("callFrames", ())
                source = _observed_origin(frames[0].get("url") if frames else initiator.get("url"))
                if source:
                    detail["source_origin"] = source
        key = (event["requestId"], origin)
        if key not in self.requests and len(self.requests) >= MAX_POLICY_BLOCKS:
            self.requests.pop(next(iter(self.requests)))
        self.requests[key] = detail

    def blocked(self, url: str, *, network_id=None, source_url=None, main_frame_id=None) -> dict | None:
        origin = _observed_origin(url)
        if origin is None:
            return None
        detail = self.requests.get((network_id, origin)) if network_id else next(
            (row for row in reversed(self.requests.values()) if row["origin"] == origin), None,
        )
        result = dict(detail) if detail else {"origin": origin}
        frame_id = result.pop("frame_id", None)
        if frame_id and main_frame_id:
            result["frame"] = "top_level" if frame_id == main_frame_id else "embedded"
        if "source_origin" not in result and (source := _observed_origin(source_url)):
            result["source_origin"] = source
        return result


def _diagnostic_url(url: str, target_origin: str) -> str | None:
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if (
        parts.scheme not in {"http", "https", "ws", "wss"}
        or not host
        or not re.fullmatch(r"[a-z0-9.:-]{1,253}", host)
    ):
        return None
    authority = f"[{host}]" if ":" in host else host
    if ping_host := _ADSCORE_PING_HOST.fullmatch(host):
        authority = f"*.{ping_host[1]}.adsco.re"
    if port is not None and port != (443 if parts.scheme in {"https", "wss"} else 80):
        authority += f":{port}"
    origin = f"{parts.scheme}://{authority}"
    path = parts.path or "/"
    if (
        path not in {"/", "/favicon.ico"}
        and (origin != target_origin or path not in _FIXTURE_PATHS)
        and (host not in _ADSCORE_HOSTS or path not in {"/engine", "/p"})
    ):
        path = "/[redacted]"
    return origin + path


class InstrumentedNetworkTrace:
    def __init__(self, origin: str, position: Callable[[], tuple[str, int]]) -> None:
        self.origin = origin
        self.position = position
        self.requests: list[dict] = []
        self.blocks: list[dict] = []
        self.data = {
            "requests": self.requests, "policy_blocks": self.blocks, "omitted_requests": 0, "omitted_blocks": 0,
        }

    def _entry(self, url: str, method: str, resource_type: str) -> dict | None:
        endpoint = _diagnostic_url(url, self.origin)
        if endpoint is None:
            return None
        phase, step = self.position()
        return {
            "url": endpoint,
            "method": method if method in _METHODS else "OTHER",
            "resource_type": resource_type.lower() if re.fullmatch(r"[a-zA-Z]{1,24}", resource_type) else "other",
            "phase": phase,
            "step": step,
        }

    def blocked(self, url: str, method: str, resource_type: str, reason: str) -> None:
        entry = self._entry(url, method, resource_type)
        if entry is None:
            return
        entry["reason"] = reason
        if entry in self.blocks:
            return
        if len(self.blocks) >= MAX_POLICY_BLOCKS:
            self.data["omitted_blocks"] += 1
            return
        self.blocks.append(entry)

    def attach(self, page: Page) -> None:
        by_request: dict[Request, dict] = {}

        def requested(request: Request) -> None:
            entry = self._entry(request.url, request.method, request.resource_type)
            if entry is None:
                return
            if len(self.requests) >= MAX_REQUESTS:
                self.data["omitted_requests"] += 1
                return
            entry["id"] = len(self.requests) + 1
            if request.is_navigation_request():
                try:
                    if request.frame == page.main_frame:
                        entry["top_level"] = True
                except PlaywrightError:
                    pass  # The frame can be unavailable before a navigation commits.
            previous = by_request.get(request.redirected_from)
            if previous is not None:
                entry["redirected_from"] = previous["id"]
            self.requests.append(entry)
            by_request[request] = entry

        def responded(response: Response) -> None:
            if (entry := by_request.get(response.request)) is not None:
                entry["status"] = response.status

        def failed(request: Request) -> None:
            if (entry := by_request.get(request)) is not None:
                error = request.failure or ""
                entry["error"] = error if re.fullmatch(r"net::ERR_[A-Z0-9_]{1,60}", error) else "request_failed"

        page.on("request", requested)
        page.on("response", responded)
        page.on("requestfailed", failed)
