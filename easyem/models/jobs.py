"""Simulation jobs and their results.

Two status fields, not one. The v2 design collapsed DRAFT, QUOTED, RESERVED,
RUNNING, COMPLETED, SETTLED, FAILED, RELEASED and REFUNDED into a single
enumeration, but those are two independent dimensions: a job can be succeeded
and not yet settled, or failed and already released. One enumeration forces an
invented state for every combination and makes "which jobs are running?"
ambiguous.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, UtcDateTime, UuidPk
from .credits import CreditAmount
from .projects import JsonDocument


class ExecutionStatus(enum.StrEnum):
    pending = "pending"
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    timed_out = "timed_out"


class SettlementStatus(enum.StrEnum):
    none = "none"
    reserved = "reserved"
    settled = "settled"
    released = "released"


class SimulationJob(UuidPk, Base):
    __tablename__ = "simulation_jobs"
    __table_args__ = (
        # A retried submission must not create a second job or a second hold.
        UniqueConstraint("idempotency_key", name="uq_job_idempotency"),
        Index("ix_jobs_account_created", "account_id", "created_at"),
        Index("ix_jobs_execution", "execution_status"),
        # The reaper query: running jobs whose worker stopped reporting.
        Index("ix_jobs_heartbeat", "execution_status", "heartbeat_at"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The exact inputs this run used. Editing the project afterwards cannot
    # change what a finished result was computed from.
    project_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("project_versions.id", ondelete="RESTRICT"), nullable=False
    )
    submitted_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    # Real Enum columns, not String. With a String column SQLAlchemy returns
    # the raw value on reload, so `job.execution_status is ExecutionStatus.
    # succeeded` is False for a job read back from the database — silently, and
    # only after a round trip. The values survive in memory, which is exactly
    # what makes the bug invisible until a worker picks up someone else's job.
    execution_status: Mapped[ExecutionStatus] = mapped_column(
        Enum(ExecutionStatus, name="execution_status", native_enum=False,
             length=16),
        nullable=False, default=ExecutionStatus.pending,
    )
    settlement_status: Mapped[SettlementStatus] = mapped_column(
        Enum(SettlementStatus, name="settlement_status", native_enum=False,
             length=16),
        nullable=False, default=SettlementStatus.none,
    )

    backend: Mapped[str] = mapped_column(String(40), nullable=False)
    backend_version: Mapped[str | None] = mapped_column(String(40))
    external_job_id: Mapped[str | None] = mapped_column(String(200))

    # Identical physics gives an identical hash: the basis for result caching.
    content_hash: Mapped[str] = mapped_column(String(71), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    cost_ceiling: Mapped[Decimal] = mapped_column(CreditAmount, nullable=False)
    cost_actual: Mapped[Decimal | None] = mapped_column(CreditAmount)
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("credit_reservations.id")
    )

    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)

    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    worker_id: Mapped[str | None] = mapped_column(String(100))
    heartbeat_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    error_code: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(String(1000))

    cpu_seconds: Mapped[float | None] = mapped_column(Float)
    peak_memory_mb: Mapped[float | None] = mapped_column(Float)
    cell_count: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    cancelled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    events: Mapped[list[JobEvent]] = relationship(
        back_populates="job", cascade="all, delete-orphan",
        order_by="JobEvent.created_at",
    )

    @property
    def is_terminal(self) -> bool:
        return self.execution_status in {
            ExecutionStatus.succeeded, ExecutionStatus.failed,
            ExecutionStatus.cancelled, ExecutionStatus.timed_out,
        }


class JobEvent(UuidPk, Base):
    """State transition log. Support cannot debug a job without it."""

    __tablename__ = "job_events"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("simulation_jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str | None] = mapped_column(String(16))
    detail: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    job: Mapped[SimulationJob] = relationship(back_populates="events")


class SimulationResult(UuidPk, Base):
    """Metadata and pointers. Field volumes live in object storage."""

    __tablename__ = "simulation_results"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("simulation_jobs.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    summary: Mapped[dict] = mapped_column(JsonDocument, nullable=False)
    artifacts: Mapped[dict | None] = mapped_column(JsonDocument)
    demonstration_only: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
