"""Process-wide mailer.

A failed send must not roll back the work that triggered it: a user whose
account was created but whose email bounced should still have an account, and a
resend button. So `send` swallows transport errors and logs them.
"""

from __future__ import annotations

import logging

from ..config import get_settings
from .email import Email, EmailBackend, build_backend

logger = logging.getLogger("easyem.email")

_mailer: EmailBackend | None = None


def get_mailer() -> EmailBackend:
    global _mailer
    if _mailer is None:
        _mailer = build_backend(get_settings())
    return _mailer


def set_mailer(backend: EmailBackend | None) -> None:
    """Swap the backend. Used by tests and by application startup."""
    global _mailer
    _mailer = backend


def send(email: Email) -> bool:
    try:
        get_mailer().send(email)
        return True
    except Exception:
        # Never log the body: it carries the token.
        logger.exception(
            "email_send_failed", extra={"to": email.to, "tags": email.tags}
        )
        return False
