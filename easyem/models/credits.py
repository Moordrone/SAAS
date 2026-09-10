"""Credit wallet, immutable ledger, and reservations.

Two kinds of data live here and they must not be confused:

  * `credit_transactions` is an **append-only ledger**. Rows are never updated
    or deleted. A mistake is corrected by an opposing `adjustment` entry. This
    is what makes the balance auditable and reconcilable.

  * `credit_reservations` is **mutable business state**. A reservation moves
    from held to settled/released/expired. It points at the ledger rows that
    caused each move.

Invariant, checked in CI and by a nightly job:
    SUM(credit_transactions.amount) per wallet == credit_wallets.balance
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Timestamped, UtcDateTime, UuidPk

# Credits are money-adjacent. Never float.
CreditAmount = Numeric(20, 6)


class LedgerOp(enum.StrEnum):
    grant = "grant"              # +  free / plan-included credits
    purchase = "purchase"        # +  bought credits
    reserve = "reserve"          # -  hold before running a job
    release = "release"          # +  cancels a hold
    consume = "consume"          # -  actual usage at settlement
    refund = "refund"            # +  money-back
    adjustment = "adjustment"    # +/- admin correction, requires a reason


# Signs are enforced at the database level, not just in Python.
_POSITIVE_OPS = {LedgerOp.grant, LedgerOp.purchase, LedgerOp.release, LedgerOp.refund}
_NEGATIVE_OPS = {LedgerOp.reserve, LedgerOp.consume}


class ReservationStatus(enum.StrEnum):
    held = "held"
    settled = "settled"
    released = "released"
    expired = "expired"


class CreditWallet(UuidPk, Timestamped, Base):
    __tablename__ = "credit_wallets"

    # Real foreign key, one wallet per account. No polymorphic owner_type.
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False, unique=True,
    )

    # Cached projection of the ledger. The ledger is the source of truth.
    balance: Mapped[Decimal] = mapped_column(
        CreditAmount, nullable=False, default=Decimal("0")
    )

    transactions: Mapped[list[CreditTransaction]] = relationship(
        back_populates="wallet"
    )

    __table_args__ = (
        CheckConstraint("balance >= 0", name="ck_wallet_balance_non_negative"),
    )


class CreditTransaction(UuidPk, Base):
    """Append-only. No UPDATE, no DELETE — enforced by convention and by tests."""

    __tablename__ = "credit_transactions"
    __table_args__ = (
        # Replaying a webhook or retrying a worker must not double-write.
        UniqueConstraint("idempotency_key", name="uq_credit_tx_idempotency"),
        CheckConstraint("amount <> 0", name="ck_credit_tx_amount_nonzero"),
        Index("ix_credit_tx_wallet_created", "wallet_id", "created_at"),
        Index("ix_credit_tx_reference", "reference_type", "reference_id"),
    )

    wallet_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("credit_wallets.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    operation: Mapped[LedgerOp] = mapped_column(
        Enum(LedgerOp, name="ledger_op"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(CreditAmount, nullable=False)

    # Balance immediately after this entry. Makes any statement reproducible
    # without replaying the whole history.
    balance_after: Mapped[Decimal] = mapped_column(CreditAmount, nullable=False)

    reference_type: Mapped[str | None] = mapped_column(String(40))
    reference_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(400))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    wallet: Mapped[CreditWallet] = relationship(back_populates="transactions")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Tx {self.operation.value} {self.amount}>"


class CreditReservation(UuidPk, Base):
    """A hold placed before a job runs, always resolved or expired."""

    __tablename__ = "credit_reservations"
    __table_args__ = (
        # One reservation per job. Double submission cannot double-hold.
        UniqueConstraint(
            "reference_type", "reference_id", name="uq_reservation_reference"
        ),
        Index("ix_reservation_expiry", "status", "expires_at"),
    )

    wallet_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("credit_wallets.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    amount: Mapped[Decimal] = mapped_column(CreditAmount, nullable=False)
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, name="reservation_status"),
        nullable=False, default=ReservationStatus.held,
    )

    reference_type: Mapped[str] = mapped_column(String(40), nullable=False)
    reference_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    reserve_tx_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("credit_transactions.id"), nullable=False
    )
    consume_tx_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("credit_transactions.id")
    )
    release_tx_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("credit_transactions.id")
    )

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    # A worker killed mid-job must not freeze the customer's credits forever.
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class WebhookEvent(UuidPk, Base):
    """Payment webhooks arrive duplicated and out of order. Dedupe at the DB."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_webhook_provider_event"),
    )

    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_raw: Mapped[str] = mapped_column(String, nullable=False)
    signature_verified: Mapped[bool] = mapped_column(nullable=False, default=False)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    processing_error: Mapped[str | None] = mapped_column(String(1000))
