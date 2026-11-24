"""Validate live workflow preview, retained artifacts, and batch retry with make dev-fixtures."""

import json
import os
import time
import uuid
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import httpx
from batch_picker import choose_batch_option
from dotenv import load_dotenv
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")
token = os.environ["ABRL_OPERATOR_TOKEN"]
api_root = os.getenv("ABRL_SMOKE_API", "http://127.0.0.1:8400").rstrip("/")
client = httpx.Client(
    base_url=api_root + "/api/v1",
    headers={"Authorization": "Bearer " + token, "X-Project-ID": "default"},
    timeout=30,
)
client.post(f"{api_root}/test/seed").raise_for_status()
target = next(row for row in client.get("/targets").json() if row["name"] == "ABRL controlled test site")
stamp = str(int(time.time()))
workflow = {
    "name": "UI checkpoint " + stamp,
    "steps": [
        {
            "action": "navigate",
            "url": "/lab?tag=static%20value&tag=[CAMPAIGN_ID]",
            "fixture_params": {"CAMPAIGN_ID": "workspace default"},
        },
        {"action": "assert_visible", "selector": "#lab-ready"},
        {"action": "checkpoint", "name": "landing-confirmed"},
        {"action": "assert_not_visible", "selector": "#abrl-ui-failure"},
    ],
}
errors = []
batch = None
preview_batch = None
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(os.getenv("ABRL_SMOKE_WEB", "http://127.0.0.1:8430"))
        page.get_by_label("Operator token").fill(token)
        page.get_by_role("button", name="Connect", exact=True).click()
        navigation = page.get_by_role("navigation", name="Workspace")
        navigation.get_by_role("button", name="Workflows", exact=True).click()
        page.get_by_role("button", name="New workflow", exact=True).click()
        page.get_by_label("Import workflow JSON").set_input_files(
            {
                "name": "guarded.json",
                "mimeType": "application/json",
                "buffer": json.dumps(workflow).encode(),
            }
        )
        expect(page.get_by_label("Step 3 action")).to_have_value("checkpoint")
        expect(page.get_by_label("Step 4 action")).to_have_value("assert_not_visible")
        expect(page.get_by_label("CAMPAIGN_ID default", exact=True)).to_have_value("workspace default")
        expect(
            page.get_by_text("/lab?tag=static%20value&tag=workspace+default", exact=True)
        ).to_be_visible()
        page.get_by_label("Checkpoint name", exact=True).fill("landing-verified")
        expect(page.get_by_text("keeps the existing action history", exact=False)).to_be_visible()
        with page.expect_download() as download:
            page.get_by_role("button", name="Export", exact=True).click()
        exported = json.loads(download.value.path().read_text())
        assert exported["steps"][2]["name"] == "landing-verified"
        assert exported["steps"][0]["fixture_params"] == {"CAMPAIGN_ID": "workspace default"}
        page.get_by_role("button", name="Save and preview", exact=True).click()
        dialog = page.get_by_role("dialog")
        choose_batch_option(dialog, "Site", target["name"])
        dialog.get_by_label("Batch name", exact=True).fill("UI preview " + stamp)
        dialog.locator("summary").filter(has_text="Advanced").click()
        expect(dialog.get_by_label("Screenshot and console capture", exact=True)).to_have_value("all")
        expect(
            dialog.get_by_label("Capture JavaScript assets and execution coverage", exact=True)
        ).to_be_checked()
        expect(dialog.get_by_label("Evidence source", exact=True)).to_have_count(0)
        expect(dialog.get_by_text("Reusable session configuration", exact=True)).to_have_count(0)
        dialog.get_by_label("CAMPAIGN_ID", exact=True).fill("été & QA")
        ready = dialog.get_by_role("group", name="Launch readiness")
        expect(ready.get_by_text("1 session · 1 maximum concurrent", exact=True)).to_be_visible()
        expect(
            dialog.get_by_text(
                target["origin"] + "/lab?tag=static%20value&tag=%C3%A9t%C3%A9+%26+QA", exact=True
            )
        ).to_be_visible()
        expect(
            dialog.get_by_text("Saved default workspace default", exact=True)
        ).to_be_visible()
        with page.expect_response(
            lambda response: response.request.method == "POST" and response.url.endswith("/batches")
        ) as launched:
            dialog.get_by_role("button", name="Create batch", exact=True).click()
        assert launched.value.ok, launched.value.text()
        preview_batch = launched.value.json()
        expect(dialog).not_to_be_visible()
        stored = next(row for row in client.get("/workflows").json() if row["name"] == workflow["name"])
        assert stored["steps"][2]["name"] == "landing-verified"
        assert preview_batch["count"] == 1 and preview_batch["mode"] == "live"
        assert preview_batch["config"]["workflow_version"] == stored["version"]
        assert preview_batch["config"]["fixture_params"] == {"CAMPAIGN_ID": "été & QA"}
        for _ in range(50):
            results = (
                client.get("/sessions", params={"batch_id": preview_batch["id"]}).raise_for_status().json()
            )
            (session,) = results["items"]
            if session["status"] in {"completed", "failed", "blocked", "cancelled", "needs_reconciliation"}:
                break
            page.wait_for_timeout(1000)
        assert session["status"] == "completed", session
        artifacts = client.get(f"/sessions/{session['id']}/artifacts").raise_for_status().json()
        retained = {item["name"]: item["id"] for item in artifacts}
        assert {"final.png", "console.json", "javascript-attempt-1.zip"} <= retained.keys(), artifacts
        assert (
            client.get(f"/artifacts/{retained['final.png']}")
            .raise_for_status()
            .content.startswith(b"\x89PNG")
        )
        assert isinstance(
            client.get(f"/artifacts/{retained['console.json']}").raise_for_status().json(), list
        )
        archive = (
            client.get(f"/artifacts/{retained['javascript-attempt-1.zip']}").raise_for_status().content
        )
        with ZipFile(BytesIO(archive)) as sources:
            assert "manifest.json" in sources.namelist()

        profile = client.post(
            "/profiles",
            json={"name": "UI allocation " + stamp, "adapter": "playwright", "persistence": "fresh"},
        )
        profile.raise_for_status()
        response = client.post(
            "/batches",
            json={
                "name": "UI allocation retry " + stamp,
                "target_id": target["id"],
                "workflow_id": stored["id"],
                "count": 2,
                "concurrency": 1,
                "adapters": ["playwright"],
                "profile_ids": [profile.json()["id"], str(uuid.uuid4())],
                "idempotency_key": str(uuid.uuid4()),
            },
        )
        response.raise_for_status()
        batch = response.json()
        before = client.get(f"/batches/{batch['id']}/items").json()["items"]
        assert sorted(item["status"] for item in before) == ["created", "failed"]
        navigation.get_by_role("button", name="Sessions", exact=True).click()
        page.get_by_role("button", name="Batches", exact=True).click()
        page.get_by_role("button", name="Creation details for " + batch["name"], exact=True).click()
        expect(
            dialog.get_by_text(
                "Selected profile is unavailable or incompatible with assigned adapter", exact=True
            )
        ).to_be_visible()
        dropped = []

        def dropped_ack(route):
            if not dropped:
                response = route.fetch()
                assert response.ok
                dropped.append(True)
                route.abort("failed")
            else:
                route.continue_()

        page.route("**/retry-creation", dropped_ack)
        dialog.get_by_role("button", name="Retry failed creation", exact=True).click()
        expect(dialog.get_by_role("alert")).to_be_visible()
        dialog.get_by_role("button", name="Retry failed creation", exact=True).click()
        expect(dialog.get_by_role("alert")).not_to_be_visible()
        after = client.get(f"/batches/{batch['id']}/items").json()["items"]
        assert [item["session_id"] for item in after] == [item["session_id"] for item in before]
        assert next(item for item in after if item["status"] == "failed")["attempts"] == 2
        dialog.get_by_role("button", name="Close", exact=True).click()

        navigation.get_by_role("button", name="Network", exact=True).click()
        page.get_by_role("button", name="Proxy inventory", exact=True).click()
        page.get_by_role("button", name="Register inventory", exact=True).first.click()
        dialog.get_by_label("Product family", exact=True).select_option("dedicated_datacenter")
        expect(dialog.get_by_label("Endpoint hostname", exact=True)).to_have_value("ddc.oxylabs.io")
        expect(dialog.get_by_label("Port", exact=True)).to_have_value("8001")
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        navigation.get_by_role("button", name="Sites", exact=True).click()
        page.get_by_role("button", name="New site", exact=True).click()
        expect(page.get_by_label("Additional navigation origins", exact=True)).to_be_visible()
        assert not errors, errors
        browser.close()
    print(
        "Workflow smoke passed with named URL overrides, a live preview, retained artifacts, and per-item retry."
    )
finally:
    if batch:
        client.post(f"/batches/{batch['id']}/cancel", json={})
    if preview_batch:
        client.post(f"/batches/{preview_batch['id']}/cancel", json={})
    client.close()
