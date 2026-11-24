"""Session IP reads use the current attempt and the complete batch."""

import pytest
import test_platform
from sqlalchemy import select

from abrl import access
from abrl.db import new_id
from abrl.platform_models import (
    BrowserProfile,
    BrowserSession,
    Membership,
    Project,
    ProviderObservation,
    SessionAttempt,
    SessionBatch,
)
from abrl.platform_service import settle_batch

db = test_platform.db
client = test_platform.client


def start_attempt(connection, row, address=None, *, proxy_check_ip=None):
    row.attempt_number += 1
    row.epoch += 1
    attempt = SessionAttempt(
        id=new_id(),
        session_id=row.id,
        number=row.attempt_number,
        epoch=row.epoch,
        proxy_check_ip=proxy_check_ip,
    )
    connection.add(attempt)
    if address is not None:
        connection.add(
            ProviderObservation(
                project_id=row.project_id,
                session_id=row.id,
                attempt_id=attempt.id,
                idempotency_key=attempt.id,
                verification_status="unavailable",
                source="site_connector",
                provider_version="test",
                observed_ip=address,
            )
        )


def test_batch_uniqueness_across_pages_with_current_attempt_addresses(db):
    batch = test_platform.create(db, mode="live", count=30)
    with db.begin() as connection:
        rows = connection.scalars(
            select(BrowserSession).order_by(BrowserSession.created_at, BrowserSession.id)
        ).all()
        for index, row in enumerate(rows, 1):
            start_attempt(connection, row, f"2001:db8::{index:x}")
        first_id, last_id = rows[0].id, rows[-1].id
        connection.get(SessionBatch, batch.id).status = "running"

    with client() as api:
        endpoint = f"/api/v1/batches/{batch.id}/ip-summary"
        assert api.get(endpoint).json() == {
            "expected_sessions": 30,
            "observed_sessions": 30,
            "distinct_ips": 30,
            "status": "pending",
        }
        with db.begin() as connection:
            connection.get(SessionBatch, batch.id).status = "completed"
        assert api.get(endpoint).json()["status"] == "unique"

        with db.begin() as connection:
            start_attempt(connection, connection.get(BrowserSession, last_id), "2001:db8::1")
            connection.get(SessionBatch, batch.id).status = "running"
        for offset, expected_id in ((0, first_id), (29, last_id)):
            response = api.get(
                "/api/v1/sessions",
                params={"batch_id": batch.id, "order": "oldest", "limit": 1, "offset": offset},
            )
            assert response.headers["cache-control"] == "no-store"
            page = response.json()
            assert page["total"] == 30
            assert page["items"][0]["id"] == expected_id
            assert page["items"][0]["observed_ip"] == "2001:db8::1"
        assert api.get(endpoint).json() == {
            "expected_sessions": 30,
            "observed_sessions": 30,
            "distinct_ips": 29,
            "status": "duplicate",
        }

        with db.begin() as connection:
            start_attempt(connection, connection.get(BrowserSession, last_id))
            connection.get(SessionBatch, batch.id).status = "completed"
        assert api.get(f"/api/v1/sessions/{last_id}").json()["observed_ip"] is None
        assert api.get(endpoint).json() == {
            "expected_sessions": 30,
            "observed_sessions": 29,
            "distinct_ips": 29,
            "status": "incomplete",
        }


def test_ip_summary_includes_failed_allocations_and_historical_observations(db):
    with db.begin() as connection:
        connection.add(
            BrowserProfile(
                id="valid",
                project_id="default",
                name="Available",
                adapter="playwright",
                persistence="persistent",
            )
        )
    batch = test_platform.create(db, mode="live", count=2, profile_ids=["valid", "missing"])
    with db.begin() as connection:
        row = connection.scalar(select(BrowserSession).where(BrowserSession.batch_id == batch.id))
        row.status = "completed"
        start_attempt(connection, row)
        attempt = connection.scalar(select(SessionAttempt).where(SessionAttempt.session_id == row.id))
        connection.add(
            ProviderObservation(
                project_id=row.project_id,
                session_id=row.id,
                attempt_id=attempt.id,
                idempotency_key=attempt.id,
                verification_status="verified",
                source="controlled_site",
                provider_version="test",
            )
        )
        settle_batch(connection, batch.id)
        assert connection.get(SessionBatch, batch.id).status == "creation_failed"
        session_id = row.id
    with client() as api:
        assert api.get(f"/api/v1/sessions/{session_id}").json()["observed_ip"] is None
        assert api.get(f"/api/v1/batches/{batch.id}/ip-summary").json() == {
            "expected_sessions": 2,
            "observed_sessions": 0,
            "distinct_ips": 0,
            "status": "incomplete",
        }
        assert api.get(f"/api/v1/batches/{batch.id}/proxy-check-ip-summary").json() == {
            "expected_sessions": 2,
            "checked_sessions": 0,
            "distinct_ips": 0,
            "unique_sessions": 0,
            "duplicate_sessions": 0,
            "duplicate_groups": [],
        }


