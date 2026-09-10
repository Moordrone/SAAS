"""Transactional email bodies.

Plain text, short, one action each. No marketing in a transactional email: it
hurts deliverability and it buries the thing the person actually needs.
"""

from __future__ import annotations

from .email import Email


def verification(*, to: str, name: str, token: str, base_url: str) -> Email:
    link = f"{base_url}/verify-email?token={token}"
    return Email(
        to=to,
        subject="Confirm your EasyEM address",
        tags=["verification"],
        text=(
            f"Hello {name},\n\n"
            "Confirm your address to start running simulations:\n\n"
            f"{link}\n\n"
            "The link is valid for 24 hours and can only be used once.\n\n"
            "If you did not create an EasyEM account, ignore this message.\n"
        ),
    )


def password_reset(*, to: str, name: str, token: str, base_url: str) -> Email:
    link = f"{base_url}/reset-password?token={token}"
    return Email(
        to=to,
        subject="Reset your EasyEM password",
        tags=["password_reset"],
        text=(
            f"Hello {name},\n\n"
            f"Reset your password here:\n\n{link}\n\n"
            "The link expires in 30 minutes and can only be used once. "
            "Resetting will sign you out everywhere.\n\n"
            "If you did not request this, ignore the message — your password "
            "has not changed.\n"
        ),
    )


def simulation_finished(
    *, to: str, name: str, project: str, job_id: str, base_url: str,
    charged: str, succeeded: bool, error: str | None = None,
) -> Email:
    link = f"{base_url}/simulations/{job_id}"
    if succeeded:
        body = (
            f"Hello {name},\n\n"
            f"Your simulation of \"{project}\" is finished.\n\n"
            f"{link}\n\n"
            f"Charged: {charged} credits.\n"
        )
    else:
        body = (
            f"Hello {name},\n\n"
            f"Your simulation of \"{project}\" did not complete.\n\n"
            f"Reason: {error or 'unknown'}\n"
            f"Charged: {charged} credits.\n\n"
            f"{link}\n"
        )
    return Email(
        to=to,
        subject=f"Simulation {'finished' if succeeded else 'failed'}: {project}",
        tags=["simulation"],
        text=body,
    )


def low_balance(*, to: str, name: str, balance: str, base_url: str) -> Email:
    return Email(
        to=to,
        subject="Your EasyEM credits are running low",
        tags=["billing"],
        text=(
            f"Hello {name},\n\n"
            f"You have {balance} credits left, which may not cover your next "
            "full-wave run.\n\n"
            f"{base_url}/billing\n"
        ),
    )
