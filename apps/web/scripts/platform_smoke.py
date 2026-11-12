"""Browser acceptance for platform controls against the running local API.

Creates explicitly named demonstration batches and a paused schedule. Optional
large-list/viewer checks use intercepted UI fixtures, never stored research data.
Run: uv run python apps/web/scripts/platform_smoke.py
"""
import base64
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx
from dotenv import load_dotenv
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")
API = os.getenv("ABRL_SMOKE_API", "http://127.0.0.1:8400")
WEB = os.getenv("ABRL_SMOKE_WEB", "http://127.0.0.1:8430")
SCREENSHOTS = Path(os.getenv("ABRL_SMOKE_SCREENSHOTS", str(ROOT / ".abrl/ui-evidence")))


def run():
    token = os.environ["ABRL_OPERATOR_TOKEN"]
    headers = {"Authorization": "Bearer " + token, "X-Project-ID": "default"}
    client = httpx.Client(base_url=API + "/api/v1", headers=headers, timeout=30)
    client.get("/projects").raise_for_status()
    client.post("/starter").raise_for_status()
    target = next(t for t in client.get("/targets").json() if t["name"] == "ABRL controlled test site")
    workflow = next(w for w in client.get("/workflows").json() if w["name"] == "Landing check")
    stamp = str(int(time.time()))
    errors = []
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(WEB)
        page.get_by_label("Operator token").fill(token)
        page.get_by_role("button", name="Connect", exact=True).click()
        expect(page.get_by_role("heading", name="Overview", exact=True)).to_be_visible()
        navigation = page.get_by_role("navigation", name="Workspace")
        expect(page.get_by_role("heading", name="Recent runs", exact=True)).to_be_visible()
        page.screenshot(path=SCREENSHOTS / "platform-overview.png", full_page=True)

        navigation.get_by_role("button", name="Sessions", exact=True).click()
        page.get_by_role("button", name="Create batch", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Batch name", exact=True).fill("UI acceptance " + stamp)
        dialog.get_by_label("Authorized site", exact=True).select_option(target["id"])
        dialog.get_by_label("Workflow", exact=True).select_option(workflow["id"])
        dialog.get_by_label("Independent sessions", exact=True).fill("3")
        dialog.get_by_label("Maximum concurrency", exact=True).fill("2")
        dialog.get_by_role("button", name="Automated preparation", exact=True).click()
        expect(dialog.get_by_text("Recorded as automated activity", exact=False)).to_be_visible()
        dialog.get_by_role("button", name="None", exact=True).click()
        dialog.get_by_role("button", name="Review batch", exact=True).click()
        expect(dialog.get_by_role("heading", name="3 independent sessions")).to_be_visible()
        dropped = []
        def dropped_ack(route):
            if route.request.method == "POST" and not dropped:
                response = route.fetch()
                assert response.ok
                dropped.append(route.request.post_data_json["idempotency_key"])
                route.abort("failed")
            else:
                route.continue_()
        page.route("**/api/v1/batches", dropped_ack)
        dialog.get_by_role("button", name="Create batch", exact=True).click()
        expect(dialog.get_by_role("alert")).to_be_visible()
        dialog.get_by_role("button", name="Create batch", exact=True).click()
        expect(dialog).not_to_be_visible()
        page.unroute("**/api/v1/batches", dropped_ack)
        expect(page.get_by_role("list", name="Browser sessions")).to_be_visible()
        page.screenshot(path=SCREENSHOTS / "sessions.png", full_page=True)
        batches = client.get("/batches?limit=50").json()["items"]
        batch = next(b for b in batches if b["name"] == "UI acceptance " + stamp)
        assert len([b for b in batches if b["name"] == "UI acceptance " + stamp]) == 1
        assert batch["count"] == 3 and batch["concurrency"] == 2
        assert batch["mode"] == "demo"

        page.get_by_role("button", name="Profiles", exact=True).click()
        page.get_by_role("button", name="New profile", exact=True).click()
        dialog.get_by_label("Profile name").fill("UI persistent " + stamp)
        dialog.get_by_role("button", name="Create profile", exact=True).click()
        expect(dialog).not_to_be_visible()
        page.get_by_role("button", name="UI persistent " + stamp, exact=True).click()
        expect(dialog.get_by_role("heading", name="UI persistent " + stamp)).to_be_visible()
        expect(dialog.get_by_text("No clean-shutdown snapshot is recorded.")).to_be_visible()
        dialog.get_by_role("button", name="Close dialog").click()

        navigation.get_by_role("button", name="Network", exact=True).click()
        page.get_by_role("button", name="New policy", exact=True).click()
        dialog.get_by_label("Policy name").fill("UI recovery " + stamp)
        dialog.get_by_role("button", name="Create policy", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(page.get_by_role("heading", name="UI recovery " + stamp)).to_be_visible()
        policy_card = page.locator(".policy-card").filter(has=page.get_by_role("heading", name="UI recovery " + stamp, exact=True))
        policy_card.get_by_role("button", name="Edit", exact=True).click()
        dialog.get_by_label("Required continuity (minutes)").fill("45")
        dialog.get_by_role("button", name="Save new version", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(policy_card.get_by_text("v2", exact=True)).to_be_visible()

        page.screenshot(path=SCREENSHOTS / "network.png", full_page=True)
        page.get_by_role("button", name="Proxy inventory", exact=True).click()
        page.get_by_role("button", name="Register inventory", exact=True).first.click()
        dialog.get_by_label("Inventory name").fill("UI disabled inventory " + stamp)
        dialog.get_by_label("Enable for eligible assignments").uncheck()
        dialog.get_by_role("button", name="Register inventory", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(page.get_by_text("UI disabled inventory " + stamp, exact=True)).to_be_visible()
        inventory_row = page.get_by_role("row").filter(has=page.get_by_text("UI disabled inventory " + stamp, exact=True))
        inventory_row.get_by_role("button", name="Edit", exact=True).click()
        dialog.get_by_label("Connection capacity", exact=True).fill("2")
        dialog.get_by_role("button", name="Save inventory", exact=True).click()
        expect(dialog).not_to_be_visible()


        navigation.get_by_role("button", name="Schedules", exact=True).click()
        page.get_by_role("button", name="New schedule", exact=True).click()
        dialog.get_by_label("Schedule name").fill("UI schedule " + stamp)
        dialog.get_by_role("button", name="Preview schedule", exact=True).click()
        expect(dialog.locator(".schedule-preview li").first).to_be_visible()
        page.screenshot(path=SCREENSHOTS / "schedule-builder.png", full_page=True)
        dialog.get_by_role("button", name="Create schedule", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(page.get_by_role("table").get_by_text("UI schedule " + stamp, exact=True)).to_be_visible()
        page.get_by_role("button", name="Pause UI schedule " + stamp).click()
        expect(page.get_by_role("button", name="Resume UI schedule " + stamp)).to_be_visible()
        page.get_by_role("button", name="UI schedule " + stamp, exact=True).click()
        dialog.get_by_label("Interval in seconds", exact=True).fill("7200")
        dialog.get_by_role("button", name="Save schedule version", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(page.get_by_role("button", name="Resume UI schedule " + stamp)).to_be_visible()


        navigation.get_by_role("button", name="Research", exact=True).click()
        page.get_by_label("Research batch").select_option(batch["id"])
        expect(page.get_by_role("heading", name="Group evidence", exact=True)).to_be_visible(timeout=30000)
        expect(page.get_by_text("demo", exact=True).first).to_be_visible()
        for heading in ["Verified provider verdicts", "Reliability and 95% intervals", "Completion trend over time", "Session latency distribution", "Execution progress", "Execution attempt performance", "Browser × environment reliability", "Individual trial outcome matrix", "Experiment execution timeline"]:
            expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
        page.locator(".trial-matrix button").first.click()
        expect(dialog.get_by_text("Ground truth", exact=True)).to_be_visible()
        dialog.get_by_role("button", name="Close dialog").click()

        # The immutable export is intentionally unavailable until all sessions finish.
        for _ in range(60):
            latest = next(b for b in client.get("/batches?limit=100").json()["items"] if b["id"] == batch["id"])
            if latest["status"] == "completed":
                break
            page.wait_for_timeout(1000)
        expect(page.get_by_role("button", name="JSON", exact=True)).to_be_enabled(timeout=15000)
        with page.expect_download() as download:
            page.get_by_role("button", name="JSON", exact=True).click()
        exported = json.loads(download.value.path().read_text())
        assert exported.get("mode", exported.get("comparison", {}).get("mode")) == "demo"
        page.screenshot(path=SCREENSHOTS / "research.png", full_page=True)

        unavailable = next((adapter for adapter in client.get("/adapters").json() if adapter["id"] in ("kameleo", "cloakbrowser") and not adapter["available"]), None)
        if unavailable:
            partial_response = client.post("/batches", json={"name": "UI partial live " + stamp, "target_id": target["id"], "workflow_id": workflow["id"], "count": 2, "concurrency": 1, "adapters": ["playwright", unavailable["id"]], "mode": "live", "artifact_policy": "all", "idempotency_key": "ui-partial-" + stamp})
            partial_response.raise_for_status()
            partial = partial_response.json()
            for _ in range(90):
                records = client.get("/sessions", params={"batch_id": partial["id"]}).json()["items"]
                if all(item["status"] in ("completed", "failed", "blocked", "cancelled", "needs_reconciliation") for item in records):
                    break
                page.wait_for_timeout(1000)
            assert len(records) == 2
            assert any(item["status"] == "completed" for item in records), records
            assert any(item["status"] in ("failed", "blocked") for item in records), records
            navigation.get_by_role("button", name="Sessions", exact=True).click()
            page.get_by_role("button", name="Batches", exact=True).click()
            page.get_by_role("button", name="UI partial live " + stamp, exact=True).click()
            expect(page.get_by_text("1–2 of 2", exact=True)).to_be_visible()
            page.screenshot(path=SCREENSHOTS / "partial-live-batch.png", full_page=True)
            successful = next(item for item in records if item["status"] == "completed")
            retained = client.get(f"/sessions/{successful['id']}/artifacts").json()
            assert retained, "Capture-all live session did not persist an artifact"
            page.get_by_role("button", name="Open session " + successful["id"]).click()
            with page.expect_download() as artifact_download:
                page.get_by_role("button", name="Download " + retained[0]["name"], exact=True).click()
            assert artifact_download.value.path().stat().st_size == retained[0]["size"]
            page.get_by_role("button", name="Close session details").click()


        navigation.get_by_role("button", name="Fleet & settings", exact=True).click()
        expect(page.get_by_role("heading", name="Worker pools", exact=True)).to_be_visible()
        page.get_by_role("button", name="Browser integrations", exact=True).click()
        expect(page.get_by_role("heading", name="Playwright Chromium", exact=True)).to_be_visible()
        page.get_by_role("button", name="Project & team", exact=True).click()
        expect(page.get_by_role("heading", name="Project members", exact=True)).to_be_visible()

        # UI-only fixtures validate virtualized list size, pagination and viewer
        # command payloads without pretending that 500 browsers were executed.
        sample = client.get("/sessions?limit=1").json()["items"][0]
        fixture = [{**sample, "id": f"ui-fixture-{i:04}", "status": "awaiting_operator" if i == 0 else "queued"} for i in range(500)]
        def session_page(route):
            start = int(parse_qs(urlparse(route.request.url).query).get("offset", ["0"])[0])
            route.fulfill(json={"items": fixture[start:start + 50], "total": 500, "limit": 50, "offset": start})
        page.route("**/api/v1/sessions?**", session_page)
        page.route("**/api/v1/sessions/ui-fixture-0000/artifacts", lambda route: route.fulfill(json=[]))
        page.route("**/api/v1/sessions/ui-fixture-0000/observations", lambda route: route.fulfill(json=[{"id":"failed-callback-fixture","verification_status":"failed","verdict":"Clean","raw_code":0,"accepted":False,"source":"callback","provider_version":"5"},{"id":"negative-verified-fixture","verification_status":"verified","verdict":"Bot","raw_code":9,"accepted":False,"source":"controlled_site","provider_version":"5"}]))
        page.route("**/api/v1/sessions/ui-fixture-0000/events", lambda route: route.fulfill(json={"items": [], "total": 0, "limit": 100, "offset": 0}))
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJd8AAAAASUVORK5CYII=")
        page.route("**/api/v1/sessions/ui-fixture-0000/frame", lambda route: route.fulfill(body=png, content_type="image/png"))
        commands = []
        def command(route):
            payload = route.request.post_data_json
            assert payload["idempotency_key"]
            commands.append(payload)
            route.fulfill(json=fixture[0])
        page.route("**/api/v1/sessions/ui-fixture-0000/commands", command)
        navigation.get_by_role("button", name="Sessions", exact=True).click()
        expect(page.get_by_text("1–50 of 500", exact=True)).to_be_visible()
        assert page.get_by_role("listitem").count() < 50
        page.get_by_role("button", name="Next page", exact=True).click()
        expect(page.get_by_text("51–100 of 500", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Open session ui-fixture-0050", exact=True)).to_be_visible()
        page.get_by_role("button", name="Previous page", exact=True).click()
        expect(page.get_by_text("1–50 of 500", exact=True)).to_be_visible()

        page.get_by_role("button", name="Open session ui-fixture-0000").click()
        expect(page.get_by_text("verdict unavailable", exact=True)).to_be_visible()
        expect(page.get_by_text("Bot", exact=True)).to_be_visible()
        expect(page.locator(".detail-drawer").get_by_text("Clean", exact=True)).not_to_be_visible()

        page.get_by_role("button", name="Open viewer", exact=True).click()
        page.get_by_role("button", name="Take control", exact=True).click()
        page.get_by_label("Text to enter in browser").fill("operator input")
        page.get_by_role("button", name="Type", exact=True).click()
        page.get_by_role("button", name="Hand back to workflow", exact=True).click()
        expect(page.get_by_role("button", name="Take control", exact=True)).to_be_visible()
        assert [c["command"] for c in commands] == ["claim_control", "type", "continue"]
        page.get_by_role("button", name="Close session details").click()

        page.set_viewport_size({"width": 390, "height": 844})
        for name in ["Overview", "Sessions", "Schedules", "Network", "Research", "Fleet & settings", "Workflows", "Sites"]:
            navigation.get_by_role("button", name=name, exact=True).click()
            expect(page.get_by_role("heading", name=name, exact=True, level=1)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), name
        navigation.get_by_role("button", name="Sessions", exact=True).click()
        page.screenshot(path=SCREENSHOTS / "mobile-sessions.png", full_page=True)
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.tagName") != "BODY"
        long_origin = "https://" + ".".join(["authorized-layout-fixture-" + "x" * 32] * 3) + ".example.test"
        long_target = {**target, "id": "ui-long-origin-layout", "name": "UI long-origin layout fixture", "origin": long_origin, "verified": False, "proof_token": None}
        page.route("**/api/v1/targets*", lambda route: route.fulfill(json=[long_target], headers={"X-Total-Count": "1"}))
        page.goto(WEB + "?view=sites")
        page.get_by_role("button", name="UI long-origin layout fixture", exact=False).click()
        expect(page.get_by_label("Navigation origin", exact=True)).to_have_value(long_origin)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=SCREENSHOTS / "mobile-long-target.png", full_page=True)

        assert not errors, errors
        browser.close()
    print("Platform UI acceptance passed: real controls, persisted demo data; isolated 500-row/viewer fixtures.")

if __name__ == "__main__":
    run()
