import asyncio
import json
import logging
import os
import random
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import dramatiq
import httpx
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import AsyncIO
from prometheus_client import Counter, Histogram
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import aliased

from abrl.browser import BrowserResult, execute_workflow
from abrl.config import CLICKHOUSE_PASSWORD, CLICKHOUSE_URL, DISPATCH_SECONDS, LEASE_SECONDS, REDIS_URL
from abrl.db import Attempt, Event, Run, Session, Target, Trial, WorkerHeartbeat, new_id, now, record
from abrl.telemetry import configure_logging

configure_logging()
log = logging.getLogger("abrl.runtime")
deliveries = Counter("abrl_queue_deliveries_total", "Workflow queue messages published")
trials_finished = Counter("abrl_trial_outcomes_total", "Logical trial outcomes", ["mode", "outcome"])
trial_time = Histogram("abrl_trial_duration_seconds", "Observed workflow duration")
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

broker = RedisBroker(url=REDIS_URL, namespace="abrl")
broker.add_middleware(AsyncIO())
dramatiq.set_broker(broker)


def relative_time(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def locked_run(session, run_id: str) -> Run | None:
    return session.scalar(select(Run).where(Run.id == run_id).with_for_update())


def lock_pair(session, trial_id: str):
    run_id = session.scalar(select(Trial.run_id).where(Trial.id == trial_id))
    if not run_id:
        return None, None
    run = locked_run(session, run_id)
    trial = session.scalar(select(Trial).where(Trial.id == trial_id).with_for_update())
    return run, trial


def touch_worker(session) -> None:
    entry = session.get(WorkerHeartbeat, WORKER_ID)
    if entry is None:
        session.add(WorkerHeartbeat(id=WORKER_ID, seen_at=now()))
    else:
        entry.seen_at = now()


def settle_run(session, run: Run) -> None:
    session.flush()
    count = session.scalar(
        select(func.count())
        .select_from(Trial)
        .where(Trial.run_id == run.id, Trial.status.in_(["queued", "running"]))
    )
    if not count and run.status in ("queued", "running", "paused", "cancelling"):
        run.status = "cancelled" if run.status == "cancelling" else "completed"
        run.finished_at = now()
        record(session, "run_" + run.status, run_id=run.id)


@dataclass
class Work:
    id: str
    run_id: str
    lease: str
    mode: str
    seed: int
    ordinal: int
    target: dict
    workflow: dict


def claim(trial_id: str) -> Work | None:
    with Session.begin() as session:
        touch_worker(session)
        run, trial = lock_pair(session, trial_id)
        if not run or not trial or trial.status != "queued":
            return None
        if run.status not in ("queued", "running"):
            trial.dispatched_at = None
            return None
        active = session.scalar(
            select(func.count()).select_from(Trial).where(Trial.run_id == run.id, Trial.status == "running")
        )
        if active:
            trial.dispatched_at = None
            return None
        if run.mode == "live":
            target = session.scalar(select(Target).where(Target.id == run.target_id).with_for_update())
            if (
                not target
                or target.version != run.target_version
                or target.verified_version != target.version
            ):
                trial.status, trial.outcome, trial.error = (
                    "finished",
                    "blocked",
                    "Target authorization has changed.",
                )
                record(session, "trial_blocked", run_id=run.id, trial_id=trial.id)
                settle_run(session, run)
                return None
            if target.last_started_at and target.last_started_at > relative_time(
                -60 / run.target["rate_limit"]
            ):
                trial.dispatched_at = None
                return None
            target.last_started_at = now()
        if run.status == "queued":
            record(session, "run_started", run_id=run.id)
        run.status = "running"
        trial.status = "running"
        trial.attempts += 1
        trial.lease_token = new_id()
        trial.lease_until = relative_time(LEASE_SECONDS)
        session.add(Attempt(id=trial.lease_token, trial_id=trial.id, number=trial.attempts))
        record(session, "trial_started", run_id=run.id, trial_id=trial.id, attempt=trial.attempts)
        return Work(
            trial.id, run.id, trial.lease_token, run.mode, run.seed, trial.ordinal, run.target, run.workflow
        )


def heartbeat(work: Work) -> bool:
    with Session.begin() as session:
        run, trial = lock_pair(session, work.id)
        if not run or not trial or trial.lease_token != work.lease or run.status == "cancelling":
            return True
        if work.mode == "live":
            target = session.get(Target, run.target_id)
            if (
                not target
                or target.version != run.target_version
                or target.verified_version != target.version
            ):
                return True
        touch_worker(session)
        trial.lease_until = relative_time(LEASE_SECONDS)
        return False


def finish(work: Work, result: BrowserResult, *, infrastructure_retry: bool = False) -> None:
    with Session.begin() as session:
        run, trial = lock_pair(session, work.id)
        if not run or not trial or trial.lease_token != work.lease:
            return
        touch_worker(session)
        attempt = session.get(Attempt, work.lease)
        outcome = "cancelled" if run.status == "cancelling" else result.outcome
        attempt.finished_at = now()
        attempt.outcome = outcome
        attempt.error = result.error
        attempt.elapsed_ms = result.elapsed_ms
        attempt.started_action = result.started_action
        trial.lease_token = trial.lease_until = trial.dispatched_at = None
        if (
            infrastructure_retry
            and not result.started_action
            and trial.attempts < 2
            and outcome == "failed"
        ):
            trial.status = "queued"
            record(session, "trial_retry_queued", run_id=run.id, trial_id=trial.id)
        else:
            trial.status = "cancelled" if outcome == "cancelled" else "finished"
            trial.outcome = outcome
            trial.error = result.error
            trial.elapsed_ms = result.elapsed_ms
            trial.metadata_json = result.metadata
            record(session, "trial_" + outcome, run_id=run.id, trial_id=trial.id)
            trials_finished.labels(work.mode, outcome).inc()
            if work.mode == "live":
                trial_time.observe(result.elapsed_ms / 1000)
            if work.mode == "live" and outcome == "blocked":
                session.execute(
                    update(Trial)
                    .where(
                        Trial.run_id == run.id,
                        Trial.status == "queued",
                    )
                    .values(status="cancelled", outcome="cancelled", dispatched_at=None)
                )
                run.status = "blocked"
                run.finished_at = now()
                record(session, "run_blocked", run_id=run.id)
            else:
                settle_run(session, run)


@dramatiq.actor(max_retries=0)
async def run_trial(trial_id: str) -> None:
    work = await asyncio.to_thread(claim, trial_id)
    if work is None:
        return

    async def cancelled() -> bool:
        return await asyncio.to_thread(heartbeat, work)

    retry = False
    if work.mode == "demo":
        await asyncio.sleep(0.06)
        rng = random.Random(f"{work.seed}:{work.ordinal}")
        sample = rng.random()
        outcome = "completed" if sample < 0.86 else "blocked" if sample < 0.93 else "failed"
        result = BrowserResult(
            outcome=outcome,
            elapsed_ms=rng.randint(280, 1800),
            error=None if outcome == "completed" else "Simulated " + outcome,
            metadata={"source": "simulated"},
            started_action=True,
        )
    else:
        try:
            result = await execute_workflow(
                origin=work.target["origin"],
                resource_origins=work.target["resource_origins"],
                steps=work.workflow["steps"],
                cancel=cancelled,
                timeout_seconds=60,
            )
            retry = result.outcome == "failed" and not result.started_action
        except Exception:
            log.exception("Unhandled workflow infrastructure error", extra={"trial_id": trial_id})
            result = BrowserResult(
                outcome="failed",
                elapsed_ms=0,
                error="Unexpected worker error",
                metadata={},
                started_action=True,
            )
    await asyncio.to_thread(finish, work, result, infrastructure_retry=retry)


@dramatiq.actor(max_retries=0)
def worker_ping() -> None:
    with Session.begin() as session:
        touch_worker(session)


def reap() -> None:
    with Session() as session:
        ids = session.scalars(
            select(Trial.id).where(Trial.status == "running", Trial.lease_until < now())
        ).all()
    for trial_id in ids:
        with Session.begin() as session:
            run, trial = lock_pair(session, trial_id)
            if not run or not trial or trial.status != "running" or trial.lease_until >= now():
                continue
            attempt = session.get(Attempt, trial.lease_token)
            if attempt:
                attempt.finished_at, attempt.outcome, attempt.error = (
                    now(),
                    "failed",
                    "Worker lease expired",
                )
            trial.status = "cancelled" if run.status == "cancelling" else "finished"
            trial.outcome = "cancelled" if trial.status == "cancelled" else "failed"
            trial.error = "Worker lost; action state unknown. The trial was not retried."
            trial.lease_until = trial.lease_token = None
            record(session, "trial_lease_expired", run_id=run.id, trial_id=trial.id)
            settle_run(session, run)


def dispatch() -> None:
    reap()
    with Session.begin() as session:
        earlier = aliased(Trial)
        active = aliased(Trial)
        waiting = session.scalars(
            select(Trial)
            .join(Run, Run.id == Trial.run_id)
            .where(
                Run.status.in_(["queued", "running"]),
                Trial.status == "queued",
                or_(Trial.dispatched_at.is_(None), Trial.dispatched_at < relative_time(-DISPATCH_SECONDS)),
                ~select(earlier.id)
                .where(
                    earlier.run_id == Trial.run_id,
                    earlier.status == "queued",
                    earlier.ordinal < Trial.ordinal,
                )
                .exists(),
                ~select(active.id)
                .where(active.run_id == Trial.run_id, active.status == "running")
                .exists(),
            )
            .order_by(Run.created_at, Trial.ordinal)
            .limit(12)
            .with_for_update(skip_locked=True, of=Trial)
        ).all()
        ids = []
        for trial in waiting:
            trial.dispatched_at = now()
            ids.append(trial.id)
    for trial_id in ids:
        run_trial.send(trial_id)
        deliveries.inc()


def forward_events() -> None:
    if not CLICKHOUSE_URL:
        return
    with Session.begin() as session:
        events = session.scalars(
            select(Event).where(Event.forwarded_at.is_(None)).order_by(Event.sequence).limit(200)
        ).all()
        if not events:
            return
        rows = "\n".join(
            compact_json(
                {
                    "sequence": event.sequence,
                    "run_id": event.run_id or "",
                    "kind": event.kind,
                    "detail": compact_json(event.detail),
                    "occurred_at": event.created_at,
                }
            )
            for event in events
        )
        response = httpx.post(
            CLICKHOUSE_URL,
            params={"query": "INSERT INTO abrl.events FORMAT JSONEachRow"},
            content=rows,
            auth=("abrl", CLICKHOUSE_PASSWORD),
            timeout=5,
            trust_env=False,
        )
        response.raise_for_status()
        for event in events:
            event.forwarded_at = now()


def compact_json(value: dict) -> str:
    return json.dumps(value, separators=(",", ":"))


async def scheduler() -> None:
    tick = 0
    while True:
        try:
            await asyncio.to_thread(dispatch)
            if tick % 10 == 0:
                await asyncio.to_thread(worker_ping.send)
        except Exception:
            log.warning(
                "Queue dispatch unavailable; pending trials remain durable", exc_info=tick % 30 == 0
            )
        if tick % 5 == 0:
            try:
                await asyncio.to_thread(forward_events)
            except Exception:
                log.warning(
                    "Analytics sink unavailable; retaining the event outbox", exc_info=tick % 30 == 0
                )
        tick += 1
        await asyncio.sleep(1)
