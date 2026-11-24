"""Session options shared by admission and browser runtimes."""

import os
from dataclasses import dataclass
from typing import Literal, TypedDict

from abrl.batch_presets import PresetApplication
from abrl.schemas import CONTROL_ACTIONS, OBSERVATION_ACTIONS

DevicePreset = Literal["phone_small", "phone", "phone_large", "tablet", "tablet_landscape"]


@dataclass(frozen=True)
class DeviceViewport:
    width: int
    height: int
    scale: int

    @property
    def size(self) -> dict[str, int]:
        return {"width": self.width, "height": self.height}


DEVICE_VIEWPORTS: dict[DevicePreset, DeviceViewport] = {
    "phone_small": DeviceViewport(360, 800, 2),
    "phone": DeviceViewport(390, 844, 3),
    "phone_large": DeviceViewport(430, 932, 3),
    "tablet": DeviceViewport(768, 1024, 2),
    "tablet_landscape": DeviceViewport(1024, 768, 2),
}

class AdapterFeatures(TypedDict):
    batch_preset_application: PresetApplication
    workflow_control: Literal["automated", "operator"]
    persistent_profiles: Literal["supported", "requires_entitlement"]
    managed_diagnostics: bool


@dataclass(frozen=True)
class AdapterContract:
    pool: Literal["standard", "vendor"]
    features: AdapterFeatures
    capabilities: tuple[str, ...] = ("responsive_viewport",)


_ADAPTER_CONTRACTS = {
    "playwright": AdapterContract("standard", {
        "batch_preset_application": "context", "workflow_control": "automated",
        "persistent_profiles": "supported", "managed_diagnostics": True,
    }, ("responsive_viewport", "mobile_emulation", "user_agent_override")),
    "manual_chrome": AdapterContract("standard", {
        "batch_preset_application": "viewport_only", "workflow_control": "operator",
        "persistent_profiles": "supported", "managed_diagnostics": False,
    }),
    "cloakbrowser": AdapterContract("vendor", {
        "batch_preset_application": "viewport_only", "workflow_control": "automated",
        "persistent_profiles": "supported", "managed_diagnostics": True,
    }),
    "kameleo": AdapterContract("vendor", {
        "batch_preset_application": "viewport_only", "workflow_control": "automated",
        "persistent_profiles": "requires_entitlement", "managed_diagnostics": True,
    }),
}


def adapter_contract(adapter: str) -> AdapterContract | None:
    return _ADAPTER_CONTRACTS.get(adapter)


def adapter_pool(adapter: str) -> str:
    return _ADAPTER_CONTRACTS[adapter].pool


def effective_adapter_limit(adapter: str, declared: int | None) -> int:
    if declared is not None and declared > 0:
        return declared
    if adapter_pool(adapter) == "standard":
        return 500
    try:
        return max(1, int(os.getenv("ABRL_VENDOR_CAPACITY", "1")))
    except ValueError:
        return 1


def operator_controlled(adapter: str) -> bool:
    contract = adapter_contract(adapter)
    return bool(contract and contract.features["workflow_control"] == "operator")


def operator_reserved_step(adapter: str, action: str) -> bool:
    return operator_controlled(adapter) and action not in OBSERVATION_ACTIONS | CONTROL_ACTIONS | {"switch_tab"}


def session_capabilities(adapter: str) -> tuple[str, ...]:
    contract = adapter_contract(adapter)
    return contract.capabilities if contract else ()


def supports_user_agent_override(adapter: str) -> bool:
    return "user_agent_override" in session_capabilities(adapter)


def device_viewport(preset: DevicePreset | None) -> DeviceViewport | None:
    if preset is None:
        return None
    if isinstance(preset, str) and preset in DEVICE_VIEWPORTS:
        return DEVICE_VIEWPORTS[preset]
    raise ValueError("Unknown device preset")
