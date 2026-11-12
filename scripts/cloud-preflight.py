#!/usr/bin/env python3
"""Read-only capacity/account preflight; --check-state explicitly tests temporary lock objects."""

import argparse
import base64
import json
import math
import os
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

REQUIRED_ENV = (
    "ABRL_OIDC_ISSUER",
    "ABRL_SESSION_SECRET",
    "ABRL_OIDC_OWNER_SUBJECT",
    "ABRL_WEB_ORIGIN",
    "ABRL_CONTROLLED_ORIGIN",
    "ABRL_OIDC_CLIENT_ID",
    "ABRL_OIDC_CLIENT_SECRET",
    "ABRL_DATABASE_URL",
    "ABRL_METRICS_TOKEN",
    "ABRL_PROFILE_STORE_URL",
    "ABRL_S3_ACCESS_KEY",
    "ABRL_S3_SECRET_KEY",
    "ABRL_PROFILE_ENCRYPTION_KEY",
)


def configured_domain(domain):
    host = str(domain or "").lower()
    reserved = ("example.com", "example.net", "example.org", "invalid", "localhost", "test")
    return bool(
        "." in host
        and not any(host == name or host.endswith("." + name) for name in reserved)
        and not any(char in host for char in "/:@?# ")
    )


def validate_runtime_origins(config, env):
    """Check exact configured HTTPS origin; live TLS/DNS is a separate deployment test."""
    errors = []
    domain = str(config.get("domain", "")).lower()
    for name in ("ABRL_WEB_ORIGIN", "ABRL_CONTROLLED_ORIGIN"):
        try:
            parsed = urlsplit(env.get(name, ""))
            valid = (
                configured_domain(domain)
                and parsed.scheme == "https"
                and parsed.hostname == domain
                and parsed.port in (None, 443)
                and not parsed.username
                and not parsed.password
                and parsed.path in ("", "/")
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            errors.append(name + " must be the HTTPS origin of the configured DNS/TLS domain.")
    return errors


def validate_config(config):
    errors = []
    pools = config.get("pools", {})
    total_max = sum(p.get("max_nodes", 0) for p in pools.values())
    if not 2 <= total_max <= 80:
        errors.append("Aggregate browser-node ceiling must be in [2, 80].")
    maximum = config.get("maximum_sessions", 500)
    if not isinstance(maximum, int) or not 1 <= maximum <= 500:
        errors.append("maximum_sessions must be an integer in [1, 500].")
    if pools.get("standard", {}).get("min_nodes", 0) < 2:
        errors.append("Standard pool requires two warm nodes.")
    for name, pool in pools.items():
        if pool.get("min_nodes", 0) > pool.get("max_nodes", 0):
            errors.append(f"{name}: min_nodes exceeds max_nodes.")
        if pool.get("memory_gib", 0) <= pool.get("reserve_memory_gib", 2) or pool.get(
            "vcpus", 0
        ) <= pool.get("reserve_vcpus", 1):
            errors.append(f"{name}: no allocatable capacity after system reserve.")
    if config.get("backup_location") == config.get("location"):
        errors.append("Disaster-recovery backup location must differ from primary.")
    if not configured_domain(config.get("domain")):
        errors.append("A real domain/DNS configuration is required.")
    if not config.get("state_locking_verified"):
        errors.append("Run --check-state against the exact backend before deployment.")
    return errors


def estimate(config):
    maximum = config.get("maximum_sessions", 500)
    rows = []
    for name, pool in config.get("pools", {}).items():
        cpu = 4 if name == "vendor" else 2
        mem = 8 if name == "vendor" else 4
        # Vendor estimate includes the additional Kameleo SDK sidecar; Cloak uses less.
        overhead_cpu = 0.45 if name == "vendor" else 0.35
        overhead_mem = 0.75 if name == "vendor" else 0.625
        slots = max(
            0,
            min(
                int((pool["vcpus"] - pool.get("reserve_vcpus", 1)) // (cpu + overhead_cpu)),
                int((pool["memory_gib"] - pool.get("reserve_memory_gib", 2)) // (mem + overhead_mem)),
            ),
        )
        nodes = math.ceil(maximum / slots) if slots else None
        price = pool.get("hourly_price")
        rows.append(
            {
                "pool": name,
                "slots_per_node_hypothesis": slots,
                "configured_max_nodes": pool["max_nodes"],
                "maximum_slots": slots * pool["max_nodes"],
                "nodes_for_all_sessions": nodes,
                "can_fit_all_sessions": bool(nodes and nodes <= pool["max_nodes"]),
                "session_vcpus_at_maximum": cpu * maximum,
                "session_memory_gib_at_maximum": mem * maximum,
                "runtime_overhead_vcpus_at_maximum": overhead_cpu * maximum,
                "runtime_overhead_memory_gib_at_maximum": overhead_mem * maximum,
                "maximum_hourly_compute_cost": None
                if price is None
                else round(price * pool["max_nodes"], 4),
                "currency": pool.get("currency"),
                "measured": False,
            }
        )
    return {
        "maximum_sessions": maximum,
        "maximum_browser_nodes": sum(p["max_nodes"] for p in config.get("pools", {}).values()),
        "fixed_nodes": 8,
        "network_attachments_at_ceiling": 9 + sum(p["max_nodes"] for p in config.get("pools", {}).values()),
        "pools": rows,
        "other_costs": "unknown until load balancer, volumes, traffic, backups and license inputs supplied",
    }


def api_get(path, token):
    request = urllib.request.Request(
        "https://api.hetzner.cloud/v1/" + path, headers={"Authorization": "Bearer " + token}
    )
    return json.load(urllib.request.urlopen(request, timeout=20))


def api_list(resource, token):
    rows = []
    page = 1
    while True:
        payload = api_get(f"{resource}?per_page=50&page={page}", token)
        rows.extend(payload[resource])
        following = payload.get("meta", {}).get("pagination", {}).get("next_page")
        if not following:
            return rows
        page = following


def account_checks(config, token, quotas):
    errors = []
    types = {row["name"]: row for row in api_list("server_types", token)}
    datacenters = api_list("datacenters", token)
    pricing = api_get("pricing", token).get("pricing", {})
    prices = {item["id"]: item for item in pricing.get("server_types", [])}
    available = {
        i
        for dc in datacenters
        if dc["location"]["name"] == config["location"]
        for i in dc["server_types"]["available"]
    }
    for name, pool in config["pools"].items():
        actual = types.get(pool["server_type"])
        if not actual or actual["architecture"] != "x86" or actual["id"] not in available:
            errors.append(f"{name}: configured amd64 server type unavailable in selected location.")
        elif actual["cores"] != pool["vcpus"] or actual["memory"] != pool["memory_gib"]:
            errors.append(f"{name}: capacity assumptions do not match provider server-type metadata.")
        if actual:
            location_price = next(
                (
                    item
                    for item in prices.get(actual["id"], {}).get("prices", [])
                    if item["location"] == config["location"]
                ),
                None,
            )
            if location_price:
                pool["hourly_price"] = float(location_price["price_hourly"]["net"])
                pool["currency"] = pricing.get("currency")
    for role, fixed in config.get("foundation", {}).items():
        actual = types.get(fixed["server_type"])
        if not actual or actual["architecture"] != "x86" or actual["id"] not in available:
            errors.append(f"{role}: fixed foundation server type unavailable or not amd64.")
    if not quotas.get("source"):
        errors.append("Quota evidence must identify its console/support source.")
    # Hetzner does not publish project server-limit information through this API.
    # Require recent console/support evidence rather than fabricate a successful quota check.
    verified = datetime.fromisoformat(quotas["verified_at"].replace("Z", "+00:00"))
    if abs((datetime.now(timezone.utc) - verified).total_seconds()) > 7 * 86400:
        errors.append("Provider quota evidence is older than seven days.")
    servers = api_get("servers?per_page=1", token)
    count = servers["meta"]["pagination"]["total_entries"]
    cluster_name = config.get("cluster_name", "abrl")
    own = api_get("servers?per_page=1&label_selector=cluster%3D" + cluster_name, token)
    count -= own["meta"]["pagination"]["total_entries"]
    required = 8 + sum(p["max_nodes"] for p in config["pools"].values())
    if quotas.get("server_limit", 0) < required + count:
        errors.append(
            "Remaining account server quota cannot accommodate foundation + configured browser ceiling."
        )
    return errors


def check_state(client, bucket, key_bytes):
    """Proves conditional create refuses a competing writer; plaintext state never written."""
    if client.get_bucket_versioning(Bucket=bucket).get("Status") in {"Enabled", "Suspended"}:
        raise ValueError("Use a dedicated never-versioned Hetzner lock/state bucket.")
    if len(key_bytes) != 32:
        raise ValueError("SSE-C key must decode to exactly 32 bytes.")
    key = ".abrl-lock-preflight/" + uuid.uuid4().hex
    sse = {"SSECustomerAlgorithm": "AES256", "SSECustomerKey": key_bytes}
    created = False
    try:
        client.put_object(Bucket=bucket, Key=key, Body=b"owner-one", IfNoneMatch="*", **sse)
        created = True
        rejected = False
        try:
            client.put_object(Bucket=bucket, Key=key, Body=b"owner-two", IfNoneMatch="*", **sse)
        except Exception as exc:
            status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status != 412:
                raise
            rejected = True
        if not rejected:
            raise ValueError("Storage accepted two competing lock owners.")
        read = client.get_object(Bucket=bucket, Key=key, **sse)
        if read.get("SSECustomerAlgorithm") != "AES256":
            raise ValueError("Object storage did not confirm customer-key encryption")
        if read["Body"].read() != b"owner-one":
            raise ValueError("Encrypted lock roundtrip did not preserve original owner.")
        try:
            client.get_object(Bucket=bucket, Key=key)
        except Exception as exc:
            status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status not in (400, 403):
                raise
        else:
            raise ValueError("Object was readable without the required customer encryption key.")
    finally:
        if created:
            client.delete_object(Bucket=bucket, Key=key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="deployment/cloud.example.json")
    parser.add_argument("--online", action="store_true")
    parser.add_argument(
        "--quotas", help="JSON evidence: verified_at, source, server_limit from Hetzner console/support"
    )
    parser.add_argument("--require-runtime", action="store_true")
    parser.add_argument("--check-state", action="store_true")
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    errors = [] if args.estimate_only else validate_config(config)
    if args.require_runtime:
        errors += ["Missing runtime secret: " + key for key in REQUIRED_ENV if not os.getenv(key)]
        errors += validate_runtime_origins(config, os.environ)
    if args.online:
        if not os.getenv("HCLOUD_TOKEN") or not args.quotas:
            parser.error("--online requires HCLOUD_TOKEN and --quotas evidence file")
        errors += account_checks(
            config, os.environ["HCLOUD_TOKEN"], json.loads(Path(args.quotas).read_text())
        )
    if args.check_state:
        import boto3

        for key in ["ABRL_STATE_ENDPOINT", "ABRL_STATE_BUCKET", "AWS_SSE_CUSTOMER_KEY"]:
            if not os.getenv(key):
                parser.error(key + " is required")
        client = boto3.client("s3", endpoint_url=os.environ["ABRL_STATE_ENDPOINT"])
        check_state(
            client,
            os.environ["ABRL_STATE_BUCKET"],
            base64.b64decode(os.environ["AWS_SSE_CUSTOMER_KEY"], validate=True),
        )
        errors = [e for e in errors if not e.startswith("Run --check-state")]
    report = {
        "mode": "estimate" if args.estimate_only else "preflight",
        "passed": not errors,
        "online_checked": args.online,
        "state_lock_tested": args.check_state,
        "origin_configuration_checked": args.require_runtime,
        "errors": errors,
        "capacity": estimate(config),
    }
    output = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).write_text(output + "\n")
    print(output)
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
