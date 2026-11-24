"""Additive research/session domain. Legacy observations are never reclassified."""

from sqlalchemy import JSON, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from abrl.db import Base, new_id, now


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    concurrency: Mapped[int] = mapped_column(default=20)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    subject: Mapped[str] = mapped_column(String(512), unique=True)
    email: Mapped[str] = mapped_column(String(320), default="")
    name: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class Membership(Base):
    __tablename__ = "project_memberships"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20))


class BrowserProfile(Base):
    __tablename__ = "browser_profiles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    adapter: Mapped[str] = mapped_column(String(32))
    persistence: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(32), default="available")
    version: Mapped[int] = mapped_column(default=0)
    epoch: Mapped[int] = mapped_column(default=0)
    owner_session_id: Mapped[str | None] = mapped_column(String(36))
    snapshot_key: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class ProfileSnapshot(Base):
    __tablename__ = "profile_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    profile_id: Mapped[str] = mapped_column(ForeignKey("browser_profiles.id"), index=True)
    generation: Mapped[int]
    epoch: Mapped[int]
    key: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64))
    environment: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    __table_args__ = (UniqueConstraint("profile_id", "generation"),)


class SessionBatch(Base):
    __tablename__ = "session_batches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    count: Mapped[int]
    concurrency: Mapped[int]
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    finished_at: Mapped[str | None] = mapped_column(String(40))
    __table_args__ = (UniqueConstraint("project_id", "idempotency_key"),)


class SessionTemplate(Base):
    __tablename__ = "session_templates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class SessionTemplateRevision(Base):
    __tablename__ = "session_template_versions"
    template_id: Mapped[str] = mapped_column(ForeignKey("session_templates.id"), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    config: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class BrowserSession(Base):
    __tablename__ = "browser_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("session_batches.id"), index=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("browser_profiles.id"), index=True)
    adapter: Mapped[str] = mapped_column(String(32))
    persistence: Mapped[str] = mapped_column(String(20))
    mode: Mapped[str] = mapped_column(String(16))
    pool: Mapped[str] = mapped_column(String(16), index=True)
    ordinal: Mapped[int]
    dispatch_order: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    admitted: Mapped[bool] = mapped_column(default=False)
    queue_reason: Mapped[str | None] = mapped_column(String(40))
    next_attempt_at: Mapped[str | None] = mapped_column(String(40))
    workflow_outcome: Mapped[str | None] = mapped_column(String(32))
    outcome: Mapped[str | None] = mapped_column(String(32))
    elapsed_ms: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    ground_truth: Mapped[str] = mapped_column(String(20), default="automated")
    epoch: Mapped[int] = mapped_column(default=0)
    owner_worker: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[str | None] = mapped_column(String(40))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    attempt_number: Mapped[int] = mapped_column(default=0)
    proxy_lease_id: Mapped[str | None] = mapped_column(String(36))
    controller_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    controller_until: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    started_at: Mapped[str | None] = mapped_column(String(40))
    finished_at: Mapped[str | None] = mapped_column(String(40))
    __table_args__ = (
        UniqueConstraint("batch_id", "ordinal"),
        Index("ix_browser_session_admission", "status", "admitted", "pool"),
    )


class SessionBatchItem(Base):
    __tablename__ = "session_batch_items"
    batch_id: Mapped[str] = mapped_column(ForeignKey("session_batches.id"), primary_key=True)
    ordinal: Mapped[int] = mapped_column(primary_key=True)
    adapter: Mapped[str] = mapped_column(String(32))
    requested_profile_id: Mapped[str | None] = mapped_column(String(36))
    launch_config: Mapped[dict | None] = mapped_column(JSON)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("browser_sessions.id"), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="failed")
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class WorkflowBlock(Base):
    __tablename__ = "workflow_blocks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    version: Mapped[int] = mapped_column(default=1)


class WorkflowBlockRevision(Base):
    __tablename__ = "workflow_block_versions"
    block_id: Mapped[str] = mapped_column(ForeignKey("workflow_blocks.id"), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    config: Mapped[dict] = mapped_column(JSON)


class SessionAttempt(Base):
    __tablename__ = "session_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("browser_sessions.id"), index=True)
    number: Mapped[int]
    epoch: Mapped[int]
    proxy_lease_id: Mapped[str | None] = mapped_column(String(36))
    proxy_check_ip: Mapped[str | None] = mapped_column(String(45))
    recording_status: Mapped[str | None] = mapped_column(String(16))
    started_at: Mapped[str] = mapped_column(String(40), default=now)
    finished_at: Mapped[str | None] = mapped_column(String(40))
    outcome: Mapped[str | None] = mapped_column(String(32))
    started_action: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("session_id", "number"),)


