"""Observational rollups of completed live browser sessions in a single batch."""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from math import isfinite

from sqlalchemy import and_, select

from abrl.platform_models import BrowserSession, ProxyInventory, ProxyLease, SessionBatch
from abrl.platform_service import TERMINAL
from abrl.proxy_preflight import LOOKUP_DATASET_STATUSES

_TRANSPORT_TIMEOUTS = ("proxy_gateway_timeout", "proxy_tunnel_timeout")
_PREFLIGHT_TIMEOUTS = (
    *_TRANSPORT_TIMEOUTS, "preflight_timeout", "broker_connect_timeout", "proxy_connect_timeout",
    "origin_tls_timeout", "lookup_response_timeout",
)
_DATASET_FIELDS = ("country", "asn", "network_type", "timezone")
_LOOKUP_FIELDS = ("exit_ip", *_DATASET_FIELDS)
_BROWSER_CHECKS = (
    "user_agent", "chromium_major", "client_hints_full_versions", "user_agent_platform", "client_hints_platform",
    "client_hints_platform_version", "client_hints_mobile", "browser_automation", "locale", "expected_language",
    "timezone", "timezone_offset", "browser_clock", "viewport", "device_pixel_ratio", "webgl",
    "fixture_http_transport", "quic_launch_flag", "ice",
)


@dataclass
class _Metrics:
    finished: int = 0
    evaluated: int = 0
    challenged: int = 0
    transport_successes: int = 0
    transport_errors: int = 0
    latency_samples: int = 0
    latency_total: float = 0.0
    unevaluated: Counter[str] = field(default_factory=Counter)

    def record(self, challenged: bool | None, reason: str | None, transport: object) -> None:
        self.finished += 1
        self.evaluated += int(challenged is not None)
        self.challenged += int(challenged is True)
        if reason is not None:
            self.unevaluated[reason] += 1
        if not isinstance(transport, dict):
            return
        successes = transport.get("successes")
        errors = transport.get("failures")
        if type(successes) is not int or successes < 0 or type(errors) is not int or errors < 0:
            return
        self.transport_successes += successes
        self.transport_errors += errors
        latency = transport.get("latency_ms")
        if type(latency) in (int, float) and isfinite(latency) and latency >= 0:
            self.latency_samples += successes
            self.latency_total += latency * successes

    def view(self) -> dict:
        transport_attempts = self.transport_successes + self.transport_errors
        return {
            "finished_sessions": self.finished,
            "evaluated_sessions": self.evaluated,
            "unevaluated_sessions": self.finished - self.evaluated,
            "unevaluated_reasons": dict(sorted(self.unevaluated.items())),
            "challenged_sessions": self.challenged,
            "challenge_rate": self.challenged / self.evaluated if self.evaluated else None,
            "transport_attempts": transport_attempts,
            "transport_errors": self.transport_errors,
            "transport_error_rate": self.transport_errors / transport_attempts if transport_attempts else None,
            "mean_transport_latency_ms": self.latency_total / self.latency_samples if self.latency_samples else None,
            "transport_latency_samples": self.latency_samples,
        }


def _challenge_evidence(row) -> tuple[bool | None, str | None]:
    if row.evidence_epoch is None:
        return None, "missing_finalized_evidence"
    if row.evidence_epoch != row.epoch:
        return None, "stale_attempt_evidence"
    if row.failure_reason == "site_challenge":
        return True, None
    if row.workflow_outcome == "completed":
        if row.document_observed is True:
            return False, None
        return None, "completed_without_document_evidence"
    reasons = (row.failure_reason, row.step_failure_reason)
    if "site_denial" in reasons:
        return None, "site_denial"
    if row.preflight_status == "blocked":
        return None, "preflight_blocked"
    if any(reason in _TRANSPORT_TIMEOUTS for reason in reasons):
        return None, "transport_timeout"
    if row.status == "cancelled":
        return None, "cancelled"
    if row.workflow_outcome == "not_started":
        return None, "workflow_not_started"
    if any(reason in ("timeout", "selector_timeout", "navigation_timeout", "launch_timeout") for reason in reasons):
        return None, "workflow_timeout"
    return None, "workflow_incomplete"


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _preflight_timed_out(reason: object, checks: dict, probes: dict) -> bool:
    if reason in _PREFLIGHT_TIMEOUTS or any(
        _object(checks.get(name)).get("reason") in _PREFLIGHT_TIMEOUTS
        for name in (*_LOOKUP_FIELDS, "sticky", "target_tls")
    ):
        return True
    return any(
        _object(attempt).get("reason") in _PREFLIGHT_TIMEOUTS
        for name in ("lookup", "sticky_lookup", "target_tls")
        if isinstance(attempts := probes.get(name), list)
        for attempt in attempts
    )


