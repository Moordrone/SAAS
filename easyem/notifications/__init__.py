"""Transactional notifications."""

from .email import (
    ConsoleEmailBackend,
    Email,
    EmailBackend,
    MemoryEmailBackend,
    SmtpEmailBackend,
    build_backend,
)
from .service import get_mailer, send, set_mailer

__all__ = [
    "Email", "EmailBackend", "ConsoleEmailBackend", "MemoryEmailBackend",
    "SmtpEmailBackend", "build_backend", "send", "get_mailer", "set_mailer",
]
