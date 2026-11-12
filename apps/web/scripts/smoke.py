"""Exercise the dashboard and a single live trial on the bundled local site."""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")
WEB_URL = os.getenv("ABRL_WEB_ORIGIN", "http://127.0.0.1:8430")
SCREENSHOTS = os.getenv("ABRL_SMOKE_SCREENSHOTS")


def capture(page, name):
    if SCREENSHOTS:
        folder = Path(SCREENSHOTS)
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=folder / f"{name}.png")


def smoke():
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1366, "height": 960}, accept_downloads=True)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(WEB_URL, wait_until="domcontentloaded")
        page.get_by_label("Operator token").fill(os.environ["ABRL_OPERATOR_TOKEN"])
        page.get_by_role("button", name="Connect", exact=True).click()
        page.get_by_role("heading", name="Overview", exact=True).wait_for()
        page.get_by_role("button", name="Load starter suite", exact=True).click()
        page.get_by_text("Starter sites and workflows are ready.").wait_for()
        capture(page, "overview")

        page.get_by_role("button", name="Sites", exact=True).click()
        page.get_by_label("Search sites", exact=True).fill("ABRL controlled test site")
        page.get_by_role("button", name="ABRL controlled test site").click()
        assert page.get_by_label("Navigation origin", exact=True).input_value().endswith(":8400")
        page.get_by_role("button", name="Workflows", exact=True).click()
        page.get_by_label("Search workflows", exact=True).fill("Sample form")
        page.get_by_role("button", name="Sample form").click()
        assert page.get_by_label("Workflow name").input_value() == "Sample form"
        assert page.get_by_label("Step 5 action").input_value() == "assert_text"

        page.get_by_role("button", name="Workflow preview", exact=True).click()
        modal = page.get_by_role("dialog")
        modal.get_by_label("Site", exact=True).select_option(label="ABRL controlled test site")
        modal.get_by_label("Workflow").select_option(label="Sample form")
        modal.get_by_label("Evidence source").select_option("live")
        modal.get_by_role("button", name="Run preview", exact=True).click()
        page.get_by_role("heading", name="Sample form", exact=True).wait_for()
        page.get_by_text("1 of 1 attempted", exact=True).first.wait_for(timeout=30_000)
        page.get_by_role("heading", name="Recent run events", exact=True).wait_for(timeout=20_000)
        capture(page, "live-run")

        with page.expect_download() as download:
            page.get_by_role("button", name="Export", exact=True).click()
        assert download.value.suggested_filename.endswith(".json")
        report = json.loads(download.value.path().read_text())
        assert report["mode"] == "live"
        assert report["completion"]["successes"] == 1
        assert report["trials"][0]["verification_status"] == "not_collected"

        page.get_by_role("button", name="Diagnostics", exact=True).click()
        page.get_by_role("heading", name="Manual AdScore observations").wait_for()
        page.set_viewport_size({"width": 390, "height": 844})
        page.get_by_role("button", name="Overview", exact=True).click()
        page.get_by_role("heading", name="Overview", exact=True).wait_for()
        capture(page, "mobile-overview")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    smoke()
    print("Dashboard smoke test passed.")
