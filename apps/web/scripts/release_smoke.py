"""Focused release verification; restores the current project's original quota."""

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")
token = os.environ["ABRL_OPERATOR_TOKEN"]
api = httpx.Client(
    base_url=os.getenv("ABRL_SMOKE_API", "http://127.0.0.1:8400") + "/api/v1",
    headers={"Authorization": "Bearer " + token, "X-Project-ID": "default"},
    timeout=30,
)
original = next(project for project in api.get("/projects").json() if project["id"] == "default")
changed_quota = original["concurrency"] + 1 if original["concurrency"] < 500 else 499

try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(os.getenv("ABRL_SMOKE_WEB", "http://127.0.0.1:8430"))
        page.get_by_label("Operator token").fill(token)
        page.get_by_role("button", name="Connect", exact=True).click()
        navigation = page.get_by_role("navigation", name="Workspace")
        page.get_by_role("button", name="Create session batch", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_role("heading", name="Create session batch", exact=True)).to_be_visible()
        expect(dialog.get_by_label("Independent sessions", exact=True)).to_have_value("30")
        dialog.get_by_role("button", name="Close dialog", exact=True).click()
        navigation.get_by_role("button", name="Fleet & settings", exact=True).click()
        page.get_by_role("button", name="Project & team", exact=True).click()
        page.get_by_role("button", name="Project settings", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_label("Project concurrency limit", exact=True)).to_have_value(
            str(original["concurrency"])
        )
        dialog.get_by_label("Project concurrency limit", exact=True).fill(str(changed_quota))
        dialog.get_by_role("button", name="Save project settings", exact=True).click()
        expect(dialog).not_to_be_visible()
        stored = next(project for project in api.get("/projects").json() if project["id"] == "default")
        assert stored["concurrency"] == changed_quota
        page.get_by_role("button", name="Project settings", exact=True).click()
        expect(dialog.get_by_label("Project concurrency limit", exact=True)).to_have_value(
            str(changed_quota)
        )
        dialog.get_by_role("button", name="Cancel", exact=True).click()

        navigation.get_by_role("button", name="Sessions", exact=True).click()
        page.get_by_role("button", name="Create batch", exact=True).click()
        dialog.get_by_role("button", name="Manual control", exact=True).click()
        lifetime = dialog.get_by_label("Manual session lifetime (seconds)", exact=True)
        expect(lifetime).to_have_value("3600")
        expect(lifetime).to_have_attribute("max", "7200")
        lifetime.fill("1800")
        dialog.get_by_label("Manual idle timeout (seconds)", exact=True).fill("1900")
        assert not lifetime.evaluate("element => element.validity.valid")
        dialog.get_by_label("Manual idle timeout (seconds)", exact=True).fill("900")
        assert lifetime.evaluate("element => element.validity.valid")
        dialog.get_by_role("button", name="Close dialog", exact=True).click()

        batches = api.get("/batches?limit=1").json()["items"]
        if batches:
            navigation.get_by_role("button", name="Research", exact=True).click()
            page.get_by_label("Research batch", exact=True).select_option(batches[0]["id"])
            expect(page.get_by_role("columnheader", name="Primary reliability", exact=True)).to_be_visible()
            expect(
                page.get_by_role("columnheader", name="Operational reliability", exact=True)
            ).to_be_visible()

        navigation.get_by_role("button", name="Sites", exact=True).click()
        page.get_by_role("button", name="New site", exact=True).click()
        rate = page.get_by_label("Trial starts per minute", exact=True)
        expect(rate).to_have_value("30")
        expect(rate).to_have_attribute("max", "6000")
        rate.fill("6000")
        assert rate.evaluate("element => element.validity.valid")
        assert not errors, errors
        browser.close()
        print(
            "Release UI verified: persisted project quota, reloaded value, default rate 30, maximum 6000; no browser errors."
        )
finally:
    response = api.put(
        "/projects/default", json={"name": original["name"], "concurrency": original["concurrency"]}
    )
    response.raise_for_status()
    api.close()
