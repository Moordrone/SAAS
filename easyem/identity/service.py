"""Signup, login, refresh rotation, email verification, password reset."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from ..config import get_settings
from ..credits import service as credits_service
from ..db import utcnow
from ..errors import (
    AccountLocked,
    EmailAlreadyRegistered,
    InvalidCredentials,
    InvalidToken,
    RefreshTokenReused,
)
from ..models import (
    Account,
    AccountType,
    LedgerOp,
    Membership,
    Role,
    Session,
    TokenPurpose,
    User,
    UserStatus,
    VerificationToken,
)
from ..notifications import send
from ..notifications.templates import password_reset as reset_email
from ..notifications.templates import verification as verification_email
from .security import (
    hash_password,
    hash_token,
    issue_access_token,
    needs_rehash,
    new_opaque_token,
    verify_password,
)

MAX_FAILED_LOGINS = 8
LOCKOUT = timedelta(minutes=15)


@dataclass
class TokenPair:
    access_token: str
    expires_in: int
    refresh_token: str
    session_id: uuid.UUID


def normalise_email(email: str) -> str:
    return email.strip().lower()


# --------------------------------------------------------------------------- #
# signup
# --------------------------------------------------------------------------- #

def signup(
    db: DbSession,
    *,
    email: str,
    password: str,
    full_name: str,
    terms_version: str | None = None,
) -> tuple[User, str]:
    """Create a user, their personal account, wallet and verification token.

    Returns the plaintext verification token; the caller emails it and must not
    log or store it.
    """
    email = normalise_email(email)

    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise EmailAlreadyRegistered()

    account = Account(
        type=AccountType.personal,
        name=full_name.strip() or email,
    )
    db.add(account)
    db.flush()

    user = User(
        email=email,
        password_hash=hash_password(password),
        full_name=full_name.strip(),
        default_account_id=account.id,
        terms_version_accepted=terms_version,
        terms_accepted_at=utcnow() if terms_version else None,
    )
    db.add(user)

    try:
        db.flush()
    except IntegrityError as exc:  # concurrent signup with the same address
        db.rollback()
        raise EmailAlreadyRegistered() from exc

    db.add(Membership(account_id=account.id, user_id=user.id, role=Role.owner))

    settings = get_settings()
    credits_service.credit(
        db,
        account.id,
        settings.signup_grant_credits,
        operation=LedgerOp.grant,
        idempotency_key=f"signup-grant:{account.id}",
        reason="signup_trial_credits",
    )

    plaintext = _issue_verification_token(
        db, user, TokenPurpose.email_verification,
        ttl_seconds=settings.email_verification_ttl_seconds,
    )
    db.flush()

    # Sending happens here, not in the API layer, so every caller — HTTP,
    # admin tooling, a future resend endpoint — gets it. A transport failure
    # does not roll back the account: the user still has one, and a resend.
    send(
        verification_email(
            to=user.email, name=user.full_name or user.email,
            token=plaintext, base_url=settings.app_base_url,
        )
    )
    return user, plaintext


def _issue_verification_token(
    db: DbSession, user: User, purpose: TokenPurpose, *, ttl_seconds: int
) -> str:
    # Any outstanding token for the same purpose is invalidated first.
    db.execute(
        update(VerificationToken)
        .where(
            VerificationToken.user_id == user.id,
            VerificationToken.purpose == purpose,
            VerificationToken.consumed_at.is_(None),
        )
        .values(consumed_at=utcnow())
    )
    plaintext, token_hash = new_opaque_token()
    now = utcnow()
    db.add(
        VerificationToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=token_hash,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
    )
    return plaintext


def _consume_verification_token(
    db: DbSession, plaintext: str, purpose: TokenPurpose
) -> User:
    token = db.scalar(
        select(VerificationToken).where(
            VerificationToken.token_hash == hash_token(plaintext),
            VerificationToken.purpose == purpose,
        )
    )
    now = utcnow()
    if token is None or token.consumed_at is not None or token.expires_at < now:
        raise InvalidToken("Token is invalid, already used, or expired")

    token.consumed_at = now
    user = db.get(User, token.user_id)
    if user is None:
        raise InvalidToken("Token is invalid")
    return user


def resend_verification(db: DbSession, email: str) -> bool:
    """Resend, silently succeeding for unknown or already-verified addresses.

    Same shape as the reset flow: a differing response is an enumeration
    oracle, and the bounce rate is the only place this should be visible.
    """
    user = db.scalar(select(User).where(User.email == normalise_email(email)))
    if user is None or user.is_email_verified or user.status is not UserStatus.active:
        return False

    settings = get_settings()
    plaintext = _issue_verification_token(
        db, user, TokenPurpose.email_verification,
        ttl_seconds=settings.email_verification_ttl_seconds,
    )
    db.flush()
    send(
        verification_email(
            to=user.email, name=user.full_name or user.email,
            token=plaintext, base_url=settings.app_base_url,
        )
    )
    return True


def verify_email(db: DbSession, plaintext: str) -> User:
    user = _consume_verification_token(db, plaintext, TokenPurpose.email_verification)
    if user.email_verified_at is None:
        user.email_verified_at = utcnow()
    db.flush()
    return user


def request_password_reset(db: DbSession, email: str) -> str | None:
    """Returns a token, or None if the address is unknown.

    The caller must respond identically either way: a differing response is an
    account-enumeration oracle.
    """
    user = db.scalar(select(User).where(User.email == normalise_email(email)))
    if user is None or user.status is not UserStatus.active:
        return None
    settings = get_settings()
    plaintext = _issue_verification_token(
        db, user, TokenPurpose.password_reset,
        ttl_seconds=settings.password_reset_ttl_seconds,
    )
    db.flush()
    send(
        reset_email(
            to=user.email, name=user.full_name or user.email,
            token=plaintext, base_url=settings.app_base_url,
        )
    )
    return plaintext


def reset_password(db: DbSession, plaintext: str, new_password: str) -> User:
    user = _consume_verification_token(db, plaintext, TokenPurpose.password_reset)
    user.password_hash = hash_password(new_password)
    user.failed_login_count = 0
    user.locked_until = None
    revoke_all_sessions(db, user.id, reason="password_reset")
    db.flush()
    return user


# --------------------------------------------------------------------------- #
# login / sessions
# --------------------------------------------------------------------------- #

def login(
    db: DbSession,
    *,
    email: str,
    password: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> TokenPair:
    user = db.scalar(select(User).where(User.email == normalise_email(email)))
    now = utcnow()

    if user is None:
        # Hash anyway so timing does not reveal whether the address exists.
        hash_password(password)
        raise InvalidCredentials()

    if user.locked_until is not None and user.locked_until > now:
        raise AccountLocked("Too many failed attempts. Try again later.")

    if user.status is not UserStatus.active or user.deleted_at is not None:
        raise InvalidCredentials()

    if not verify_password(password, user.password_hash):
        user.failed_login_count += 1
        if user.failed_login_count >= MAX_FAILED_LOGINS:
            user.locked_until = now + LOCKOUT
            user.failed_login_count = 0
        db.flush()
        raise InvalidCredentials()

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now

    return _start_session(db, user, user_agent=user_agent, ip_address=ip_address)


def _start_session(
    db: DbSession,
    user: User,
    *,
    user_agent: str | None,
    ip_address: str | None,
) -> TokenPair:
    settings = get_settings()
    plaintext, token_hash = new_opaque_token()
    now = utcnow()
    family_id = uuid.uuid4()

    session = Session(
        user_id=user.id,
        family_id=family_id,
        refresh_token_hash=token_hash,
        created_at=now,
        expires_at=now + timedelta(seconds=settings.refresh_token_ttl_seconds),
        user_agent=user_agent,
        ip_address=ip_address,
    )
    db.add(session)
    db.flush()

    access, ttl = issue_access_token(
        user_id=user.id, session_id=session.id, account_id=user.default_account_id
    )
    return TokenPair(
        access_token=access,
        expires_in=ttl,
        refresh_token=plaintext,
        session_id=session.id,
    )


def refresh(
    db: DbSession,
    refresh_token: str,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> TokenPair:
    """Rotate a refresh token, detecting reuse.

    Each refresh consumes one token and mints its successor in the same family.
    Presenting a token that was already consumed means a copy leaked, so every
    session in that family is revoked. Losing one legitimate session is a small
    price for closing a stolen one.
    """
    session = db.scalar(
        select(Session).where(Session.refresh_token_hash == hash_token(refresh_token))
    )
    now = utcnow()

    if session is None:
        raise InvalidToken("Unknown refresh token")

    if session.used_at is not None:
        _revoke_family(db, session.family_id, reason="refresh_token_reuse")
        db.flush()
        raise RefreshTokenReused(
            "This session was revoked because a refresh token was reused."
        )

    if session.revoked_at is not None or session.expires_at < now:
        raise InvalidToken("Refresh token expired or revoked")

    user = db.get(User, session.user_id)
    if user is None or user.status is not UserStatus.active or user.deleted_at:
        raise InvalidToken("User is not active")

    session.used_at = now

    settings = get_settings()
    plaintext, token_hash = new_opaque_token()
    child = Session(
        user_id=user.id,
        family_id=session.family_id,
        parent_id=session.id,
        refresh_token_hash=token_hash,
        created_at=now,
        expires_at=now + timedelta(seconds=settings.refresh_token_ttl_seconds),
        user_agent=user_agent or session.user_agent,
        ip_address=ip_address or session.ip_address,
    )
    db.add(child)
    db.flush()

    access, ttl = issue_access_token(
        user_id=user.id, session_id=child.id, account_id=user.default_account_id
    )
    return TokenPair(
        access_token=access,
        expires_in=ttl,
        refresh_token=plaintext,
        session_id=child.id,
    )


def _revoke_family(db: DbSession, family_id: uuid.UUID, *, reason: str) -> int:
    result = db.execute(
        update(Session)
        .where(Session.family_id == family_id, Session.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoked_reason=reason)
    )
    return result.rowcount or 0


def logout(db: DbSession, refresh_token: str) -> None:
    session = db.scalar(
        select(Session).where(Session.refresh_token_hash == hash_token(refresh_token))
    )
    if session is not None:
        _revoke_family(db, session.family_id, reason="logout")
        db.flush()


def delete_account(db: DbSession, user: User) -> User:
    """Soft delete, with the address scrambled.

    Keeping the original address on a soft-deleted row means that address can
    never register again, because the UNIQUE constraint still holds it. The row
    stays for invoices and audit; the address is released.
    """
    now = utcnow()
    user.email = f"deleted+{user.id}@deleted.easyem.com"
    user.status = UserStatus.deleted
    user.deleted_at = now
    user.full_name = "Deleted user"
    user.password_hash = hash_password(uuid.uuid4().hex)
    user.mfa_secret_encrypted = None
    revoke_all_sessions(db, user.id, reason="account_deleted")
    db.flush()
    return user


def purge_expired_sessions(db: DbSession, *, older_than_days: int = 30) -> int:
    """Delete sessions that are expired or long revoked.

    Every login and every refresh rotation writes a row. Nothing removed them,
    so the table grew without bound — slowly, and therefore unnoticed until it
    is a problem.
    """
    cutoff = utcnow() - timedelta(days=older_than_days)
    result = db.execute(
        delete(Session).where(
            or_(Session.expires_at < cutoff, Session.revoked_at < cutoff)
        )
    )
    db.flush()
    return result.rowcount or 0


def revoke_all_sessions(db: DbSession, user_id: uuid.UUID, *, reason: str) -> int:
    result = db.execute(
        update(Session)
        .where(Session.user_id == user_id, Session.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoked_reason=reason)
    )
    db.flush()
    return result.rowcount or 0
