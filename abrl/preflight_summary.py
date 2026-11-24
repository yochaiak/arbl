"""Aggregate a bounded sample of recorded Decodo attempts for rollout monitoring."""

from collections import Counter, defaultdict
from math import ceil
from statistics import median

from sqlalchemy import select

from abrl.platform_models import SessionEvent


def preflight_summary(db, project_id: str, limit: int):
    query = (
        select(SessionEvent.detail)
        .where(SessionEvent.project_id == project_id, SessionEvent.kind == "proxy_preflight")
        .order_by(SessionEvent.sequence.desc())
        .limit(limit)
    )
    total = 0
    fields = ("exit_ip", "country", "asn", "network_type", "timezone")
    missing = dict.fromkeys(fields, 0)
    blocks = Counter()
    sticky_compared = sticky_mismatches = 0
    samples = defaultdict(list)
    for report in db.scalars(query):
        total += 1
        if report["blocked"]:
            blocks[report["reason"]] += 1
        checks = report["checks"]
        for name in fields:
            check = checks.get(name, {})
            if check.get("status") in {None, "not_run", "unavailable"} or check.get("confirmed") is False:
                missing[name] += 1
        sticky = checks.get("sticky", {})
        changed = sticky.get("reason") == "sticky_ip_changed"
        if sticky.get("status") == "passed" or changed:
            sticky_compared += 1
            sticky_mismatches += int(changed)
        timings = report["timings_ms"]
        if isinstance(timings.get("total"), (float, int)):
            samples["total"].append(timings["total"])
        for probe in ("lookup", "sticky_lookup", "target_tls"):
            for attempt in timings.get(probe, []):
                for name, elapsed in attempt.items():
                    samples[f"{probe}.{name}"].append(elapsed)
    latency = {}
    for name, readings in samples.items():
        readings.sort()
        latency[name] = {"samples": len(readings), "p50": median(readings), "p95": readings[ceil(0.95 * len(readings)) - 1]}
    return {
        "attempts": total, "limit": limit, "blocking_reasons": blocks,
        "missing_evidence_rates": {name: count / total if total else None for name, count in missing.items()},
        "sticky_compared": sticky_compared, "sticky_mismatches": sticky_mismatches,
        "latency_ms": latency,
    }
