"""Credit ledger operations.

Every mutation follows the same shape:

    1. Lock the wallet row (SELECT ... FOR UPDATE).
    2. Append one or more immutable ledger entries.
    3. Recompute the cached balance from those entries.

Concurrency is handled by the row lock, replay by the idempotency key's UNIQUE
constraint. Neither is optional: without the lock two simultaneous jobs can both
pass the balance check, and without the key a retried worker debits twice.

The job lifecycle is:

    reserve(cost_max)  ->  settle(cost_actual)   # consume actual, release rest
                       ->  release()             # job failed, full refund
                       ->  expire()              # worker died, reaper cleans up
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from ..config import get_settings
from ..db import utcnow
from ..errors import (
    DuplicateReservation,
    InsufficientCredits,
    LedgerViolation,
    NotFound,
    ReservationAlreadyResolved,
)
from ..models import (
    CreditReservation,
    CreditTransaction,
    CreditWallet,
    LedgerOp,
    ReservationStatus,
)

_POSITIVE = {LedgerOp.grant, LedgerOp.purchase, LedgerOp.release, LedgerOp.refund}
_NEGATIVE = {LedgerOp.reserve, LedgerOp.consume}


def _quantize(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.000001"))


# --------------------------------------------------------------------------- #
# wallet access
# --------------------------------------------------------------------------- #

def get_or_create_wallet(db: DbSession, account_id: uuid.UUID) -> CreditWallet:
    wallet = db.scalar(select(CreditWallet).where(CreditWallet.account_id == account_id))
    if wallet is None:
        wallet = CreditWallet(account_id=account_id, balance=Decimal("0"))
        db.add(wallet)
        db.flush()
    return wallet


def _lock_wallet(db: DbSession, wallet_id: uuid.UUID) -> CreditWallet:
    """Serialise concurrent writers on this wallet.

    SQLite ignores FOR UPDATE (it serialises writes anyway); PostgreSQL honours
    it, which is what production relies on.
    """
    stmt = select(CreditWallet).where(CreditWallet.id == wallet_id)
    if db.bind is not None and db.bind.dialect.name != "sqlite":
        stmt = stmt.with_for_update()
    wallet = db.scalar(stmt)
    if wallet is None:
        raise NotFound("Wallet not found")
    return wallet


# --------------------------------------------------------------------------- #
# core append
# --------------------------------------------------------------------------- #

def _append(
    db: DbSession,
    wallet: CreditWallet,
    *,
    operation: LedgerOp,
    amount: Decimal,
    idempotency_key: str,
    reference_type: str | None = None,
    reference_id: uuid.UUID | None = None,
    reason: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    allow_negative: bool = False,
) -> CreditTransaction:
    """Append one ledger entry and roll the cached balance forward."""
    amount = _quantize(amount)

    if amount == 0:
        raise LedgerViolation("Ledger entries must be non-zero")
    if operation in _POSITIVE and amount < 0:
        raise LedgerViolation(f"{operation.value} must be positive")
    if operation in _NEGATIVE and amount > 0:
        raise LedgerViolation(f"{operation.value} must be negative")

    new_balance = _quantize(wallet.balance + amount)
    if new_balance < 0 and not allow_negative:
        raise InsufficientCredits(
            f"Balance {wallet.balance} cannot cover {abs(amount)} credits",
            available=str(wallet.balance),
            required=str(abs(amount)),
        )

    tx = CreditTransaction(
        wallet_id=wallet.id,
        operation=operation,
        amount=amount,
        balance_after=new_balance,
        reference_type=reference_type,
        reference_id=reference_id,
        idempotency_key=idempotency_key,
        reason=reason,
        created_by_user_id=actor_user_id,
        created_at=utcnow(),
    )
    db.add(tx)
    wallet.balance = new_balance

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise
    return tx


def _find_by_key(db: DbSession, idempotency_key: str) -> CreditTransaction | None:
    return db.scalar(
        select(CreditTransaction).where(
            CreditTransaction.idempotency_key == idempotency_key
        )
    )


# --------------------------------------------------------------------------- #
# public operations
# --------------------------------------------------------------------------- #

def credit(
    db: DbSession,
    account_id: uuid.UUID,
    amount: Decimal | int | str,
    *,
    operation: LedgerOp,
    idempotency_key: str,
    reference_type: str | None = None,
    reference_id: uuid.UUID | None = None,
    reason: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> CreditTransaction:
    """Add credits: grant, purchase or refund. Idempotent by key."""
    if operation not in _POSITIVE:
        raise LedgerViolation(f"{operation.value} is not a credit operation")

    existing = _find_by_key(db, idempotency_key)
    if existing is not None:
        return existing

    wallet = _lock_wallet(db, get_or_create_wallet(db, account_id).id)
    return _append(
        db, wallet,
        operation=operation,
        amount=_quantize(amount),
        idempotency_key=idempotency_key,
        reference_type=reference_type,
        reference_id=reference_id,
        reason=reason,
        actor_user_id=actor_user_id,
    )


def reserve(
    db: DbSession,
    account_id: uuid.UUID,
    amount: Decimal | int | str,
    *,
    reference_type: str,
    reference_id: uuid.UUID,
    ttl_seconds: int | None = None,
) -> CreditReservation:
    """Hold `amount` credits for a job that is about to run.

    The amount should be the *upper bound* of the estimate, not the expected
    cost: the customer is told this number and must never be charged more.
    """
    amount = _quantize(amount)
    if amount <= 0:
        raise LedgerViolation("Reservation amount must be positive")

    existing = db.scalar(
        select(CreditReservation).where(
            CreditReservation.reference_type == reference_type,
            CreditReservation.reference_id == reference_id,
        )
    )
    if existing is not None:
        if existing.status is ReservationStatus.held:
            return existing  # idempotent resubmission
        raise DuplicateReservation(
            f"Reservation for {reference_type}:{reference_id} already {existing.status.value}"
        )

    settings = get_settings()
    ttl = ttl_seconds or settings.credit_reservation_ttl_seconds

    wallet = _lock_wallet(db, get_or_create_wallet(db, account_id).id)
    tx = _append(
        db, wallet,
        operation=LedgerOp.reserve,
        amount=-amount,
        idempotency_key=f"reserve:{reference_type}:{reference_id}",
        reference_type=reference_type,
        reference_id=reference_id,
    )
    now = utcnow()
    reservation = CreditReservation(
        wallet_id=wallet.id,
        amount=amount,
        status=ReservationStatus.held,
        reference_type=reference_type,
        reference_id=reference_id,
        reserve_tx_id=tx.id,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl),
    )
    db.add(reservation)
    db.flush()
    return reservation


def settle(
    db: DbSession,
    reservation: CreditReservation,
    actual_amount: Decimal | int | str,
) -> CreditReservation:
    """Charge the real cost and give back the difference.

    Two ledger entries rather than one net entry, so a statement shows what was
    held and what was actually used. `actual_amount` is capped at the reserved
    amount: the quoted price is a ceiling the customer was shown.
    """
    if reservation.status is not ReservationStatus.held:
        raise ReservationAlreadyResolved(
            f"Reservation is {reservation.status.value}"
        )

    actual = _quantize(actual_amount)
    if actual < 0:
        raise LedgerViolation("Actual cost cannot be negative")
    actual = min(actual, reservation.amount)

    wallet = _lock_wallet(db, reservation.wallet_id)
    ref = (reservation.reference_type, reservation.reference_id)

    # Give the hold back first so the intermediate balance never dips.
    release_tx = _append(
        db, wallet,
        operation=LedgerOp.release,
        amount=reservation.amount,
        idempotency_key=f"settle-release:{ref[0]}:{ref[1]}",
        reference_type=ref[0],
        reference_id=ref[1],
    )
    consume_tx = None
    if actual > 0:
        consume_tx = _append(
            db, wallet,
            operation=LedgerOp.consume,
            amount=-actual,
            idempotency_key=f"consume:{ref[0]}:{ref[1]}",
            reference_type=ref[0],
            reference_id=ref[1],
        )

    reservation.status = ReservationStatus.settled
    reservation.release_tx_id = release_tx.id
    reservation.consume_tx_id = consume_tx.id if consume_tx else None
    reservation.resolved_at = utcnow()
    db.flush()
    return reservation


def release(
    db: DbSession,
    reservation: CreditReservation,
    *,
    reason: str = "job_failed",
    status: ReservationStatus = ReservationStatus.released,
) -> CreditReservation:
    """Return the full hold. Used when a job fails or is cancelled unstarted."""
    if reservation.status is not ReservationStatus.held:
        raise ReservationAlreadyResolved(f"Reservation is {reservation.status.value}")

    wallet = _lock_wallet(db, reservation.wallet_id)
    tx = _append(
        db, wallet,
        operation=LedgerOp.release,
        amount=reservation.amount,
        idempotency_key=f"release:{reservation.reference_type}:{reservation.reference_id}",
        reference_type=reservation.reference_type,
        reference_id=reservation.reference_id,
        reason=reason,
    )
    reservation.status = status
    reservation.release_tx_id = tx.id
    reservation.resolved_at = utcnow()
    db.flush()
    return reservation


def adjust(
    db: DbSession,
    account_id: uuid.UUID,
    amount: Decimal | int | str,
    *,
    reason: str,
    actor_user_id: uuid.UUID,
    idempotency_key: str,
) -> CreditTransaction:
    """Manual admin correction. Always requires a reason and an actor."""
    if not reason.strip():
        raise LedgerViolation("Adjustments require a reason")

    existing = _find_by_key(db, idempotency_key)
    if existing is not None:
        return existing

    wallet = _lock_wallet(db, get_or_create_wallet(db, account_id).id)
    return _append(
        db, wallet,
        operation=LedgerOp.adjustment,
        amount=_quantize(amount),
        idempotency_key=idempotency_key,
        reason=reason,
        actor_user_id=actor_user_id,
        allow_negative=False,
    )


# --------------------------------------------------------------------------- #
# maintenance
# --------------------------------------------------------------------------- #

def expire_stale_reservations(db: DbSession, *, limit: int = 500) -> list[uuid.UUID]:
    """Reaper. A worker killed mid-job leaves a hold behind; free it.

    Run this on a schedule. Without it, one crashed worker permanently reduces
    a customer's usable balance, and they will not know why.
    """
    now = utcnow()
    stale: Iterable[CreditReservation] = db.scalars(
        select(CreditReservation)
        .where(
            CreditReservation.status == ReservationStatus.held,
            CreditReservation.expires_at < now,
        )
        .limit(limit)
    ).all()

    freed: list[uuid.UUID] = []
    for reservation in stale:
        release(
            db, reservation,
            reason="reservation_expired",
            status=ReservationStatus.expired,
        )
        freed.append(reservation.id)
    return freed


def reconcile(db: DbSession) -> list[dict]:
    """Recompute every balance from the ledger and report divergences.

    This is the check that proves the cache never drifted. It should run
    nightly and page someone on any non-empty result.
    """
    rows = db.execute(
        select(
            CreditWallet.id,
            CreditWallet.balance,
            func.coalesce(func.sum(CreditTransaction.amount), 0).label("ledger_sum"),
        )
        .outerjoin(CreditTransaction, CreditTransaction.wallet_id == CreditWallet.id)
        .group_by(CreditWallet.id, CreditWallet.balance)
    ).all()

    return [
        {
            "wallet_id": r.id,
            "cached_balance": _quantize(r.balance),
            "ledger_sum": _quantize(r.ledger_sum),
        }
        for r in rows
        if _quantize(r.balance) != _quantize(r.ledger_sum)
    ]
