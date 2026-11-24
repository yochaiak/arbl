"""Exercise the structured workflow editor with the local workspace services."""

import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")


def smoke():
    token = os.environ["ABRL_OPERATOR_TOKEN"]
    web = os.getenv("ABRL_SMOKE_WEB", "http://127.0.0.1:8430")
    api_url = os.getenv("ABRL_SMOKE_API", "http://127.0.0.1:8400").rstrip("/") + "/api/v1"
    suffix = uuid4().hex[:8]
    block_name = f"Workflow builder block {suffix}"
    workflow_name = f"Workflow builder journey {suffix}"
    headers = {"Authorization": "Bearer " + token, "X-Project-ID": "default"}
    with httpx.Client(base_url=api_url, headers=headers, timeout=10) as api, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 960}, accept_downloads=True)
        page.goto(web)
        page.get_by_label("Operator token").fill(token)
        page.get_by_role("button", name="Connect", exact=True).click()
        page.get_by_role("navigation", name="Workspace").get_by_role("button", name="Workflows", exact=True).click()
        page.get_by_role("button", name="New workflow", exact=True).click()

        page.get_by_role("button", name="Workflow blocks", exact=True).click()
        library = page.get_by_role("dialog", name="Project workflow blocks")
        library.get_by_label("Block name", exact=True).fill(block_name)
        library.get_by_label("Step 1 action", exact=True).select_option("wait_for")
        library.get_by_label("Element selector", exact=True).fill("#lab-ready")
        with page.expect_response(
            lambda response: response.url.endswith("/workflow-blocks") and response.request.method == "POST"
        ) as creation:
            library.get_by_role("button", name="Save new block", exact=True).click()
        assert creation.value.ok, creation.value.text()
        block = creation.value.json()
        assert block["version"] == 1
        expect(library.get_by_label("Edit workflow block", exact=True)).to_have_value(block["id"])
        library.get_by_role("button", name="Close dialog", exact=True).click()

        page.get_by_label("Workflow name", exact=True).fill(workflow_name)
        page.get_by_label("Navigation path or URL", exact=True).fill("/lab")
        page.get_by_role("button", name="Add workflow input", exact=True).click()
        inputs = page.get_by_role("group", name="Workflow inputs")
        inputs.get_by_label("Input name", exact=True).fill("variant")
        inputs.get_by_label("Input type", exact=True).select_option("boolean")
        inputs.get_by_label("Default value", exact=True).select_option("false")

        page.get_by_role("button", name="Add step", exact=True).click()
        page.get_by_label("Step 2 action", exact=True).select_option("if")
        page.get_by_label("Condition", exact=True).select_option("value_equals")
        page.get_by_label("Workflow value name", exact=True).fill("variant")
        page.get_by_label("Compare as", exact=True).select_option("boolean")
        page.get_by_label("Expected value", exact=True).select_option("false")
        match = page.get_by_role("group", name="When the condition matches")
        match.get_by_role("button", name="Add nested step", exact=True).click()
        match.get_by_label("Step 2.1 action", exact=True).select_option("use_block")
        match.get_by_label("Reusable block", exact=True).select_option(block["id"])
        expect(match.get_by_label("Pinned block revision", exact=True)).to_have_value("1")
        otherwise = page.get_by_role("group", name="Otherwise")
        otherwise.get_by_role("button", name="Add nested step", exact=True).click()
        otherwise.get_by_label("Step 2.else.1 action", exact=True).select_option("repeat")
        repeat = otherwise.get_by_role("group", name="Repeat these steps")
        repeat.get_by_role("button", name="Add nested step", exact=True).click()
        repeat.get_by_label("Step 2.else.1.1 action", exact=True).select_option("wait_for_hidden")
        repeat.get_by_label("Element selector", exact=True).fill(".spinner")

        final_check = page.get_by_role("group", name="Success check")
        final_check.get_by_label("Verify the result after the last step").check()
        final_check.get_by_label("Condition selector", exact=True).fill("#lab-ready")
        with page.expect_response(
            lambda response: response.url.endswith("/workflows") and response.request.method == "POST"
        ) as creation:
            page.get_by_role("button", name="Save workflow", exact=True).click()
        assert creation.value.ok, creation.value.text()
        workflow = creation.value.json()
        expect(page.get_by_role("button", name="Save new version", exact=True)).to_be_visible()
        assert workflow["schema_version"] == 2
        assert workflow["inputs"]["variant"]["default"] is False
        assert workflow["success_check"] == {"kind": "visible", "selector": "#lab-ready"}
        saved_reference = workflow["steps"][1]["steps"][0]
        assert saved_reference["block_version"] == 1
        assert saved_reference["steps"][0]["selector"] == "#lab-ready"
        assert workflow["steps"][1]["else_steps"][0]["steps"][0]["action"] == "wait_for_hidden"

        api.put("/workflow-blocks/" + block["id"], json={
            "name": block_name,
            "steps": [{"action": "wait_for", "selector": "#later-revision"}],
        }).raise_for_status()
        with page.expect_download() as download:
            page.get_by_role("button", name="Export", exact=True).click()
        exported = json.loads(download.value.path().read_text())
        assert exported["steps"][1]["steps"][0]["steps"][0]["selector"] == "#lab-ready"
        page.get_by_role("button", name="New workflow", exact=True).click()
        page.get_by_label("Import workflow JSON").set_input_files({
            "name": "workflow.json",
            "mimeType": "application/json",
            "buffer": json.dumps(exported).encode(),
        })
        expect(page.get_by_label("Workflow name", exact=True)).to_have_value(workflow_name)
        expect(page.get_by_label("Pinned block revision", exact=True)).to_have_value("1")
        expect(page.get_by_label("Step 2.else.1.1 action", exact=True)).to_have_value("wait_for_hidden")
        browser.close()


if __name__ == "__main__":
    smoke()
    print("Structured workflow builder, pinned blocks, and JSON round-trip passed.")
