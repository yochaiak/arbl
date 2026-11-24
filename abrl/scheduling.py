"""Timezone-aware trigger calculation with durable occurrence materialization."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, or_, select

from abrl.config import CONTROLLED_ORIGIN
from abrl.network import FIXTURE_ORIGIN
from abrl.platform_models import Schedule, ScheduledOccurrence, SessionBatch
from abrl.platform_schemas import BatchInput
from abrl.platform_service import create_batch, uses_retired_lab_fixture


def schedule_retirement_reason(config):
    batch = config.get("batch")
    if not isinstance(batch, dict):
        return "Saved batch configuration is invalid. Review the schedule."
    if batch.get("mode") != "live":
        return "This schedule used simulated execution. Save a live configuration to resume."
    pinned = config.get("pinned")
    if (
        not isinstance(pinned, dict)
        or not isinstance(pinned.get("target"), dict)
        or not isinstance(pinned["target"].get("origin"), str)
        or not isinstance(pinned.get("workflow"), dict)
        or not isinstance(pinned["workflow"].get("steps"), list)
    ):
        return "Saved batch configuration is invalid. Review the schedule."
    if uses_retired_lab_fixture(pinned["target"], pinned["workflow"]):
        return "This schedule used the retired Lab site. Select an authorized site to resume."
    return None


def scheduled_batch_input(config, key):
    # Preexisting schedule revisions did not have a JavaScript capture setting.
    return BatchInput.model_validate({"capture_javascript": False, **config["batch"], "idempotency_key": key})


def dispatch_occurrence(db, schedule, occurrence, key):
    config = schedule.config
    try:
        with db.begin_nested():
            data = scheduled_batch_input(config, key)
            batch = create_batch(db, data, schedule.project_id, pinned=config["pinned"])
    except (KeyError, TypeError, ValidationError):
        occurrence.status, occurrence.reason = "skipped", "Saved batch configuration is invalid. Review the schedule."
    except HTTPException as exc:
        if exc.status_code not in {400, 404, 409, 422}:
            raise
        occurrence.status, occurrence.reason = "skipped", str(exc.detail)
    else:
        occurrence.batch_id, occurrence.status = batch.id, "dispatched"


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


def next_fire(config: dict, after: datetime) -> datetime | None:
    after = after.astimezone(UTC)
    start = parse_time(config.get("start_at")) or after
    end = parse_time(config.get("end_at"))
    if config["kind"] == "once":
        result = start if start > after else None
    elif config["kind"] == "interval":
        seconds = config["interval_seconds"]
        anchor = start.astimezone(UTC)
        count = max(0, int((after - anchor).total_seconds() // seconds) + 1)
        result = anchor + timedelta(seconds=count * seconds)
    else:
        # CronTrigger uses weekday names correctly. Normalize POSIX numeric days.
        parts = config["expression"].split()
        if len(parts) != 5:
            raise ValueError("Use a five-field cron expression")
        if any(c.isdigit() for c in parts[4]):
            raise ValueError("Use weekday names (mon-sun), ranges, or * instead of numeric weekdays")
        tz = ZoneInfo(config["timezone"])
        trigger = CronTrigger.from_crontab(" ".join(parts), timezone=tz)
        cursor = max(after + timedelta(microseconds=1), start.astimezone(UTC))
        result = None
        for _ in range(100):
            candidate = trigger.get_next_fire_time(None, cursor)
            if candidate is None:
                break
            absolute = candidate.astimezone(UTC)
            # Skip imaginary times and second repeated wall-clock occurrence.
            roundtrip = absolute.astimezone(tz)
            if (
                roundtrip.replace(tzinfo=None) == candidate.replace(tzinfo=None)
                and roundtrip.fold == 0
                and absolute > after
            ):
                result = absolute
                break
            cursor = absolute + timedelta(seconds=1)
    return result if result and (not end or result <= end.astimezone(UTC)) else None


def preview(config, start=None, count=5):
    after = start or datetime.now(UTC)
    output = []
    for _ in range(count):
        value = next_fire(config, after)
        if value is None:
            break
        output.append(
            {"utc": value.isoformat(), "local": value.astimezone(ZoneInfo(config["timezone"])).isoformat()}
        )
        after = value
    return output


def _occurrence_at(db, schedule, scheduled_for):
    return db.scalar(
        select(ScheduledOccurrence).where(
            ScheduledOccurrence.schedule_id == schedule.id,
            ScheduledOccurrence.version == schedule.version,
            ScheduledOccurrence.scheduled_for == scheduled_for,
        )
    )


def _active_batch(db, schedule_id):
    return db.scalar(
        select(SessionBatch.id)
        .join(ScheduledOccurrence, ScheduledOccurrence.batch_id == SessionBatch.id)
        .where(
            ScheduledOccurrence.schedule_id == schedule_id,
            SessionBatch.status.in_(("queued", "running", "paused", "cancelling")),
        )
        .limit(1)
    )


def _materialize_schedule(db, schedule, instant):
    config = schedule.config
    scheduled = parse_time(schedule.next_fire_at)
    # Bound the catch-up scan to 1,441 minute starts, regardless of downtime.
    horizon = instant - timedelta(seconds=config["grace_seconds"])
    if scheduled < horizon:
        candidate = next_fire(config, horizon - timedelta(microseconds=1))
        if candidate and candidate <= instant:
            scheduled = candidate
    for _ in range(1441):
        candidate = next_fire(config, scheduled)
        if not candidate or candidate > instant:
            break
        scheduled = candidate
    following = next_fire(config, instant)
    schedule.next_fire_at = following.isoformat() if following else None
    scheduled_for = scheduled.isoformat()
    if _occurrence_at(db, schedule, scheduled_for):
        return
    occurrence = ScheduledOccurrence(
        schedule_id=schedule.id,
        version=schedule.version,
        scheduled_for=scheduled_for,
        status="queued",
    )
    db.add(occurrence)
    active = _active_batch(db, schedule.id)
    late = (instant - scheduled).total_seconds()
    if late > config["grace_seconds"] or (config["misfire"] == "skip" and late > 60):
        occurrence.status, occurrence.reason = "skipped", "Missed execution outside grace policy"
    elif active and config["overlap"] == "skip":
        occurrence.status, occurrence.reason = "skipped", "Previous occurrence still active"
    elif active and config["overlap"] == "queue_one":
        queued = db.scalar(
            select(ScheduledOccurrence.id).where(
                ScheduledOccurrence.schedule_id == schedule.id, ScheduledOccurrence.status == "waiting"
            )
        )
        occurrence.status = "skipped" if queued else "waiting"
        occurrence.reason = "Previous occurrence still active"
    else:
        dispatch_occurrence(
            db, schedule, occurrence, f"schedule:{schedule.id}:{schedule.version}:{scheduled_for}"
        )


def materialize_due(db, instant=None):
    instant = instant or datetime.now(UTC)
    retired = db.scalars(
        select(Schedule).where(
            Schedule.enabled.is_(True),
            or_(
                func.coalesce(Schedule.config["batch"]["mode"].as_string(), "demo") != "live",
                Schedule.config["pinned"]["target"]["origin"].as_string().in_(
                    [origin for origin in (FIXTURE_ORIGIN, CONTROLLED_ORIGIN) if origin]
                ),
            ),
        ).with_for_update(skip_locked=True)
    ).all()
    for schedule in retired:
        if schedule_retirement_reason(schedule.config):
            schedule.enabled = False
            schedule.next_fire_at = None
    db.flush()
    rows = db.scalars(
        select(Schedule)
        .where(Schedule.enabled.is_(True), Schedule.next_fire_at <= instant.isoformat())
        .order_by(Schedule.next_fire_at)
        .limit(50)
        .with_for_update(skip_locked=True)
    ).all()
    for schedule in rows:
        scheduled_for = schedule.next_fire_at
        try:
            with db.begin_nested():
                _materialize_schedule(db, schedule, instant)
        except (KeyError, TypeError, ValueError):
            schedule.enabled = False
            schedule.next_fire_at = None
            if not _occurrence_at(db, schedule, scheduled_for):
                db.add(ScheduledOccurrence(
                    schedule_id=schedule.id,
                    version=schedule.version,
                    scheduled_for=scheduled_for,
                    status="skipped",
                    reason="Schedule timing is invalid. Review the schedule.",
                ))
    waiting = db.scalars(
        select(ScheduledOccurrence)
        .where(ScheduledOccurrence.status == "waiting")
        .limit(50)
        .with_for_update(skip_locked=True)
    ).all()
    for occurrence in waiting:
        schedule = db.get(Schedule, occurrence.schedule_id)
        retirement = schedule_retirement_reason(schedule.config)
        if retirement or not schedule.enabled or occurrence.version != schedule.version:
            occurrence.status, occurrence.reason = "skipped", retirement or "Schedule paused or changed"
            continue
        if _active_batch(db, schedule.id):
            continue
        try:
            overdue = (instant - parse_time(occurrence.scheduled_for)).total_seconds()
            grace_seconds = schedule.config["grace_seconds"]
        except (KeyError, TypeError, ValueError):
            occurrence.status, occurrence.reason = "skipped", "Schedule timing is invalid. Review the schedule."
            continue
        if overdue > grace_seconds:
            occurrence.status, occurrence.reason = "skipped", "Queued occurrence exceeded grace period"
            continue
        dispatch_occurrence(db, schedule, occurrence, f"schedule:{schedule.id}:{occurrence.id}")
