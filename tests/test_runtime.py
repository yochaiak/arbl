from datetime import UTC, datetime, timedelta
from inspect import unwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from abrl import runtime
from abrl.browser import BrowserResult
from abrl.db import Attempt, Base, Run, Target, Trial, Workflow
from abrl.reports import report, wilson
from abrl.schemas import Step, TargetInput


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessionmaker_ = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(runtime, "Session", sessionmaker_)
    with sessionmaker_.begin() as session:
        target = Target(verified_version=1, proof_token="test")
        workflow = Workflow()
        session.add_all([target, workflow])
        session.flush()
        run = Run(
            target_id=target.id,
            workflow_id=workflow.id,
            target_version=1,
            workflow_version=1,
            target={
                "name": "Local fixture",
                "origin": "https://fixture.example",
                "mode": "browser_only",
                "session_policy": "fresh",
                "rate_limit": 60,
            },
            workflow={"name": "Smoke"},
            phase="confirmation",
            mode="demo",
            repetitions=2,
            seed=42,
        )
        session.add(run)
        session.flush()
        first, second = Trial(run_id=run.id, ordinal=1), Trial(run_id=run.id, ordinal=2)
        session.add_all([first, second])
        session.flush()
        ids = run.id, first.id, second.id
    yield sessionmaker_, ids
    engine.dispose()


def result(outcome, *, started=True):
    return BrowserResult(outcome=outcome, elapsed_ms=10, error=None, metadata={}, started_action=started)


@pytest.mark.parametrize(("wait_ms", "timeout_seconds"), [(None, 60), (60_000, 120)])
async def test_preview_timeout_includes_the_configured_navigation_wait(monkeypatch, wait_ms, timeout_seconds):
    steps = [{"action": "navigate", "url": "/page", "post_navigation_wait_ms": wait_ms}]
    work = SimpleNamespace(
        mode="live",
        target={"origin": "https://app.example", "resource_origins": []},
        workflow={"steps": steps},
    )
    completed = result("completed")
    execute = AsyncMock(return_value=completed)
    finish = Mock()
    monkeypatch.setattr(runtime, "claim", lambda _: work)
    monkeypatch.setattr(runtime, "execute_workflow", execute)
    monkeypatch.setattr(runtime, "finish", finish)

    await unwrap(runtime.run_trial.fn)("trial")

    assert execute.await_args.kwargs["steps"] == steps
    assert execute.await_args.kwargs["timeout_seconds"] == timeout_seconds
    finish.assert_called_once_with(work, completed, infrastructure_retry=False)


def test_redelivery_and_retry_keep_one_logical_observation(database):
    sessions, (run_id, first, second) = database
    original = runtime.claim(first)
    assert original and not runtime.claim(first)
    runtime.finish(original, result("failed", started=False), infrastructure_retry=True)
    retry = runtime.claim(first)
    assert retry and retry.lease != original.lease
    runtime.finish(original, result("completed"))  # A late result has no lease.
    runtime.finish(retry, result("completed"))
    runtime.finish(runtime.claim(second), result("failed"))
    with sessions() as session:
        summary = report(session, session.get(Run, run_id))
        assert summary["counts"]["planned"] == 2
        assert summary["completion"]["successes"] == 1
        assert summary["completion"]["total"] == 2
        assert summary["first_attempt"]["successes"] == 0
        assert summary["recommendation"]["status"] == "simulated"
        assert len(session.scalars(select(Attempt)).all()) == 3


def test_worker_loss_preserves_completed_results(database):
    sessions, (run_id, first, second) = database
    runtime.finish(runtime.claim(first), result("completed"))
    lost = runtime.claim(second)
    with sessions.begin() as session:
        session.get(Trial, second).lease_until = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    runtime.reap()
    assert runtime.claim(second) is None
    runtime.finish(lost, result("completed"))
    with sessions() as session:
        assert session.get(Trial, first).outcome == "completed"
        assert session.get(Trial, second).outcome == "failed"
        assert session.get(Run, run_id).status == "completed"


def test_wilson_and_validation():
    assert wilson(0, 0)["low"] is None
    estimate = wilson(50, 100)
    assert estimate["low"] == pytest.approx(0.4038315)
    assert estimate["high"] == pytest.approx(0.5961685)
    assert wilson(300, 300)["low"] > 0.95
    with pytest.raises(ValueError, match="site-relative"):
        Step(action="navigate", url="//elsewhere.test")
    with pytest.raises(ValueError, match="exactly one"):
        Step(action="fill", selector="#sample", value="literal", secret_ref="example")
    with pytest.raises(ValueError):
        TargetInput(name="Invalid", origin="http://localhost:5432", authorized=True)


def test_live_access_denial_ends_run_without_dispatching_remaining_trials(database):
    sessions, (run_id, first, second) = database
    with sessions.begin() as session:
        session.get(Run, run_id).mode = "live"
    work = runtime.claim(first)
    assert work
    runtime.finish(work, result("blocked"))
    assert runtime.claim(second) is None
    with sessions() as session:
        run = session.get(Run, run_id)
        assert run.status == "blocked"
        summary = report(session, run)
        assert summary["counts"]["blocked"] == 1
        assert summary["counts"]["cancelled"] == 1
        assert summary["recommendation"]["status"] == "incomplete"
