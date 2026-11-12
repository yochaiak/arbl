"""Independent release regressions for lifecycle fencing and durable controls.

These use isolated SQLite state and deterministic worker events; no live provider
credentials, browser actions, or production data are involved.
"""

import base64
from datetime import UTC, datetime, timedelta

import pytest
import test_platform
from sqlalchemy import select
from test_platform import client, create, data

from abrl import session_artifacts, session_runner
from abrl.browser import BrowserResult
from abrl.platform_models import (
    BrowserProfile,
    BrowserSession,
    ProviderObservation,
    Schedule,
    ScheduledOccurrence,
    SessionArtifact,
    SessionBatch,
    SessionCommand,
)
from abrl.platform_schemas import ScheduleInput
from abrl.platform_service import batch_snapshot
from abrl.research import comparison
from abrl.scheduling import materialize_due

# Reuse the isolated database fixture without importing a shadowed parameter.
db = test_platform.db


def admitted_work(factory, **changes):
    batch = create(factory, **changes)
    with factory.begin() as connection:
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard")
    assert work is not None
    return batch, work


def expire_runtime(factory, work):
    with factory.begin() as connection:
        connection.get(BrowserSession, work.id).lease_until = "2000-01-01T00:00:00+00:00"
        session_runner.reap_lost(connection)


def test_paused_batch_remains_paused_when_one_of_two_active_sessions_finishes(db):
    batch, first = admitted_work(db, count=3, concurrency=2)
    second = session_runner.claim("standard")
    assert second is not None
    with client() as api:
        assert api.post(f"/api/v1/batches/{batch.id}/pause").status_code == 200
    session_runner.finish(first, BrowserResult("completed", 1, None, {"runtime_stopped": True}, True))
    with db.begin() as connection:
        assert connection.get(SessionBatch, batch.id).status == "paused"
        session_runner.admission_tick(connection)
        queued = connection.scalar(select(BrowserSession).where(BrowserSession.status == "queued"))
        assert queued is not None and queued.admitted is False


def test_late_manual_frame_cannot_reopen_control_after_handoff(db):
    _, work = admitted_work(db)
    with db.begin() as connection:
        connection.get(BrowserSession, work.id).status = "awaiting_operator"
    endpoint = f"/api/v1/sessions/{work.id}/commands"
    with client() as api:
        assert (
            api.post(
                endpoint, json={"command": "claim_control", "idempotency_key": "claim-review"}
            ).status_code
            == 200
        )
        assert (
            api.post(
                endpoint, json={"command": "continue", "idempotency_key": "handoff-review"}
            ).status_code
            == 200
        )
        # A screenshot requested before the HTTP handoff may arrive afterwards.
        session_runner.record_activity(work, {"kind": "manual_frame", "jpeg_base64": "AQ=="})
        with db() as connection:
            assert connection.get(BrowserSession, work.id).status == "ready"
        assert (
            api.post(
                endpoint, json={"command": "claim_control", "idempotency_key": "late-claim-review"}
            ).status_code
            == 409
        )


def test_expired_runtime_cannot_consume_a_queued_manual_input(db):
    _, work = admitted_work(db)
    with db.begin() as connection:
        connection.get(BrowserSession, work.id).status = "awaiting_operator"
        connection.add(
            SessionCommand(
                session_id=work.id,
                epoch=work.epoch,
                idempotency_key="queued-click-review",
                payload={"command": "click", "x": 100, "y": 120},
                actor_id="local-owner",
            )
        )
    expire_runtime(db, work)
    assert session_runner.poll_control(work) == {"command": "stop"}


def test_quarantined_runtime_still_reserves_project_admission_capacity(db):
    from abrl.platform_models import Project

    with db.begin() as connection:
        connection.get(Project, "default").concurrency = 1
    _, work = admitted_work(db, count=2, concurrency=2)
    expire_runtime(db, work)
    with db.begin() as connection:
        assert connection.get(BrowserProfile, work.profile_id).status == "quarantined"
        assert connection.get(BrowserProfile, work.profile_id).owner_session_id == work.id
        session_runner.admission_tick(connection)
        queued = connection.scalar(select(BrowserSession).where(BrowserSession.status == "queued"))
        assert queued is not None and queued.admitted is False, (
            "Unconfirmed old runtime must retain capacity"
        )


