"""Check mixed-adapter preflight and preset evidence in the fixture workspace."""

import os
from pathlib import Path

from batch_picker import choose_batch_option
from dotenv import load_dotenv
from playwright.sync_api import expect, sync_playwright

load_dotenv(Path(__file__).resolve().parents[3] / ".env")


def smoke():
    token = os.environ["ABRL_OPERATOR_TOKEN"]
    web = os.getenv("ABRL_SMOKE_WEB", "http://127.0.0.1:8430")
    preset = {
        "id": "desktop_1280",
        "catalog_version": 1,
        "application": "viewport_only",
        "device_class": "adapter_managed",
        "viewport_class": "desktop",
        "viewport": {"width": 1280, "height": 720},
        "device_pixel_ratio": None,
        "locale": None,
        "timezone": None,
        "mode": "round_robin",
        "seed": 177,
    }
    pending = {
        "id": "ui-pending",
        "adapter": "manual_chrome",
        "batch_id": "adapter-alignment-fixture",
        "workflow_name": "Interaction",
        "workflow_version": 1,
        "batch_preset": preset,
        "persistence": "fresh",
        "status": "starting",
        "mode": "live",
        "ground_truth": "operator",
        "epoch": 2,
        "attempt_number": 2,
        "created_at": "2026-01-01T00:00:00Z",
        "metadata": {"environment_epoch": 1, "viewport": {"width": 1440, "height": 900}},
    }
    checked = {
        **pending,
        "id": "ui-checked",
        "status": "running",
        "metadata": {
            "viewport_conformance": {"checks": 2, "matched": 2},
            "viewport": {"width": 1280, "height": 720},
        },
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/v1/sessions?**", lambda route: route.fulfill(json={
            "items": [pending, checked], "total": 2, "limit": 50, "offset": 0,
        }))
        for session_id in ("ui-pending", "ui-checked"):
            for endpoint in ("artifacts", "observations"):
                page.route(f"**/api/v1/sessions/{session_id}/{endpoint}", lambda route: route.fulfill(json=[]))
        page.route("**/api/v1/sessions/ui-*/events", lambda route: route.fulfill(json={
            "items": [], "total": 0, "limit": 100, "offset": 0,
        }))
        page.goto(web)
        page.get_by_label("Operator token").fill(token)
        page.get_by_role("button", name="Connect", exact=True).click()
        nav = page.get_by_role("navigation", name="Workspace")

        nav.get_by_role("button", name="Fleet & settings", exact=True).click()
        page.get_by_role("button", name="Browser integrations", exact=True).click()
        for name, preset_scope in (
            ("Playwright Chromium", "Viewport, DPR, locale, and timezone"),
            ("Manual Google Chrome", "Viewport only"),
            ("Kameleo Chroma", "Viewport only"),
            ("CloakBrowser", "Viewport only"),
        ):
            card = page.locator(".adapter-card").filter(has=page.get_by_role("heading", name=name, exact=True))
            expect(card.get_by_text("Worker capacity", exact=True)).to_be_visible()
            expect(card.get_by_text("unverified", exact=True)).to_be_visible()
            expect(card.get_by_text(preset_scope, exact=True)).to_be_visible()

        nav.get_by_role("button", name="Sessions", exact=True).click()
        page.get_by_role("button", name="Create batch", exact=True).click()
        dialog = page.get_by_role("dialog")
        choose_batch_option(dialog, "Site", "ABRL controlled test site")
        choose_batch_option(dialog, "Workflow", "Interaction")
        dialog.get_by_role("checkbox", name="Manual Google Chrome").check()
        expect(dialog.get_by_text("Manual Google Chrome waits for operator handoff.", exact=False)).to_be_visible()
        dialog.locator("summary").filter(has_text="Advanced").click()
        expect(dialog.get_by_label("New profile persistence", exact=True)).to_have_value("fresh")
        expect(dialog.get_by_role("group", name="Reuse existing profiles")).to_be_visible()
        dialog.get_by_label("Preset distribution", exact=True).select_option("round_robin")
        dialog.get_by_label("Viewport presets", exact=True).select_option(["desktop_1280", "compact_390"])
        dialog.get_by_label("Locale and timezone", exact=True).select_option(["en-GB"])

        with page.expect_response(lambda response: response.request.method == "POST"
                                  and response.url.endswith("/batches/preflight")
                                  and response.request.post_data_json.get("seed") == 177) as preflight:
            dialog.get_by_label("Batch seed", exact=True).fill("177")
        assert preflight.value.ok, preflight.value.text()
        payload = preflight.value.request.post_data_json
        assert payload["adapters"] == ["playwright", "manual_chrome"], payload["adapters"]
        assert payload["batch_presets"]["locales"] == ["en-GB"]
        notice = next(row for row in preflight.value.json()["notices"] if row["code"] == "operator_reserved_steps")
        expect(dialog.get_by_role("group", name="Launch readiness").get_by_text(notice["message"])).to_be_visible()
        dialog.get_by_role("button", name="Close dialog", exact=True).click()

        page.get_by_role("button", name="manual_chrome ui-pending", exact=False).click()
        drawer = page.locator(".detail-drawer")
        expect(drawer.get_by_role("heading", name="Batch preset assignment")).to_be_visible()
        expect(drawer.get_by_text("Awaiting viewport checks", exact=True)).to_be_visible()
        expect(drawer.get_by_text("1440 × 900", exact=False)).to_have_count(0)
        drawer.get_by_role("button", name="Close session details").click()
        page.get_by_role("button", name="manual_chrome ui-checked", exact=False).click()
        expect(drawer.get_by_text("2 of 2 viewport checks matched · Attempt 2", exact=True)).to_be_visible()
        assert not errors, errors
        browser.close()

    print("Mixed-adapter preflight, capability reporting, and preset evidence UI passed.")


if __name__ == "__main__":
    smoke()
