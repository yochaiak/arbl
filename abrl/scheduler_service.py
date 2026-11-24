"""Single leader scheduling, admission, recovery and durable outbox delivery."""

import asyncio
import logging

from sqlalchemy import text

from abrl.db import Session, engine
from abrl.runtime import dispatch, forward_events, worker_ping
from abrl.scheduling import materialize_due
from abrl.session_artifacts import expire_artifacts
from abrl.session_outbox import forward_session_events
from abrl.session_runner import admission_tick, reap_lost

log = logging.getLogger("abrl.scheduler")


def tick():
    with Session.begin() as db:
        materialize_due(db)
    with Session.begin() as db:
        reap_lost(db)
        admission_tick(db, require_worker_heartbeat=True)


async def serve():
    while True:
        try:
            # Session-level lock uses a dedicated connection throughout leadership.
            with engine.connect() as connection:
                postgres = engine.dialect.name == "postgresql"
                if postgres and not connection.scalar(text("SELECT pg_try_advisory_lock(8473002)")):
                    await asyncio.sleep(3)
                    continue
                try:
                    count = 0
                    while True:
                        if postgres:
                            connection.execute(text("SELECT 1"))
                        await asyncio.to_thread(tick)
                        try:
                            await asyncio.to_thread(dispatch)
                            if count % 10 == 0:
                                await asyncio.to_thread(worker_ping.send)
                            if count % 5 == 0:
                                await asyncio.to_thread(forward_events)
                                await asyncio.to_thread(forward_session_events)
                        except Exception:
                            log.warning("Queue/analytics unavailable; PostgreSQL retains pending work")
                        if count % 60 == 0:
                            await asyncio.to_thread(expire_artifacts)
                        count += 1
                        await asyncio.sleep(1)
                finally:
                    if postgres and not connection.invalidated:
                        connection.execute(text("SELECT pg_advisory_unlock(8473002)"))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Scheduler leadership interrupted")
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(serve())
