"""Saved experiment definition/version/readiness/execution browser acceptance."""

import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")


def run():
    name = "UI saved experiment " + str(int(time.time()))
    with httpx.Client(
        base_url=os.getenv("ABRL_SMOKE_API", "http://127.0.0.1:8400") + "/api/v1",
        headers={"Authorization": "Bearer " + os.environ["ABRL_OPERATOR_TOKEN"]},
        timeout=30,
    ) as api:
        api.post("/starter").raise_for_status()
        workflows = {workflow["name"]: workflow["id"] for workflow in api.get("/workflows").json()}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(os.getenv("ABRL_SMOKE_WEB", "http://127.0.0.1:8430"))
        page.get_by_label("Operator token").fill(os.environ["ABRL_OPERATOR_TOKEN"])
        page.get_by_role("button", name="Connect", exact=True).click()
        page.get_by_role("navigation").get_by_role("button", name="Research", exact=True).click()
        page.get_by_role("button", name="Experiments", exact=True).click()
        page.get_by_role("button", name="New experiment", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Experiment name", exact=True).fill(name)
        dialog.get_by_label("Experiment hypothesis").fill(
            "Compare independent saved workflow groups under a declared baseline."
        )
        dialog.get_by_label("Batch name", exact=True).fill("Saved experiment group A")
        dialog.get_by_label("Run concurrency", exact=True).fill("1")
        dialog.get_by_label("Workflow", exact=True).select_option(workflows["Landing check"])
        dialog.get_by_label("Independent sessions", exact=True).fill("1")
        dialog.get_by_role("button", name="Add group", exact=True).click()
        dialog.get_by_label("Batch name", exact=True).fill("Saved experiment group B")
        dialog.get_by_label("Workflow", exact=True).select_option(workflows["Sample form"])
        dialog.get_by_label("Independent sessions", exact=True).fill("1")
        dialog.get_by_role("button", name="Save draft experiment", exact=True).click()
        expect(dialog).not_to_be_visible()
        row = page.get_by_role("row").filter(has=page.get_by_role("button", name=name, exact=True))
        expect(row.get_by_text("2 / 2", exact=True)).to_be_visible()
        row.get_by_role("button", name="Validate", exact=True).click()
        expect(
            dialog.get_by_text("No configuration blockers were reported for this evidence mode.")
        ).to_be_visible()
        dialog.get_by_role("button", name="Close", exact=True).click()
        row.get_by_role("button", name="Execute " + name, exact=True).click()
        expect(page.get_by_role("status")).to_contain_text("was created")
        row.get_by_role("button", name="History for " + name, exact=True).click()
        expect(dialog.get_by_text("Saved experiment group A", exact=True)).to_be_visible()
        expect(dialog.get_by_text("Saved experiment group B", exact=True)).to_be_visible()
        dialog.get_by_role("button", name="Close dialog").click()
        row.get_by_role("button", name=name, exact=True).click()
        dialog.get_by_label("Description", exact=True).fill(
            "Version two preserves the earlier execution snapshot."
        )
        dialog.get_by_role("button", name="Save experiment version", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(row.get_by_text("v2", exact=True)).to_be_visible()
        expect(row.get_by_role("button", name="Execute " + name, exact=True)).to_be_disabled()
        row.get_by_role("button", name="Clone " + name, exact=True).click()
        expect(page.get_by_role("button", name=name + " copy", exact=True)).to_be_visible()
        row.get_by_role("button", name="History for " + name, exact=True).click()
        expect(dialog.get_by_text("definition v1", exact=False)).to_be_visible()
        dialog.get_by_role("button", name="View comparison", exact=True).first.click()
        expect(page.get_by_role("heading", name="Group evidence", exact=True)).to_be_visible(timeout=30000)
        page.get_by_role("button", name="Experiments", exact=True).click()
        row.get_by_role("button", name="History for " + name, exact=True).click()
        dialog.get_by_role("button", name="View execution report", exact=True).click()
        expect(page.get_by_role("heading", name="Experiment execution report", exact=True)).to_be_visible()
        conditions = page.get_by_label("Matched conditions", exact=True)
        expect(conditions.locator("option")).to_have_count(2)
        options = conditions.locator("option").evaluate_all(
            "options => options.map(option => option.value)"
        )
        conditions.select_option(options[1])
        expect(conditions).to_have_value(options[1])
        expect(page.get_by_role("button", name="JSON", exact=True)).to_be_enabled(timeout=45000)
        with page.expect_download() as download:
            page.get_by_role("button", name="JSON", exact=True).click()
        report = json.loads(download.value.path().read_text())
        assert report["scope"] == "experiment_run" and report["mode"] == "demo"
        assert len(report["strata"]) == 2 and len(report["trials"]) == 2
        assert len({trial["batch_id"] for trial in report["trials"]}) == 2
        assert not report["recommendation"]["candidates"]
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.get_by_role("heading", name="Experiment execution report", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.get_by_role("button", name="Experiments", exact=True).click()
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.get_by_role("heading", name="Experiment definitions")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        browser.close()
    print(
        "Saved experiment browser acceptance passed: multi-group drafts, validation, run history, versioning, clone, report navigation, mobile."
    )


if __name__ == "__main__":
    run()
