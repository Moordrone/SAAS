"""A single error shape for the whole API: RFC 9457 problem+json.

Every error carries a stable machine-readable `code` that clients may branch on,
and a `request_id` that ties the response to the server logs.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_CONTENT_TYPE = "application/problem+json"


class AppError(Exception):
    """Base class for expected, client-visible failures."""

    status: int = 400
    code: str = "bad_request"
    title: str = "Bad request"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail or self.title
        self.extra = extra
        super().__init__(self.detail)


# --- identity -------------------------------------------------------------

class EmailAlreadyRegistered(AppError):
    status, code, title = 409, "email_already_registered", "Email already registered"


class InvalidCredentials(AppError):
    status, code, title = 401, "invalid_credentials", "Invalid email or password"


class AccountLocked(AppError):
    status, code, title = 423, "account_locked", "Account temporarily locked"


class EmailNotVerified(AppError):
    status, code, title = 403, "email_not_verified", "Email address not verified"


class InvalidToken(AppError):
    status, code, title = 401, "invalid_token", "Invalid or expired token"


class RefreshTokenReused(AppError):
    """A refresh token was presented twice. The whole session family is revoked."""

    status, code, title = 401, "refresh_token_reused", "Session revoked"


class NotAuthenticated(AppError):
    status, code, title = 401, "not_authenticated", "Authentication required"


class Forbidden(AppError):
    status, code, title = 403, "forbidden", "Insufficient permissions"


class NotFound(AppError):
    status, code, title = 404, "not_found", "Resource not found"


# --- credits --------------------------------------------------------------

class InsufficientCredits(AppError):
    status, code, title = 402, "insufficient_credits", "Insufficient credits"


class LedgerViolation(AppError):
    """Raised when an operation would break a ledger invariant. Never expected."""

    status, code, title = 500, "ledger_violation", "Credit ledger inconsistency"


class ReservationAlreadyResolved(AppError):
    status, code, title = 409, "reservation_already_resolved", "Reservation already resolved"


class DuplicateReservation(AppError):
    status, code, title = 409, "duplicate_reservation", "Reservation already exists"


# --- rendering ------------------------------------------------------------

def _problem(
    *, status: int, code: str, title: str, detail: str, request: Request, **extra: Any
) -> JSONResponse:
    payload: dict[str, Any] = {
        "type": f"https://docs.easyem.com/errors/{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
        "request_id": getattr(request.state, "request_id", None),
    }
    payload.update({k: v for k, v in extra.items() if v is not None})
    return JSONResponse(
        status_code=status, content=payload, media_type=PROBLEM_CONTENT_TYPE
    )


def install_error_handlers(app) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        return _problem(
            status=exc.status,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            request=request,
            **exc.extra,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return _problem(
            status=422,
            code="validation_error",
            title="Request validation failed",
            detail="One or more fields are invalid.",
            request=request,
            errors=[
                {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]}
                for e in exc.errors()
            ],
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        return _problem(
            status=exc.status_code,
            code="http_error",
            title=str(exc.detail),
            detail=str(exc.detail),
            request=request,
        )
