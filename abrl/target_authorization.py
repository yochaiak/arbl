"""Live authorization for pinned versions of registered sites."""

from collections.abc import Mapping
from typing import Any

from abrl.db import Target

LIVE_SITE_APPROVAL_REQUIRED = "Select the current version of a registered site."


def target_is_live_ready(target: Target | None, version: int, config: Mapping[str, Any]) -> bool:
    if target is None or target.version != version or not config.get("origin"):
        return False
    # Older snapshots may predate the explicit authorization flag.
    return config.get("authorized", target.verified_version == version) is True
