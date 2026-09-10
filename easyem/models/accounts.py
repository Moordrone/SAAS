"""Accounts: the single entity that owns things and pays for them.

A personal account is an organization with one member. This is the correction
to the v1 polymorphic wallet and the v2 nullable owner_user_id/organization_id
pair: every project, wallet, subscription and job hangs off exactly one
`account_id`, with a real foreign key. Converting a solo user into a team is an
INSERT into memberships, not a data migration.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Timestamped, UuidPk


class AccountType(enum.StrEnum):
    personal = "personal"
    organization = "organization"


class Role(enum.StrEnum):
    owner = "owner"          # exactly one per account, cannot be removed
    admin = "admin"          # manages members, billing
    member = "member"        # uses the product
    viewer = "viewer"        # read-only


class Account(UuidPk, Timestamped, Base):
    __tablename__ = "accounts"

    type: Mapped[AccountType] = mapped_column(
        Enum(AccountType, name="account_type"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(80), unique=True)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Account {self.type.value} {self.name!r}>"


class Membership(UuidPk, Timestamped, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("account_id", "user_id", name="uq_membership_account_user"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="membership_role"), nullable=False, default=Role.member
    )

    account: Mapped[Account] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")  # noqa: F821
