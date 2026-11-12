from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from abrl.schemas import Input

Adapter = Literal["playwright", "cloakbrowser", "kameleo", "manual_chrome"]


class Preparation(Input):
    mode: Literal["none", "automated", "manual"] = "none"
    min_seconds: int = Field(default=30, ge=1, le=300)
    max_seconds: int = Field(default=90, ge=1, le=300)
    paths: list[str] = Field(default_factory=lambda: ["/"], max_length=20)
    idle_timeout_seconds: int = Field(default=900, ge=30, le=3600)
    lifetime_seconds: int = Field(default=3600, ge=30, le=7200)

    @model_validator(mode="after")
    def validate_paths(self):
        if self.mode == "manual" and self.idle_timeout_seconds > self.lifetime_seconds:
            raise ValueError("Manual lifetime must cover its idle timeout")
        if self.max_seconds < self.min_seconds:
            raise ValueError("Maximum preparation duration cannot be below minimum")
        if any(not p.startswith("/") or p.startswith("//") or "\\" in p for p in self.paths):
            raise ValueError("Preparation paths must be site relative")
        return self


class ProfileInput(Input):
    name: str = Field(min_length=1, max_length=100)
    adapter: Adapter = "playwright"
    persistence: Literal["fresh", "persistent"] = "fresh"


class BatchInput(Input):
    name: str = Field(default="Browser experiment", min_length=1, max_length=100)
    target_id: str
    workflow_id: str
    session_template_id: str | None = None
    session_template_version: int | None = Field(default=None, ge=1)
    count: int = Field(default=30, ge=1, le=2000)
    concurrency: int = Field(default=1, ge=1, le=500)
    adapters: list[Adapter] = Field(default_factory=lambda: ["playwright"], min_length=1, max_length=4)
    persistence: Literal["fresh", "persistent"] = "fresh"
    profile_ids: list[str] = Field(default_factory=list, max_length=2000)
    preparation: Preparation = Field(default_factory=Preparation)
    proxy_policy_id: str | None = None
    mode: Literal["live", "demo"] = "demo"
    phase: Literal["pilot", "confirmation"] = "pilot"
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    idempotency_key: str = Field(min_length=8, max_length=100)
    hypothesis: str = Field(default="", max_length=2000)
    reliability_target: float = Field(default=0.95, gt=0, le=1)
    timeout_seconds: int = Field(default=600, ge=10, le=7200)
    request_budget: int = Field(default=1000, ge=1, le=100000)
    traffic_budget_bytes: int = Field(default=104857600, ge=1024, le=2 * 1024**3)
    artifact_policy: Literal["none", "on_failure", "all"] = "none"
    artifact_retention_days: int = Field(default=30, ge=1, le=365)
    cost_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    cost_per_session: float | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def pilot_group_defaults(cls, value):
        if isinstance(value, dict) and "count" not in value:
            value = {
                **value,
                "count": (300 if value.get("phase") == "confirmation" else 30)
                * len(value.get("adapters", ["playwright"])),
            }
        return value

    @model_validator(mode="after")
    def profiles_match(self):
        if bool(self.session_template_id) != (self.session_template_version is not None):
            raise ValueError("A session template requires its immutable version")
        preparation_budget = (
            self.preparation.lifetime_seconds
            if self.preparation.mode == "manual"
            else self.preparation.max_seconds
            if self.preparation.mode == "automated"
            else 0
        )
        if self.timeout_seconds + preparation_budget > 13200:
            raise ValueError("Combined preparation and workflow budget cannot exceed 13200 seconds")
        if self.profile_ids and (
            len(set(self.profile_ids)) != self.count or len(self.profile_ids) != self.count
        ):
            raise ValueError("Supply one unique profile per session")
        if self.concurrency > self.count:
            raise ValueError("Concurrency cannot exceed session count")
        if "manual_chrome" in self.adapters and self.preparation.mode != "manual":
            raise ValueError("Manual Chrome requires manual preparation")
        return self


