"""Measured transport quantities and explicitly estimated, never invented billing."""

import hashlib
import json
import math
import re
from collections import defaultdict


def amount(value):
    return (
        float(value)
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
        else None
    )


def currency(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z]{3}", value) else None


def record_attempt_usage(prior_metadata, metadata, *, attempt_id, proxy_config):
    """Idempotent per-attempt records retain recovery traffic in the logical sample."""
    usage = metadata.get("transport_usage") or {}
    measured = usage.get("measurement") == "browser_payload_transport_bytes"
    total = usage.get("total_bytes")
    total = (
        total
        if measured and isinstance(total, int) and not isinstance(total, bool) and total >= 0
        else None
    )
    provider = "oxylabs" if proxy_config is not None else None
    policy = proxy_config or {}
    price, unit = amount(policy.get("cost_per_gb")), currency(policy.get("cost_currency"))
    estimate = (
        0.0
        if provider is None
        else total / 1_000_000_000 * price
        if total is not None and price is not None and unit
        else None
    )
    row = {
        "attempt_id": attempt_id,
        "provider": provider,
        "proxy_bytes": total if provider else 0,
        "browser_payload_bytes": total,
        "upstream_bytes": usage.get("upstream_bytes"),
        "downstream_bytes": usage.get("downstream_bytes"),
        "vendor_control_bytes": metadata.get("vendor_control_bytes"),
        "cost_per_gb": price,
        "cost_currency": unit,
        "estimated_proxy_cost": estimate,
        "measurement": usage.get("measurement", "unavailable"),
        "billing_basis": "configured_decimal_GB_rate_times_measured_proxy_payload; protocol_overhead_and_invoice_adjustments_excluded",
        "billing_status": "estimated_not_billed",
        "budget_exhausted": bool(usage.get("budget_exhausted")),
    }
    prior = [r for r in prior_metadata.get("usage_attempts", []) if r.get("attempt_id") != attempt_id]
    return [*prior, row]


def session_cost(metadata, config, *, attempt_ids=None):
    records = metadata.get("usage_attempts", [])
    if not records and metadata.get("transport_usage"):
        records = record_attempt_usage(
            {},
            metadata,
            attempt_id=attempt_ids[0] if attempt_ids and len(attempt_ids) == 1 else "unrecorded",
            proxy_config=metadata.get("proxy"),
        )
    missing_usage = sorted(set(attempt_ids or []) - {row.get("attempt_id") for row in records})
    subtotals = defaultdict(float)
    for record in records:
        value = amount(record.get("estimated_proxy_cost"))
        if value is not None:
            subtotals[record.get("cost_currency") or "unspecified"] += value
    nonzero_units = {
        r.get("cost_currency") for r in records if amount(r.get("estimated_proxy_cost")) not in (None, 0)
    }
    proxy_known = (
        bool(records)
        and all(amount(r.get("estimated_proxy_cost")) is not None for r in records)
        and len(nonzero_units) <= 1
        and not missing_usage
    )
    proxy_total = sum(amount(r["estimated_proxy_cost"]) for r in records) if proxy_known else None
    proxy_unit = next(iter(nonzero_units), None) or next(
        (r.get("cost_currency") for r in records if r.get("cost_currency")), None
    )
    declared_unit = currency(metadata.get("cost_currency")) or currency(config.get("cost_currency"))
    unit = declared_unit or proxy_unit
    infrastructure = amount(metadata.get("infrastructure_cost_total"))
    browser = amount(metadata.get("browser_cost_total"))
    missing = [
        name
        for name, value in (
            ("proxy", proxy_total),
            ("infrastructure", infrastructure),
            ("browser_license", browser),
        )
        if value is None
    ]
    if proxy_unit and unit and proxy_unit != unit:
        missing.append("currency_conversion")
    total, basis = amount(metadata.get("cost_total")), "recorded_total_unverified"
    if total is None:
        total, basis = amount(config.get("cost_per_session")), "configured_all_in_estimate"
    if total is None and not missing and declared_unit:
        total, basis = proxy_total + infrastructure + browser, "component_estimate"
    if not declared_unit:
        total = None
        if (
            not missing
            or metadata.get("cost_total") is not None
            or config.get("cost_per_session") is not None
        ):
            missing.append("currency")
    return {
        "total": total,
        "currency": unit,
        "basis": basis if total is not None else "unknown",
        "billing_status": "not_reconciled_to_invoice",
        "estimated_proxy_cost": proxy_total,
        "proxy_currency": proxy_unit,
        "known_proxy_subtotals": dict(subtotals),
        "infrastructure": infrastructure,
        "browser_license": browser,
        "missing_components": missing,
        "usage_attempts": records,
        "missing_usage_attempts": missing_usage,
        "missing_usage_attempt_count": len(missing_usage),
        "measured_proxy_bytes": sum(r.get("proxy_bytes") or 0 for r in records),
        "measured_attempts": sum(r.get("browser_payload_bytes") is not None for r in records),
    }


