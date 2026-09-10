"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session as DbSession

from ...identity import service as identity
from ..deps import get_db
from .schemas import (
    LoginIn,
    PasswordResetIn,
    PasswordResetRequestIn,
    RefreshIn,
    SignupIn,
    TokenOut,
    UserOut,
    VerifyEmailIn,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _client(request: Request) -> tuple[str | None, str | None]:
    return (
        request.headers.get("user-agent"),
        request.client.host if request.client else None,
    )


@router.post("/signup", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupIn, db: DbSession = Depends(get_db)) -> UserOut:
    # The service sends the verification email. The token is deliberately not
    # returned: a token in an HTTP response is a token in a proxy log.
    user, _token = identity.signup(
        db,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        terms_version=payload.terms_version,
    )
    return UserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        email_verified=user.is_email_verified,
        default_account_id=user.default_account_id,
        created_at=user.created_at,
    )


@router.post("/login", response_model=TokenOut)
def login(payload: LoginIn, request: Request, db: DbSession = Depends(get_db)) -> TokenOut:
    ua, ip = _client(request)
    pair = identity.login(
        db, email=payload.email, password=payload.password,
        user_agent=ua, ip_address=ip,
    )
    return TokenOut(
        access_token=pair.access_token,
        expires_in=pair.expires_in,
        refresh_token=pair.refresh_token,
    )


@router.post("/refresh", response_model=TokenOut)
def refresh(payload: RefreshIn, request: Request, db: DbSession = Depends(get_db)) -> TokenOut:
    ua, ip = _client(request)
    pair = identity.refresh(
        db, payload.refresh_token, user_agent=ua, ip_address=ip
    )
    return TokenOut(
        access_token=pair.access_token,
        expires_in=pair.expires_in,
        refresh_token=pair.refresh_token,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(payload: RefreshIn, db: DbSession = Depends(get_db)) -> Response:
    identity.logout(db, payload.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/verify-email", response_model=UserOut)
def verify_email(payload: VerifyEmailIn, db: DbSession = Depends(get_db)) -> UserOut:
    user = identity.verify_email(db, payload.token)
    return UserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        email_verified=user.is_email_verified,
        default_account_id=user.default_account_id,
        created_at=user.created_at,
    )


@router.post("/resend-verification", status_code=status.HTTP_202_ACCEPTED)
def resend_verification(
    payload: PasswordResetRequestIn, db: DbSession = Depends(get_db)
) -> dict:
    identity.resend_verification(db, payload.email)
    return {"status": "accepted"}


@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED)
def request_password_reset(
    payload: PasswordResetRequestIn, db: DbSession = Depends(get_db)
) -> dict:
    identity.request_password_reset(db, payload.email)
    # Identical response whether or not the address exists: no enumeration.
    return {"status": "accepted"}


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
def confirm_password_reset(
    payload: PasswordResetIn, db: DbSession = Depends(get_db)
) -> Response:
    identity.reset_password(db, payload.token, payload.new_password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