class SessionEvent(Base):
    __tablename__ = "session_events"
    sequence: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("browser_sessions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    actor_id: Mapped[str | None] = mapped_column(String(36))
    forwarded_at: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class SessionCommand(Base):
    __tablename__ = "session_commands"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("browser_sessions.id"), index=True)
    actor_id: Mapped[str] = mapped_column(String(36))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSON)
    request_hash: Mapped[str | None] = mapped_column(String(64))
    epoch: Mapped[int]
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    consumed_at: Mapped[str | None] = mapped_column(String(40))
    __table_args__ = (UniqueConstraint("session_id", "idempotency_key"),)


class ProxyPolicy(Base):
    __tablename__ = "proxy_policies"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(default=1)
    config: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class ProxyInventory(Base):
    __tablename__ = "proxy_inventory"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    config: Mapped[dict] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(default=True)
    active_leases: Mapped[int] = mapped_column(default=0)
    health: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class ProxyLease(Base):
    __tablename__ = "proxy_leases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("browser_sessions.id"), index=True)
    inventory_id: Mapped[str] = mapped_column(ForeignKey("proxy_inventory.id"))
    epoch: Mapped[int]
    config: Mapped[dict] = mapped_column(JSON)
    rationale: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    expires_at: Mapped[str] = mapped_column(String(40))
    released_at: Mapped[str | None] = mapped_column(String(40))


class Schedule(Base):
    __tablename__ = "schedules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(default=1)
    enabled: Mapped[bool] = mapped_column(default=True)
    config: Mapped[dict] = mapped_column(JSON)
    next_fire_at: Mapped[str | None] = mapped_column(String(40), index=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class ScheduleRevision(Base):
    __tablename__ = "schedule_versions"
    schedule_id: Mapped[str] = mapped_column(ForeignKey("schedules.id"), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    config: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class ScheduledOccurrence(Base):
    __tablename__ = "scheduled_occurrences"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    schedule_id: Mapped[str] = mapped_column(ForeignKey("schedules.id"), index=True)
    version: Mapped[int]
    scheduled_for: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20))
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("session_batches.id"))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    __table_args__ = (UniqueConstraint("schedule_id", "version", "scheduled_for"),)


class ProviderObservation(Base):
    __tablename__ = "provider_observations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("browser_sessions.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("session_attempts.id"))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    verification_status: Mapped[str] = mapped_column(String(32))
    verdict: Mapped[str | None] = mapped_column(String(32))
    raw_code: Mapped[int | None]
    accepted: Mapped[bool | None]
    source: Mapped[str] = mapped_column(String(20), default="adscore")
    request_hash: Mapped[str | None] = mapped_column(String(64))
    observed_ip: Mapped[str | None] = mapped_column(String(45))
    provider_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    __table_args__ = (UniqueConstraint("session_id", "idempotency_key"),)


class FleetSlot(Base):
    __tablename__ = "fleet_slots"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    pool: Mapped[str] = mapped_column(String(16))
    capacity: Mapped[int] = mapped_column(default=1)
    seen_at: Mapped[str] = mapped_column(String(40), default=now)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ResearchSnapshot(Base):
    __tablename__ = "research_snapshots"
    batch_id: Mapped[str] = mapped_column(ForeignKey("session_batches.id"), primary_key=True)
    checksum: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class ProxyPolicyRevision(Base):
    __tablename__ = "proxy_policy_versions"
    policy_id: Mapped[str] = mapped_column(ForeignKey("proxy_policies.id"), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    config: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class Experiment(Base):
    __tablename__ = "experiments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    config: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class ExperimentRevision(Base):
    __tablename__ = "experiment_versions"
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    config: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class ExperimentRun(Base):
    __tablename__ = "experiment_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    version: Mapped[int]
    idempotency_key: Mapped[str] = mapped_column(String(100))
    batch_ids: Mapped[list] = mapped_column(JSON)
    randomization: Mapped[dict] = mapped_column(JSON, default=dict)
    report_snapshot: Mapped[dict | None] = mapped_column(JSON)
    report_checksum: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    __table_args__ = (UniqueConstraint("experiment_id", "idempotency_key"),)


class SessionArtifact(Base):
    __tablename__ = "session_artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("browser_sessions.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    media_type: Mapped[str] = mapped_column(String(80))
    key: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64))
    size: Mapped[int]
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    expires_at: Mapped[str] = mapped_column(String(40), index=True)
    deleted_at: Mapped[str | None] = mapped_column(String(40))
    attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("session_attempts.id", name="fk_session_artifacts_attempt_id")
    )
    video_sequence: Mapped[int | None]
    video_start_ms: Mapped[int | None]
    video_duration_ms: Mapped[int | None]
    __table_args__ = (
        UniqueConstraint("attempt_id", "video_sequence", name="uq_session_artifacts_attempt_sequence"),
    )
