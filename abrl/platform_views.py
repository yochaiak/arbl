"""Public response contracts consumed by OpenAPI-generated clients."""

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, create_model

from abrl.batch_presets import PresetApplication, PresetId, PresetLocale, PresetMode, ViewportClass
from abrl.browser_settings import AdapterFeatures
from abrl.platform_schemas import ProxyInventoryInput, ProxyPolicyInput, ScheduleInput

T = TypeVar("T")
RecordingStatus = Literal["recording", "ready", "partial", "unavailable", "expired"]


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class ProfileView(BaseModel):
    id: str
    project_id: str
    name: str
    adapter: str
    persistence: str
    status: str
    version: int
    epoch: int
    owner_session_id: str | None
    created_at: str


class PresetViewport(BaseModel):
    width: int
    height: int


class BatchPresetView(BaseModel):
    catalog_version: Literal[1]
    id: PresetId
    application: PresetApplication
    device_class: Literal["desktop", "adapter_managed"]
    viewport_class: ViewportClass
    viewport: PresetViewport
    device_pixel_ratio: int | None
    locale: PresetLocale | None
    timezone: str | None
    mode: PresetMode
    seed: int


class SessionView(BaseModel):
    id: str
    project_id: str
    profile_id: str
    batch_id: str
    workflow_name: str | None = None
    workflow_version: int | None = None
    batch_preset: BatchPresetView | None = None
    adapter: str
    persistence: str
    status: str
    queue_reason: str | None = None
    next_attempt_at: str | None = None
    mode: str
    pool: str
    workflow_outcome: str | None
    outcome: str | None
    elapsed_ms: int | None
    error: str | None
    ordinal: int
    admitted: bool
    ground_truth: str
    epoch: int
    attempt_number: int
    controller_id: str | None
    controller_until: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    metadata: dict
    observed_ip: str | None = None
    proxy_check_ip: str | None = None
    recording_status: RecordingStatus = "unavailable"
    recording_available: bool = False


class RecordingSegmentView(BaseModel):
    sequence: int
    start_ms: int
    duration_ms: int


class SessionAttemptRecordingView(BaseModel):
    attempt_number: int
    status: RecordingStatus
    segments: list[RecordingSegmentView]


class SessionRecordingManifest(BaseModel):
    attempts: list[SessionAttemptRecordingView]


class BatchView(BaseModel):
    id: str
    project_id: str
    name: str
    status: str
    count: int
    concurrency: int
    created_at: str
    finished_at: str | None
    mode: str
    config: dict
    counts: dict[str, int]
    creation: dict[str, int]


class BatchItemView(BaseModel):
    batch_id: str
    ordinal: int
    adapter: str
    requested_profile_id: str | None
    session_id: str | None
    status: str
    error: str | None
    attempts: int
    created_at: str


class BatchPreflightNotice(BaseModel):
    code: str
    message: str
    group: int | None = None


class BatchDependencyVersions(BaseModel):
    target_id: str
    target_version: int
    workflow_id: str
    workflow_version: int
    proxy_policies: dict[str, int]


class BatchPreflightView(BaseModel):
    dependencies: BatchDependencyVersions
    expected_dependencies: str
    count: int
    notices: list[BatchPreflightNotice]


class BatchIPSummary(BaseModel):
    expected_sessions: int
    observed_sessions: int
    distinct_ips: int
    status: Literal["duplicate", "unique", "incomplete", "pending"]


UnevaluatedChallengeReason = Literal[
    "missing_finalized_evidence", "stale_attempt_evidence", "completed_without_document_evidence", "site_denial",
    "preflight_blocked", "transport_timeout", "cancelled", "workflow_not_started", "workflow_timeout",
    "workflow_incomplete",
]


