import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_platform
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from test_platform import create, data

from abrl import platform_service, session_runner
from abrl.browser import BrowserResult
from abrl.db import Target, TargetVersion, now
from abrl.platform_models import (
    BrowserProfile,
    BrowserSession,
    FleetSlot,
    Schedule,
    ScheduledOccurrence,
    SessionAttempt,
    SessionBatch,
    SessionEvent,
)
from abrl.platform_schemas import ScheduleInput
from abrl.scheduling import materialize_due

db = test_platform.db


def claim_session(db, **changes):
    create(db, **changes)
    with db.begin() as connection:
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard")
    assert work is not None
    return work


def test_stop_blocks_progress_actions_and_retries(db):
    work = claim_session(db)
    with db.begin() as connection:
        connection.get(BrowserSession, work.id).status = "stopping"

    session_runner.record_activity(work, {"kind": "workflow_started"})
    with pytest.raises(RuntimeError, match="Session is stopping"):
        session_runner.record_activity(work, {"kind": "action_started", "action": "click"})
    session_runner.finish(work, BrowserResult(
        "failed", 1, "Transient startup failed", {"runtime_stopped": True, "failure_kind": "startup_transient"},
        False,
    ))
    with db() as connection:
        session = connection.get(BrowserSession, work.id)
        attempt = connection.get(SessionAttempt, work.lease)
        assert session.status == attempt.outcome == "cancelled"
        assert session.next_attempt_at is None and not attempt.started_action
        assert connection.get(BrowserProfile, work.profile_id).owner_session_id is None


@pytest.mark.parametrize("already_reaped", [False, True])
def test_late_same_epoch_shutdown_preserves_quarantine(db, already_reaped):
    work = claim_session(db)
    session_runner.record_activity(work, {"kind": "action_started", "action": "click"})
    with db.begin() as connection:
        connection.get(BrowserSession, work.id).lease_until = platform_service.utc_after(-5)
        if already_reaped:
            session_runner.reap_lost(connection)
    late = BrowserResult("completed", 1, None, {"runtime_stopped": True}, True)
    session_runner.finish(work, late)
    session_runner.finish(work, late)
    with db() as connection:
        session = connection.get(BrowserSession, work.id)
        profile = connection.get(BrowserProfile, work.profile_id)
        attempt = connection.get(SessionAttempt, work.lease)
        assert session.status == attempt.outcome == "needs_reconciliation"
        assert profile.status == "quarantined" and profile.owner_session_id == work.id
        assert session.metadata_json["late_shutdown_confirmed"]
        evidence = list(connection.scalars(
            select(SessionEvent).where(
                SessionEvent.session_id == work.id, SessionEvent.kind == "late_runtime_shutdown_confirmed"
            )
        ))
        assert len(evidence) == 1


def test_startup_retries_are_durable_and_bounded(db):
    work = claim_session(db)
    for attempt in range(1, 4):
        session_runner.finish(work, BrowserResult(
            "failed", 1, "Transient startup failed",
            {"runtime_stopped": True, "failure_kind": "startup_transient"}, False,
        ))
        with db.begin() as connection:
            row = connection.get(BrowserSession, work.id)
            assert row.attempt_number == attempt
            if attempt == 3:
                assert row.status == "failed" and row.next_attempt_at is None
                break
            assert row.status == "queued" and row.queue_reason == "retry_backoff"
            assert row.next_attempt_at > now()
            connection.get(Target, "target").last_started_at = None
            session_runner.admission_tick(connection)
            assert not row.admitted
        assert session_runner.claim("standard") is None
        with db.begin() as connection:
            connection.get(BrowserSession, work.id).next_attempt_at = platform_service.utc_after(-1)
            session_runner.admission_tick(connection)
        work = session_runner.claim("standard")
        assert work is not None


def test_durable_mutation_marker_blocks_recovery_when_result_omits_it(db):
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "origin": "https://fixture.example"}
    work = claim_session(db)
    session_runner.record_activity(work, {"kind": "action_started", "action": "click", "non_idempotent": True})
    session_runner.finish(work, BrowserResult(
        "failed", 1, "Browser lost connection",
        {
            "runtime_stopped": True,
            "failure_kind": "transport",
            "transport_summary": {"attempts": 1, "successes": 0, "failures": 1},
        },
        False,
    ))
    with db() as connection:
        row = connection.get(BrowserSession, work.id)
        assert row.status == "needs_reconciliation" and row.next_attempt_at is None
        assert connection.get(SessionAttempt, work.lease).started_action


