"""Traceable comparisons over logical sessions, never over retry attempts."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import itertools
import json
import math
from collections import Counter, defaultdict
from types import SimpleNamespace

import numpy as np
from scipy.stats import fisher_exact
from sqlalchemy import select

from abrl.costs import group_costs, session_cost
from abrl.platform_models import (
    BrowserSession,
    ExperimentRevision,
    ExperimentRun,
    ProviderObservation,
    SessionAttempt,
    SessionBatch,
    SessionBatchItem,
)
from abrl.reports import wilson

TERMINAL = {"completed", "failed", "blocked", "cancelled", "needs_reconciliation"}
VERIFIED_SOURCES = {"adscore", "controlled_site", "site_connector"}
ENVIRONMENT_KEYS = ("os", "architecture", "browser_version", "wrapper_version", "engine_version")


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def diagnostic_rates(rows):
    """Verified categorical observations use their own explicit denominators."""
    verified = [r for r in rows if r["verified"] and not r.get("conflicting")]
    human = [r for r in verified if r["ground_truth"] == "human"]
    automated = [r for r in verified if r["ground_truth"] == "automated"]

    def rate(n, d):
        return {"numerator": n, "denominator": d, "rate": n / d if d else None}

    return {
        "bot_rate": rate(sum(r["raw_codes"] == [9] for r in verified), len(verified)),
        "human_clean_rate": rate(sum(r["raw_codes"] == [0] for r in human), len(human)),
        "false_positive_bot_rate": rate(sum(r["raw_codes"] == [9] for r in human), len(human)),
        "automated_clean_rate": rate(sum(r["raw_codes"] == [0] for r in automated), len(automated)),
        "unknown_code_rate": rate(
            sum(any(c not in {0, 3, 6, 9} for c in r["raw_codes"]) for r in verified), len(verified)
        ),
        "verification_failure_rate": rate(
            sum(any(o["verification_status"] == "failed" for o in r["observations"]) for r in rows),
            len(rows),
        ),
        "operational_failure_rate": rate(
            sum(r["complete"] and r["status"] != "completed" for r in rows), len(rows)
        ),
        "verification_coverage": rate(len(verified), len(rows)),
    }


def _interval(successes: int, total: int, rows: list[dict], *, seed: int) -> dict:
    independent = wilson(successes, total)
    cluster_map = defaultdict(list)
    for row in rows:
        cluster_map[row["cluster_id"]].append(int(row["success"]))
    if len(cluster_map) == total:
        return {**independent, "method": "wilson", "clusters": total, "eligible": total > 0}
    if len(cluster_map) < 2:
        return {
            **independent,
            "low": None,
            "high": None,
            "method": "cluster_bootstrap",
            "clusters": len(cluster_map),
            "eligible": False,
        }
    # Resample whole participant/profile clusters, preserving cluster sizes.
    sizes = np.array([len(values) for values in cluster_map.values()], dtype=float)
    sums = np.array([sum(values) for values in cluster_map.values()], dtype=float)
    rng = np.random.default_rng(seed)
    choices = rng.integers(0, len(sizes), size=(2000, len(sizes)))
    replicates = sums[choices].sum(axis=1) / sizes[choices].sum(axis=1)
    low, high = np.quantile(replicates, [0.025, 0.975])
    # Percentile bootstrap is degenerate for all-success/all-failure clusters.
    # A conservative effective-cluster Wilson envelope prevents 100 repeated
    # visits to one successful profile from masquerading as 100 independent wins.
    effective_clusters = float(sizes.sum() ** 2 / (sizes * sizes).sum())
    boundary = wilson(independent["rate"] * effective_clusters, effective_clusters)
    return {
        **independent,
        "low": min(float(low), boundary["low"]),
        "high": max(float(high), boundary["high"]),
        "method": "cluster_bootstrap_cluster_wilson_envelope",
        "clusters": len(cluster_map),
        "effective_clusters": effective_clusters,
        "bootstrap_replicates": 2000,
        "bootstrap_seed": seed,
        "eligible": len(cluster_map) >= 30,
    }


def holm_adjust(p_values: list[float]) -> list[float]:
    """Holm's step-down family-wise correction, returned in original order."""
    adjusted, previous = [0.0] * len(p_values), 0.0
    for rank, index in enumerate(sorted(range(len(p_values)), key=p_values.__getitem__)):
        previous = max(previous, min(1.0, (len(p_values) - rank) * p_values[index]))
        adjusted[index] = previous
    return adjusted


def _cluster_pair_test(a: list[dict], b: list[dict], seed: int) -> tuple[float | None, str]:
    """Whole-cluster permutation for disjoint independent participant sets.

    Shared participants/profiles across configurations need a declared paired
    design, which this batch schema does not currently encode. Do not invent an
    independent p-value in that case.
    """
    clusters_a, clusters_b = defaultdict(list), defaultdict(list)
    for row in a:
        clusters_a[row["cluster_id"]].append(int(row["success"]))
    for row in b:
        clusters_b[row["cluster_id"]].append(int(row["success"]))
    if set(clusters_a) & set(clusters_b):
        return None, "paired_design_required"
    if len(clusters_a) < 5 or len(clusters_b) < 5:
        return None, "insufficient_independent_clusters"
    values = list(clusters_a.values()) + list(clusters_b.values())
    sizes, sums = np.array([len(v) for v in values]), np.array([sum(v) for v in values])
    split = len(clusters_a)
    observed = abs(sum(r["success"] for r in a) / len(a) - sum(r["success"] for r in b) / len(b))
    rng = np.random.default_rng(seed)
    extreme = 0
    for _ in range(1999):
        order = rng.permutation(len(values))
        left, right = order[:split], order[split:]
        delta = abs(sums[left].sum() / sizes[left].sum() - sums[right].sum() / sizes[right].sum())
        extreme += delta >= observed - 1e-12
    return (extreme + 1) / 2000, "cluster_permutation"