@dataclass
class _DiagnosticEvidence:
    preflight_reports: int = 0
    preflight_timeout_sessions: int = 0
    missing_lookup_fields: Counter[str] = field(default_factory=Counter)
    missing_lookup_dataset_statuses: dict[str, Counter[str]] = field(
        default_factory=lambda: defaultdict(Counter)
    )
    browser_reports: int = 0
    browser_reports_without_checks: int = 0
    browser_unavailable_or_inconclusive_fields: Counter[str] = field(default_factory=Counter)

    def record(self, row) -> None:
        if row.preflight_status in ("passed", "partial", "unavailable", "blocked", "cancelled"):
            self.preflight_reports += 1
            checks = _object(row.preflight_checks)
            self.preflight_timeout_sessions += int(
                _preflight_timed_out(row.preflight_reason, checks, _object(row.preflight_probes))
            )
            for name in _LOOKUP_FIELDS:
                check = _object(checks.get(name))
                valid_statuses = ("passed",) if name == "exit_ip" else ("passed", "failed")
                if check.get("status") not in valid_statuses or check.get("confirmed") is False:
                    self.missing_lookup_fields[name] += 1
                    status = check.get("dataset_status")
                    if name in _DATASET_FIELDS and isinstance(status, str) and status in LOOKUP_DATASET_STATUSES:
                        self.missing_lookup_dataset_statuses[name][status] += 1
        if row.browser_status in ("passed", "partial", "failed"):
            self.browser_reports += 1
            checks = _object(row.browser_checks)
            if not any(name in checks for name in _BROWSER_CHECKS):
                self.browser_reports_without_checks += 1
            for name in _BROWSER_CHECKS:
                if checks.get(name) in ("unavailable", "timeout", "inconclusive"):
                    self.browser_unavailable_or_inconclusive_fields[name] += 1

    def view(self) -> dict:
        return {
            "preflight_reports": self.preflight_reports,
            "preflight_timeout_sessions": self.preflight_timeout_sessions,
            "missing_lookup_fields": dict(sorted(self.missing_lookup_fields.items())),
            "missing_lookup_dataset_statuses": {
                name: dict(sorted(counts.items())) for name, counts in sorted(self.missing_lookup_dataset_statuses.items())
            },
            "browser_reports": self.browser_reports,
            "browser_reports_without_checks": self.browser_reports_without_checks,
            "browser_unavailable_or_inconclusive_fields": dict(
                sorted(self.browser_unavailable_or_inconclusive_fields.items())
            ),
        }


def _confirmed_value(check: object) -> object:
    if isinstance(check, dict) and check.get("status") == "passed" and check.get("confirmed") is True:
        return check.get("observed")
    return None


def _egress_dimensions(sticky_status: str | None, asn: object, country: object) -> tuple[int | None, str | None]:
    if sticky_status != "passed":
        return None, None
    observed_asn = _confirmed_value(asn)
    observed_country = _confirmed_value(country)
    return (
        observed_asn if type(observed_asn) is int and 1 <= observed_asn <= 4_294_967_295 else None,
        observed_country if isinstance(observed_country, str) and re.fullmatch(r"[A-Z]{2}", observed_country) else None,
    )


def batch_challenge_summary(db, batch: SessionBatch) -> dict:
    metadata = BrowserSession.metadata_json
    preflight = metadata["proxy_preflight"]
    diagnostic = metadata["browser_diagnostic"]
    query = (
        select(
            ProxyInventory.id.label("pool"),
            BrowserSession.status,
            BrowserSession.workflow_outcome,
            BrowserSession.epoch,
            metadata["environment_epoch"].as_integer().label("evidence_epoch"),
            metadata["causal_failure"]["reason"].as_string().label("failure_reason"),
            metadata["step_failure"]["reason"].as_string().label("step_failure_reason"),
            metadata["authorized_document_observed"].as_boolean().label("document_observed"),
            preflight["status"].as_string().label("preflight_status"),
            preflight["reason"].as_string().label("preflight_reason"),
            preflight["checks"].label("preflight_checks"),
            preflight["probe_attempts"].label("preflight_probes"),
            diagnostic["status"].as_string().label("browser_status"),
            diagnostic["checks"].label("browser_checks"),
            metadata["transport_summary"].label("transport"),
        )
        .select_from(BrowserSession)
        .outerjoin(
            ProxyLease,
            and_(
                ProxyLease.id == BrowserSession.proxy_lease_id,
                ProxyLease.session_id == BrowserSession.id,
                ProxyLease.epoch == BrowserSession.epoch,
            ),
        )
        .outerjoin(
            ProxyInventory,
            and_(ProxyInventory.id == ProxyLease.inventory_id, ProxyInventory.project_id == batch.project_id),
        )
        .where(
            BrowserSession.batch_id == batch.id,
            BrowserSession.project_id == batch.project_id,
            BrowserSession.mode == "live",
            BrowserSession.status.in_(TERMINAL),
            BrowserSession.finished_at.is_not(None),
        )
    )
    overall = _Metrics()
    diagnostics = _DiagnosticEvidence()
    dimensions = {name: defaultdict(_Metrics) for name in ("pool", "asn", "country")}
    for row in db.execute(query):
        challenged, reason = _challenge_evidence(row)
        asn, country, transport = None, None, None
        if row.evidence_epoch == row.epoch:
            diagnostics.record(row)
            if row.pool:
                checks = _object(row.preflight_checks)
                asn, country = _egress_dimensions(
                    _object(checks.get("sticky")).get("status"), checks.get("asn"), checks.get("country"),
                )
            transport = row.transport
        overall.record(challenged, reason, transport)
        for dimension, value in (("pool", row.pool), ("asn", asn), ("country", country)):
            dimensions[dimension][value].record(challenged, reason, transport)
    return {
        "overall": overall.view(),
        "diagnostics": diagnostics.view(),
        **{
            f"by_{name}": [
                {"value": value, **groups[value].view()}
                for value in sorted(groups, key=lambda key: (key is None, key))
            ]
            for name, groups in dimensions.items()
        },
    }
