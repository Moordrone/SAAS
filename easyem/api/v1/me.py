"""Current-user endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...models import User
from ..deps import current_user
from .schemas import UserOut

router = APIRouter(tags=["me"])


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        email_verified=user.is_email_verified,
        default_account_id=user.default_account_id,
        created_at=user.created_at,
    )
