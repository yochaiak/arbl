"""Public response contracts consumed by OpenAPI-generated clients."""

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, create_model

from abrl.platform_schemas import ProxyInventoryInput, ProxyPolicyInput, ScheduleInput

T = TypeVar("T")


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


class SessionView(BaseModel):
    id: str
    project_id: str
    profile_id: str
    batch_id: str
    adapter: str
    persistence: str
    status: str
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
    created_at: str
