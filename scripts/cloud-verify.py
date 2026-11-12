#!/usr/bin/env python3
"""Inspect cluster health without changing workloads; output evidence, never invent a live pass."""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def kubectl(*args):
    return json.loads(subprocess.check_output(["kubectl", *args, "-o", "json"], text=True))


def assess(nodes, pods, postgres):
    errors = []
    roles = {"control": 0, "app": 0, "data": 0}
    workers = 0
    for node in nodes["items"]:
        role = node["metadata"]["labels"].get("abrl.io/role")
        ready = any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"])
        if role in roles and ready:
            roles[role] += 1
        if role == "browser":
            workers += 1
    for role, required in [("control", 3), ("app", 2), ("data", 3)]:
        if roles[role] < required:
            errors.append(f"{role}: {roles[role]} ready nodes; require {required}")
    if workers > 80:
        errors.append("Worker nodes exceed hard maximum 80")
    if postgres.get("status", {}).get("readyInstances", 0) != 3:
        errors.append("PostgreSQL does not have three ready instances")
    conditions = postgres.get("status", {}).get("conditions", [])
    if not any(c["type"] == "ContinuousArchiving" and c["status"] == "True" for c in conditions):
        errors.append("Continuous WAL archiving has not been verified healthy")
    for pod in pods["items"]:
        if pod["status"].get("phase") in {"Succeeded", "Pending"}:
            continue
        if any(c.get("restartCount", 0) > 3 for c in pod["status"].get("containerStatuses", [])):
            errors.append("Repeated restarts: " + pod["metadata"]["name"])
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    errors = assess(
        kubectl("get", "nodes"),
        kubectl("-n", "abrl", "get", "pods"),
        kubectl("-n", "abrl", "get", "cluster", "abrl-postgres"),
    )
    report = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "checks": "read-only live cluster health",
        "passed": not errors,
        "errors": errors,
        "not_proven": [
            "failure recovery",
            "DR RPO/RTO",
            "500-browser capacity",
            "browser egress isolation",
        ],
    }
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