def _environment(session: BrowserSession, config: dict) -> dict:
    metadata = session.metadata_json or {}
    proxy = metadata.get("proxy") or {}
    if not isinstance(proxy, dict):
        proxy = {}
    policy = config.get("proxy_policy") or {}
    return {
        **{key: metadata.get(key) for key in ENVIRONMENT_KEYS},
        "persistence": session.persistence,
        "preparation": metadata.get("preparation_mode", config.get("preparation", {}).get("mode", "none")),
        "preparation_config": config.get("preparation", {"mode": "none"}),
        "workflow_timeout_seconds": config.get("timeout_seconds"),
        "request_budget": config.get("request_budget"),
        "traffic_budget_bytes": config.get("traffic_budget_bytes"),
        "proxy_product": proxy.get("product", "unrecorded" if policy else "direct"),
        "proxy_region": proxy.get("country", policy.get("country")),
        "proxy_policy_id": policy.get("id"),
        "proxy_policy_version": policy.get("version"),
        "dns_mode": proxy.get("dns_mode", "direct_pinned" if not policy else "unrecorded"),
    }


def _match_environment(environment: dict) -> dict:
    # Browser and wrapper versions define the candidate; OS, networking and
    # persistence define its matched comparison environment.
    return {
        key: value
        for key, value in environment.items()
        if key not in {"browser_version", "wrapper_version", "engine_version"}
    }


def _verification(observations: list[dict], attempt_id: str | None) -> dict:
    current = [row for row in observations if row["attempt_id"] == attempt_id]
    verified = [
        row
        for row in current
        if row["verification_status"] == "verified" and row["source"] in VERIFIED_SOURCES
    ]
    codes = {row["raw_code"] for row in verified}
    return {
        "verified": bool(verified),
        "clean": bool(verified) and codes == {0},
        "conflicting": len(codes) > 1,
        "verification_status": "conflicting"
        if len(codes) > 1
        else "verified"
        if verified
        else "failed"
        if current
        else "unavailable",
        "raw_codes": sorted(c for c in codes if c is not None),
    }


def _observation(row: ProviderObservation) -> dict:
    return {
        "id": row.id,
        "attempt_id": row.attempt_id,
        "verification_status": row.verification_status,
        "raw_code": row.raw_code,
        "verdict": "Clean" if row.raw_code == 0 else row.verdict,
        "accepted": row.accepted,
        "source": row.source,
        "provider_version": row.provider_version,
        "created_at": row.created_at,
    }


