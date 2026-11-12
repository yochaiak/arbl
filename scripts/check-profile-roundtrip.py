"""Run two owned local Chromium profiles, restore them, and preserve acceptance evidence."""

import json
import time
import uuid
from pathlib import Path

import httpx

from abrl.config import OPERATOR_TOKEN

terminal = {"completed", "failed", "blocked", "cancelled", "needs_reconciliation"}
with httpx.Client(
    base_url="http://127.0.0.1:8400/api/v1",
    headers={"Authorization": "Bearer " + OPERATOR_TOKEN},
    timeout=15,
) as c:

    def post(path, value):
        r = c.post(path, json=value)
        r.raise_for_status()
        return r.json()

    target = post(
        "/targets",
        {
            "name": "Live session acceptance " + uuid.uuid4().hex[:6],
            "origin": "http://127.0.0.1:8400",
            "mode": "browser_only",
            "rate_limit": 60,
            "concurrency": 2,
            "authorized": True,
        },
    )
    workflow = post(
        "/workflows",
        {
            "name": "Persistent repeat evidence",
            "steps": [
                {"action": "navigate", "url": "/lab/repeat"},
                {"action": "extract", "selector": "#visit-count"},
                {"action": "assert_visible", "selector": "#visit-count"},
            ],
        },
    )
    template = {
        "name": "Live persistence acceptance",
        "target_id": target["id"],
        "workflow_id": workflow["id"],
        "mode": "live",
        "count": 2,
        "concurrency": 2,
        "adapters": ["playwright"],
        "persistence": "persistent",
        "preparation": {"mode": "automated", "min_seconds": 2, "max_seconds": 2, "paths": ["/lab"]},
        "idempotency_key": "live-" + uuid.uuid4().hex,
    }
    evidence = []
    for generation in range(2):
        batch = post("/batches", template)
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline:
            rows = c.get("/sessions", params={"batch_id": batch["id"]}).json()["items"]
            if len(rows) == 2 and all(r["status"] in terminal for r in rows):
                break
            time.sleep(0.5)
        print(
            "Generation",
            generation + 1,
            [(r["status"], r["error"], r["metadata"].get("profile_saved")) for r in rows],
        )
        assert len(rows) == 2 and all(r["status"] == "completed" for r in rows), rows
        if generation:
            assert all(r["metadata"].get("input_profile_snapshot") for r in rows)
        evidence.append({"batch_id": batch["id"], "sessions": rows})
        template = {
            **template,
            "name": "Live persistence restore acceptance",
            "profile_ids": [r["profile_id"] for r in sorted(rows, key=lambda r: r["ordinal"])],
            "idempotency_key": "restore-" + uuid.uuid4().hex,
        }
    for r in rows:
        snapshots = c.get("/profiles/" + r["profile_id"] + "/snapshots").json()
        assert snapshots["total"] == 2, snapshots
    Path(".abrl/evidence").mkdir(parents=True, exist_ok=True)
    Path(".abrl/evidence/session-roundtrip.json").write_text(json.dumps(evidence, indent=2))
    print("Real Chromium session restore and exclusive snapshots verified")