class ChallengeMetrics(BaseModel):
    finished_sessions: int
    evaluated_sessions: int = Field(
        description="Sessions with a detected challenge or a completed workflow that observed an authorized document."
    )
    unevaluated_sessions: int
    unevaluated_reasons: dict[UnevaluatedChallengeReason, int] = Field(
        description="One reason per unevaluated session. Counts sum to unevaluated_sessions."
    )
    challenged_sessions: int
    challenge_rate: float | None = Field(
        description="Detected challenges divided by evaluated sessions, or null when none were evaluated."
    )
    transport_attempts: int
    transport_errors: int
    transport_error_rate: float | None
    mean_transport_latency_ms: float | None
    transport_latency_samples: int


class ChallengeGroup(ChallengeMetrics, Generic[T]):
    value: T | None = Field(
        description="A proxy inventory ID or an ASN or country from confirmed sticky preflight evidence. "
        "Null denotes unavailable evidence."
    )


class ChallengeDiagnostics(BaseModel):
    preflight_reports: int
    preflight_timeout_sessions: int = Field(
        description="Preflight reports with at least one recorded probe or overall deadline timeout."
    )
    missing_lookup_fields: dict[str, int] = Field(
        description="Count of preflight reports without usable evidence for each lookup field. Omitted fields have zero."
    )
    missing_lookup_dataset_statuses: dict[str, dict[str, int]] = Field(
        description="Breakdown of recorded local dataset statuses for missing lookup fields. Older reports may omit them."
    )
    browser_reports: int
    browser_reports_without_checks: int = Field(
        description="Managed browser reports without recorded comparison checks."
    )
    browser_unavailable_or_inconclusive_fields: dict[str, int] = Field(
        description="Count of managed browser reports with an unavailable, timed out, or inconclusive check by field."
    )


class BatchChallengeSummary(BaseModel):
    overall: ChallengeMetrics
    diagnostics: ChallengeDiagnostics = Field(
        description="Overlapping diagnostics for finalized attempts. These counts do not affect the challenge rate."
    )
    by_pool: list[ChallengeGroup[str]]
    by_asn: list[ChallengeGroup[int]]
    by_country: list[ChallengeGroup[str]]


class ProxyCheckSession(BaseModel):
    id: str
    ordinal: int


class ProxyCheckDuplicateGroup(BaseModel):
    proxy_check_ip: str | None
    sessions: list[ProxyCheckSession]


class BatchProxyCheckIPSummary(BaseModel):
    expected_sessions: int
    checked_sessions: int
    distinct_ips: int
    unique_sessions: int
    duplicate_sessions: int
    duplicate_groups: list[ProxyCheckDuplicateGroup]


class PreflightLatency(BaseModel):
    samples: int
    p50: float
    p95: float


class ProxyPreflightSummary(BaseModel):
    attempts: int
    limit: int
    blocking_reasons: dict[str, int]
    missing_evidence_rates: dict[str, float | None]
    sticky_compared: int
    sticky_mismatches: int
    latency_ms: dict[str, PreflightLatency]


class AdapterView(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    name: str
    available: bool
    status: str
    reason: str | None
    capabilities: list[str]
    version: str | None = None
    max_concurrency: int | None = None
    features: AdapterFeatures | None = None
    worker_capacity: int | None = None
    verification: Literal["unverified"] = "unverified"


# Historical configurations remain readable when new input-only capability rules
# become stricter. Retain their field contracts without running input validators.
PolicyFields = create_model(
    "PolicyFields",
    **{name: (field.annotation, field) for name, field in ProxyPolicyInput.model_fields.items()},
)
InventoryFields = create_model(
    "InventoryFields",
    **{name: (field.annotation, field) for name, field in ProxyInventoryInput.model_fields.items()},
)


class PolicyView(PolicyFields):
    id: str
    version: int
    created_at: str


class InventoryView(InventoryFields):
    id: str
    active_leases: int
    health: dict
    readiness: dict


class ScheduleView(ScheduleInput):
    id: str
    version: int
    enabled: bool
    next_fire_at: str | None
    pause_reason: str | None = None
    dependencies: BatchDependencyVersions | None = None
    created_at: str
