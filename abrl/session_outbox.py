"""At-least-once analytics delivery; PostgreSQL remains the authoritative event source."""

import json

import httpx
from sqlalchemy import select

from abrl.config import CLICKHOUSE_PASSWORD, CLICKHOUSE_URL
from abrl.db import Session, now
from abrl.platform_models import SessionEvent


def forward_session_events():
    if not CLICKHOUSE_URL:
        return
    with Session.begin() as db:
        records = list(
            db.scalars(
                select(SessionEvent)
                .where(SessionEvent.forwarded_at.is_(None))
                .order_by(SessionEvent.sequence)
                .limit(500)
                .with_for_update(skip_locked=True)
            )
        )
        if not records:
            return
        data = "\n".join(
            json.dumps(
                {
                    "sequence": r.sequence,
                    "project_id": r.project_id,
                    "session_id": r.session_id or "",
                    "kind": r.kind,
                    "detail": json.dumps(r.detail, separators=(",", ":")),
                    "created_at": r.created_at,
                }
            )
            for r in records
        )
        response = httpx.post(
            CLICKHOUSE_URL,
            params={"query": "INSERT INTO abrl.session_events FORMAT JSONEachRow"},
            content=data,
            auth=("abrl", CLICKHOUSE_PASSWORD),
            timeout=5,
            trust_env=False,
        )
        response.raise_for_status()
        for row in records:
            row.forwarded_at = now()
