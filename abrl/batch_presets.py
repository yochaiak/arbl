"""Versioned layout and localization settings for browser batch sessions."""

import hashlib
from typing import Literal, TypedDict

from pydantic import Field, model_validator

from abrl.schemas import Input

PresetId = Literal["desktop_1280", "desktop_1440", "compact_390", "tablet_768"]
PresetLocale = Literal["en-US", "en-GB", "he-IL"]
PresetMode = Literal["fixed", "round_robin", "seeded"]
PresetApplication = Literal["context", "viewport_only"]
ViewportClass = Literal["desktop", "phone", "tablet"]
MAX_BATCH_SEED = 2**31 - 1

# Keep version 1 entries stable so persisted assignments remain readable and reproducible.
_VIEWPORTS: dict[PresetId, tuple[ViewportClass, int, int, int]] = {
    "desktop_1280": ("desktop", 1280, 720, 1),
    "desktop_1440": ("desktop", 1440, 900, 2),
    "compact_390": ("phone", 390, 844, 3),
    "tablet_768": ("tablet", 768, 1024, 2),
}
_TIMEZONES: dict[PresetLocale, str] = {
    "en-US": "America/New_York",
    "en-GB": "Europe/London",
    "he-IL": "Asia/Jerusalem",
}
_MODES = ("fixed", "round_robin", "seeded")


class BatchPresetOptions(Input):
    catalog_version: Literal[1] = 1
    mode: PresetMode = "fixed"
    presets: list[PresetId] = Field(min_length=1, max_length=len(_VIEWPORTS))
    locales: list[PresetLocale] = Field(default_factory=lambda: ["en-US"], min_length=1, max_length=len(_TIMEZONES))

    @model_validator(mode="after")
    def validate_distribution(self):
        if len(set(self.presets)) != len(self.presets) or len(set(self.locales)) != len(self.locales):
            raise ValueError("Batch presets and locales must be unique")
        if self.mode == "fixed" and (len(self.presets) != 1 or len(self.locales) != 1):
            raise ValueError("Fixed distribution requires one preset and one locale")
        return self


class PresetDetails(TypedDict):
    catalog_version: Literal[1]
    id: PresetId
    application: PresetApplication
    device_class: Literal["desktop", "adapter_managed"]
    viewport_class: ViewportClass
    viewport: dict[str, int]
    device_pixel_ratio: int | None
    locale: PresetLocale | None
    timezone: str | None
    mode: PresetMode
    seed: int


def _details(preset: PresetId, locale: PresetLocale | None, mode: PresetMode, seed: int) -> PresetDetails:
    viewport_class, width, height, scale = _VIEWPORTS[preset]
    return {
        "catalog_version": 1,
        "id": preset,
        "application": "context" if locale else "viewport_only",
        "device_class": "desktop" if locale else "adapter_managed",
        "viewport_class": viewport_class,
        "viewport": {"width": width, "height": height},
        "device_pixel_ratio": scale if locale else None,
        "locale": locale,
        "timezone": _TIMEZONES[locale] if locale else None,
        "mode": mode,
        "seed": seed,
    }


def select_preset(
    options: BatchPresetOptions, *, ordinal: int, seed: int, application: PresetApplication = "context",
) -> PresetDetails:
    """Select from request-ordered options using a one-based adapter ordinal."""
    locales = tuple(options.locales) if application == "context" else (None,)
    count = len(options.presets) * len(locales)
    cycle, position = divmod(ordinal - 1, count)
    if options.mode == "seeded":
        # Shuffle each cycle without replacement, independent of allocation or retry order.
        prefix = f"abrl/batch-presets/v1/{seed}/{cycle}/"
        order = sorted(range(count), key=lambda index: hashlib.sha256(f"{prefix}{index}".encode()).digest())
        position = order[position]
    preset_index, locale_index = divmod(position, len(locales))
    return _details(options.presets[preset_index], locales[locale_index], options.mode, seed)


def preset_details(assignment: object) -> PresetDetails | None:
    """Project only the catalog fields and the bounded integer seed from a stored assignment."""
    if not isinstance(assignment, dict):
        return None
    version = assignment.get("catalog_version")
    application = assignment.get("application", "context")
    preset, locale, mode, seed = (assignment.get(key) for key in ("id", "locale", "mode", "seed"))
    if (
        type(version) is not int or version != 1
        or not isinstance(preset, str) or preset not in _VIEWPORTS
        or not isinstance(mode, str) or mode not in _MODES
        or type(seed) is not int or not 0 <= seed <= MAX_BATCH_SEED
    ):
        return None
    if application == "viewport_only" and locale is None:
        return _details(preset, None, mode, seed)
    if application == "context" and isinstance(locale, str) and locale in _TIMEZONES:
        return _details(preset, locale, mode, seed)
    return None
