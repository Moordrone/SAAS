"""Request and response models for /v1."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

MIN_PASSWORD_LENGTH = 12


class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)
    full_name: str = Field(min_length=1, max_length=200)
    terms_version: str | None = Field(default=None, max_length=32)

    @field_validator("password")
    @classmethod
    def _not_trivial(cls, v: str) -> str:
        if v.lower() in {"password1234", "changemechange"} or len(set(v)) < 5:
            raise ValueError("Password is too predictable")
        return v


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str


class VerifyEmailIn(BaseModel):
    token: str


class PasswordResetRequestIn(BaseModel):
    email: EmailStr


class PasswordResetIn(BaseModel):
    token: str
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    type: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    full_name: str
    email_verified: bool
    default_account_id: uuid.UUID
    created_at: datetime


class BalanceOut(BaseModel):
    account_id: uuid.UUID
    balance: Decimal
    held: Decimal
    available: Decimal


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    operation: str
    amount: Decimal
    balance_after: Decimal
    reference_type: str | None
    reference_id: uuid.UUID | None
    reason: str | None
    created_at: datetime


class Page(BaseModel):
    """Cursor pagination. Offsets break exactly when a customer gets big."""

    items: list
    next_cursor: str | None = None
