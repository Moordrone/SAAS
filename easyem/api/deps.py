"""Shared FastAPI dependencies: current user, current account, authorisation."""

from __future__ import annotations

import uuid

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..db import get_session
from ..errors import Forbidden, InvalidToken, NotAuthenticated
from ..identity.security import decode_access_token
from ..models import Membership, Role, Session, User


def get_db() -> DbSession:  # pragma: no cover - thin wrapper
    yield from get_session()


def current_user(
    authorization: str | None = Header(default=None),
    db: DbSession = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise NotAuthenticated()

    payload = decode_access_token(authorization.split(" ", 1)[1].strip())

    session = db.get(Session, uuid.UUID(payload["sid"]))
    if session is None or session.revoked_at is not None:
        raise InvalidToken("Session revoked")

    user = db.get(User, uuid.UUID(payload["sub"]))
    if user is None or user.deleted_at is not None:
        raise InvalidToken("User not found")
    return user


def verified_user(user: User = Depends(current_user)) -> User:
    """Gate for anything that costs money: the AI copilot, simulations.

    Unverified accounts are free to create; they must not be free to spend.
    """
    if not user.is_email_verified:
        from ..errors import EmailNotVerified

        raise EmailNotVerified()
    return user


def require_account_role(
    db: DbSession, user: User, account_id: uuid.UUID, *allowed: Role
) -> Membership:
    membership = db.scalar(
        select(Membership).where(
            Membership.account_id == account_id, Membership.user_id == user.id
        )
    )
    if membership is None:
        # Do not distinguish "no access" from "does not exist".
        raise Forbidden("No access to this account")
    if allowed and membership.role not in allowed:
        raise Forbidden(f"Requires one of: {', '.join(r.value for r in allowed)}")
    return membership
