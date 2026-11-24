"""Live UI/API validation for immutable templates, finite budgets and server queries."""

import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
from batch_picker import choose_batch_option
from dotenv import load_dotenv
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")
token = os.environ["ABRL_OPERATOR_TOKEN"]
client = httpx.Client(
    base_url=os.getenv("ABRL_SMOKE_API", "http://127.0.0.1:8400") + "/api/v1",
    headers={"Authorization": "Bearer " + token, "X-Project-ID": "default"}, timeout=30,
)
client.post(os.getenv("ABRL_SMOKE_API", "http://127.0.0.1:8400") + "/test/seed").raise_for_status()
target = next(row for row in client.get("/targets").json() if row["name"] == "ABRL schedule test site")
workflow = next(row for row in client.get("/workflows").json() if row["name"] == "Landing check")
stamp = str(int(time.time()))
name = "UI reusable " + stamp
schedule = None
evidence = Path(os.getenv("ABRL_SMOKE_SCREENSHOTS", str(ROOT / ".abrl/ui-evidence")))
evidence.mkdir(parents=True, exist_ok=True)
errors = []
queries = []

try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: queries.append(request.url) if "/api/v1/" in request.url else None)
        page.goto(os.getenv("ABRL_SMOKE_WEB", "http://127.0.0.1:8430"))
        page.get_by_label("Operator token").fill(token)
        page.get_by_role("button", name="Connect", exact=True).click()
        navigation = page.get_by_role("navigation", name="Workspace")
        navigation.get_by_role("button", name="Schedules", exact=True).click()
        page.get_by_role("button", name="New schedule", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Schedule name").fill(name)
        dialog.get_by_role("button", name="Configure sessions", exact=True).click()
        choose_batch_option(dialog, "Site", target["name"])
        choose_batch_option(dialog, "Workflow", workflow["name"])
        dialog.get_by_label("Independent sessions", exact=True).fill("1")
        dialog.locator("summary").filter(has_text="Advanced").click()
        dialog.get_by_label("HTTP request budget per session", exact=True).fill("250")
        dialog.get_by_label("Transport budget per session (MiB)", exact=True).fill("25")
        dialog.get_by_label("Template name", exact=True).fill(name)
        dialog.get_by_role("button", name="Save new template", exact=True).click()
        expect(dialog.get_by_label("Batch name", exact=True)).to_be_disabled()
        template = client.get("/session-templates", params={"q": name}).json()["items"][0]
        assert template["version"] == 1
        assert template["batch"]["request_budget"] == 250
        assert template["batch"]["traffic_budget_bytes"] == 25 * 1048576
        dialog.get_by_role("button", name="Detach to edit", exact=True).click()
        expect(dialog.get_by_label("Batch name", exact=True)).to_be_enabled()
        dialog.get_by_label("HTTP request budget per session", exact=True).fill("500")
        dialog.get_by_role("button", name="Save template revision", exact=True).click()
        expect(dialog.get_by_label("Template version", exact=True)).to_have_value("2")
        dialog.get_by_label("Template version", exact=True).fill("1")
        dialog.get_by_role("button", name="Load template version", exact=True).click()
        expect(dialog.get_by_label("HTTP request budget per session", exact=True)).to_have_value("250")
        expect(dialog.get_by_label("HTTP request budget per session", exact=True)).to_be_disabled()
        dialog.screenshot(path=evidence / "session-template-pinned.png")
        box = dialog.bounding_box()
        assert box and abs((box["x"] + box["width"] / 2) - 720) < 3
        dialog.get_by_label("Search session templates", exact=True).fill(name)
        expect(dialog.get_by_label("Saved session template", exact=True).get_by_role("option", name=name + " · v2", exact=True)).to_be_attached()
        dialog.get_by_label("Saved session template", exact=True).select_option(template["id"])
        dialog.get_by_label("Template version", exact=True).fill("1")
        dialog.get_by_role("button", name="Load template version", exact=True).click()
        expect(dialog.get_by_label("HTTP request budget per session", exact=True)).to_have_value("250")
        dialog.get_by_role("button", name="Create schedule", exact=True).click()
        expect(dialog).not_to_be_visible()
        schedule = client.get("/schedules", params={"q": name}).json()["items"][0]
        assert schedule["batch"]["session_template_version"] == 1
        assert schedule["batch"]["session_template_id"] == template["id"]
        assert schedule["batch"]["request_budget"] == 250
        assert schedule["batch"]["artifact_policy"] == "all"
        assert schedule["batch"]["capture_javascript"] is True
        calendar = page.get_by_role("region", name="Upcoming schedule calendar", exact=True)
        next_date = datetime.fromisoformat(schedule["next_fire_at"]).astimezone(ZoneInfo("Asia/Jerusalem"))
        date_label = next_date.strftime("%A, %b %-d, %Y")
        day = calendar.get_by_role("button", name=re.compile("^" + re.escape(date_label) + ","))
        day.click()
        expect(calendar.get_by_role("button", name=name, exact=True)).to_be_visible()
        expect(calendar.locator("time").filter(has_text=next_date.strftime("%-I:%M %p"))).to_be_visible()
        day.press("ArrowRight")
        expect(calendar.get_by_role("group", name="Calendar days").locator('button[aria-pressed="true"]')).to_be_focused()
        page.keyboard.press("ArrowLeft")
        expect(day).to_be_focused()
        calendar.get_by_role("button", name="Next calendar week", exact=True).click()
        expect(calendar.get_by_role("button", name=name, exact=True)).not_to_be_visible()
        calendar.get_by_role("button", name="Previous calendar week", exact=True).click()
        expect(calendar.get_by_role("button", name=name, exact=True)).to_be_visible()
        calendar.screenshot(path=evidence / "schedule-calendar.png")

        navigation.get_by_role("button", name="Network", exact=True).click()
        page.get_by_role("button", name="New policy", exact=True).click()
        dialog.get_by_label("Policy name", exact=True).fill(name)
        dialog.get_by_label("Maximum proxy connections per session", exact=True).fill("4")
        dialog.get_by_role("button", name="Create policy", exact=True).click()
        expect(dialog).not_to_be_visible()
        assert client.get("/proxy-policies", params={"q": name}).json()["items"][0]["connection_load"] == 4
        page.get_by_role("button", name="Proxy inventory", exact=True).click()
        page.get_by_role("button", name="Register inventory", exact=True).first.click()
        dialog.get_by_label("Inventory name", exact=True).fill(name)
        dialog.get_by_label("Enable for eligible assignments", exact=True).uncheck()
        dialog.get_by_label("Known cost per GB (optional)", exact=True).fill("2.5")
        dialog.get_by_label("Proxy cost currency", exact=True).fill("USD")
        dialog.get_by_role("button", name="Register inventory", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(page.get_by_role("columnheader", name="Reserved connections", exact=True)).to_be_visible()
        item = client.get("/proxy-inventory", params={"q": name}).json()["items"][0]
        assert item["cost_per_gb"] == 2.5 and item["cost_currency"] == "USD"
        page.get_by_label("Search inventory", exact=True).fill(name)
        page.screenshot(path=evidence / "network-filtered.png", full_page=True)

        navigation.get_by_role("button", name="Fleet & settings", exact=True).click()
        expect(page.get_by_role("heading", name="Cost coverage", exact=True)).to_be_visible()
        expect(page.get_by_text("Unknown until every component is priced and reconciled", exact=True)).to_be_visible()
        page.get_by_label("Search workers", exact=True).fill("local")
        page.get_by_label("Worker readiness", exact=True).select_option("true")
        page.screenshot(path=evidence / "fleet-cost-coverage.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        page.screenshot(path=evidence / "mobile-fleet-cost.png", full_page=True)
        assert any(parse_qs(urlparse(url).query).get("q") == [name] and "/session-templates?" in url for url in queries)
        assert not errors, errors
        browser.close()
        print("Template UI/API passed: immutable revisions, explicit detach, pinned schedule reuse, budgets, connection capacity, currency, server filtering/order/pagination and mobile cost coverage.")
finally:
    if schedule:
        client.post(f"/schedules/{schedule['id']}/pause").raise_for_status()
    client.close()
