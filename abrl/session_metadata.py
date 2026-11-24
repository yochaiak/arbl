"""Redact adapter internals before retaining or publishing session data."""

import re

from abrl.diagnostic_bundle_lifecycle import public_lifecycle_failure

_KEY_SEPARATORS = str.maketrans("", "", "_.- ")
_PRIVATE_FIELDS = frozenset({
    "authorization", "authorizationcode", "backendws", "browserws", "cdpendpoint", "cookie", "cookies", "correlation",
    "credential", "credentials", "headers", "jpegbase64", "kameleoprofileid", "licensekey", "passwordref",
    "proxyauth", "proxyauthorization", "requestedauthority", "requestheaders", "responseheaders", "secretref",
    "sessionkey", "setcookie", "upstreamauthority", "username", "usernameref", "vendorprofileid", "viewerframe",
    "websocketdebuggerurl",
})
_PRIVATE_SUFFIXES = ("apikey", "credentials", "password", "privatekey", "secret", "secretkey", "secrets", "token")
_PRIVATE_TEXT = re.compile(
    r"\bwss?://|\bhttps?://[^/\s?#]+@|(?:^|\bauthorization\s*:\s*)(?:Bearer|Basic)\s+\S+"
    r"|[?&](?:access[_-]?token|api[_-]?key|authorization|client[_-]?secret|password|signature|token)=[^\s&]+",
    re.IGNORECASE,
)


def _private_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.lower().translate(_KEY_SEPARATORS)
    return normalized in _PRIVATE_FIELDS or normalized.endswith(_PRIVATE_SUFFIXES)


def redact_session_text(value: str | None) -> str | None:
    return "[redacted]" if isinstance(value, str) and _PRIVATE_TEXT.search(value) else value


def _redact(value):
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items() if not _private_key(key)}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return redact_session_text(value) if isinstance(value, str) else value


def redact_session_metadata(value: object) -> dict:
    return _redact(value) if isinstance(value, dict) else {}


def public_session_event_detail(kind: str, detail: object) -> dict:
    if not isinstance(detail, dict):
        return {}
    if kind == "proxy_transport_issue":
        # Older workers published gateway addresses on these events.
        detail = {key: detail[key] for key in ("reason", "attempt", "upstream_status") if key in detail}
    if kind == "operator_step_reserved":
        step = public_lifecycle_failure(detail) or {}
        return {key: step[key] for key in ("step", "action", "step_path") if step.get(key) is not None}
    if kind == "launched" and "viewport_conformance" in detail:
        detail = {**detail, "viewport_conformance": public_viewport_conformance(detail["viewport_conformance"])}
    return redact_session_metadata(detail)


def public_viewport_conformance(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    checked, matched = value.get("checks"), value.get("matched")
    if type(checked) is int and type(matched) is int and 0 <= matched <= checked <= 2**53 - 1:
        return {"checks": checked, "matched": matched}
    return None


def current_viewport_conformance(metadata: object, epoch: int) -> dict[str, int] | None:
    if not isinstance(metadata, dict):
        return None
    # Launch can confirm the initial viewport before the finished attempt replaces the diagnostics.
    recorded_epoch = metadata.get("viewport_conformance_epoch", metadata.get("environment_epoch"))
    if type(recorded_epoch) is int and recorded_epoch == epoch:
        return public_viewport_conformance(metadata.get("viewport_conformance"))
    return None
