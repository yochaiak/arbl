"""Check the simplified batch form and timing edits for saved grouped schedules."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from batch_picker import choose_batch_option
from dotenv import load_dotenv
from playwright.sync_api import expect, sync_playwright

load_dotenv(Path(__file__).resolve().parents[3] / ".env")


def smoke():
    token = os.environ["ABRL_OPERATOR_TOKEN"]
    api_root = os.getenv("ABRL_SMOKE_API", "http://127.0.0.1:8400").rstrip("/")
    web = os.getenv("ABRL_SMOKE_WEB", "http://127.0.0.1:8430")
    headers = {"Authorization": f"Bearer {token}", "X-Project-ID": "default"}
    with httpx.Client(base_url=api_root + "/api/v1", headers=headers, timeout=10) as api:
        api.post(api_root + "/test/seed").raise_for_status()
        name = "Saved batch settings " + uuid4().hex[:8]
        site = next(row for row in api.get("/targets").raise_for_status().json() if row["name"] == "ABRL schedule test site")
        workflow = next(row for row in api.get("/workflows").raise_for_status().json() if row["name"] == "Landing check")
        batch = {
            "name": name,
            "target_id": site["id"],
            "workflow_id": workflow["id"],
            "idempotency_key": "saved-settings-" + uuid4().hex,
            "session_groups": [{"adapter": "playwright", "count": 2, "device_preset": "phone"}],
        }
        saved = api.post("/schedules", json={
            "name": name,
            "batch": batch,
            "kind": "interval",
            "interval_seconds": 3600,
            "start_at": (datetime.now(UTC) + timedelta(hours=6)).isoformat(),
        }).raise_for_status().json()
        schedule_path = f"/schedules/{saved['id']}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 950})
                page.goto(web)
                page.get_by_label("Operator token").fill(token)
                page.get_by_role("button", name="Connect", exact=True).click()
                navigation = page.get_by_role("navigation", name="Workspace")

                site_name = name + " site"
                navigation.get_by_role("button", name="Sites", exact=True).click()
                page.get_by_role("button", name="New site", exact=True).click()
                page.get_by_label("Site name", exact=True).fill(site_name)
                page.get_by_label("Navigation origin", exact=True).fill("https://picker-smoke.example")
                with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/v1/targets")) as created:
                    page.get_by_role("button", name="Register site", exact=True).click()
                assert created.value.ok and created.value.json()["live_ready"], created.value.text()
                expect(page.get_by_role("button", name="Verify site", exact=True)).to_have_count(0)

                navigation.get_by_role("button", name="Sessions", exact=True).click()
                page.get_by_role("button", name="Create batch", exact=True).click()
                modal = page.get_by_role("dialog")
                expect(modal.get_by_role("group", name="Browser distribution")).to_be_visible()
                expect(modal.get_by_label("Independent sessions")).to_be_editable()
                expect(modal.get_by_label("Search authorized site", exact=True)).to_have_count(0)
                expect(modal.get_by_label("Search workflow", exact=True)).to_have_count(0)
                expect(modal.get_by_text("Session groups and CSV", exact=True)).to_have_count(0)
                expect(modal.get_by_label("Import session groups CSV")).to_have_count(0)
                choose_batch_option(modal, "Site", site_name, keyboard=True)
                choose_batch_option(modal, "Workflow", workflow["name"], keyboard=True)
                expect(modal.get_by_role("button", name="Create batch", exact=True)).to_be_enabled()
                page.set_viewport_size({"width": 390, "height": 844})
                picker = modal.get_by_role("button", name="Site", exact=True)
                picker.click()
                search = modal.get_by_role("combobox", name="Find site", exact=True)
                expect(search).to_be_focused()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                search.press("Escape")
                expect(picker).to_be_focused()
                expect(modal.get_by_role("listbox", name="Site", exact=True)).to_have_count(0)
                page.set_viewport_size({"width": 1440, "height": 950})
                modal.get_by_role("button", name="Cancel", exact=True).click()

                navigation.get_by_role("button", name="Schedules", exact=True).click()
                page.get_by_role("table").get_by_role("button", name=name, exact=True).click()
                expect(modal.get_by_text("This schedule runs with its saved per-session settings.", exact=False)).to_be_visible()
                expect(modal.get_by_role("button", name="Configure sessions")).to_have_count(0)
                expect(modal.get_by_label("Refresh pinned dependency versions")).to_have_count(0)
                modal.get_by_label("Interval in seconds", exact=True).fill("7200")
                with page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith(schedule_path)) as edit:
                    modal.get_by_role("button", name="Save schedule version", exact=True).click()
                updated = edit.value.json()
                assert edit.value.ok, updated
                assert updated["interval_seconds"] == 7200
                assert updated["batch"] == saved["batch"]
                assert updated["dependencies"] == saved["dependencies"]
                expect(modal).not_to_be_visible()
                browser.close()
        finally:
            api.post(schedule_path + "/pause").raise_for_status()
    print("Batch form and saved schedule compatibility checks passed.")


if __name__ == "__main__":
    smoke()
