"""The ledger tests. These are the ones that must never be allowed to fail.

Every assertion here corresponds to a way of losing money or a customer's
trust: double-charging, silently freezing credits, or letting the cached
balance drift away from the ledger.
"""

from decimal import Decimal

import pytest

from easyem.credits import service as credits
from easyem.errors import (
    DuplicateReservation,
    InsufficientCredits,
    ReservationAlreadyResolved,
)
from easyem.models import CreditTransaction, LedgerOp, ReservationStatus
from tests.conftest import new_job_id


def _grant(db, account, amount, key="grant-1"):
    return credits.credit(
        db, account.id, amount, operation=LedgerOp.grant, idempotency_key=key
    )


def _balance(db, account):
    return credits.get_or_create_wallet(db, account.id).balance


# --- the core invariant ---------------------------------------------------

def test_cached_balance_always_equals_ledger_sum(db, account):
    _grant(db, account, 100, key="g1")
    credits.credit(db, account.id, 40, operation=LedgerOp.purchase, idempotency_key="p1")

    job = new_job_id()
    res = credits.reserve(db, account.id, 50, reference_type="simulation_job", reference_id=job)
    credits.settle(db, res, 12)

    job2 = new_job_id()
    res2 = credits.reserve(db, account.id, 30, reference_type="simulation_job", reference_id=job2)
    credits.release(db, res2)

    assert credits.reconcile(db) == []

    ledger_sum = sum(
        t.amount for t in db.query(CreditTransaction).all()
    )
    assert _balance(db, account) == Decimal(ledger_sum)
    assert _balance(db, account) == Decimal("128.000000")  # 140 - 12


def test_balance_after_is_a_faithful_running_total(db, account):
    _grant(db, account, 100, key="g1")
    credits.credit(db, account.id, 25, operation=LedgerOp.purchase, idempotency_key="p1")

    rows = db.query(CreditTransaction).order_by(CreditTransaction.created_at).all()
    running = Decimal("0")
    for row in rows:
        running += row.amount
        assert row.balance_after == running


# --- idempotency ----------------------------------------------------------

def test_replayed_grant_credits_once(db, account):
    first = _grant(db, account, 50, key="webhook-evt-abc")
    second = _grant(db, account, 50, key="webhook-evt-abc")

    assert first.id == second.id
    assert _balance(db, account) == Decimal("50.000000")
    assert db.query(CreditTransaction).count() == 1


def test_webhook_replayed_three_times_credits_once(db, account):
    for _ in range(3):
        credits.credit(
            db, account.id, 200,
            operation=LedgerOp.purchase,
            idempotency_key="stripe:evt_1MqM2eLkd",
        )
    assert _balance(db, account) == Decimal("200.000000")


def test_resubmitting_the_same_job_reuses_the_reservation(db, account):
    _grant(db, account, 100)
    job = new_job_id()

    a = credits.reserve(db, account.id, 30, reference_type="simulation_job", reference_id=job)
    b = credits.reserve(db, account.id, 30, reference_type="simulation_job", reference_id=job)

    assert a.id == b.id
    assert _balance(db, account) == Decimal("70.000000")


# --- spending limits ------------------------------------------------------

def test_cannot_reserve_more_than_available(db, account):
    _grant(db, account, 10)
    with pytest.raises(InsufficientCredits):
        credits.reserve(
            db, account.id, 11, reference_type="simulation_job", reference_id=new_job_id()
        )
    assert _balance(db, account) == Decimal("10.000000")


def test_two_sequential_holds_cannot_exceed_the_balance(db, account):
    _grant(db, account, 100)
    credits.reserve(db, account.id, 60, reference_type="simulation_job", reference_id=new_job_id())

    with pytest.raises(InsufficientCredits):
        credits.reserve(
            db, account.id, 60, reference_type="simulation_job", reference_id=new_job_id()
        )


def test_customer_is_never_charged_above_the_quote(db, account):
    """The reserved amount is what the customer was shown. It is a ceiling."""
    _grant(db, account, 100)
    job = new_job_id()
    res = credits.reserve(db, account.id, 40, reference_type="simulation_job", reference_id=job)

    credits.settle(db, res, 999)  # solver reported far more than estimated

    assert _balance(db, account) == Decimal("60.000000")  # charged 40, not 999


# --- settlement -----------------------------------------------------------

