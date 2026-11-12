#!/usr/bin/env python3
"""Measure real 100→250→500 browser stages against a registered owned fixture.

Requires an authenticated OIDC session cookie in ABRL_LOAD_COOKIE (or local-only
ABRL_OPERATOR_TOKEN). Never substitutes simulated trials. Stops at first failure.
"""

import argparse
import json
import os
import time
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ACTIVE = {"starting", "preparing", "ready", "running", "awaiting_operator", "stopping"}
TERMINAL = {"completed", "failed", "blocked", "cancelled", "needs_reconciliation"}


def request(base, path, method="GET", payload=None, project="default"):
    headers = {"Content-Type": "application/json", "X-Project-ID": project}
    if os.getenv("ABRL_LOAD_COOKIE"):
        headers["Cookie"] = os.environ["ABRL_LOAD_COOKIE"]
    elif os.getenv("ABRL_OPERATOR_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["ABRL_OPERATOR_TOKEN"]
    else:
        raise ValueError(
            "Provide an authenticated ABRL_LOAD_COOKIE; token is supported only for local development"
        )
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/api/v1" + path, data=body, headers=headers, method=method
    )
    return json.load(urllib.request.urlopen(req, timeout=30))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--project", default="default")
    parser.add_argument(
        "--template",
        required=True,
        help="BatchInput JSON for an authorized owned fixture; mode must be live",
    )
    parser.add_argument("--maximum", required=True, type=int, choices=[100, 250, 500])
    parser.add_argument("--deadline-seconds", type=int, default=3600)
    parser.add_argument("--output", required=True)
    parser.add_argument("--acknowledge-owned-fixture", action="store_true", required=True)
    args = parser.parse_args()
    template = json.loads(Path(args.template).read_text())
    if template.get("mode") != "live" or template.get("adapters", ["playwright"]) != ["playwright"]:
        parser.error(
            "Standard capacity test requires live Playwright; vendor validation is a separate licensed run"
        )
    if not 60 <= args.deadline_seconds <= 14400:
        parser.error("deadline must be from 60 to 14400 seconds")
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "simulated": False,
        "project": args.project,
        "target_id": template["target_id"],
        "workflow_id": template["workflow_id"],
        "stages": [],
    }
    output = Path(args.output)
    if output.exists():
        parser.error("Use a new evidence filename; existing evidence is immutable")
    for count in [100, 250, 500]:
        if count > args.maximum:
            break
        body = {
            **template,
            "count": count,
            "concurrency": count,
            "idempotency_key": "capacity-" + uuid.uuid4().hex,
            "name": f"Live capacity validation {count}",
        }
        batch = request(args.url, "/batches", "POST", body, args.project)
        identity = batch["id"]
        stage = {"requested": count, "batch_id": identity, "peak_active": 0, "samples": [], "passed": False}
        report["stages"].append(stage)
        start = time.monotonic()
        try:
            while time.monotonic() - start < args.deadline_seconds:
                before = time.monotonic()
                rows = request(args.url, "/sessions?limit=500&batch_id=" + identity, project=args.project)
                items = rows if isinstance(rows, list) else rows.get("items", [])
                states = Counter(row["status"] for row in items)
                active = sum(states.get(state, 0) for state in ACTIVE)
                stage["peak_active"] = max(stage["peak_active"], active)
                stage["samples"].append(
                    {
                        "elapsed_seconds": round(time.monotonic() - start, 2),
                        "active": active,
                        "states": dict(states),
                        "control_request_ms": round((time.monotonic() - before) * 1000, 2),
                    }
                )
                if len(items) == count and all(row["status"] in TERMINAL for row in items):
                    stage["passed"] = stage["peak_active"] >= count and states.get("completed", 0) == count
                    stage["final_sessions"] = items
                    break
                time.sleep(5)
        finally:
            stage["elapsed_seconds"] = round(time.monotonic() - start, 2)
            output.write_text(json.dumps(report, indent=2) + "\n")
        if not stage["passed"]:
            request(args.url, "/batches/" + identity + "/cancel", "POST", project=args.project)
            raise SystemExit(
                f"Stage {count} did not pass; batch cancellation requested. Evidence: {output}"
            )
        print(f"Completed {count} live concurrent browser sessions; evidence: {output}")
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["passed"] = all(stage["passed"] for stage in report["stages"])
    output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
