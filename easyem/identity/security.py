"""Password hashing, opaque token generation, and access-token signing."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ..config import get_settings
from ..db import utcnow
from ..errors import InvalidToken

# Argon2id with library defaults, which track current OWASP guidance.
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Argon2 parameters get stronger over time; upgrade hashes on login."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def new_opaque_token() -> tuple[str, str]:
    """Return (plaintext, sha256 hex).

    Refresh tokens and email links are opaque and high-entropy, so a fast hash
    is correct here: there is nothing to brute-force. Only the hash is stored,
    so a database read does not yield usable tokens.
    """
    plaintext = secrets.token_urlsafe(48)
    return plaintext, hash_token(plaintext)


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def issue_access_token(
    *, user_id: uuid.UUID, session_id: uuid.UUID, account_id: uuid.UUID
) -> tuple[str, int]:
    settings = get_settings()
    now = utcnow()
    ttl = settings.access_token_ttl_seconds
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),
        "act": str(account_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
        "typ": "access",
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256"), ttl


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise InvalidToken(str(exc)) from exc
    if payload.get("typ") != "access":
        raise InvalidToken("Wrong token type")
    return payload


def constant_time_compare(a: str, b: str) -> bool:
    return secrets.compare_digest(a, b)
