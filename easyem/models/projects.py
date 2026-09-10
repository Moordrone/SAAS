"""Projects and their versioned scientific definition.

Relational metadata stays relational; only the EM problem statement lives in
JSONB. Ownership, status and history are things the SaaS needs to query, join
and authorise on — burying them in a document would throw that away.

Every version is immutable once written. A simulation references the exact
version that produced it, so a result can always be traced back to the input
that generated it.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from ..db import Base, Timestamped, UtcDateTime, UuidPk

# JSONB on PostgreSQL for indexing and containment queries; plain JSON on
# SQLite so the test suite runs without a server.
JsonDocument = JSON().with_variant(JSONB(), "postgresql")


class ProjectStatus(enum.StrEnum):
    """Project lifecycle only.

    Note what is absent: queued, running, completed. Those are *job* states. A
    project can have ten simulations in ten different states at once, so
    collapsing them onto the project was a modelling error worth undoing.
    """

    draft = "draft"
    active = "active"
    archived = "archived"
    deleted = "deleted"


class Project(UuidPk, Timestamped, Base):
    __tablename__ = "projects"
    __table_args__ = (
        Index("ix_projects_account_status", "account_id", "status"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus, name="project_status", native_enum=False, length=20),
        nullable=False, default=ProjectStatus.draft,
    )

    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    versions: Mapped[list[ProjectVersion]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectVersion.version_number",
    )


class ProjectVersion(UuidPk, Base):
    """One immutable snapshot of the EM problem statement."""

    __tablename__ = "project_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "version_number", name="uq_version_number"),
        Index("ix_project_versions_hash", "content_hash"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)

    definition: Mapped[dict] = mapped_column(JsonDocument, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)

    # Canonical hash of the definition. Two versions with the same hash describe
    # the same physical problem, which makes result caching and deduplication
    # possible, and gives a customer a reproducibility token to quote.
    content_hash: Mapped[str] = mapped_column(String(71), nullable=False)

    is_valid: Mapped[bool] = mapped_column(nullable=False, default=False)
    validation_report: Mapped[dict | None] = mapped_column(JsonDocument)

    change_summary: Mapped[str | None] = mapped_column(String(500))
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    project: Mapped[Project] = relationship(back_populates="versions")