def test_skip_overlap_waits_for_cancelling_batch_to_stop(db):
    prior, work = admitted_work(db)
    instant = datetime.now(UTC)
    with db.begin() as connection:
        connection.get(SessionBatch, prior.id).status = "cancelling"
        connection.get(BrowserSession, work.id).status = "stopping"
        batch = data()
        config = ScheduleInput(
            name="Overlap safety",
            kind="interval",
            interval_seconds=60,
            start_at=instant - timedelta(minutes=1),
            batch=batch,
            overlap="skip",
        ).model_dump(mode="json")
        config["pinned"] = batch_snapshot(connection, batch, "default")
        schedule = Schedule(
            project_id="default", name="Overlap safety", config=config, next_fire_at=instant.isoformat()
        )
        connection.add(schedule)
        connection.flush()
        connection.add(
            ScheduledOccurrence(
                schedule_id=schedule.id,
                version=1,
                scheduled_for=(instant - timedelta(minutes=1)).isoformat(),
                status="dispatched",
                batch_id=prior.id,
            )
        )
        connection.flush()
        materialize_due(connection, instant + timedelta(seconds=1))
        latest = connection.scalar(
            select(ScheduledOccurrence)
            .where(ScheduledOccurrence.schedule_id == schedule.id)
            .order_by(ScheduledOccurrence.scheduled_for.desc())
        )
        assert latest.status == "skipped" and latest.batch_id is None


@pytest.fixture
def artifact_store(db, monkeypatch, tmp_path):
    monkeypatch.setattr(session_artifacts, "Session", db)
    monkeypatch.setattr(session_artifacts, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(session_artifacts, "storage", lambda: None)
    monkeypatch.setenv("ABRL_ARTIFACT_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"r" * 32).decode())
    return tmp_path


def artifact_payload():
    return {
        "name": "review.txt",
        "media_type": "text/plain",
        "data_base64": base64.b64encode(b"isolated review fixture").decode(),
    }


def test_expired_quarantined_attempt_cannot_publish_artifact(db, artifact_store):
    _, work = admitted_work(db)
    expire_runtime(db, work)
    with pytest.raises(ValueError, match="expired|fenc|lease|active"):
        session_artifacts.store_artifact(work, artifact_payload())
    with db() as connection:
        assert connection.scalar(select(SessionArtifact)) is None
    assert list(artifact_store.rglob("*.aes")) == []


def test_rejected_stale_artifact_leaves_no_untracked_ciphertext(db, artifact_store):
    _, work = admitted_work(db)
    with db.begin() as connection:
        connection.get(BrowserSession, work.id).lease_token = "replacement-lease"
    with pytest.raises(ValueError, match="expired|fenc|lease|active"):
        session_artifacts.store_artifact(work, artifact_payload())
    assert list(artifact_store.rglob("*.aes")) == [], (
        "Rejected ciphertext would escape metadata-based retention"
    )


def test_failed_artifact_metadata_commit_removes_uploaded_ciphertext(db, artifact_store, monkeypatch):
    _, work = admitted_work(db)

    def unavailable_event(*args, **kwargs):
        raise RuntimeError("Injected metadata failure after encrypted upload")

    monkeypatch.setattr(session_artifacts, "event", unavailable_event)
    with pytest.raises(RuntimeError, match="Injected metadata failure"):
        session_artifacts.store_artifact(work, artifact_payload())
    with db() as connection:
        assert connection.scalar(select(SessionArtifact)) is None
    assert list(artifact_store.rglob("*.aes")) == []


def test_completed_workflow_remains_complete_when_verified_gate_denies_action(db):
    batch, work = admitted_work(db)
    with db.begin() as connection:
        stored_batch = connection.get(SessionBatch, batch.id)
        stored_batch.status = "completed"
        stored_batch.config = {
            **stored_batch.config,
            "target": {**stored_batch.config["target"], "mode": "instrumented"},
        }
        session = connection.get(BrowserSession, work.id)
        session.status = "blocked"
        session.outcome = "blocked"
        session.workflow_outcome = "completed"
        connection.add(
            ProviderObservation(
                project_id="default",
                session_id=work.id,
                attempt_id=work.lease,
                idempotency_key="negative-review",
                verification_status="verified",
                verdict="Bot",
                raw_code=3,
                accepted=False,
                source="controlled_site",
                provider_version="5",
            )
        )
    with db() as connection:
        result = comparison(connection, connection.get(SessionBatch, batch.id))
        assert result["trials"][0]["workflow_completed"] is True
        assert result["trials"][0]["success"] is False
        assert result["groups"][0]["completion_rate"] == 1
