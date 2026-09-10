"""FastAPI entrypoint.

One deployable unit (ADR-02), organised in modules with explicit boundaries.
Extraction into a separate service happens when a load, security or deployment
constraint demands it — not before.
"""

from __future__ import annotations

import logging
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import logs
from .api.ratelimit import AUTH_PATHS, limiter
from .api.v1.router import api_v1
from .config import get_settings
from .errors import PROBLEM_CONTENT_TYPE, install_error_handlers
from .notifications import get_mailer

settings = get_settings()
logs.configure(json_output=settings.environment not in ("dev", "test"))
logger = logging.getLogger("easyem")

app = FastAPI(
    title="EasyEM API",
    version="0.1.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
)

install_error_handlers(app)

# Explicit origins, never a wildcard: browsers reject a wildcard alongside
# credentials, and it would be the wrong answer even if they did not.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key",
                   "X-Request-Id"],
    expose_headers=["X-Request-Id", "Retry-After"],
    max_age=600,
)


def _client_key(request: Request) -> str:
    """Identify the caller for rate limiting.

    Falls back to the socket address. Behind a proxy the real client is in
    X-Forwarded-For, which is trivially spoofed unless the proxy is trusted —
    so it is only honoured when TRUSTED_PROXY is set.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and os.environ.get("TRUSTED_PROXY"):
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)

    path = request.url.path
    is_auth = path in AUTH_PATHS
    limit = settings.auth_rate_limit if is_auth else settings.api_rate_limit
    window = (
        settings.auth_rate_window_seconds if is_auth
        else settings.api_rate_window_seconds
    )
    scope = "auth" if is_auth else "api"

    allowed, retry_after = limiter.check(
        f"{scope}:{_client_key(request)}", limit, window
    )
    if not allowed:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        logger.warning(
            "rate_limited",
            extra={"path": path, "scope": scope, "request_id": request_id},
        )
        return JSONResponse(
            status_code=429,
            media_type=PROBLEM_CONTENT_TYPE,
            headers={"Retry-After": str(retry_after)},
            content={
                "type": "https://docs.easyem.com/errors/rate_limited",
                "title": "Too many requests",
                "status": 429,
                "detail": f"Try again in {retry_after} seconds.",
                "code": "rate_limited",
                "request_id": request_id,
            },
        )
    return await call_next(request)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a request id and emit one structured line per request.

    The id is echoed in every problem+json body, so a customer support ticket
    with a request id resolves to exactly one log entry.
    """
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id
    started = time.perf_counter()

    response = await call_next(request)

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["x-request-id"] = request_id
    logger.info(
        "request",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response


@app.get("/v1/health", tags=["ops"])
def health() -> dict:
    return {
        "status": "ok",
        "environment": settings.environment,
        "email_backend": settings.email_backend,
        "mailer": type(get_mailer()).__name__,
    }


app.include_router(api_v1)
