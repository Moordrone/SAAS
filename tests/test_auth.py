"""Authentication tests, with emphasis on refresh-token reuse detection."""

from decimal import Decimal

import pytest

from easyem.credits import service as credits
from easyem.errors import (
    AccountLocked,
    EmailAlreadyRegistered,
    InvalidCredentials,
    InvalidToken,
    RefreshTokenReused,
)
from easyem.identity import service as identity
from easyem.identity.security import decode_access_token, verify_password
from easyem.models import Membership, Role, Session

PASSWORD = "correct-horse-battery-staple"


def _signup(db, email="ing@example.com"):
    return identity.signup(
        db, email=email, password=PASSWORD, full_name="Yassine Ing", terms_version="2026-01",
    )


# --- signup ---------------------------------------------------------------

def test_signup_creates_account_membership_and_wallet(db):
    user, token = _signup(db)

    assert user.default_account_id is not None
    assert token  # verification token returned to the caller, never stored raw

    membership = db.query(Membership).filter_by(user_id=user.id).one()
    assert membership.role is Role.owner
    assert membership.account_id == user.default_account_id

    wallet = credits.get_or_create_wallet(db, user.default_account_id)
    assert wallet.balance == Decimal("50.000000")  # trial grant


def test_password_is_hashed_not_stored(db):
    user, _ = _signup(db)
    assert PASSWORD not in user.password_hash
    assert user.password_hash.startswith("$argon2")
    assert verify_password(PASSWORD, user.password_hash)


def test_email_is_normalised(db):
    user, _ = _signup(db, email="  ING@Example.COM ")
    assert user.email == "ing@example.com"


def test_duplicate_email_is_rejected(db):
    _signup(db)
    with pytest.raises(EmailAlreadyRegistered):
        _signup(db)


def test_new_user_is_not_verified(db):
    user, _ = _signup(db)
    assert not user.is_email_verified


def test_email_verification_works_once(db):
    user, token = _signup(db)
    identity.verify_email(db, token)
    assert user.is_email_verified

    with pytest.raises(InvalidToken):
        identity.verify_email(db, token)


# --- login ----------------------------------------------------------------

def test_login_returns_a_usable_access_token(db):
    user, _ = _signup(db)
    pair = identity.login(db, email="ing@example.com", password=PASSWORD)

    payload = decode_access_token(pair.access_token)
    assert payload["sub"] == str(user.id)
    assert payload["act"] == str(user.default_account_id)


def test_wrong_password_is_rejected(db):
    _signup(db)
    with pytest.raises(InvalidCredentials):
        identity.login(db, email="ing@example.com", password="wrong-password-here")


def test_unknown_email_gives_the_same_error(db):
    with pytest.raises(InvalidCredentials):
        identity.login(db, email="nobody@example.com", password=PASSWORD)


def test_account_locks_after_repeated_failures(db):
    _signup(db)
    for _ in range(identity.MAX_FAILED_LOGINS):
        with pytest.raises(InvalidCredentials):
            identity.login(db, email="ing@example.com", password="bad-password-x")

    # Correct password now also refused: the lock is on the account.
    with pytest.raises(AccountLocked):
        identity.login(db, email="ing@example.com", password=PASSWORD)


# --- refresh rotation -----------------------------------------------------

def test_refresh_rotates_the_token(db):
    _signup(db)
    first = identity.login(db, email="ing@example.com", password=PASSWORD)
    second = identity.refresh(db, first.refresh_token)

    assert second.refresh_token != first.refresh_token
    assert second.session_id != first.session_id


def test_reusing_a_refresh_token_revokes_the_whole_family(db):
    """A replayed refresh token means it leaked. Kill every session it spawned."""
    _signup(db)
    first = identity.login(db, email="ing@example.com", password=PASSWORD)
    second = identity.refresh(db, first.refresh_token)

    # Attacker replays the token the legitimate client already spent.
    with pytest.raises(RefreshTokenReused):
        identity.refresh(db, first.refresh_token)

    # The legitimate successor is dead too — that is the point.
    with pytest.raises(InvalidToken):
        identity.refresh(db, second.refresh_token)

    sessions = db.query(Session).all()
    assert all(s.revoked_at is not None for s in sessions)
    assert all(s.revoked_reason == "refresh_token_reuse" for s in sessions)


def test_logout_revokes_the_family(db):
    _signup(db)
    pair = identity.login(db, email="ing@example.com", password=PASSWORD)
    identity.logout(db, pair.refresh_token)

    with pytest.raises(InvalidToken):
        identity.refresh(db, pair.refresh_token)


def test_unknown_refresh_token_is_rejected(db):
    with pytest.raises(InvalidToken):
        identity.refresh(db, "not-a-real-token")


# --- password reset -------------------------------------------------------

def test_password_reset_changes_password_and_kills_sessions(db):
    _signup(db)
    pair = identity.login(db, email="ing@example.com", password=PASSWORD)

    token = identity.request_password_reset(db, "ing@example.com")
    identity.reset_password(db, token, "a-brand-new-passphrase")

    with pytest.raises(InvalidToken):
        identity.refresh(db, pair.refresh_token)

    with pytest.raises(InvalidCredentials):
        identity.login(db, email="ing@example.com", password=PASSWORD)

    identity.login(db, email="ing@example.com", password="a-brand-new-passphrase")


def test_reset_request_for_unknown_email_returns_none_not_an_error(db):
    """No account-enumeration oracle: the caller responds identically either way."""
    assert identity.request_password_reset(db, "ghost@example.com") is None


def test_reset_token_is_single_use(db):
    _signup(db)
    token = identity.request_password_reset(db, "ing@example.com")
    identity.reset_password(db, token, "a-brand-new-passphrase")

    with pytest.raises(InvalidToken):
        identity.reset_password(db, token, "yet-another-passphrase")
