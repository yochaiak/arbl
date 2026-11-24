"""Build a project-scoped export from sanitized diagnostic projections."""

import re
from collections.abc import Iterable, Mapping
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import String, cast, select

from abrl.batch_presets import preset_details
from abrl.diagnostic_bundle_lifecycle import bundle_lifecycle
from abrl.diagnostic_bundle_observations import bundle_browser, bundle_network
from abrl.platform_models import BrowserSession, ProxyLease, SessionAttempt, SessionBatch, SessionBatchItem
from abrl.proxy import FIXED_PRODUCTS
from abrl.session_metadata import current_viewport_conformance

_PINNED_DEPENDENCIES = {"target_id": str, "target_version": int, "workflow_id": str, "workflow_version": int}
_COMPARABLE_SETTINGS = (
    "device_preset", "fixture_params", "group_fixture_params", "owned_diagnostic", "preparation", "proxy_policy",
    "referrer", "timeout_seconds", "user_agent", "workflow_inputs",
)
Baseline = tuple[BrowserSession, str]


def _current_metadata(session: BrowserSession) -> dict:
    metadata = session.metadata_json
    if not isinstance(metadata, dict):
        return {}
    epoch = metadata.get("environment_epoch")
    if type(epoch) is not int or epoch != session.epoch:
        return {}
    return metadata


def _baseline_evidence(session: BrowserSession) -> str | None:
    if (
        session.mode != "live" or session.status != "completed"
        or session.outcome not in ("completed", None) or session.workflow_outcome not in ("completed", None)
    ):
        return None
    metadata = _current_metadata(session)
    if metadata.get("causal_failure") or metadata.get("step_failure"):
        return None
    recorded = session.metadata_json
    if not isinstance(recorded, dict) or "browser_diagnostic" not in recorded:
        return "workflow_completed"
    report = metadata.get("browser_diagnostic")
    if (
        not isinstance(report, dict)
        or any(
            key in report and (type(report[key]) is not int or report[key] != expected)
            for key, expected in (("epoch", session.epoch), ("attempt", session.attempt_number))
        )
        or report.get("observation_status") == "unavailable" or report.get("reason")
    ):
        return None
    checks = report.get("checks")
    if isinstance(checks, dict) and any(verdict in ("failed", "mismatch") for verdict in checks.values()):
        return None
    return report["status"] if report.get("status") in ("passed", "partial") else None


def _explicit_baseline(db, session: BrowserSession, reference: str) -> Baseline:
    reference = reference.lower()
    if not re.fullmatch(r"[0-9a-f]{8}[0-9a-f-]{0,28}", reference):
        raise HTTPException(422, "Enter a full baseline session ID or at least eight characters of its prefix.")
    matches = db.scalars(
        select(BrowserSession)
        .where(BrowserSession.project_id == session.project_id, BrowserSession.id.startswith(reference))
        .limit(2)
    ).all()
    if not matches:
        raise HTTPException(404, "Baseline session not found in this project.")
    if len(matches) != 1:
        raise HTTPException(409, "The baseline prefix matches multiple sessions. Enter more characters.")
    baseline = matches[0]
    evidence = _baseline_evidence(baseline)
    if baseline.id == session.id or evidence is None:
        raise HTTPException(409, "Choose a different completed live session with no recorded diagnostic failure.")
    return baseline, evidence


def _prefer_passing(candidates: Iterable[BrowserSession], fallback: Baseline | None = None) -> Baseline | None:
    for row in candidates:
        evidence = _baseline_evidence(row)
        if evidence == "passed":
            return row, evidence
        if evidence is not None and fallback is None:
            fallback = row, evidence
    return fallback


def _automatic_baseline(db, session: BrowserSession) -> Baseline | None:
    if session.mode != "live":
        return None
    launch_config = db.scalar(select(SessionBatchItem.launch_config).where(
        SessionBatchItem.batch_id == session.batch_id, SessionBatchItem.session_id == session.id,
    ))
    comparable = (
        select(BrowserSession, SessionBatchItem.launch_config)
        .outerjoin(SessionBatchItem, SessionBatchItem.session_id == BrowserSession.id)
        .where(
            BrowserSession.project_id == session.project_id,
            BrowserSession.id != session.id,
            BrowserSession.adapter == session.adapter,
            BrowserSession.persistence == session.persistence,
            BrowserSession.mode == "live",
            BrowserSession.status == "completed",
        )
        .order_by(BrowserSession.finished_at.desc().nullslast(), BrowserSession.id.desc())
        .execution_options(yield_per=32)
    )
    with db.execute(comparable.where(BrowserSession.batch_id == session.batch_id)) as candidates:
        same_batch = _prefer_passing(row for row, config in candidates if config == launch_config)
    if same_batch is not None and same_batch[1] == "passed":
        return same_batch

    return _baseline_from_matching_batch(db, session, launch_config, comparable, same_batch)


