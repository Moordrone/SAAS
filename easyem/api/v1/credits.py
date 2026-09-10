"""Credit balance and statement endpoints."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ...credits import service as credits_service
from ...models import CreditReservation, CreditTransaction, ReservationStatus, User
from ..deps import current_user, get_db
from .schemas import BalanceOut, TransactionOut

router = APIRouter(prefix="/credits", tags=["credits"])


@router.get("/balance", response_model=BalanceOut)
def balance(
    user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> BalanceOut:
    wallet = credits_service.get_or_create_wallet(db, user.default_account_id)
    held = db.scalar(
        select(func.coalesce(func.sum(CreditReservation.amount), 0)).where(
            CreditReservation.wallet_id == wallet.id,
            CreditReservation.status == ReservationStatus.held,
        )
    ) or Decimal("0")
    # `balance` already excludes holds; `held` is shown so the customer can see
    # why a pending job is affecting what they can spend.
    return BalanceOut(
        account_id=user.default_account_id,
        balance=wallet.balance + Decimal(held),
        held=Decimal(held),
        available=wallet.balance,
    )


@router.get("/transactions", response_model=list[TransactionOut])
def transactions(
    limit: int = Query(default=50, le=200, ge=1),
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> list[TransactionOut]:
    wallet = credits_service.get_or_create_wallet(db, user.default_account_id)
    rows = db.scalars(
        select(CreditTransaction)
        .where(CreditTransaction.wallet_id == wallet.id)
        .order_by(CreditTransaction.created_at.desc())
        .limit(limit)
    ).all()
    return [
        TransactionOut(
            id=t.id,
            operation=t.operation.value,
            amount=t.amount,
            balance_after=t.balance_after,
            reference_type=t.reference_type,
            reference_id=t.reference_id,
            reason=t.reason,
            created_at=t.created_at,
        )
        for t in rows
    ]