def _safe_config(config: dict) -> dict:
    """Snapshot reproducible configuration; strip credentials if legacy input had them."""
    prohibited = {"password", "token", "signature", "authorization", "viewer_frame", "cookie", "secret"}

    def clean(value):
        if isinstance(value, dict):
            return {
                key: "[redacted]" if key.lower() in prohibited else clean(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return clean(config)


def _batch_trials(db, batch: SessionBatch) -> list[dict]:
    config = batch.config
    sessions = list(
        db.scalars(
            select(BrowserSession)
            .where(BrowserSession.batch_id == batch.id)
            .order_by(BrowserSession.ordinal)
        )
    )
    attempts = list(
        db.scalars(
            select(SessionAttempt)
            .join(BrowserSession)
            .where(BrowserSession.batch_id == batch.id)
            .order_by(SessionAttempt.number)
        )
    )
    observed = list(
        db.scalars(
            select(ProviderObservation)
            .join(BrowserSession)
            .where(BrowserSession.batch_id == batch.id)
            .order_by(ProviderObservation.created_at)
        )
    )
    attempts_by_session, observations_by_session = defaultdict(list), defaultdict(list)
    for row in attempts:
        attempts_by_session[row.session_id].append(row)
    for row in observed:
        observations_by_session[row.session_id].append(_observation(row))
    evidence_mode = config.get("target", {}).get("mode", "browser_only")
    instrumented = evidence_mode == "instrumented"
    trials = []
    for item in db.scalars(
        select(SessionBatchItem)
        .where(SessionBatchItem.batch_id == batch.id, SessionBatchItem.session_id.is_(None))
        .order_by(SessionBatchItem.ordinal)
    ):
        sessions.append(
            SimpleNamespace(
                id=f"creation:{batch.id}:{item.ordinal}",
                batch_id=batch.id,
                ordinal=item.ordinal,
                adapter=item.adapter,
                profile_id=item.requested_profile_id or f"uncreated:{batch.id}:{item.ordinal}",
                persistence=config.get("persistence", "fresh"),
                mode=config.get("mode", "demo"),
                status="cancelled" if item.status == "cancelled" else "failed",
                outcome="cancelled" if item.status == "cancelled" else "creation_failed",
                workflow_outcome="not_started",
                elapsed_ms=None,
                epoch=0,
                error=item.error,
                ground_truth="human" if item.adapter == "manual_chrome" else "automated",
                metadata_json={},
                creation_status=item.status,
            )
        )
    sessions.sort(key=lambda row: row.ordinal)
    for session in sessions:
        session_attempts = attempts_by_session[session.id]
        observations = observations_by_session[session.id]
        last = session_attempts[-1] if session_attempts else None
        first = next((a for a in session_attempts if a.number == 1), None)
        verification = _verification(observations, last.id if last else None)
        first_verification = _verification(observations, first.id if first else None)
        metadata = session.metadata_json or {}
        simulated = (
            session.mode == "demo"
            or metadata.get("simulated") is True
            or metadata.get("evidence_source") == "simulation"
        )
        complete = session.status in TERMINAL
        workflow_complete = (
            session.workflow_outcome == "completed"
            if session.workflow_outcome is not None
            else session.outcome == "completed" and session.status == "completed"
        )
        success = workflow_complete and (verification["clean"] if instrumented else True)
        first_success = bool(
            first
            and (workflow_complete if last and first.id == last.id else first.outcome == "completed")
            and (first_verification["clean"] if instrumented else True)
        )
        # A manual control needs participant clustering, not just independent
        # profile directories. The missing identity remains explicitly unknown.
        participant = metadata.get("participant_id") if session.ground_truth in {"human", "mixed"} else None
        cluster_id = "participant:" + str(participant) if participant else "profile:" + session.profile_id
        cost_details = session_cost(metadata, config)
        cost, cost_basis = cost_details["total"], cost_details["basis"]
        trials.append(
            {
                "id": session.id,
                "session_id": None if hasattr(session, "creation_status") else session.id,
                "record_source": "batch_creation"
                if hasattr(session, "creation_status")
                else "browser_session",
                "batch_id": batch.id,
                "target_id": config.get("target_id"),
                "target_version": config.get("target_version"),
                "workflow_id": config.get("workflow_id"),
                "workflow_version": config.get("workflow_version"),
                "evidence_mode": evidence_mode,
                "cost_currency": cost_details["currency"],
                "declared_environment_id": _hash(
                    {
                        key: config.get(key)
                        for key in (
                            "persistence",
                            "preparation",
                            "proxy_policy",
                            "timeout_seconds",
                            "request_budget",
                            "traffic_budget_bytes",
                        )
                    }
                ),
                "ordinal": session.ordinal,
                "adapter": session.adapter,
                "profile_id": session.profile_id,
                "cluster_id": cluster_id,
                "participant_recorded": bool(participant),
                "ground_truth": session.ground_truth,
                "mode": session.mode,
                "simulated": simulated,
                "status": session.status,
                "outcome": session.outcome,
                "workflow_outcome": session.workflow_outcome or session.outcome,
                "complete": complete,
                "success": success,
                "workflow_completed": workflow_complete,
                "first_attempt_success": first_success,
                "recovered": success and not first_success,
                "elapsed_ms": session.elapsed_ms,
                "environment": _environment(session, config),
                "environment_epoch": session.epoch,
                "cost": cost,
                "cost_details": cost_details,
                "cost_basis": cost_basis if cost is not None else "unknown",
                "error": session.error,
                **verification,
                "observations": observations,
                "attempts": [
                    {
                        "id": a.id,
                        "number": a.number,
                        "epoch": a.epoch,
                        "outcome": a.outcome,
                        "started_action": a.started_action,
                        "started_at": a.started_at,
                        "finished_at": a.finished_at,
                    }
                    for a in session_attempts
                ],
            }
        )
    return trials


def _attribute_environments(trials):
    limitations = []
    # Missing pre-launch metadata must not remove failures from a healthy group.
    known = defaultdict(dict)
    for row in trials:
        if row["environment"]["browser_version"]:
            known[(row["adapter"], row["declared_environment_id"])][_canonical(row["environment"])] = row[
                "environment"
            ]
    ambiguous_adapters = set()
    for row in trials:
        row["group_environment"] = row["environment"]
        if not row["environment"]["browser_version"] and not row["simulated"]:
            environments = known[(row["adapter"], row["declared_environment_id"])]
            if len(environments) == 1 and not row["workflow_completed"]:
                row["group_environment"] = next(iter(environments.values()))
                row["environment_attribution"] = "declared_group_prelaunch_failure"
                limitations.append(
                    "Pre-launch failures without runtime metadata remain in their declared adapter group denominator."
                )
            elif len(environments) > 1 or row["workflow_completed"]:
                ambiguous_adapters.add(row["adapter"])
                limitations.append(
                    "Unattributed failures across multiple runtime environments prevent qualification of that adapter."
                )
    return limitations, ambiguous_adapters


def _analyse(trials, config, batch, *, attributed=False, inherited_limitations=(), ambiguous_adapters=None):
    mode = config.get("mode", "demo")
    evidence_mode = config.get("target", {}).get("mode", "browser_only")
    instrumented = evidence_mode == "instrumented"
    seed = int(config.get("seed", 42))
    threshold = config.get("reliability_target", 0.95)
    limitations = list(inherited_limitations)
    if not attributed:
        attributed_limitations, ambiguous_adapters = _attribute_environments(trials)
        limitations.extend(attributed_limitations)
    ambiguous_adapters = ambiguous_adapters or set()
    grouped = defaultdict(list)
    for row in trials:
        # Keep evidence modes separate even if corrupted/imported data mixes them.
        key = (row["adapter"], row["simulated"], _canonical(row["group_environment"]))
        grouped[key].append(row)
    groups, group_rows = [], {}
    for (adapter, simulated, environment_json), rows in sorted(grouped.items()):
        environment = json.loads(environment_json)
        group_id = _hash([adapter, simulated, environment])[:16]
        group_rows[group_id] = rows
        n, successes = len(rows), sum(row["success"] for row in rows)
        interval = _interval(successes, n, rows, seed=seed)
        times = sorted(
            row["elapsed_ms"] for row in rows if row["elapsed_ms"] is not None and row["complete"]
        )
        verified = sum(row["verified"] for row in rows)
        clean = sum(row["clean"] for row in rows)
        distribution = Counter(
            str(o["raw_code"])
            for row in rows
            for o in row["observations"]
            if o["verification_status"] == "verified" and o["source"] in VERIFIED_SOURCES
        )
        all_costs = [row["cost"] for row in rows]
        known_cost = sum(all_costs) if all(c is not None for c in all_costs) else None
        currencies = {row["cost_currency"] for row in rows if row["cost"] is not None}
        if len(currencies) > 1:
            known_cost = None
            limitations.append(
                "Costs in different currencies require explicit conversion before aggregation."
            )
        eligible = (
            interval["eligible"]
            and adapter not in ambiguous_adapters
            and not simulated
            and bool(environment.get("browser_version"))
            and all(row["participant_recorded"] or row["ground_truth"] == "automated" for row in rows)
        )
        if interval["method"] != "wilson":
            limitations.append(
                "Repeated profiles/participants use whole-cluster bootstrap (2,000 replicates) with an effective-cluster Wilson envelope; fewer than 30 clusters cannot qualify."
            )
        if not all(row["participant_recorded"] or row["ground_truth"] == "automated" for row in rows):
            limitations.append(
                "Manual participant identity is missing; human/mixed sessions cannot claim independent-trial qualification."
            )
        groups.append(
            {
                "id": group_id,
                "adapter": adapter,
                "environment": environment,
                "simulated": simulated,
                "n": n,
                "successes": successes,
                "rate": interval["rate"],
                "low": interval["low"],
                "high": interval["high"],
                "interval": interval,
                "eligible": eligible,
                "p50_ms": float(np.median(times)) if times else None,
                "p95_ms": times[math.ceil(0.95 * len(times)) - 1] if times else None,
                "completed": sum(row["workflow_completed"] for row in rows),
                "completion_rate": sum(row["workflow_completed"] for row in rows) / n,
                "verified": verified,
                "verification_coverage": verified / n,
                "clean": clean,
                "verified_clean_rate": clean / verified if verified else None,
                "verdicts": dict(distribution),
                "diagnostics": diagnostic_rates(rows),
                "latency_stddev_ms": float(np.std(times, ddof=1)) if len(times) > 1 else None,
                "latency_variance_ms2": float(np.var(times, ddof=1)) if len(times) > 1 else None,
                "verified_observations": sum(distribution.values()),
                "verification_states": dict(Counter(row["verification_status"] for row in rows)),
                "first_attempt": _interval(
                    sum(row["first_attempt_success"] for row in rows),
                    n,
                    [{**row, "success": row["first_attempt_success"]} for row in rows],
                    seed=seed,
                ),
                "operational_reliability": _interval(
                    sum(row["status"] == "completed" for row in rows),
                    n,
                    [{**row, "success": row["status"] == "completed"} for row in rows],
                    seed=seed,
                ),
                "recovered": sum(row["recovered"] for row in rows),
                "cost_total": known_cost,
                "cost_per_success": known_cost / successes
                if known_cost is not None and successes
                else None,
                "cost_currency": next(iter(currencies))
                if len(currencies) == 1
                else config.get("cost_currency"),
                "cost_basis": sorted(set(row["cost_basis"] for row in rows)),
                "cost_metering": group_costs(rows),
                "outcomes": dict(Counter(row["status"] for row in rows)),
            }
        )
    comparisons = []
    for a, b in itertools.combinations(groups, 2):
        if a["simulated"] != b["simulated"] or _match_environment(a["environment"]) != _match_environment(
            b["environment"]
        ):
            continue
        if a["interval"]["method"] == b["interval"]["method"] == "wilson":
            # Explicitly inspect cross-group cluster overlap as well.
            overlap = {r["cluster_id"] for r in group_rows[a["id"]]} & {
                r["cluster_id"] for r in group_rows[b["id"]]
            }
            if overlap:
                p_value, method = None, "paired_design_required"
            else:
                p_value = float(
                    fisher_exact(
                        [
                            [a["successes"], a["n"] - a["successes"]],
                            [b["successes"], b["n"] - b["successes"]],
                        ]
                    ).pvalue
                )
                method = "fisher_exact"
        else:
            p_value, method = _cluster_pair_test(group_rows[a["id"]], group_rows[b["id"]], seed)
        comparisons.append(
            {
                "a": a["id"],
                "b": b["id"],
                "difference": a["rate"] - b["rate"],
                "p_value": p_value,
                "adjusted_p_value": None,
                "significant": False,
                "method": method,
            }
        )
    valid_tests = [row for row in comparisons if row["p_value"] is not None]
    for row, adjusted in zip(
        valid_tests, holm_adjust([row["p_value"] for row in valid_tests]), strict=True
    ):
        row.update(adjusted_p_value=adjusted, significant=adjusted < 0.05)
    all_complete = (
        len(trials) == batch.count
        and all(row["complete"] for row in trials)
        and batch.status == "completed"
    )
    clean_evidence = mode == "live" and not any(row["simulated"] for row in trials)
    qualified = [
        group
        for group in groups
        if group["eligible"]
        and group["low"] is not None
        and group["low"] >= threshold
        and group["operational_reliability"]["low"] is not None
        and group["operational_reliability"]["low"] >= threshold
    ]
    comparable_costs = (
        len({g["cost_currency"] for g in qualified if g["cost_per_success"] is not None}) <= 1
    )
    if not comparable_costs:
        limitations.append(
            "Candidate costs use different currencies; cost does not break reliability/latency ties."
        )
    qualified.sort(
        key=lambda g: (
            -g["low"],
            g["p50_ms"] if g["p50_ms"] is not None else math.inf,
            g["cost_per_success"] if comparable_costs and g["cost_per_success"] is not None else math.inf,
            g["id"],
        )
    )
    candidates = [g["id"] for g in qualified]
    if not clean_evidence:
        recommendation = {
            "status": "simulated" if mode == "demo" else "incompatible_evidence",
            "message": "Simulated observations cannot produce a final live recommendation.",
            "candidates": [],
        }
    elif not all_complete:
        recommendation = {
            "status": "incomplete",
            "message": "Complete all planned logical sessions before a final recommendation.",
            "candidates": [],
        }
    elif config.get("phase") != "confirmation":
        recommendation = {
            "status": "pilot",
            "message": "Declare and run a separate confirmation experiment before qualification.",
            "candidates": [],
        }
    elif not qualified:
        no_qualified = bool(groups) and all(
            (g["high"] is not None and g["high"] < threshold)
            or (
                g["operational_reliability"]["high"] is not None
                and g["operational_reliability"]["high"] < threshold
            )
            for g in groups
        )
        recommendation = {
            "status": "no_qualified_candidate" if no_qualified else "insufficient_evidence",
            "message": "No candidate meets the configured lower confidence bound and evidence requirements.",
            "candidates": [],
        }
    else:
        strata = {_canonical(_match_environment(g["environment"])) for g in qualified}
        recommendation = {
            "status": "qualified"
            if len(qualified) == 1
            else "qualified_per_environment"
            if len(strata) > 1
            else "tie",
            "message": "Qualified under these target, workflow and environment conditions; ordered by reliability bound, latency and known cost."
            if len(qualified) == 1
            else "Multiple candidates qualify. Treat matched reliability as tied unless a corrected pairwise test distinguishes them; compare operating cost and latency.",
            "candidates": candidates,
        }
    limitations.append(
        "Qualification also requires the operational reliability lower bound to meet the threshold; completed workflows remain separately visible when cleanup or persistence fails."
    )
    if not instrumented:
        limitations.append(
            "Browser-only rankings measure observable workflow completion. Any independently verified provider observations remain separate from that primary outcome."
        )
    if any(group["cost_total"] is None for group in groups):
        limitations.append("Missing cost inputs remain unknown and cannot support a cost advantage claim.")
    if any(c["method"] == "paired_design_required" for c in comparisons):
        limitations.append(
            "Cross-configuration shared profiles require a predeclared paired design; no independent p-value is claimed."
        )
    limitations.append(
        "All planned logical sessions, including infrastructure failures and cancellations, stay in operational denominators; retries never add samples."
    )
    snapshot = {
        "batch_id": batch.id,
        "config": _safe_config(config),
        "planned": batch.count,
        "status": batch.status,
        "created_at": batch.created_at,
        "finished_at": batch.finished_at,
        "trials": trials,
    }
    result = {
        "batch_id": batch.id,
        "mode": mode,
        "evidence_mode": evidence_mode,
        "status": batch.status,
        "phase": config.get("phase", "pilot"),
        "reliability_target": threshold,
        "groups": groups,
        "comparisons": comparisons,
        "recommendation": recommendation,
        "limitations": list(dict.fromkeys(limitations)),
        "snapshot": snapshot,
        "trials": trials,
        "snapshot_sha256": _hash(snapshot),
        "config_sha256": _hash(snapshot["config"]),
        "methodology": {
            "independent_interval": "95% Wilson",
            "cluster_interval": "Whole-cluster percentile bootstrap with effective-cluster Wilson boundary envelope",
            "pairwise": "Two-sided Fisher exact or cluster permutation; Holm correction across reported tests",
            "primary_outcome": "completed workflow and verified Clean"
            if instrumented
            else "observable workflow completion",
            "seed": seed,
            "execution_order": "seeded randomized adapter assignment",
            "first_attempt_separate": True,
            "sample_unit": "logical session",
            "reproduction": "Create a separate batch using the pinned target/workflow versions, adapter distribution, seed and proxy policy; restore compatible profile generations and record new trial identities. Never overwrite this snapshot.",
        },
    }
    result["analysis_sha256"] = _hash(result)
    return result


def comparison(db, batch: SessionBatch) -> dict:
    return _analyse(_batch_trials(db, batch), batch.config, batch)


def experiment_comparison(db, run: ExperimentRun) -> dict:
    """Compare every declared allocation, without pooling targets or environments."""
    revision = db.get(ExperimentRevision, (run.experiment_id, run.version))
    batches = [db.get(SessionBatch, identifier) for identifier in run.batch_ids]
    if not revision or not batches or any(batch is None for batch in batches):
        raise ValueError("The immutable experiment definition or a declared batch is missing")
    planned = sum(batch.count for batch in batches)
    collected = {batch.id: _batch_trials(db, batch) for batch in batches}
    all_trials = [trial for rows in collected.values() for trial in rows]
    complete = (
        len(all_trials) == planned
        and all(trial["complete"] for trial in all_trials)
        and all(batch.status == "completed" for batch in batches)
    )
    mode = revision.config["groups"][0]["mode"]
    incompatible = any(t["mode"] != mode or t["simulated"] != (mode == "demo") for t in all_trials)
    phases = {batch.config.get("phase", "pilot") for batch in batches}
    declared = defaultdict(list)
    for batch in batches:
        config = batch.config
        conditions = {
            key: config.get(key)
            for key in (
                "target_id",
                "target_version",
                "workflow_id",
                "workflow_version",
                "mode",
                "phase",
                "reliability_target",
                "cost_currency",
            )
        }
        conditions.update(
            evidence_mode=config.get("target", {}).get("mode", "browser_only"),
            target_config_sha256=_hash(config.get("target", {})),
            workflow_config_sha256=_hash(config.get("workflow", {})),
        )
        declared[_canonical(conditions)].append(batch)
    strata = []
    for conditions_json, matched_batches in sorted(declared.items()):
        rows = [trial for batch in matched_batches for trial in collected[batch.id]]
        attribution_notes, ambiguous = _attribute_environments(rows)
        environments = defaultdict(list)
        for trial in rows:
            environments[_canonical(_match_environment(trial["group_environment"]))].append(trial)
        for environment_json, matched_trials in sorted(environments.items()):
            conditions = {**json.loads(conditions_json), "environment": json.loads(environment_json)}
            stratum_id = _hash(conditions)[:16]
            for trial in matched_trials:
                trial["stratum_id"] = stratum_id
            config = {
                **matched_batches[0].config,
                "seed": (run.randomization or {}).get("seed", matched_batches[0].config.get("seed", 42)),
                "source_batches": [
                    {"batch_id": batch.id, "config": _safe_config(batch.config)}
                    for batch in matched_batches
                ],
            }
            report = _analyse(
                matched_trials,
                config,
                SimpleNamespace(
                    id=run.id,
                    count=len(matched_trials),
                    status="completed" if complete else "running",
                    created_at=run.created_at,
                    finished_at=max((batch.finished_at or "" for batch in batches), default="") or None,
                ),
                attributed=True,
                inherited_limitations=attribution_notes,
                ambiguous_adapters=ambiguous,
            )
            report.update(id=stratum_id, conditions=conditions, batch_ids=[b.id for b in matched_batches])
            identifiers = {group["id"]: _hash([stratum_id, group["id"]])[:16] for group in report["groups"]}
            for group in report["groups"]:
                group.update(id=identifiers[group["id"]], stratum_id=stratum_id)
            for pair in report["comparisons"]:
                pair.update(a=identifiers[pair["a"]], b=identifiers[pair["b"]], stratum_id=stratum_id)
            report["recommendation"]["candidates"] = [
                identifiers[c] for c in report["recommendation"]["candidates"]
            ]
            if incompatible or len(phases) != 1:
                report["recommendation"] = {
                    "status": "incompatible_evidence",
                    "candidates": [],
                    "message": "A run with mixed evidence or pilot/confirmation phases cannot qualify candidates.",
                }
            strata.append(report)
    comparisons = [pair for report in strata for pair in report["comparisons"]]
    tests = [pair for pair in comparisons if pair["p_value"] is not None]
    for pair, adjusted in zip(tests, holm_adjust([p["p_value"] for p in tests]), strict=True):
        pair.update(adjusted_p_value=adjusted, significant=adjusted < 0.05)
    for report in strata:
        report["methodology"]["pairwise"] = (
            "Matched-stratum tests; Holm correction across all reported run tests"
        )
        report["methodology"]["execution_order"] = (run.randomization or {}).get(
            "algorithm", "legacy_unrecorded"
        )
        report["analysis_sha256"] = _hash({k: v for k, v in report.items() if k != "analysis_sha256"})
    status = (
        "completed"
        if complete
        else "creation_failed"
        if any(batch.status == "creation_failed" for batch in batches)
        else "cancelled"
        if all(batch.status == "cancelled" for batch in batches)
        else "incomplete"
    )
    snapshot = {
        "scope": "experiment_run",
        "experiment_id": run.experiment_id,
        "run_id": run.id,
        "version": run.version,
        "config": _safe_config(revision.config),
        "planned": planned,
        "created_at": run.created_at,
        "status": status,
        "randomization": run.randomization or {},
        "batches": [
            {"id": b.id, "count": b.count, "status": b.status, "config": _safe_config(b.config)}
            for b in batches
        ],
        "trials": all_trials,
    }
    result = {
        "scope": "experiment_run",
        "experiment_id": run.experiment_id,
        "run_id": run.id,
        "version": run.version,
        "concurrency": revision.config.get("concurrency", 1),
        "batch_id": None,
        "batch_ids": run.batch_ids,
        "mode": mode,
        "evidence_mode": "stratified",
        "status": status,
        "phase": next(iter(phases)) if len(phases) == 1 else "mixed",
        "reliability_target": None,
        "randomization": run.randomization or {},
        "strata": strata,
        "groups": [group for report in strata for group in report["groups"]],
        "comparisons": comparisons,
        "trials": all_trials,
        "snapshot": snapshot,
        "snapshot_sha256": _hash(snapshot),
        "config_sha256": _hash(snapshot["config"]),
        "recommendation": {
            "status": "incompatible_evidence"
            if incompatible
            else "simulated"
            if mode == "demo"
            else "stratified",
            "candidates": [],
            "message": "Recommendations apply only within each target/workflow/environment/evidence stratum; no pooled winner is reported.",
        },
        "limitations": list(
            dict.fromkeys(
                [
                    "Different targets, workflow versions, environments, evidence modes, phases, thresholds and cost currencies are never pooled.",
                    "Every declared batch must finish before any stratum can qualify; failed creation allocations remain in the planned denominator.",
                    *[note for report in strata for note in report["limitations"]],
                ]
            )
        ),
        "methodology": {
            "primary_outcome": "Per-stratum verified Clean plus completion, or browser-only completion",
            "pairwise": "Matched-stratum tests with Holm correction across all run tests",
            "sample_unit": "logical allocation, including creation failures; retries add no samples",
            "execution_order": (run.randomization or {}).get("algorithm", "legacy_unrecorded"),
            "seed": (run.randomization or {}).get("seed"),
            "reproduction": "Use the immutable experiment revision and recorded cross-group dispatch plan; never overwrite the exported snapshot.",
        },
    }
    result["analysis_sha256"] = _hash(result)
    return result


def _percent(value):
    return "Unavailable" if value is None else f"{value:.1%}"


def _condition_lines(conditions):
    environment = conditions["environment"]
    return [
        f"Target {conditions['target_id']} v{conditions['target_version']}; workflow {conditions['workflow_id']} v{conditions['workflow_version']}.",
        f"Evidence: {conditions['evidence_mode']}; phase: {conditions['phase']}; required lower confidence bound: {_percent(conditions['reliability_target'])}.",
        f"Environment: {environment.get('os')} / {environment.get('architecture')}; profile: {environment.get('persistence')}; preparation: {environment.get('preparation')}.",
        f"Budgets: {environment.get('request_budget')} browser requests; {environment.get('traffic_budget_bytes')} transport bytes; {environment.get('workflow_timeout_seconds')} workflow seconds.",
        f"Network: {environment.get('proxy_product')} / {environment.get('proxy_region') or 'region unspecified'}; DNS: {environment.get('dns_mode')}; cost currency: {conditions.get('cost_currency') or 'unknown'}.",
    ]


def _metering_line(group):
    metering = group.get("cost_metering")
    if not metering:
        return "Transport cost metering is unavailable in this historical snapshot."
    return (
        f"Measured proxy bytes: {metering['measured_proxy_bytes']}; known proxy estimates by currency: "
        f"{metering['known_proxy_subtotals']}. Estimates are not reconciled provider billing."
    )


def _report_rows(data):
    return [
        [
            group["adapter"] + (" / " + group["stratum_id"][:8] if group.get("stratum_id") else ""),
            str(group["n"]),
            f"{group['successes']} / {group['n']}",
            f"{_percent(group['low'])} to {_percent(group['high'])}",
            _percent(group["verification_coverage"]),
            "Unknown" if group["p50_ms"] is None else f"{group['p50_ms']:,.0f}",
            "Unknown" if group["cost_per_success"] is None else f"{group['cost_per_success']:.4f}",
        ]
        for group in data["groups"]
    ]


def export_comparison(data: dict, format: str) -> bytes:
    """Render exclusively from the supplied immutable snapshot; never query live data."""
    if _hash(data["snapshot"]) != data["snapshot_sha256"]:
        raise ValueError("Research snapshot checksum does not match")
    if data.get("trials") != data["snapshot"]["trials"]:
        raise ValueError("Research dataset does not match its snapshot")
    if _hash({k: v for k, v in data.items() if k != "analysis_sha256"}) != data.get("analysis_sha256"):
        raise ValueError("Research analysis checksum does not match its immutable export")
    if format == "json":
        return json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False).encode()
    if format == "csv":
        stream = io.StringIO()
        fields = [
            "run_id",
            "stratum_id",
            "batch_id",
            "target_id",
            "target_version",
            "workflow_id",
            "workflow_version",
            "snapshot_sha256",
            "mode",
            "evidence_mode",
            "id",
            "session_id",
            "record_source",
            "ordinal",
            "adapter",
            "profile_id",
            "ground_truth",
            "status",
            "outcome",
            "success",
            "first_attempt_success",
            "recovered",
            "elapsed_ms",
            "verification_status",
            "raw_codes",
            "environment_epoch",
            "environment",
            "observations",
            "attempts",
            "cost",
            "cost_details",
            "error",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for trial in data["trials"]:
            values = {
                **trial,
                "batch_id": trial.get("batch_id", data["batch_id"]),
                "run_id": data.get("run_id"),
                "snapshot_sha256": data["snapshot_sha256"],
                "mode": trial.get("mode", data["mode"]),
                "evidence_mode": trial.get("evidence_mode", data["evidence_mode"]),
            }
            for key, value in values.items():
                if isinstance(value, (dict, list)):
                    values[key] = _canonical(value)
                elif isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                    values[key] = "'" + value
            writer.writerow(values)
        return stream.getvalue().encode()
    if format == "html":
        return _html_report(data).encode()
    if format == "pdf":
        return _pdf_report(data)
    raise ValueError("Supported comparison formats: json, csv, html, pdf")


def _html_report(data):
    def escape(value):
        return html.escape(str(value))

    headers = ["Browser", "N", "Primary success", "95% interval", "Verified", "p50 ms", "Cost / success"]
    rows = "".join(
        "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>" for row in _report_rows(data)
    )
    environments = "".join(
        f"<h3>{escape(g['adapter'])} / {escape(g['id'])}</h3><pre>{escape(json.dumps(g['environment'], indent=2))}</pre>"
        for g in data["groups"]
    )
    analysis = "".join(
        f"<h3>{escape(g['adapter'])} / {escape(g['id'])}</h3>"
        f"<p>First attempt: {g['first_attempt']['successes']} / {g['n']}; recovered: {g['recovered']}. "
        f"Completion: {_percent(g['completion_rate'])}; verified Clean among verified sessions: {_percent(g['verified_clean_rate'])}. "
        f"Latency p95: {escape(g['p95_ms'])} ms. Interval: {escape(g['interval']['method'])}.</p>"
        f"<p>Verified raw verdict counts: {escape(g['verdicts'])}. Operational outcomes: {escape(g['outcomes'])}. "
        f"Cost total: {escape(g['cost_total'])}; basis: {escape(g['cost_basis'])}; currency: {escape(g['cost_currency'])}.</p>"
        f"<p>{escape(_metering_line(g))}</p>"
        for g in data["groups"]
    )
    trials = "".join(
        f"<tr><td>{escape(t['id'])}</td><td>{escape(t['status'])}</td><td>{escape(t['verification_status'])}</td><td>{escape(t['raw_codes'])}</td><td>{len(t['attempts'])}</td></tr>"
        for t in data["trials"]
    )
    strata = "".join(
        f"<h3>Stratum {escape(s['id'])}</h3>"
        + "".join(f"<p>{escape(line)}</p>" for line in _condition_lines(s["conditions"]))
        + f"<p>{escape(s['recommendation']['status'])}: {escape(s['recommendation']['message'])}</p>"
        f"<p>Candidate IDs: {escape(', '.join(s['recommendation']['candidates']) or 'None')}</p>"
        for s in data.get("strata", [])
    )
    identity = "Experiment run " + data["run_id"] if data.get("run_id") else "Batch " + data["batch_id"]
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'><title>ABRL Research Report</title>"
        "<style>body{font:15px/1.5 system-ui;color:#17212f;max-width:1120px;margin:48px auto;padding:0 24px}"
        "h1{font-size:32px}h2{margin-top:32px}th,td{text-align:left;padding:10px;border-bottom:1px solid #d8e1eb}"
        "table{width:100%;border-collapse:collapse}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f5f9;padding:16px}"
        ".evidence{background:#eaf2ff;padding:16px;border-left:4px solid #3966bd}.hash{overflow-wrap:anywhere;font:12px monospace}"
        "@media print{body{margin:0}thead{display:table-header-group}tr{break-inside:avoid}}</style>"
        f"<h1>ABRL Research Report</h1><p class='evidence'>{'SIMULATED - NOT LIVE EVIDENCE' if data['mode'] == 'demo' else 'LIVE OBSERVATIONS'} / {escape(data['evidence_mode'])} / {escape(data['phase'])}</p>"
        f"<p>{escape(identity)}. Qualification lower bound: {'see matched conditions' if data.get('strata') else _percent(data['reliability_target'])}.</p>"
        f"<h2>Decision</h2><p>{escape(data['recommendation']['status'])}: {escape(data['recommendation']['message'])}</p>"
        + (f"<h2>Matched conditions and decisions</h2>{strata}" if strata else "")
        + "<h2>Results</h2><table><thead><tr>"
        + "".join(f"<th>{h}</th>" for h in headers)
        + f"</tr></thead><tbody>{rows}</tbody></table>"
        + f"<h2>Reliability, recovery and cost</h2>{analysis}"
        + f"<h2>Pairwise effects</h2><pre>{escape(json.dumps(data['comparisons'], indent=2))}</pre>"
        f"<h2>Methodology</h2><pre>{escape(json.dumps(data['methodology'], indent=2))}</pre>"
        "<h2>Limitations</h2><ul>"
        + "".join(f"<li>{escape(line)}</li>" for line in data["limitations"])
        + "</ul>"
        f"<h2>Environment and reproducibility</h2>{environments}<pre>{escape(json.dumps(data['snapshot']['config'], indent=2))}</pre>"
        f"<p class='hash'>Snapshot SHA-256: {escape(data['snapshot_sha256'])}</p>"
        f"<h2>Trial trace</h2><table><thead><tr><th>Session / allocation</th><th>Outcome</th><th>Verification</th><th>Raw codes</th><th>Attempts</th></tr></thead><tbody>{trials}</tbody></table></html>"
    )


def _pdf_report(data):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    stream = io.BytesIO()
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="SmallResearch", fontSize=8, leading=11, alignment=TA_LEFT, spaceAfter=5, wordWrap="CJK"
        )
    )
    styles.add(
        ParagraphStyle(
            name="EvidenceResearch",
            fontSize=10,
            leading=15,
            textColor=colors.HexColor("#2552a5"),
            backColor=colors.HexColor("#eaf2ff"),
            borderPadding=10,
            spaceAfter=16,
        )
    )
    small = styles["SmallResearch"]
    for name in ("Heading2", "Heading3"):
        styles[name].keepWithNext = True

    def p(value, style=small):
        return Paragraph(html.escape(str(value)), style)

    story = [
        p("ABRL Research Report", styles["Title"]),
        Spacer(1, 8),
        p(
            ("SIMULATED - NOT LIVE EVIDENCE" if data["mode"] == "demo" else "LIVE OBSERVATIONS")
            + f" | {data['evidence_mode']} | {data['phase']}",
            styles["EvidenceResearch"],
        ),
        p(f"Experiment run: {data['run_id']}" if data.get("run_id") else f"Batch: {data['batch_id']}"),
        p(
            "Qualification lower confidence bound: "
            + ("see matched conditions" if data.get("strata") else _percent(data["reliability_target"]))
        ),
        p("Decision", styles["Heading2"]),
        p(data["recommendation"]["status"] + ": " + data["recommendation"]["message"]),
    ]
    if data.get("strata"):
        story.append(p("Matched conditions and decisions", styles["Heading2"]))
        for stratum in data["strata"]:
            story.append(p("Stratum " + stratum["id"], styles["Heading3"]))
            story.extend(p(line) for line in _condition_lines(stratum["conditions"]))
            story.append(
                p(stratum["recommendation"]["status"] + ": " + stratum["recommendation"]["message"])
            )
            story.append(
                p("Candidate IDs: " + (", ".join(stratum["recommendation"]["candidates"]) or "None"))
            )
    story.append(p("Results", styles["Heading2"]))
    headers = ["Browser", "N", "Success", "95% interval", "Verified", "p50 ms", "Cost / success"]
    table = LongTable(
        [[p(cell) for cell in row] for row in [headers, *_report_rows(data)]],
        colWidths=[73, 24, 48, 112, 52, 51, 65],
        repeatRows=1,
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf2ff")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#d8e1eb")),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.append(table)
    story.append(p("Reliability, recovery and cost", styles["Heading2"]))
    for group in data["groups"]:
        story.append(
            p(
                f"{group['adapter']} / {group['id']}: First-attempt success "
                f"{group['first_attempt']['successes']} / {group['n']}; recovered {group['recovered']}. "
                f"Completion {_percent(group['completion_rate'])}; verified Clean rate among verified sessions "
                f"{_percent(group['verified_clean_rate'])}. p95 latency {group['p95_ms']} ms."
            )
        )
        story.append(
            p(
                f"Interval: {group['interval']['method']}; clusters {group['interval']['clusters']}. "
                f"Raw verified verdict counts: {group['verdicts']}. Outcomes: {group['outcomes']}. "
                f"Cost total {group['cost_total']}, basis {group['cost_basis']}, currency {group['cost_currency'] or 'unspecified'}."
            )
        )
        story.append(p(_metering_line(group)))
    story.append(p("Pairwise effects", styles["Heading2"]))
    if not data["comparisons"]:
        story.append(p("No eligible matched pair is available."))
    for pair in data["comparisons"]:
        story.append(
            p(
                f"{pair['a']} vs {pair['b']}: difference {pair['difference']:+.3f}; "
                f"p={pair['p_value']}, Holm-adjusted p={pair['adjusted_p_value']}; "
                f"significant={pair['significant']}; method={pair['method']}."
            )
        )
    for title, lines in (
        (
            "Methodology",
            [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in data["methodology"].items()],
        ),
        ("Limitations", data["limitations"]),
        (
            "Environment and reproduction",
            [f"{g['adapter']} / {g['id']}: {_canonical(g['environment'])}" for g in data["groups"]],
        ),
    ):
        story.append(p(title, styles["Heading2"]))
        story.extend(p(line) for line in lines)
    story.append(p("Pinned configuration", styles["Heading2"]))
    story.extend(p(f"{key}: {_canonical(value)}") for key, value in data["snapshot"]["config"].items())
    story.append(p("Snapshot SHA-256: " + data["snapshot_sha256"]))
    story.extend(
        [
            p("Trial trace", styles["Heading2"]),
            p(
                "Full observation and attempt identifiers are included in the matching JSON/CSV dataset. Each row is one logical allocation. Creation failures have no browser session or attempt."
            ),
        ]
    )
    trace = [["Session or allocation / browser", "Outcome", "Verified / raw codes", "Attempts"]]
    trace += [
        [
            f"{t['id']} / {t['adapter']}",
            t["status"],
            f"{t['verification_status']} / {t['raw_codes']}",
            len(t["attempts"]),
        ]
        for t in data["trials"]
    ]
    table = LongTable(
        [[p(cell) for cell in row] for row in trace], colWidths=[200, 80, 100, 45], repeatRows=1
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf2ff")),
                ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.lightgrey),
            ]
        )
    )
    story.append(table)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(22 * mm, 13 * mm, "ABRL | " + data["snapshot_sha256"][:16])
        canvas.drawRightString(A4[0] - 22 * mm, 13 * mm, f"Page {doc.page}")
        canvas.restoreState()

    document = SimpleDocTemplate(
        stream,
        pagesize=A4,
        rightMargin=22 * mm,
        leftMargin=22 * mm,
        topMargin=20 * mm,
        bottomMargin=22 * mm,
        title="ABRL Research Report",
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return stream.getvalue()
