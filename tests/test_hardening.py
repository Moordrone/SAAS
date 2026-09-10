"""Rate limiting, CORS, logging and session hygiene.

The audit found all four missing. These tests exist so they cannot go missing
again quietly.
"""

import json
import logging
from datetime import timedelta

from easyem.api.ratelimit import limiter
from easyem.db import utcnow
from easyem.identity import service as identity
from easyem.logs import JsonFormatter
from easyem.models import Session

PASSWORD = "correct-horse-battery-staple"


# --- rate limiting --------------------------------------------------------

def test_login_is_rate_limited(client):
    """Account lockout stops brute force against one account. This stops the
    other shape: one attempt each against thousands of accounts."""
    seen_429 = False
    for _ in range(30):
        r = client.post("/v1/auth/login",
                        json={"email": "x@example.com", "password": "wrong-pass-1"})
        if r.status_code == 429:
            seen_429 = True
            break
    assert seen_429, "login accepted 30 attempts from one client"


def test_rate_limit_response_is_problem_json_with_retry_after(client):
    for _ in range(30):
        r = client.post("/v1/auth/login",
                        json={"email": "x@example.com", "password": "wrong-pass-1"})
        if r.status_code == 429:
            break
    assert r.headers["content-type"].startswith("application/problem+json")
    assert int(r.headers["Retry-After"]) > 0
    assert r.json()["code"] == "rate_limited"


def test_signup_is_rate_limited(client):
    codes = [
        client.post("/v1/auth/signup", json={
            "email": f"u{i}@example.com", "password": PASSWORD, "full_name": "U",
        }).status_code
        for i in range(30)
    ]
    assert 429 in codes


def test_reading_is_not_limited_as_tightly_as_authenticating(client):
    """A working app polls. Only the endpoints an attacker gains from should
    be on the tight budget."""
    limiter.reset()
    codes = {client.get("/v1/health").status_code for _ in range(25)}
    assert codes == {200}


# --- CORS -----------------------------------------------------------------

def test_cors_allows_the_configured_origin(client):
    r = client.options("/v1/health", headers={
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "GET",
    })
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_rejects_an_unknown_origin(client):
    r = client.options("/v1/health", headers={
        "Origin": "https://evil.example.com",
        "Access-Control-Request-Method": "GET",
    })
    assert r.headers.get("access-control-allow-origin") != "https://evil.example.com"


def test_idempotency_key_header_survives_preflight(client):
    r = client.options("/v1/projects", headers={
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "idempotency-key",
    })
    allowed = r.headers.get("access-control-allow-headers", "").lower()
    assert "idempotency-key" in allowed


# --- logging --------------------------------------------------------------

def test_extra_fields_survive_the_formatter():
    """The request middleware passes request_id and duration as extra fields.
    Without a JSON formatter they vanish and production logs are prose."""
    record = logging.LogRecord(
        "easyem", logging.INFO, __file__, 1, "request", None, None
    )
    record.request_id = "abc-123"
    record.duration_ms = 12.5

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "request"
    assert payload["request_id"] == "abc-123"
    assert payload["duration_ms"] == 12.5
    assert payload["level"] == "INFO"


def test_request_id_is_echoed_to_the_caller(client):
    r = client.get("/v1/health", headers={"X-Request-Id": "trace-me"})
    assert r.headers["x-request-id"] == "trace-me"


# --- email hygiene --------------------------------------------------------

def test_the_verification_token_never_appears_in_the_response(client, outbox):
    """A token in an HTTP response is a token in every proxy log between here
    and the browser."""
    r = client.post("/v1/auth/signup", json={
        "email": "quiet@example.com", "password": PASSWORD, "full_name": "Q",
    })
    mail = outbox.last_to("quiet@example.com")
    token = mail.text.split("verify-email?token=")[1].split()[0]
    assert token not in r.text


def test_resend_verification_does_not_reveal_whether_the_account_exists(client):
    known = client.post("/v1/auth/signup", json={
        "email": "known@example.com", "password": PASSWORD, "full_name": "K",
    })
    assert known.status_code == 201

    a = client.post("/v1/auth/resend-verification", json={"email": "known@example.com"})
    b = client.post("/v1/auth/resend-verification", json={"email": "ghost@example.com"})
    assert a.status_code == b.status_code == 202
    assert a.json() == b.json()


