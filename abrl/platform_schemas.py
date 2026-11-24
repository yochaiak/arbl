import re
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from abrl.batch_presets import MAX_BATCH_SEED, BatchPresetOptions
from abrl.browser_settings import DevicePreset, adapter_contract, supports_user_agent_override
from abrl.network import normalize_origin
from abrl.proxy import MAX_PROXY_CONNECTIONS
from abrl.schemas import FixtureParameterName, FixtureParams, Input, WorkflowValue

Adapter = Literal["playwright", "cloakbrowser", "kameleo", "manual_chrome"]
USER_AGENT_MAX_LENGTH = 1024
REFERRER_MAX_LENGTH = 2048


def validate_user_agent(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"[ -~]+", value):
        value = value.strip(" ")
        if 1 <= len(value) <= USER_AGENT_MAX_LENGTH:
            return value
    raise ValueError(f"User-Agent must contain 1–{USER_AGENT_MAX_LENGTH} printable ASCII characters")


def validate_referrer(value: object) -> str | None:
    if value is None:
        return None
    message = (
        f"Initial referrer must be an absolute HTTP(S) URL of up to {REFERRER_MAX_LENGTH} characters "
        "without credentials, fragments, whitespace, or control characters"
    )
    if (
        not isinstance(value, str)
        or len(value) > REFERRER_MAX_LENGTH
        or re.search(r"[\s\x00-\x1f\x7f-\x9f\\#]", value)
    ):
        raise ValueError(message)
    try:
        parts = urlsplit(value)
        if parts.scheme in {"http", "https"} and parts.netloc and "@" not in parts.netloc:
            HttpUrl(value)
            return value
    except ValueError as exc:
        raise ValueError(message) from exc
    raise ValueError(message)


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


WorkflowValues = dict[FixtureParameterName, WorkflowValue]


class SessionGroupInput(Input):
    adapter: Adapter
    count: int = Field(default=1, ge=1, le=2000)
    profile_ids: list[str | None] = Field(default_factory=list, max_length=2000)
    persistence: Literal["fresh", "persistent"] | None = None
    proxy_policy_id: str | None = None
    use_direct: bool = False
    device_preset: DevicePreset | None = None
    user_agent: str | None = None
    workflow_inputs: WorkflowValues = Field(default_factory=dict, max_length=100)
    fixture_params: FixtureParams | None = None

    @model_validator(mode="after")
    def group_options(self):
        if len(self.profile_ids) > self.count:
            raise ValueError("A session group has more profile assignments than sessions")
        if self.use_direct and self.proxy_policy_id:
            raise ValueError("Choose a proxy policy or direct connectivity for each group")
        self.user_agent = validate_user_agent(self.user_agent)
        if self.user_agent and not supports_user_agent_override(self.adapter):
            raise ValueError("This adapter does not support a User-Agent override")
        return self


class BatchInput(Input):
    name: str = Field(default="Workflow batch", min_length=1, max_length=100)
    target_id: str
    workflow_id: str
    expected_workflow_version: int | None = Field(default=None, ge=1)
    expected_dependencies: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    fixture_params: FixtureParams | None = None
    workflow_inputs: WorkflowValues = Field(default_factory=dict, max_length=100)
    session_groups: list[SessionGroupInput] = Field(default_factory=list, max_length=2000)
    session_template_id: str | None = None
    session_template_version: int | None = Field(default=None, ge=1)
    count: int = Field(default=30, ge=1, le=2000)
    concurrency: int = Field(default=1, ge=1, le=500)
    adapters: list[Adapter] = Field(default_factory=lambda: ["playwright"], min_length=1, max_length=4)
    user_agent: str | None = Field(default=None, min_length=1, max_length=USER_AGENT_MAX_LENGTH)
    device_preset: DevicePreset | None = None
    batch_presets: BatchPresetOptions | None = None
    referrer: str | None = Field(default=None, min_length=1, max_length=REFERRER_MAX_LENGTH)
    persistence: Literal["fresh", "persistent"] = "fresh"
    profile_ids: list[str] = Field(default_factory=list, max_length=2000)
    preparation: Preparation = Field(default_factory=Preparation)
    proxy_policy_id: str | None = None
    mode: Literal["live", "demo"] = "live"
    phase: Literal["pilot", "confirmation"] = "pilot"
    seed: int | None = Field(default=None, ge=0, le=MAX_BATCH_SEED)
    idempotency_key: str = Field(min_length=8, max_length=100)
    hypothesis: str = Field(default="", max_length=2000)
    reliability_target: float = Field(default=0.95, gt=0, le=1)
    timeout_seconds: int = Field(default=600, ge=10, le=7200)
    request_budget: int = Field(default=1000, ge=1, le=100000)
    traffic_budget_bytes: int = Field(default=104857600, ge=1024, le=2 * 1024**3)
    artifact_policy: Literal["none", "on_failure", "all"] = "all"
    artifact_retention_days: int = Field(default=30, ge=1, le=365)
    capture_javascript: bool = True
    cost_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    cost_per_session: float | None = Field(default=None, ge=0)

    @field_validator("user_agent", mode="before")
    @classmethod
    def user_agent_header(cls, value: object) -> str | None:
        return validate_user_agent(value)

    @field_validator("referrer", mode="before")
    @classmethod
    def entry_referrer(cls, value: object) -> str | None:
        return validate_referrer(value)

    @model_validator(mode="before")
    @classmethod
    def pilot_group_defaults(cls, value):
        if isinstance(value, dict) and "count" not in value and not value.get("session_groups"):
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
        if self.session_groups:
            self.count = sum(group.count for group in self.session_groups)
            if self.count > 2000 or self.profile_ids:
                raise ValueError("Use up to 2000 sessions and assign profiles within session groups")
            self.adapters = list(dict.fromkeys(group.adapter for group in self.session_groups))
            assigned = [profile for group in self.session_groups for profile in group.profile_ids if profile]
            if len(set(assigned)) != len(assigned):
                raise ValueError("Each existing profile may be used by only one session in the batch")
        if self.profile_ids and (
            len(set(self.profile_ids)) != self.count or len(self.profile_ids) != self.count
        ):
            raise ValueError("Supply one unique profile per session")
        if self.concurrency > self.count:
            raise ValueError("Concurrency cannot exceed session count")
        if self.batch_presets is not None:
            if self.mode != "live" or any(
                adapter_contract(adapter) is None for adapter in self.adapters
            ):
                raise ValueError("Batch presets require live sessions with viewport support")
            if self.user_agent or self.device_preset or any(
                group.user_agent or group.device_preset for group in self.session_groups
            ):
                raise ValueError("Batch presets cannot be combined with User-Agent or device overrides")
        if self.user_agent is not None and not self.session_groups and not all(map(supports_user_agent_override, self.adapters)):
            raise ValueError("Custom User-Agent requires Playwright Chromium for every session")
        return self


