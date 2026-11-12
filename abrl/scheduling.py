"""Timezone-aware trigger calculation with durable occurrence materialization."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from abrl.platform_models import Schedule, ScheduledOccurrence, SessionBatch
from abrl.platform_schemas import BatchInput
from abrl.platform_service import create_batch


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


def materialize_due(db, instant=None):
    instant = instant or datetime.now(UTC)
    rows = db.scalars(
        select(Schedule)
        .where(Schedule.enabled.is_(True), Schedule.next_fire_at <= instant.isoformat())
        .order_by(Schedule.next_fire_at)
        .limit(50)
        .with_for_update(skip_locked=True)
    ).all()
    for schedule in rows:
        config = schedule.config
        scheduled = parse_time(schedule.next_fire_at)
        # Ignore old backlog outside the grace window. At most 1,441 minute-based
        # occurrences fit the maximum grace, independent of scheduler downtime.
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
        occurrence = ScheduledOccurrence(
            schedule_id=schedule.id,
            version=schedule.version,
            scheduled_for=scheduled.isoformat(),
            status="queued",
        )
        db.add(occurrence)
        active = db.scalar(
            select(SessionBatch.id)
            .join(ScheduledOccurrence, ScheduledOccurrence.batch_id == SessionBatch.id)
            .where(
                ScheduledOccurrence.schedule_id == schedule.id,
                SessionBatch.status.in_(("queued", "running", "paused", "cancelling")),
            )
            .limit(1)
        )
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
            data = BatchInput.model_validate(
                {
                    **config["batch"],
                    "idempotency_key": f"schedule:{schedule.id}:{schedule.version}:{scheduled.isoformat()}",
                }
            )
            pinned = {**config["pinned"], **data.model_dump(mode="json")}
            batch = create_batch(db, data, schedule.project_id, pinned=pinned)
            occurrence.batch_id, occurrence.status = batch.id, "dispatched"
        following = next_fire(config, instant)
        schedule.next_fire_at = following.isoformat() if following else None
        # next_fire=None marks natural expiration, not an explicit pause.
        # A final queued occurrence can therefore still run.
    waiting = db.scalars(
        select(ScheduledOccurrence)
        .where(ScheduledOccurrence.status == "waiting")
        .limit(50)
        .with_for_update(skip_locked=True)
    ).all()
    for occurrence in waiting:
        schedule = db.get(Schedule, occurrence.schedule_id)
        active = db.scalar(
            select(SessionBatch.id)
            .join(ScheduledOccurrence, ScheduledOccurrence.batch_id == SessionBatch.id)
            .where(
                ScheduledOccurrence.schedule_id == schedule.id,
                SessionBatch.status.in_(("queued", "running", "paused", "cancelling")),
            )
            .limit(1)
        )
        if active:
            continue
        if (instant - parse_time(occurrence.scheduled_for)).total_seconds() > schedule.config[
            "grace_seconds"
        ]:
            occurrence.status, occurrence.reason = "skipped", "Queued occurrence exceeded grace period"
            continue
        if not schedule.enabled or occurrence.version != schedule.version:
            occurrence.status, occurrence.reason = "skipped", "Schedule paused or changed"
            continue
        data = BatchInput.model_validate(
            {**schedule.config["batch"], "idempotency_key": f"schedule:{schedule.id}:{occurrence.id}"}
        )
        batch = create_batch(
            db,
            data,
            schedule.project_id,
            pinned={**schedule.config["pinned"], **data.model_dump(mode="json")},
        )
        occurrence.batch_id, occurrence.status = batch.id, "dispatched"