def test_resend_actually_sends_a_fresh_token(client, outbox):
    client.post("/v1/auth/signup", json={
        "email": "again@example.com", "password": PASSWORD, "full_name": "A",
    })
    first = outbox.last_to("again@example.com").text
    client.post("/v1/auth/resend-verification", json={"email": "again@example.com"})
    second = outbox.last_to("again@example.com").text
    assert first != second, "resend reused the old token"


# --- account deletion -----------------------------------------------------

def test_a_deleted_address_can_register_again(db, outbox):
    """Soft delete kept the address on the row, and the UNIQUE constraint then
    blocked that person from ever coming back."""
    user, _ = identity.signup(
        db, email="leaving@example.com", password=PASSWORD, full_name="L"
    )
    identity.delete_account(db, user)

    fresh, _ = identity.signup(
        db, email="leaving@example.com", password=PASSWORD, full_name="L again"
    )
    assert fresh.id != user.id
    # The old row survives for invoices and audit, with the address released.
    assert user.email.startswith("deleted+")


def test_deleting_an_account_ends_its_sessions(db):
    user, _ = identity.signup(
        db, email="bye@example.com", password=PASSWORD, full_name="B"
    )
    pair = identity.login(db, email="bye@example.com", password=PASSWORD)
    identity.delete_account(db, user)

    import pytest

    from easyem.errors import InvalidToken

    with pytest.raises(InvalidToken):
        identity.refresh(db, pair.refresh_token)


# --- session hygiene ------------------------------------------------------

def test_expired_sessions_are_purged(db):
    """Every login and every rotation writes a row. Nothing removed them."""
    user, _ = identity.signup(
        db, email="sessions@example.com", password=PASSWORD, full_name="S"
    )
    identity.login(db, email="sessions@example.com", password=PASSWORD)

    stale = db.query(Session).all()
    for session in stale:
        session.expires_at = utcnow() - timedelta(days=90)
    db.flush()

    assert identity.purge_expired_sessions(db) == len(stale)
    assert db.query(Session).count() == 0


def test_live_sessions_are_left_alone(db):
    identity.signup(db, email="live@example.com", password=PASSWORD, full_name="L")
    identity.login(db, email="live@example.com", password=PASSWORD)
    assert identity.purge_expired_sessions(db) == 0
    assert db.query(Session).count() == 1


# --- waiting list ---------------------------------------------------------

def test_waitlist_accepts_an_address(client, db):
    from easyem.models import WaitlistEntry

    r = client.post("/v1/waitlist", json={
        "email": "Ing@Lab.example", "context": "24 GHz radar patches on Rogers",
    })
    assert r.status_code == 202

    entry = db.query(WaitlistEntry).one()
    assert entry.email == "ing@lab.example"          # normalised
    assert "radar" in entry.context


def test_waitlist_does_not_reveal_existing_addresses(client):
    """Same enumeration concern as password reset, for the same reason."""
    first = client.post("/v1/waitlist", json={"email": "dup@example.com"})
    second = client.post("/v1/waitlist", json={"email": "dup@example.com"})
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()


def test_resubmitting_keeps_the_longer_answer(client, db):
    """Someone coming back with more detail is telling us something."""
    from easyem.models import WaitlistEntry

    client.post("/v1/waitlist", json={"email": "more@example.com", "context": "patches"})
    client.post("/v1/waitlist", json={
        "email": "more@example.com",
        "context": "patches, mostly 5.8 GHz arrays on RO4003C for drone links",
    })
    entry = db.query(WaitlistEntry).filter_by(email="more@example.com").one()
    assert "drone" in entry.context
    assert db.query(WaitlistEntry).count() == 1


def test_waitlist_is_rate_limited(client):
    codes = [
        client.post("/v1/waitlist", json={"email": f"s{i}@example.com"}).status_code
        for i in range(30)
    ]
    assert 429 in codes