class BatchLaunchInput(BatchInput):
    """Settings for an immediate batch launch."""


class ScheduleInput(Input):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["once", "interval", "cron"]
    timezone: str = "Asia/Jerusalem"
    expression: str | None = Field(default=None, max_length=100)
    interval_seconds: int | None = Field(default=None, ge=60, le=31536000)
    start_at: datetime | None = None
    end_at: datetime | None = None
    batch: BatchInput
    refresh_dependencies: bool = False
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


def _proxy_target_origin(value: str) -> str:
    origin = normalize_origin(value)
    if not origin.startswith("https://") or "?" in value or "#" in value:
        raise ValueError("Proxy target origin must be an HTTPS origin without a path, query, or fragment")
    return origin


ProxyTargetOrigin = Annotated[str, Field(max_length=256), AfterValidator(_proxy_target_origin)]
ProxyProvider = Literal["oxylabs", "decodo"]


class ProxyPolicyInput(Input):
    name: str = Field(min_length=1, max_length=100)
    provider: ProxyProvider = "oxylabs"
    target_origin: ProxyTargetOrigin | None = None
    country: str | None = Field(default=None, pattern="^[A-Z]{2}$")
    allowed_asns: list[Annotated[int, Field(strict=True, ge=1, le=4294967295)]] = Field(default_factory=list, max_length=256)
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
    initial_navigation_timeout_ms: int | None = Field(
        default=None, ge=100, le=120_000, strict=True,
        description="Override the initial DOMContentLoaded deadline for sessions using this proxy policy.",
    )
    max_latency_ms: float | None = Field(default=None, gt=0)
    min_transport_success_rate: float | None = Field(default=None, ge=0, le=1)
    connection_load: int = Field(default=1, ge=1, le=MAX_PROXY_CONNECTIONS)

    @model_validator(mode="before")
    @classmethod
    def provider_defaults(cls, value):
        if isinstance(value, dict) and value.get("provider") == "decodo":
            return {"products": ["residential"], "country": "US", "continuity": "sticky", **value}
        return value

    @model_validator(mode="after")
    def product_policy(self):
        if self.allowed_asns and self.provider != "decodo":
            raise ValueError("ASN pre-flight requires a Decodo policy")
        if not self.products:
            raise ValueError("Allow at least one proxy product")
        if any(
            len(country) != 2 or not country.isalpha() or not country.isupper()
            for country in self.fallback_countries
        ):
            raise ValueError("Fallback countries require uppercase two-letter codes")
        if self.provider == "decodo":
            if set(self.products) != {"residential"} or self.country != "US" or self.fallback_countries:
                raise ValueError("Decodo policies require US Residential inventory")
            if self.continuity not in {"none", "sticky"} or self.exclusive or self.allow_direct:
                raise ValueError("Decodo US Residential supports rotating or sticky proxy sessions only")
        if (self.continuity in {"fixed", "exclusive"} or self.exclusive) and set(self.products) == {
            "residential"
        }:
            raise ValueError("Residential sessions cannot provide fixed or exclusive inventory")
        return self


class ProxyInventoryInput(Input):
    name: str = Field(min_length=1, max_length=100)
    provider: ProxyProvider = "oxylabs"
    target_origin: ProxyTargetOrigin | None = None
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
        from abrl.proxy import DECODO_GATEWAY, DECODO_PORT, FIXED_GATEWAYS

        if isinstance(value, dict) and value.get("provider") == "decodo":
            return {
                "product": "residential", "host": DECODO_GATEWAY, "port": DECODO_PORT, "country": "US", **value
            }
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
