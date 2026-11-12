"""Real API/browser validation for workflow guards, checkpoints, and batch allocation retry."""

import json
import os
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")
token = os.environ["ABRL_OPERATOR_TOKEN"]
client = httpx.Client(
    base_url=os.getenv("ABRL_SMOKE_API", "http://127.0.0.1:8400") + "/api/v1",
    headers={"Authorization": "Bearer " + token, "X-Project-ID": "default"},
    timeout=30,
)
client.post("/starter").raise_for_status()
target = next(row for row in client.get("/targets").json() if row["name"] == "ABRL controlled test site")
stamp = str(int(time.time()))
workflow = {
    "name": "UI checkpoint " + stamp,
    "steps": [
        {"action": "navigate", "url": "/lab"},
        {"action": "assert_visible", "selector": "#lab-ready"},
        {"action": "checkpoint", "name": "landing-confirmed"},
        {"action": "assert_not_visible", "selector": "#abrl-ui-failure"},
    ],
}
errors = []
batch = None
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
        page.get_by_label("Checkpoint name", exact=True).fill("landing-verified")
        expect(page.get_by_text("it does not reset action history", exact=False)).to_be_visible()
        with page.expect_download() as download:
            page.get_by_role("button", name="Export", exact=True).click()
        exported = json.loads(download.value.path().read_text())
        assert exported["steps"][2]["name"] == "landing-verified"
        page.get_by_role("button", name="Save and preview", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Site", exact=True).select_option(target["id"])
        dialog.get_by_label("Evidence source", exact=True).select_option("live")
        dialog.get_by_role("button", name="Run preview", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(page.get_by_role("heading", name="Workflow assessment", exact=True)).to_be_visible(
            timeout=45000
        )
        stored = next(row for row in client.get("/workflows").json() if row["name"] == workflow["name"])
        assert stored["steps"][2]["name"] == "landing-verified"
        run = next(row for row in client.get("/runs").json() if row["workflow_id"] == stored["id"])
        for _ in range(50):
            result = client.get(f"/runs/{run['id']}/report").json()
            if result.get("counts", {}).get("completed") == 1:
                break
            page.wait_for_timeout(1000)
        assert result["counts"]["completed"] == 1, result.get("counts")

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
                "mode": "demo",
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
        "Workflow release smoke passed: visual guards/checkpoints, live execution, JSON roundtrip, per-item retry with lost acknowledgement, product gateways, navigation-origin control."
    )
finally:
    if batch:
        client.post(f"/batches/{batch['id']}/cancel", json={})
    client.close()