class ScheduleInput(Input):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["once", "interval", "cron"]
    timezone: str = "Asia/Jerusalem"
    expression: str | None = Field(default=None, max_length=100)
    interval_seconds: int | None = Field(default=None, ge=60, le=31536000)
    start_at: datetime | None = None
    end_at: datetime | None = None
    batch: BatchInput
    overlap: Literal["skip", "queue_one", "allow"] = "skip"
    misfire: Literal["skip", "coalesce"] = "coalesce"
    grace_seconds: int = Field(default=900, ge=0, le=86400)

    @model_validator(mode="after")
    def trigger_fields(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Unknown IANA timezone") from exc
        if self.kind == "once" and self.start_at is None:
            raise ValueError("One-time schedule requires start_at")
        if self.kind == "interval" and self.interval_seconds is None:
            raise ValueError("Interval schedule requires interval_seconds")
        if self.kind == "cron" and not self.expression:
            raise ValueError("Calendar schedule requires a cron expression")
        for dt in (self.start_at, self.end_at):
            if dt and dt.tzinfo is None:
                raise ValueError("Schedule timestamps require a timezone offset")
        if self.end_at and self.start_at and self.end_at <= self.start_at:
            raise ValueError("End must follow start")
        return self


class ProxyPolicyInput(Input):
    name: str = Field(min_length=1, max_length=100)
    country: str | None = Field(default=None, pattern="^[A-Z]{2}$")
    fallback_countries: list[str] = Field(default_factory=list, max_length=20)
    products: list[Literal["residential", "isp", "dedicated_isp", "datacenter", "dedicated_datacenter"]] = (
        Field(default_factory=lambda: ["datacenter", "isp", "dedicated_isp"])
    )
    continuity: Literal["none", "sticky", "strict", "fixed", "exclusive"] = "fixed"
    continuity_minutes: int = Field(default=60, ge=1, le=1440)
    exclusive: bool = False
    allow_direct: bool = False
    failure_policy: Literal["connection_recovery", "fixed"] = "connection_recovery"
    max_replacements: int = Field(default=1, ge=0, le=3)
    provider_resolved_ack: bool = False
    max_latency_ms: float | None = Field(default=None, gt=0)
    min_transport_success_rate: float | None = Field(default=None, ge=0, le=1)
    connection_load: int = Field(default=1, ge=1, le=32)

    @model_validator(mode="after")
    def product_policy(self):
        if not self.products:
            raise ValueError("Allow at least one proxy product")
        if any(
            len(country) != 2 or not country.isalpha() or not country.isupper()
            for country in self.fallback_countries
        ):
            raise ValueError("Fallback countries require uppercase two-letter codes")
        if (self.continuity in {"fixed", "exclusive"} or self.exclusive) and set(self.products) == {
            "residential"
        }:
            raise ValueError("Residential sessions cannot provide fixed or exclusive inventory")
        return self


class ProxyInventoryInput(Input):
    name: str = Field(min_length=1, max_length=100)
    product: Literal["residential", "isp", "dedicated_isp", "datacenter", "dedicated_datacenter"]
    host: str = Field(default="pr.oxylabs.io", pattern=r"^[A-Za-z0-9.-]+$", max_length=253)
    port: int = Field(default=7777, ge=1, le=65535)
    username_ref: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
    password_ref: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
    country: str = Field(pattern="^[A-Z]{2}$")
    capacity: int = Field(default=1, ge=1, le=500)
    max_duration_minutes: int = Field(default=60, ge=1, le=1440)
    exclusive: bool = False
    cost_per_gb: float | None = Field(default=None, ge=0)
    cost_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def product_defaults(cls, value):
        from abrl.proxy import FIXED_GATEWAYS

        if isinstance(value, dict) and value.get("product") in FIXED_GATEWAYS:
            value = {**value}
            value.setdefault("host", FIXED_GATEWAYS[value["product"]])
            value.setdefault("port", 8001)
        return value

    @model_validator(mode="after")
    def documented_gateway(self):
        from abrl.proxy import validate_inventory_config

        self.host = self.host.lower()
        validate_inventory_config(self.model_dump())
        return self


class CommandInput(Input):
    command: Literal[
        "stop", "continue", "claim_control", "release_control", "click", "type", "key", "scroll", "navigate"
    ]
    idempotency_key: str | None = Field(default=None, max_length=100)
    x: float | None = Field(default=None, ge=0, le=10000)
    y: float | None = Field(default=None, ge=0, le=10000)
    text: str | None = Field(default=None, max_length=1000)
    key: str | None = Field(default=None, max_length=50)
    url: str | None = Field(default=None, max_length=1500)
    delta_y: int | None = Field(default=None, ge=-10000, le=10000)


class MemberInput(Input):
    subject: str = Field(min_length=1, max_length=512)
    role: Literal["owner", "operator", "analyst", "viewer"]
    email: str = Field(default="", max_length=320)


class ProjectInput(Input):
    name: str = Field(min_length=1, max_length=100)
    concurrency: int = Field(default=20, ge=1, le=500)


class ExperimentInput(Input):
    name: str = Field(min_length=1, max_length=100)
    concurrency: int = Field(default=1, ge=1, le=500)
    description: str = Field(default="", max_length=4000)
    hypothesis: str = Field(default="", max_length=4000)
    provider: Literal["adscore"] = "adscore"
    baseline_adapter: Adapter = "playwright"
    tags: list[str] = Field(default_factory=list, max_length=20)
    groups: list[BatchInput] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def homogeneous_evidence(self):
        if len({group.mode for group in self.groups}) != 1:
            raise ValueError("An experiment cannot combine simulated and live groups")
        if any(len(tag) > 80 for tag in self.tags):
            raise ValueError("Tags are limited to80characters")
        return self


class SessionTemplateInput(Input):
    name: str = Field(min_length=1, max_length=100)
    batch: BatchInput
