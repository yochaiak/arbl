"""Verify the local Docker Chromium sandbox profile."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_seccomp_keeps_sandbox_with_dropped_outer_capabilities():
    profile = json.loads((ROOT / "infrastructure/docker/chromium-seccomp.json").read_text())
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    allowed = {
        name
        for rule in profile["syscalls"]
        if rule["action"] == "SCMP_ACT_ALLOW" and not rule.get("includes")
        for name in rule["names"]
    }
    assert {"clone", "setns", "unshare", "chroot"} <= allowed
