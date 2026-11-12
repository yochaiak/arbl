from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from abrl.config import DATABASE_URL, ROOT


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Target(Base):
    __tablename__ = "targets"
    project_id: Mapped[str] = mapped_column(
        String(36), default="default", server_default="default", index=True
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    version: Mapped[int] = mapped_column(default=1)
    proof_token: Mapped[str] = mapped_column(String(96))
    verified_version: Mapped[int] = mapped_column(default=0)
    last_started_at: Mapped[str | None] = mapped_column(String(40))


class TargetVersion(Base):
    __tablename__ = "target_versions"
    target_id: Mapped[str] = mapped_column(ForeignKey("targets.id"), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    config: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class Workflow(Base):
    __tablename__ = "workflows"
    project_id: Mapped[str] = mapped_column(
        String(36), default="default", server_default="default", index=True
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    version: Mapped[int] = mapped_column(default=1)


class WorkflowVersion(Base):
    __tablename__ = "workflow_versions"
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    config: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class Run(Base):
    __tablename__ = "runs"
    project_id: Mapped[str] = mapped_column(
        String(36), default="default", server_default="default", index=True
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    target_id: Mapped[str] = mapped_column(ForeignKey("targets.id"), index=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"))
    target_version: Mapped[int]
    workflow_version: Mapped[int]
    target: Mapped[dict] = mapped_column(JSON)
    workflow: Mapped[dict] = mapped_column(JSON)
    phase: Mapped[str] = mapped_column(String(20))
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    repetitions: Mapped[int]
    seed: Mapped[int]
    reliability_target: Mapped[float] = mapped_column(default=0.95)
    created_at: Mapped[str] = mapped_column(String(40), default=now, index=True)
    finished_at: Mapped[str | None] = mapped_column(String(40))


class Trial(Base):
    __tablename__ = "trials"
    __table_args__ = (
        UniqueConstraint("run_id", "ordinal"),
        Index("ix_trials_queue", "status", "dispatched_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    ordinal: Mapped[int]
    status: Mapped[str] = mapped_column(String(20), default="queued")
    outcome: Mapped[str | None] = mapped_column(String(20))
    elapsed_ms: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(default=0)
    dispatched_at: Mapped[str | None] = mapped_column(String(40))
    lease_until: Mapped[str | None] = mapped_column(String(40))
    lease_token: Mapped[str | None] = mapped_column(String(36))


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("trial_id", "number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trial_id: Mapped[str] = mapped_column(ForeignKey("trials.id"), index=True)
    number: Mapped[int]
    started_at: Mapped[str] = mapped_column(String(40), default=now)
    finished_at: Mapped[str | None] = mapped_column(String(40))
    outcome: Mapped[str | None] = mapped_column(String(20))
    elapsed_ms: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(Text)
    started_action: Mapped[bool | None] = mapped_column(Boolean)


class Event(Base):
    __tablename__ = "events"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(48))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    forwarded_at: Mapped[str | None] = mapped_column(String(40))


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    seen_at: Mapped[str] = mapped_column(String(40), default=now)


class ManualObservation(Base):
    __tablename__ = "manual_observations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    verification_status: Mapped[str] = mapped_column(String(20))
    verdict: Mapped[str | None] = mapped_column(String(32))
    raw_code: Mapped[int | None]
    source: Mapped[str] = mapped_column(String(32), default="manual_lab")


if DATABASE_URL == "sqlite:///.abrl/lab.sqlite3":
    (ROOT / ".abrl").mkdir(exist_ok=True)
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine, expire_on_commit=False)


def record(session, kind: str, *, run_id: str | None = None, **detail) -> None:
    session.add(Event(kind=kind, run_id=run_id, detail=detail))