def _comparable_settings(config: Mapping[str, object], overrides: dict | None) -> tuple:
    overrides = overrides if isinstance(overrides, dict) else {}
    preset = preset_details(overrides.get("batch_preset"))
    if preset is not None:
        preset = {key: value for key, value in preset.items() if key not in {"mode", "seed"}}
    return (*(overrides.get(name, config.get(name)) for name in _COMPARABLE_SETTINGS), preset)


def _baseline_from_matching_batch(
    db, session: BrowserSession, launch_config: dict | None, comparable, fallback: Baseline | None,
) -> Baseline | None:
    config = db.scalar(select(SessionBatch.config).where(
        SessionBatch.id == session.batch_id, SessionBatch.project_id == session.project_id,
    ))
    if not isinstance(config, dict) or any(
        type(config.get(name)) is not kind for name, kind in _PINNED_DEPENDENCIES.items()
    ):
        return fallback
    dependencies = [
        cast(SessionBatch.config[name].as_string(), String) == str(config[name]) for name in _PINNED_DEPENDENCIES
    ]

    query = comparable.join(SessionBatch, SessionBatch.id == BrowserSession.batch_id).where(
        SessionBatch.project_id == session.project_id, SessionBatch.id != session.batch_id, *dependencies,
    ).add_columns(*(SessionBatch.config[name].label(name) for name in _COMPARABLE_SETTINGS))
    settings = _comparable_settings(config, launch_config)
    with db.execute(query) as candidates:
        return _prefer_passing((
            candidate[0] for candidate in candidates if _comparable_settings(candidate._mapping, candidate[1]) == settings
        ), fallback)


def _assigned_continuity(db, session: BrowserSession) -> str | None:
    if session.mode != "live" or session.attempt_number < 1:
        return None
    attempt_scope = (
        SessionAttempt.session_id == session.id,
        SessionAttempt.number == session.attempt_number,
        SessionAttempt.epoch == session.epoch,
    )
    if session.proxy_lease_id is None:
        attempt = db.scalar(select(SessionAttempt.number).where(
            *attempt_scope, SessionAttempt.proxy_lease_id.is_(None),
        ))
        return "direct" if attempt is not None else None
    product, strict, has_key = (
        ProxyLease.config["product"].as_string(),
        ProxyLease.config["strict_affinity"].as_boolean(),
        ProxyLease.config["session_key"].as_string() != "",
    )
    lease = db.execute(
        select(product, strict, has_key)
        .join(SessionAttempt, SessionAttempt.proxy_lease_id == ProxyLease.id)
        .where(
            *attempt_scope, ProxyLease.id == session.proxy_lease_id,
            ProxyLease.session_id == session.id, ProxyLease.epoch == session.epoch,
        )
    ).one_or_none()
    if lease is None:
        return None
    if lease[0] == "residential":
        return ("strict" if lease[1] is True else "sticky") if lease[2] is True else "rotating"
    return "fixed" if lease[0] in FIXED_PRODUCTS else None


def _session_entry(db, session: BrowserSession) -> dict:
    metadata = _current_metadata(session)
    conformance = current_viewport_conformance(session.metadata_json, session.epoch) if session.attempt_number else None
    assignment = db.scalar(select(SessionBatchItem.launch_config["batch_preset"]).where(
        SessionBatchItem.batch_id == session.batch_id, SessionBatchItem.session_id == session.id,
    ))
    try:
        identity = str(UUID(session.id))
    except (TypeError, ValueError):
        identity = None
    return {
        "session_id": identity,
        "batch_preset": preset_details(assignment),
        "proxy_network": bundle_network(
            metadata, assigned_continuity=_assigned_continuity(db, session),
            epoch=session.epoch, attempt_number=session.attempt_number,
        ),
        "browser_diagnostics": bundle_browser(session, {**metadata, "viewport_conformance": conformance}),
        "lifecycle_outcome": bundle_lifecycle(session, metadata),
    }


def build_diagnostic_bundle(db, session: BrowserSession, *, baseline_id: str | None, auto_baseline: bool) -> dict:
    baseline = None
    selection = "not_requested"
    if baseline_id is not None:
        baseline = _explicit_baseline(db, session, baseline_id)
        selection = "explicit"
    elif auto_baseline:
        baseline = _automatic_baseline(db, session)
        selection = "automatic" if baseline is not None else "unavailable"
    return {
        "schema": "abrl.diagnostic_bundle",
        "version": 1,
        "session": _session_entry(db, session),
        "baseline": _session_entry(db, baseline[0]) if baseline is not None else None,
        "baseline_evidence": baseline[1] if baseline is not None else None,
        "baseline_selection": selection,
    }
