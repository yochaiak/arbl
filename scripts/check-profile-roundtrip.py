"""Verify Chromium profile persistence against the owned local fixture."""

import json
import time
import uuid
from pathlib import Path

import httpx

from abrl.config import API_ORIGIN, OPERATOR_TOKEN

TERMINAL = {"completed", "failed", "blocked", "cancelled", "needs_reconciliation"}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def batch_sessions(api, batch_id):
    deadline = time.monotonic() + 70
    while time.monotonic() < deadline:
        response = api.get("/sessions", params={"batch_id": batch_id})
        rows = response.raise_for_status().json()["items"]
        if len(rows) == 2 and all(row["status"] in TERMINAL for row in rows):
            return sorted(rows, key=lambda row: row["ordinal"])
        time.sleep(0.5)
    raise RuntimeError("Timed out waiting for both fixture sessions. Check the scheduler and standard runner.")


def profile_snapshots(api, profile_id):
    response = api.get(f"/profiles/{profile_id}/snapshots")
    return response.raise_for_status().json()["items"]


def verify_roundtrip(api, target_id):
    def post(path, payload):
        return api.post(path, json=payload).raise_for_status().json()

    launch = {
        "name": "Chromium profile roundtrip",
        "target_id": target_id,
        "count": 2,
        "concurrency": 2,
        "adapters": ["playwright"],
        "persistence": "persistent",
        "artifact_policy": "none",
        "capture_javascript": False,
    }
    profiles = []
    initial_snapshots = {}
    evidence = []
    for visit in (1, 2):
        workflow = post("/workflows", {
            "name": f"Chromium profile roundtrip visit {visit}",
            "steps": [
                {"action": "navigate", "url": "/lab/repeat?persistent=1"},
                {"action": "assert_text", "selector": "#visit-count", "value": str(visit)},
            ],
        })
        batch = post("/batches", {
            **launch, "workflow_id": workflow["id"], "profile_ids": profiles,
            "idempotency_key": "profile-roundtrip-" + uuid.uuid4().hex,
        })
        rows = batch_sessions(api, batch["id"])
        statuses = ", ".join(row["status"] for row in rows)
        require(all(row["status"] == "completed" for row in rows), f"Visit {visit} did not complete ({statuses}).")
        assigned = [row["profile_id"] for row in rows]
        require(len(set(assigned)) == 2, "Expected two independent Chromium profiles.")
        require(visit == 1 or assigned == profiles, "Restore batch used a different profile assignment.")
        profiles = assigned

        sessions = []
        for row in rows:
            profile_id = row["profile_id"]
            metadata = row["metadata"]
            require(metadata.get("profile_saved") is True, f"Visit {visit} did not save its profile.")
            snapshots = profile_snapshots(api, profile_id)
            generations = [snapshot["generation"] for snapshot in snapshots]
            require(generations == list(range(visit, 0, -1)), f"Unexpected snapshot generations on visit {visit}.")
            restored = metadata.get("input_profile_snapshot")
            if visit == 1:
                require(not restored, "Initial launch unexpectedly restored a snapshot.")
                initial_snapshots[profile_id] = snapshots[0]["checksum"]
            else:
                require(metadata.get("profile_launch_state") == "restored", "Chromium did not confirm profile restore.")
                require(
                    restored and restored.get("checksum") == initial_snapshots[profile_id] == snapshots[1]["checksum"],
                    "The resumed session did not use the snapshot saved by its initial launch.",
                )
            sessions.append({
                "session_id": row["id"], "profile_id": profile_id,
                "visit_count": visit, "snapshot_generations": generations,
            })
        evidence.append({"batch_id": batch["id"], "sessions": sessions})
        print(f"Visit {visit} confirmed for both profiles with snapshot generation {visit}.")
    return evidence


def main():
    require(OPERATOR_TOKEN, "Set the local operator token and start make dev-fixtures.")
    with httpx.Client(
        base_url=f"{API_ORIGIN}/api/v1",
        headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
        timeout=15,
        trust_env=False,
    ) as api:
        fixture = api.post(f"{API_ORIGIN}/test/seed")
        require(fixture.status_code != 404, "Start make dev-fixtures before running make profile-test.")
        target_id = fixture.raise_for_status().json()["target_id"]
        evidence = verify_roundtrip(api, target_id)
    path = Path(".abrl/evidence/session-roundtrip.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2) + "\n")
    print("Chromium save and restore verified for two independent profiles.")


if __name__ == "__main__":
    try:
        main()
    except (httpx.HTTPError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None