def group_costs(rows):
    subtotals = defaultdict(float)
    for row in rows:
        for unit, value in row["cost_details"]["known_proxy_subtotals"].items():
            subtotals[unit] += value
    return {
        "known_proxy_subtotals": dict(subtotals),
        "measured_proxy_bytes": sum(r["cost_details"]["measured_proxy_bytes"] for r in rows),
        "measured_attempts": sum(r["cost_details"]["measured_attempts"] for r in rows),
        "missing_usage_attempt_count": sum(r["cost_details"]["missing_usage_attempt_count"] for r in rows),
        "sessions_with_complete_proxy_estimate": sum(
            r["cost_details"]["estimated_proxy_cost"] is not None for r in rows
        ),
        "sessions_with_total_cost": sum(r["cost"] is not None for r in rows),
        "billing_status": "estimates_not_provider_invoices",
    }


def fleet_cost(workers, pricing):
    """Deduplicate observed live runner nodes; idle/unobserved nodes remain unknown."""
    if not isinstance(pricing, dict):
        pricing = {}
    unit = currency(pricing.get("currency"))
    rates = pricing.get("node_hourly", {})
    rates = rates if isinstance(rates, dict) else {}
    nodes, missing = {}, []
    for worker in workers:
        if not worker.get("ready"):
            continue
        name = worker.get("node_name")
        if not name:
            missing.append("worker_node_identity:" + worker["id"])
            continue
        previous = nodes.get(name)
        if previous and previous["pool"] != worker["pool"]:
            previous["pool"] = "ambiguous"
        else:
            nodes[name] = {"name": name, "pool": worker["pool"]}
    total = 0.0
    for node in nodes.values():
        rate = amount(rates.get(node["pool"])) if unit else None
        node["configured_hourly_estimate"] = rate
        if rate is None:
            missing.append("node_price:" + node["name"])
        else:
            total += rate
    fixed = amount(pricing.get("fixed_hourly")) if unit else None
    return {
        "hourly_estimate": total if nodes and not missing else None,
        "known_hourly_subtotal": total if unit else None,
        "configured_fixed_hourly": fixed,
        "currency": unit,
        "basis": "configured_hourly_prices_times_distinct_observed_live_runner_nodes",
        "billing_status": "estimated_not_billed",
        "scope": "observed_live_workers_across_projects",
        "inventory_coverage": "active_runner_nodes_only; idle_browser_nodes_and_runtime_only_nodes_may_be_unobserved",
        "total_fleet_hourly_estimate": None,
        "unknown_components": [
            "unobserved_or_idle_nodes",
            "storage_and_network",
            "browser_licenses",
            *([] if fixed is not None else ["fixed_foundation"]),
        ],
        "missing_prices_or_identity": missing,
        "observed_nodes": list(nodes.values()),
        "price_configuration_sha256": hashlib.sha256(
            json.dumps(pricing, sort_keys=True).encode()
        ).hexdigest(),
    }
