"""Server list filters, stable pagination, and project isolation."""

import test_platform
from sqlalchemy import select

from abrl import api
from abrl.db import TargetVersion, Workflow, WorkflowVersion
from abrl.platform_models import BrowserProfile, BrowserSession, Project, ProxyInventory, SessionBatch

db = test_platform.db
client = test_platform.client


def test_profile_search_is_literal_paginated_and_project_scoped(db):
    with db.begin() as connection:
        connection.add(Project(id="other", name="Other"))
        connection.flush()
        for name in ("Alpha", "alphabet", "A%literal"):
            connection.add(
                BrowserProfile(project_id="default", name=name, adapter="playwright", persistence="fresh")
            )
        connection.add(
            BrowserProfile(
                project_id="other", name="Alpha private", adapter="playwright", persistence="fresh"
            )
        )
    with client() as app:
        page = app.get("/api/v1/profiles?q=alpha&order=name_desc&limit=1").json()
        assert page["total"] == 2 and len(page["items"]) == 1
        next_page = app.get("/api/v1/profiles?q=alpha&order=name_desc&limit=1&offset=1").json()
        assert {page["items"][0]["name"], next_page["items"][0]["name"]} == {"Alpha", "alphabet"}
        literal = app.get("/api/v1/profiles", params={"q": "%"}).json()
        assert [row["name"] for row in literal["items"]] == ["A%literal"]
        assert app.get("/api/v1/profiles?order=untrusted_column").status_code == 422
        assert app.get("/api/v1/profiles", params={"q": "x" * 101}).status_code == 422
        assert app.get("/api/v1/profiles?limit=501").status_code == 422


def test_session_and_batch_filters_apply_before_pagination(db):
    batch = test_platform.create(db, count=3, mode="demo")
    with db.begin() as connection:
        rows = list(connection.scalars(select(BrowserSession).where(BrowserSession.batch_id == batch.id)))
        rows[0].status = "failed"
        rows[0].adapter = "cloakbrowser"
        rows[0].mode = "live"
        rows[0].elapsed_ms = 500
        connection.get(SessionBatch, batch.id).name = "Filtered batch"
    with client() as app:
        result = app.get(
            "/api/v1/sessions?status=failed&adapter=cloakbrowser&mode=live&order=duration_desc&limit=1"
        ).json()
        assert result["total"] == 1 and result["items"][0]["elapsed_ms"] == 500
        assert app.get("/api/v1/batches?q=Filtered&mode=demo&order=name_asc").json()["total"] == 1
        assert app.get("/api/v1/batches?q=Filtered&mode=live").json()["total"] == 0


def test_inventory_filter_uses_product_and_enabled_fields(db):
    with db.begin() as connection:
        for index, enabled in enumerate((True, False)):
            connection.add(
                ProxyInventory(
                    project_id="default",
                    name=f"Inventory {index}",
                    enabled=enabled,
                    config={
                        "name": f"Inventory {index}",
                        "product": "residential",
                        "host": "pr.oxylabs.io",
                        "port": 7777,
                        "username_ref": "TEST_USER",
                        "password_ref": "TEST_PASS",
                        "country": "IL",
                        "capacity": 1,
                        "max_duration_minutes": 30,
                        "exclusive": False,
                        "enabled": enabled,
                        "cost_per_gb": None,
                    },
                )
            )
    with client() as app:
        result = app.get(
            "/api/v1/proxy-inventory?enabled=false&product=residential&country=il&order=name_asc"
        ).json()
        assert result["total"] == 1 and result["items"][0]["enabled"] is False


def test_legacy_workflow_lists_preserve_arrays_and_expose_filtered_total(db, monkeypatch):
    monkeypatch.setattr(api, "Session", db)
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "authorized": True}
        row = Workflow(project_id="default", version=1)
        connection.add(row)
        connection.flush()
        connection.add(
            WorkflowVersion(
                workflow_id=row.id,
                version=1,
                config={
                    "name": "Landing second",
                    "steps": [
                        {"action": "navigate", "url": "/lab"},
                        {"action": "assert_visible", "selector": "#ready"},
                    ],
                },
            )
        )
    with client() as app:
        response = app.get("/api/v1/workflows?q=Landing&limit=1&order=name_asc")
        assert response.status_code == 200, response.text
        assert response.headers["x-total-count"] == "2"
        assert isinstance(response.json(), list) and len(response.json()) == 1
        assert (
            app.get("/api/v1/workflows?q=Landing&limit=1&offset=1&order=name_asc").json()[0]["id"]
            != response.json()[0]["id"]
        )
        target_response = app.get("/api/v1/targets?mode=browser_only&verified=true&limit=1")
        assert target_response.headers["x-total-count"] == "1"
        assert app.get("/api/v1/workflows?order=invalid").status_code == 422
