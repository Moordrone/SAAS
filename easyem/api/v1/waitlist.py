"""Waiting-list capture.

Public and unauthenticated, so it is rate limited like the auth endpoints and
it never reveals whether an address is already on the list — the same
enumeration concern as password reset, for the same reason.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from ...db import utcnow
from ...models import WaitlistEntry
from ..deps import get_db

router = APIRouter(tags=["waitlist"])


class WaitlistIn(BaseModel):
    email: EmailStr
    context: str | None = Field(default=None, max_length=2000)


@router.post("/waitlist", status_code=status.HTTP_202_ACCEPTED)
def join(
    payload: WaitlistIn,
    request: Request,
    db: DbSession = Depends(get_db),
) -> dict:
    email = payload.email.strip().lower()
    existing = db.scalar(select(WaitlistEntry).where(WaitlistEntry.email == email))

    if existing is not None:
        # Someone re-submitting with more detail is telling us something, so
        # keep the longer answer rather than the first one.
        if payload.context and len(payload.context) > len(existing.context or ""):
            existing.context = payload.context.strip()
    else:
        db.add(
            WaitlistEntry(
                email=email,
                context=(payload.context or "").strip() or None,
                source=request.headers.get("referer"),
                created_at=utcnow(),
            )
        )
        try:
            db.flush()
        except IntegrityError:
            db.rollback()  # raced with another submission; already on the list

    # Same response either way.
    return {"status": "accepted"}