def test_corrupt_profile_download_does_not_publish_cache(monkeypatch, tmp_path):
    work = SimpleNamespace(
        lease="attempt", profile_id="profile", snapshot={
            "generation": "1-1", "key": "private-reference", "checksum": "invalid-checksum",
            "environment": {
                "os": session_runner.platform.system(), "architecture": session_runner.platform.machine(),
            },
        },
    )
    calls = []

    def download(_bucket, _key, pending):
        calls.append(pending)
        Path(pending).write_bytes(b"corrupt download")

    monkeypatch.setattr(session_runner, "profile_s3", lambda: SimpleNamespace(download_file=download))
    store = SimpleNamespace(root=tmp_path, restore=lambda *_, **__: pytest.fail("Checksum was not verified"))
    with pytest.raises(ValueError, match="checksum does not match"):
        session_runner._restore_profile(work, store, tmp_path / "runtime")
    assert len(calls) == 1
    assert list((tmp_path / "profile").iterdir()) == []


@pytest.mark.parametrize("finalization_fails", [False, True])
async def test_missing_profile_archive_fails_before_launch_and_cleans_workdir(
    db, monkeypatch, tmp_path, finalization_fails,
):
    work = claim_session(db, persistence="persistent")
    work.snapshot = {
        "generation": "1-1", "checksum": "checksum",
        "environment": {"os": session_runner.platform.system(), "architecture": session_runner.platform.machine()},
    }
    directory = tmp_path / "work" / work.id / work.lease
    directory.mkdir(parents=True)
    directory.joinpath("partial-profile").touch()
    launch = AsyncMock()
    monkeypatch.setattr(session_runner, "WORK_ROOT", tmp_path / "work")
    monkeypatch.setattr(session_runner, "snapshot_store", lambda: SimpleNamespace(root=tmp_path / "archives"))
    monkeypatch.setattr(session_runner, "profile_s3", lambda: None)
    monkeypatch.setattr(session_runner, "execute_workflow", launch)

    if finalization_fails:
        monkeypatch.setattr(session_runner, "_finalize_work", AsyncMock(side_effect=RuntimeError("Database offline")))
        with pytest.raises(RuntimeError, match="Database offline"):
            await session_runner.execute(work)
    else:
        await session_runner.execute(work)
        with db() as connection:
            row = connection.get(BrowserSession, work.id)
            profile = connection.get(BrowserProfile, work.profile_id)
            assert row.status == "failed" and row.workflow_outcome == "not_started"
            assert row.metadata_json["step_failure"]["reason"] == "profile_restore_failed"
            assert row.metadata_json["step_failure"]["phase"] == "profile"
            assert row.metadata_json["lifecycle_timeline"]["events"][0]["event"] == "failure_observed"
            assert row.error == "The saved profile snapshot file is missing"
            assert row.next_attempt_at is None and profile.owner_session_id is None
    launch.assert_not_called()
    assert not directory.exists()


@pytest.mark.asyncio
async def test_finalization_retries_only_transient_database_errors(monkeypatch):
    attempts = []

    class Deadlock(Exception):
        sqlstate = "40P01"

    def finish(*args, **kwargs):
        attempts.append((args, kwargs))
        if len(attempts) == 1:
            raise OperationalError(None, None, Deadlock())

    monkeypatch.setattr(session_runner, "finish", finish)
    result = BrowserResult("completed", 1, None, {"recording_status": "ready"}, False)
    await session_runner._finalize_work("assignment", result, None, None)
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert result.metadata["recording_status"] == "ready"

    def configuration_failure(*_args, **_kwargs):
        raise OperationalError(None, None, RuntimeError())

    monkeypatch.setattr(session_runner, "finish", configuration_failure)
    with pytest.raises(OperationalError):
        await session_runner._finalize_work("assignment", result, None, None)


