"""Run-wide allocation, authorization and immutable export integration checks."""

import copy
import random

import pytest
import test_platform
from sqlalchemy import select
from test_platform import client, data

from abrl import experiment_api, session_runner
from abrl.platform_models import BrowserSession, ExperimentRun, SessionBatch

db = test_platform.db


@pytest.fixture
def experiment_db(db, monkeypatch):
    monkeypatch.setattr(experiment_api, "Session", db)
    return db


def create_run(api, *, key="run-request-001", experiment_id=None, concurrency=1):
    if experiment_id is None:
        groups = [
            data(count=8, adapters=[adapter], seed=19).model_dump(mode="json")
            for adapter in ("playwright", "cloakbrowser")
        ]
        response = api.post(
            "/api/v1/experiments",
            json={"name": "Matched browsers", "groups": groups, "concurrency": concurrency},
        )
        assert response.status_code == 201, response.text
        experiment_id = response.json()["id"]
        assert api.post(f"/api/v1/experiments/{experiment_id}/validate").status_code == 200
    response = api.post(f"/api/v1/experiments/{experiment_id}/runs", headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    return experiment_id, response.json()


def test_seeded_order_spans_groups_is_idempotent_and_drives_admission(experiment_db):
    with client() as api:
        experiment_id, created = create_run(api)
        _, repeated = create_run(api, experiment_id=experiment_id)
        assert created == repeated
    with experiment_db.begin() as db:
        run = db.get(ExperimentRun, created["id"])
        expected = [(group, ordinal) for group in range(2) for ordinal in range(1, 9)]
        random.Random(19).shuffle(expected)
        assert [(item["group_index"], item["ordinal"]) for item in run.randomization["order"]] == expected
        rows = list(db.scalars(select(BrowserSession).order_by(BrowserSession.dispatch_order)))
        assert [r.dispatch_order for r in rows] == list(range(1, 17))
        assert len({r.created_at for r in rows}) == 1
        assert {r.batch_id for r in rows[:5]} == set(created["batch_ids"])
        session_runner.admission_tick(db)
        admitted = [r for r in rows if r.admitted]
        assert admitted[0].id == rows[0].id
        assert len(admitted) == 1  # default run-wide cap applies across all group batches


def test_run_exports_every_batch_once_and_returns_frozen_evidence(experiment_db):
    with client() as api:
        experiment_id, created = create_run(api)
        base = f"/api/v1/experiments/{experiment_id}/runs/{created['id']}"
        assert api.get(base + "/export").status_code == 409
        with experiment_db.begin() as db:
            for batch_id in created["batch_ids"]:
                db.get(SessionBatch, batch_id).status = "completed"
            for row in db.scalars(select(BrowserSession)):
                row.status = row.outcome = row.workflow_outcome = "completed"
                row.metadata_json = {
                    "simulated": True,
                    "os": "Linux",
                    "architecture": "amd64",
                    "browser_version": "Demo",
                }
        first = api.get(base + "/export?format=json")
        assert first.status_code == 200, first.text
        frozen = first.json()
        assert len(frozen["trials"]) == 16
        assert set(frozen["batch_ids"]) == set(created["batch_ids"])
        assert frozen["recommendation"]["status"] == "simulated"
        with experiment_db.begin() as db:
            row = db.scalar(select(BrowserSession))
            row.metadata_json = {"late_metadata": True}
        assert api.get(base + "/comparison").json() == frozen
        assert api.get(base + "/export?format=json").json() == frozen
        assert api.get(base + "/export?format=csv").text.count("\n") == 17
        assert api.get(base.replace(experiment_id, "unknown") + "/comparison").status_code == 404


def test_failed_creation_retry_reuses_reserved_position(experiment_db):
    with client() as api:
        _, created = create_run(api)
    with experiment_db.begin() as db:
        run = db.get(ExperimentRun, created["id"])
        plan = copy.deepcopy(run.randomization)
        batch = db.get(SessionBatch, created["batch_ids"][0])
        row = db.scalar(select(BrowserSession).where(BrowserSession.batch_id == batch.id))
        position = row.dispatch_order
        row.dispatch_order = None
        experiment_api.apply_experiment_dispatch(db, batch)
        assert row.dispatch_order == position
        assert run.randomization == plan


def test_explicit_run_concurrency_remains_bounded_by_each_group(experiment_db):
    with client() as api:
        _, created = create_run(api, concurrency=3)
    with experiment_db.begin() as db:
        session_runner.admission_tick(db)
        rows = list(db.scalars(select(BrowserSession).where(BrowserSession.admitted.is_(True))))
        assert len(rows) == 2  # Two groups each permit one, even though the run permits three.
        assert len({r.batch_id for r in rows}) == 2
        assert db.get(ExperimentRun, created["id"]).randomization["concurrency"] == 3


def test_experiment_list_filters_order_and_pagination(experiment_db):
    with client() as api:
        experiment_id, _ = create_run(api)
        result = api.get(
            "/api/v1/experiments",
            params={"q": "Matched", "status": "ready", "mode": "demo", "order": "name_asc", "limit": 1},
        )
        assert result.status_code == 200
        assert result.json()["total"] == 1 and result.json()["items"][0]["id"] == experiment_id
        assert api.get("/api/v1/experiments?q=%25").json()["total"] == 0
        assert api.get("/api/v1/experiments?mode=live").json()["total"] == 0
        assert api.get("/api/v1/experiments?offset=1").json()["items"] == []
        assert api.get("/api/v1/experiments?order=untrusted_sql").status_code == 422