def test_proxy_check_ip_is_current_and_excluded_from_observations_and_batch_results(db):
    address = "2001:db8::fe"
    batch = test_platform.create(db, mode="live", count=2)
    with db.begin() as connection:
        rows = connection.scalars(select(BrowserSession).where(BrowserSession.batch_id == batch.id)).all()
        for row in rows:
            row.status = "completed"
            start_attempt(connection, row, proxy_check_ip=address)
        settle_batch(connection, batch.id)
        session_id = rows[0].id

    with client() as api:
        base = f"/api/v1/sessions/{session_id}"
        detail = api.get(base)
        assert detail.headers["cache-control"] == "no-store"
        assert detail.json()["observed_ip"] is None
        assert detail.json()["proxy_check_ip"] == address
        sessions = api.get("/api/v1/sessions", params={"batch_id": batch.id}).json()["items"]
        assert [item["proxy_check_ip"] for item in sessions] == [address, address]
        assert all(item["observed_ip"] is None for item in sessions)
        assert api.get(f"/api/v1/batches/{batch.id}/ip-summary").json() == {
            "expected_sessions": 2,
            "observed_sessions": 0,
            "distinct_ips": 0,
            "status": "incomplete",
        }
        for endpoint in (
            f"{base}/observations",
            f"{base}/events",
        ):
            response = api.get(endpoint)
            assert response.status_code == 200, response.text
            assert "proxy_check_ip" not in response.text and address not in response.text

        with db.begin() as connection:
            start_attempt(connection, connection.get(BrowserSession, session_id))
        assert api.get(base).json()["proxy_check_ip"] is None


def test_batch_proxy_check_summary_counts_every_session_and_groups_latest_duplicates(db):
    batch = test_platform.create(db, mode="live", count=30)
    duplicate_ips = {1: "2001:db8::a", 30: "2001:db8::a", 2: "2001:db8::b", 15: "2001:db8::b", 25: "2001:db8::b"}
    with db.begin() as connection:
        rows = connection.scalars(select(BrowserSession).where(BrowserSession.batch_id == batch.id)).all()
        sessions = {row.ordinal: {"id": row.id, "ordinal": row.ordinal} for row in rows}
        for row in rows:
            checked_ip = duplicate_ips.get(row.ordinal, f"2001:db8:1::{row.ordinal:x}")
            start_attempt(connection, row, "2001:db8::c", proxy_check_ip=checked_ip if row.ordinal != 3 else None)

    with client() as api:
        paged = api.get("/api/v1/sessions", params={"batch_id": batch.id, "limit": 1}).json()
        assert paged["total"] == 30 and len(paged["items"]) == 1

        endpoint = f"/api/v1/batches/{batch.id}/proxy-check-ip-summary"
        group_a = {"proxy_check_ip": "2001:db8::a", "sessions": [sessions[ordinal] for ordinal in (1, 30)]}
        group_b = {"proxy_check_ip": "2001:db8::b", "sessions": [sessions[ordinal] for ordinal in (2, 15, 25)]}
        summary = api.get(endpoint)
        assert summary.status_code == 200
        assert summary.headers["cache-control"] == "no-store"
        assert summary.json() == {
            "expected_sessions": 30,
            "checked_sessions": 29,
            "distinct_ips": 26,
            "unique_sessions": 24,
            "duplicate_sessions": 5,
            "duplicate_groups": [group_a, group_b],
        }

        with db.begin() as connection:
            start_attempt(connection, connection.get(BrowserSession, sessions[30]["id"]))
        assert api.get(endpoint).json() == {
            "expected_sessions": 30,
            "checked_sessions": 28,
            "distinct_ips": 26,
            "unique_sessions": 25,
            "duplicate_sessions": 3,
            "duplicate_groups": [group_b],
        }


@pytest.mark.parametrize("role,visible", [("owner", True), ("operator", True), ("analyst", False), ("viewer", False)])
def test_ip_reads_require_project_scope_and_an_operator_role(db, role, visible):
    address = "2001:db8::1"
    proxy_check_ip = "2001:db8::2"
    batch = test_platform.create(db, mode="live", count=2)
    with db.begin() as connection:
        first, second = connection.scalars(
            select(BrowserSession).where(BrowserSession.batch_id == batch.id).order_by(BrowserSession.ordinal)
        ).all()
        start_attempt(connection, first, address, proxy_check_ip=proxy_check_ip)
        start_attempt(connection, second, "2001:db8::3", proxy_check_ip=proxy_check_ip)
        connection.get(SessionBatch, batch.id).status = "completed"
        session_id = first.id
        members = [{"id": row.id, "ordinal": row.ordinal} for row in (first, second)]
        local_user = access.ensure_bootstrap(connection)
        connection.get(Membership, ("default", local_user.id)).role = role
        connection.add(Project(id="other", name="Another project"))
        connection.add(Membership(project_id="other", user_id=local_user.id, role="owner"))

    with client() as api:
        session_url = f"/api/v1/sessions/{session_id}"
        summary_url = f"/api/v1/batches/{batch.id}/ip-summary"
        proxy_summary_url = f"/api/v1/batches/{batch.id}/proxy-check-ip-summary"
        session_list = api.get("/api/v1/sessions", params={"q": session_id}).json()["items"]
        for session in (api.get(session_url).json(), session_list[0]):
            assert session["observed_ip"] == (address if visible else None)
            assert session["proxy_check_ip"] == (proxy_check_ip if visible else None)
        summary = api.get(summary_url)
        assert summary.json()["status"] == "unique"
        assert address not in summary.text and proxy_check_ip not in summary.text
        proxy_summary = api.get(proxy_summary_url)
        assert proxy_summary.status_code == 200
        assert proxy_summary.json() == {
            "expected_sessions": 2,
            "checked_sessions": 2,
            "distinct_ips": 1,
            "unique_sessions": 0,
            "duplicate_sessions": 2,
            "duplicate_groups": [{"proxy_check_ip": proxy_check_ip if visible else None, "sessions": members}],
        }
        other_project = {"X-Project-ID": "other"}
        assert api.get(session_url, headers=other_project).status_code == 404
        assert api.get(summary_url, headers=other_project).status_code == 404
        assert api.get(proxy_summary_url, headers=other_project).status_code == 404
        assert api.get("/api/v1/sessions", headers=other_project).json()["items"] == []
