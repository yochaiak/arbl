"""Exercise a live workflow preview against the make dev-fixtures test site."""

import json
import os
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import httpx
from batch_picker import choose_batch_option
from dotenv import load_dotenv
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")
WEB_URL = os.getenv("ABRL_WEB_ORIGIN", "http://127.0.0.1:8430")
API_URL = os.getenv("ABRL_SMOKE_API", "http://127.0.0.1:8400").rstrip("/")
SCREENSHOTS = os.getenv("ABRL_SMOKE_SCREENSHOTS")


def capture(page, name):
    if SCREENSHOTS:
        folder = Path(SCREENSHOTS)
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=folder / f"{name}.png")


def smoke():
    token = os.environ["ABRL_OPERATOR_TOKEN"]
    headers = {"Authorization": f"Bearer {token}", "X-Project-ID": "default"}
    seed = httpx.post(f"{API_URL}/test/seed", headers=headers, timeout=10)
    seed.raise_for_status()
    batch_name = f"Workflow preview smoke {uuid4().hex[:8]}"
    errors = []
    with httpx.Client(base_url=f"{API_URL}/api/v1", headers=headers, timeout=10) as api, sync_playwright() as playwright:
        workflows = api.get("/workflows", params={"q": "Sample form"}).raise_for_status().json()
        workflow = next(entry for entry in workflows if entry["name"] == "Sample form")
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1366, "height": 960}, accept_downloads=True)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(WEB_URL, wait_until="domcontentloaded")
        page.get_by_label("Operator token").fill(token)
        page.get_by_role("button", name="Connect", exact=True).click()
        page.get_by_role("heading", name="Overview", exact=True).wait_for()
        expect(page.get_by_role("button", name="Load starter suite", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="Research", exact=True)).to_have_count(0)
        capture(page, "overview")

        page.get_by_role("button", name="Sites", exact=True).click()
        page.get_by_label("Search sites", exact=True).fill("ABRL controlled test site")
        page.get_by_role("button", name="ABRL controlled test site").click()
        assert page.get_by_label("Navigation origin", exact=True).input_value() == API_URL
        page.get_by_role("button", name="Workflows", exact=True).click()
        page.get_by_label("Search workflows", exact=True).fill("Sample form")
        page.get_by_role("button", name="Sample form").click()
        assert page.get_by_label("Workflow name").input_value() == "Sample form"
        assert page.get_by_label("Step 5 action").input_value() == "assert_text"

        page.get_by_role("button", name="Workflow preview", exact=True).click()
        modal = page.get_by_role("dialog", name="Preview workflow")
        modal.get_by_label("Batch name", exact=True).fill(batch_name)
        choose_batch_option(modal, "Site", "ABRL controlled test site")
        choose_batch_option(modal, "Workflow", workflow["name"])
        expect(modal.get_by_label("Independent sessions", exact=True)).to_have_value("1")
        modal.locator("summary").filter(has_text="Advanced").click()
        expect(modal.get_by_label("Screenshot and console capture", exact=True)).to_have_value("all")
        expect(modal.get_by_label("Capture JavaScript assets and execution coverage")).to_be_checked()
        modal.get_by_label("Device", exact=True).select_option("phone")
        modal.get_by_role("button", name="Create batch", exact=True).click()
        expect(modal).not_to_be_visible()
        expect(page.get_by_role("heading", name="Sessions", exact=True)).to_be_visible()

        listed = api.get("/batches", params={"q": batch_name}).raise_for_status().json()["items"]
        batch, = listed
        assert batch["mode"] == "live" and batch["count"] == 1
        sessions = api.get("/sessions", params={"batch_id": batch["id"]}).raise_for_status().json()["items"]
        session, = sessions
        page.get_by_role("button", name=f"Open session {session['id']}", exact=True).click()
        detail = page.get_by_role("complementary", name="Session details")
        expect(detail.get_by_text("Phone · Mobile and touch emulation · 390 × 844", exact=True)).to_be_visible(
            timeout=30_000
        )
        png = detail.get_by_role("button", name="Download final.png", exact=True)
        expect(png).to_be_visible(timeout=60_000)
        for name in ("final.png", "console.json", "javascript-attempt-1.zip"):
            with page.expect_download() as download:
                detail.get_by_role("button", name=f"Download {name}", exact=True).click()
            assert download.value.suggested_filename == name
            if name.endswith(".png"):
                assert download.value.path().read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            elif name.endswith(".json"):
                assert isinstance(json.loads(download.value.path().read_text()), list)
            else:
                with ZipFile(download.value.path()) as archive:
                    assert "manifest.json" in archive.namelist()
        capture(page, "live-session")
        detail.get_by_role("button", name="Close session details").click()

        page.get_by_role("button", name="Diagnostics", exact=True).click()
        expect(page.get_by_role("heading", name="Diagnostics", exact=True)).to_be_visible()
        expect(page.get_by_role("heading", name="Manual AdScore observations")).to_have_count(0)
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