def test_settle_charges_actual_and_returns_the_difference(db, account):
    _grant(db, account, 100)
    job = new_job_id()
    res = credits.reserve(db, account.id, 40, reference_type="simulation_job", reference_id=job)
    assert _balance(db, account) == Decimal("60.000000")

    credits.settle(db, res, Decimal("13.5"))

    assert res.status is ReservationStatus.settled
    assert _balance(db, account) == Decimal("86.500000")
    ops = [t.operation for t in db.query(CreditTransaction).all()]
    assert LedgerOp.release in ops and LedgerOp.consume in ops


def test_failed_job_refunds_the_whole_hold(db, account):
    _grant(db, account, 100)
    job = new_job_id()
    res = credits.reserve(db, account.id, 40, reference_type="simulation_job", reference_id=job)

    credits.release(db, res, reason="MESH_FAILED")

    assert res.status is ReservationStatus.released
    assert _balance(db, account) == Decimal("100.000000")


def test_zero_cost_settlement_charges_nothing(db, account):
    _grant(db, account, 100)
    res = credits.reserve(
        db, account.id, 25, reference_type="simulation_job", reference_id=new_job_id()
    )
    credits.settle(db, res, 0)
    assert _balance(db, account) == Decimal("100.000000")


def test_a_reservation_cannot_be_settled_twice(db, account):
    _grant(db, account, 100)
    res = credits.reserve(
        db, account.id, 40, reference_type="simulation_job", reference_id=new_job_id()
    )
    credits.settle(db, res, 10)

    with pytest.raises(ReservationAlreadyResolved):
        credits.settle(db, res, 10)
    assert _balance(db, account) == Decimal("90.000000")


def test_a_settled_job_cannot_be_reserved_again(db, account):
    _grant(db, account, 100)
    job = new_job_id()
    res = credits.reserve(db, account.id, 20, reference_type="simulation_job", reference_id=job)
    credits.settle(db, res, 20)

    with pytest.raises(DuplicateReservation):
        credits.reserve(db, account.id, 20, reference_type="simulation_job", reference_id=job)


# --- the reaper -----------------------------------------------------------

def test_expired_reservation_is_freed_by_the_reaper(db, account):
    """A worker killed mid-job must not freeze credits forever."""
    _grant(db, account, 100)
    res = credits.reserve(
        db, account.id, 40,
        reference_type="simulation_job", reference_id=new_job_id(),
        ttl_seconds=-1,  # already expired
    )
    assert _balance(db, account) == Decimal("60.000000")

    freed = credits.expire_stale_reservations(db)

    assert freed == [res.id]
    assert res.status is ReservationStatus.expired
    assert _balance(db, account) == Decimal("100.000000")
    assert credits.reconcile(db) == []


def test_reaper_leaves_live_reservations_alone(db, account):
    _grant(db, account, 100)
    credits.reserve(
        db, account.id, 40,
        reference_type="simulation_job", reference_id=new_job_id(),
        ttl_seconds=3600,
    )
    assert credits.expire_stale_reservations(db) == []
    assert _balance(db, account) == Decimal("60.000000")


# --- admin ----------------------------------------------------------------

def test_adjustment_requires_a_reason(db, account):
    from easyem.errors import LedgerViolation

    _grant(db, account, 100)
    with pytest.raises(LedgerViolation):
        credits.adjust(
            db, account.id, 10, reason="   ",
            actor_user_id=new_job_id(), idempotency_key="adj-1",
        )


def test_negative_adjustment_cannot_push_balance_below_zero(db, account):
    _grant(db, account, 10)
    with pytest.raises(InsufficientCredits):
        credits.adjust(
            db, account.id, -50, reason="clawback",
            actor_user_id=new_job_id(), idempotency_key="adj-2",
        )
    assert _balance(db, account) == Decimal("10.000000")


def test_reconcile_detects_a_tampered_balance(db, account):
    """If someone UPDATEs a balance directly, the nightly job must catch it."""
    _grant(db, account, 100)
    wallet = credits.get_or_create_wallet(db, account.id)
    wallet.balance = Decimal("999")
    db.flush()

    divergences = credits.reconcile(db)
    assert len(divergences) == 1
    assert divergences[0]["cached_balance"] == Decimal("999.000000")
    assert divergences[0]["ledger_sum"] == Decimal("100.000000")