def test_large_queue_checks_adapter_and_worker_capacity_once_per_pass(db, monkeypatch):
    probes = []

    def discover():
        probes.append(True)
        return [{"id": "playwright", "available": True}]

    monkeypatch.setattr(platform_service, "execution_capabilities", discover)
    create(db, count=50, concurrency=8)
    with db.begin() as connection:
        session_runner.admission_tick(connection, require_worker_heartbeat=True)
        queued = list(connection.scalars(select(BrowserSession)))
        assert {row.queue_reason for row in queued} == {"worker_unavailable"}
        assert not any(row.admitted for row in queued)
        connection.add(FleetSlot(id="worker", pool="standard", capacity=2, seen_at=now()))
        session_runner.admission_tick(connection, require_worker_heartbeat=True)
        assert sum(row.admitted for row in queued) == 2
        assert {row.queue_reason for row in queued if not row.admitted} == {"worker_capacity"}
    assert len(probes) == 2


def test_scheduler_skips_bad_occurrences_and_deduplicates_delivery(db):
    scheduled = datetime.now(UTC)
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "origin": "https://schedule.example"}
        batch = data()
        config = ScheduleInput(
            name="Scheduled", kind="once", start_at=scheduled, batch=batch,
        ).model_dump(mode="json")
        config["pinned"] = platform_service.batch_snapshot(connection, batch, "default")
        schedules = {
            "bad-batch": {**config, "batch": {**config["batch"], "count": 0}},
            "bad-time": {**config, "start_at": "invalid"},
            "valid": config,
        }
        for identifier, definition in schedules.items():
            connection.add(Schedule(
                id=identifier, project_id="default", name=identifier, config=definition,
                next_fire_at=scheduled.isoformat(),
            ))
        materialize_due(connection, scheduled + timedelta(seconds=1))
        connection.get(Schedule, "valid").next_fire_at = scheduled.isoformat()
        materialize_due(connection, scheduled + timedelta(seconds=1))
    with db() as connection:
        occurrences = {row.schedule_id: row for row in connection.scalars(select(ScheduledOccurrence))}
        assert {name: row.status for name, row in occurrences.items()} == {
            "bad-batch": "skipped", "bad-time": "skipped", "valid": "dispatched",
        }
        assert "configuration is invalid" in occurrences["bad-batch"].reason
        assert "timing is invalid" in occurrences["bad-time"].reason
        assert len(list(connection.scalars(select(SessionBatch)))) == 1
        assert len(list(connection.scalars(select(ScheduledOccurrence)))) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_poll", ["fleet_touch", "claim"])
async def test_worker_keeps_draining_after_poll_and_task_errors(monkeypatch, caplog, failed_poll):
    work_items = [SimpleNamespace(id="failed-session"), SimpleNamespace(id="successful-session")]
    pending = iter(work_items)
    executed = []
    poll_failed = False
    ticks = 0

    def fail_once(operation):
        nonlocal poll_failed
        if operation == failed_poll and not poll_failed:
            poll_failed = True
            raise OperationalError("private polling statement", {}, RuntimeError("private polling failure"))

    def fleet_touch(pool, capacity):
        assert (pool, capacity) == ("standard", 1)
        fail_once("fleet_touch")
        return ["playwright"]

    def claim(pool, *, supported_adapters):
        assert pool == "standard"
        assert supported_adapters == ["playwright"]
        fail_once("claim")
        return next(pending, None)

    async def execute(work):
        executed.append(work.id)
        if work is work_items[0]:
            raise RuntimeError("private session finalization failure")
        session_runner.STOPPING = True

    async def call_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def next_tick(_):
        nonlocal ticks
        ticks += 1
        assert ticks <= 20, "The worker did not drain the queue"
        await asyncio.sleep(0)

    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_: None)
    monkeypatch.setattr(session_runner, "STOPPING", False)
    monkeypatch.setattr(session_runner, "fleet_touch", fleet_touch)
    monkeypatch.setattr(session_runner, "claim", claim)
    monkeypatch.setattr(session_runner, "execute", execute)
    monkeypatch.setattr(
        session_runner,
        "asyncio",
        SimpleNamespace(
            get_running_loop=asyncio.get_running_loop,
            to_thread=call_inline,
            create_task=asyncio.create_task,
            sleep=next_tick,
            timeout=asyncio.timeout,
            gather=asyncio.gather,
        ),
    )
    caplog.set_level(logging.WARNING, logger=session_runner.log.name)

    await session_runner.run(once=False, capacity=1)

    assert poll_failed
    assert executed == ["failed-session", "successful-session"]
    assert "OperationalError" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "private polling statement" not in caplog.text
    assert "private polling failure" not in caplog.text
    assert "private session finalization failure" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
