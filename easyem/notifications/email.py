"""Transactional email.

Abstracted the same way payment and the solver are, and for the same reason:
the provider will change. What must not change is that a verification token
reaches the person who asked for it.

Three backends. `console` prints — the development default, and it makes the
token visible without a mail server. `memory` collects, so tests can assert on
what was sent rather than mocking. `smtp` sends.

**Tokens never touch a log.** They appear in the rendered body and nowhere else.
A token in an application log is a password in an application log.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.message import EmailMessage

logger = logging.getLogger("easyem.email")


@dataclass
class Email:
    to: str
    subject: str
    text: str
    html: str | None = None
    tags: list[str] = field(default_factory=list)


class EmailBackend(ABC):
    @abstractmethod
    def send(self, email: Email) -> None: ...


class ConsoleEmailBackend(EmailBackend):
    """Prints to stdout. The development default."""

    def send(self, email: Email) -> None:
        print(f"\n{'=' * 70}\nTO: {email.to}\nSUBJECT: {email.subject}\n{'-' * 70}")
        print(email.text)
        print("=" * 70)


class MemoryEmailBackend(EmailBackend):
    """Collects for assertions. Lets a test read the token a user would receive
    instead of reaching into the database to fake verification — which is how
    the broken signup flow went unnoticed in the first place."""

    def __init__(self) -> None:
        self.outbox: list[Email] = []

    def send(self, email: Email) -> None:
        self.outbox.append(email)

    def last_to(self, address: str) -> Email | None:
        for email in reversed(self.outbox):
            if email.to.lower() == address.lower():
                return email
        return None

    def clear(self) -> None:
        self.outbox.clear()


class SmtpEmailBackend(EmailBackend):
    def __init__(
        self, host: str, port: int, username: str, password: str,
        sender: str, use_tls: bool = True,
    ) -> None:
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.sender, self.use_tls = sender, use_tls

    def send(self, email: Email) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = email.to
        message["Subject"] = email.subject
        message.set_content(email.text)
        if email.html:
            message.add_alternative(email.html, subtype="html")

        with smtplib.SMTP(self.host, self.port, timeout=20) as server:
            if self.use_tls:
                server.starttls(context=ssl.create_default_context())
            if self.username:
                server.login(self.username, self.password)
            server.send_message(message)
        # Address and purpose, never the body: it contains the token.
        logger.info("email_sent", extra={"to": email.to, "tags": email.tags})


def build_backend(settings) -> EmailBackend:
    kind = getattr(settings, "email_backend", "console")
    if kind == "memory":
        return MemoryEmailBackend()
    if kind == "smtp":
        return SmtpEmailBackend(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            sender=settings.email_from,
            use_tls=settings.smtp_use_tls,
        )
    return ConsoleEmailBackend()
