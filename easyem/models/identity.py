"""Users, refresh-token sessions and single-use tokens.

Sessions implement rotating refresh tokens with reuse detection: each refresh
mints a new row in the same `family_id`. Presenting an already-used token means
it leaked, so the entire family is revoked rather than just that token.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Timestamped, UtcDateTime, UuidPk


class UserStatus(enum.StrEnum):
    active = "active"
    suspended = "suspended"
    deleted = "deleted"


class TokenPurpose(enum.StrEnum):
    email_verification = "email_verification"
    password_reset = "password_reset"


class User(UuidPk, Timestamped, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)

    email_verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status"), nullable=False, default=UserStatus.active
    )

    # Billing home. Every user has one from signup.
    default_account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )

    country: Mapped[str | None] = mapped_column(String(2))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    locale: Mapped[str] = mapped_column(String(16), default="en", nullable=False)

    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_secret_encrypted: Mapped[str | None] = mapped_column(String(512))

    terms_version_accepted: Mapped[str | None] = mapped_column(String(32))
    terms_accepted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    marketing_consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    # Soft delete: GDPR erasure must not orphan invoices or audit entries.
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    memberships: Mapped[list[Membership]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.email}>"


class Session(UuidPk, Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_family", "family_id"),
        Index("ix_sessions_user_active", "user_id", "revoked_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # All descendants of one login share a family. Reuse revokes the family.
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    refresh_token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )

    user_agent: Mapped[str | None] = mapped_column(String(400))
    ip_address: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    revoked_reason: Mapped[str | None] = mapped_column(String(64))


class VerificationToken(UuidPk, Base):
    """Single-use, hashed, expiring. The plaintext only ever leaves in an email."""

    __tablename__ = "verification_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purpose: Mapped[TokenPurpose] = mapped_column(
        Enum(TokenPurpose, name="token_purpose"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class WaitlistEntry(UuidPk, Base):
    """Someone who asked to be told when this is ready.

    Kept deliberately thin: an address, optionally a line about what they
    design, and where they came from. `context` is the field that matters —
    it is the answer to "which component do I build next", which is otherwise
    a guess.
    """

    __tablename__ = "waitlist_entries"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    context: Mapped[str | None] = mapped_column(String(2000))
    source: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    contacted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
