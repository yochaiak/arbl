"""Opt-in runtime evidence against the owned loopback fixture."""

import json
import os
import platform
import re

import pytest
from test_browser import fixture as fixture
from test_workflow_execution import workflow

from abrl.adapters import adapter_capabilities
from abrl.batch_presets import BatchPresetOptions, select_preset
from abrl.browser import execute_workflow

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv("ABRL_ADAPTER_ALIGNMENT_SMOKE") != "1", reason="Licensed adapter smoke is opt-in",
)]

_ADAPTERS = ("playwright", "manual_chrome", "kameleo", "cloakbrowser")
_COMPATIBILITY = ("adapter", "browser_version", "os", "architecture", "wrapper_version", "engine_version")
_PREREQUISITES = {
    "missing_binary", "missing_display", "missing_engine", "missing_license", "missing_package",
    "incompatible_license", "engine_unavailable", "engine_auth_failed", "engine_version_mismatch",
    "capacity_exhausted", "capacity_unverified", "unverified_license", "persistent_export_unavailable",
    "unsupported_platform", "containment_unavailable",
}


def _public_version(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/+-]{0,79}", value) else None


def _report(adapter, case, status, readiness, *, metadata=None, checks=None):
    metadata = metadata or {}
    code = metadata.get("adapter_status") or (metadata.get("causal_failure") or {}).get("reason")
    if status == "blocked" and not code:
        code = readiness["status"]
    report = {
        "adapter": adapter, "case": case, "status": status, "os": platform.system(),
        "sdk_version": _public_version(metadata.get("wrapper_version") or readiness.get("version")),
        "engine_version": _public_version(metadata.get("engine_version")),
        "browser_version": _public_version(metadata.get("browser_version")),
        "reason": code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else None,
        "checks": checks or {},
    }
    print(json.dumps(report, sort_keys=True), flush=True)
    return report


@pytest.mark.parametrize("adapter", _ADAPTERS)
@pytest.mark.parametrize("case", ("workflow", "cancellation", "profile_restore"))
async def test_real_adapter_alignment(fixture, tmp_path, adapter, case):
    readiness = next(item for item in adapter_capabilities() if item["id"] == adapter)
    if not readiness["available"]:
        _report(adapter, case, "blocked", readiness)
        pytest.fail("Adapter prerequisites are unavailable", pytrace=False)
    origin, _, _ = fixture
    operator = readiness["features"]["workflow_control"] == "operator"
    preset = select_preset(
        BatchPresetOptions(presets=["desktop_1280"]), ordinal=1, seed=42,
        application=readiness["features"]["batch_preset_application"],
    )
    kinds, artifacts = set(), set()
    cancel_pending = False

    async def emit(event):
        nonlocal cancel_pending
        kind = event["kind"]
        kinds.add(kind)
        if kind == "artifact":
            artifacts.add(event["media_type"])
        if case == "cancellation" and kind == "workflow_step" and event.get("action") == "wait_for":
            cancel_pending = True

    async def cancelled():
        return cancel_pending

    async def handoff():
        # Exercise the authenticated control callback contract with a fixture operator.
        return {"command": "continue"}

    async def run(path, *, profile=None, expected=None):
        steps = [{"action": "navigate", "url": path}]
        if case == "workflow" and not operator:
            steps.append({"action": "open_tab", "tab": "second", "url": path})
        if case == "cancellation":
            steps.append({"action": "wait_for", "selector": "#fixture-never-visible", "timeout_ms": 8000})
        selector = "#state" if case == "profile_restore" and expected else "#profile-cookie" if profile else "#name"
        check = {"kind": "text_contains", "selector": selector, "text": "alignment"} if expected else {
            "kind": "visible", "selector": selector,
        }
        config = workflow(steps, success_check=check)
        return await execute_workflow(
            origin=origin, resource_origins=[], steps=config["steps"], workflow=config,
            adapter=adapter, cancel=cancelled, emit=emit, control=handoff if operator else None,
            batch_preset=preset, artifact_policy="all" if case == "workflow" else "none",
            profile_dir=str(profile) if profile else None, expected_compatibility=expected,
        )

    if case == "profile_restore":
        profile = tmp_path / "profile"
        initialized = await run("/lab?profile=alignment", profile=profile)
        result = initialized
        checks = {"profile_saved": initialized.metadata.get("profile_saved") is True}
        if initialized.outcome == "completed" and checks["profile_saved"]:
            expected = {key: initialized.metadata[key] for key in _COMPATIBILITY if key in initialized.metadata}
            result = await run("/lab?profile=read", profile=profile, expected=expected)
            checks["profile_restored"] = result.metadata.get("profile_launch_state") == "restored"
            checks["state_restored"] = result.metadata.get("completion_status") == "verified"
    else:
        result = await run("/lab")
        checks = {}
        if case == "workflow":
            count = 1 if operator else 2
            checks.update(
                viewport_conformance=result.metadata.get("viewport_conformance") == {"checks": count, "matched": count},
                artifacts=result.metadata.get("artifact_capture") == "completed" and "image/png" in artifacts,
                operator_handoff="manual_handoff" in kinds if operator else None,
            )
    checks["runtime_stopped"] = result.metadata.get("runtime_stopped") is True
    checks["outcome"] = result.outcome == ("cancelled" if case == "cancellation" else "completed")
    passed = all(value for value in checks.values() if value is not None)
    status = "passed" if passed else "blocked" if result.metadata.get("adapter_status") in _PREREQUISITES else "failed"
    _report(adapter, case, status, readiness, metadata=result.metadata, checks=checks)
    if not passed:
        pytest.fail(f"{adapter} {case} {status}", pytrace=False)
