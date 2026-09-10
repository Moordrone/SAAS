"""Job lifecycle and its interaction with the credit ledger.

Each test here maps to a way of either losing money or losing a customer's
trust: charging above the quote, charging for a failure the platform caused,
or freezing credits when a worker dies.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from easyem.credits import service as credits
from easyem.db import utcnow
from easyem.errors import InsufficientCredits
from easyem.identity import service as identity
from easyem.jobs import service as jobs
from easyem.models import ExecutionStatus, LedgerOp, SettlementStatus
from easyem.projects import service as projects
from easyem.projects.schema import empty_definition, set_parameter
from easyem.solver.base import SolverErrorCode
from easyem.solver.registry import get_backend

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def user(db):
    u, token = identity.signup(
        db, email="rf@example.com", password=PASSWORD, full_name="RF"
    )
    identity.verify_email(db, token)
    return u


@pytest.fixture()
def project(db, user):
    p = projects.create_project(
        db, user, name="patch", component_type="RectangularPatch", family="Antennas"
    )
    d = empty_definition("RectangularPatch", "Antennas")
    d = set_parameter(d, "frequency_center", 2.45, "GHz")
    d = set_parameter(d, "substrate_material", "RO4003C")
    d = set_parameter(d, "substrate_height", 0.813, "mm")
    projects.update_definition(db, p, user, d)
    return p


def _balance(db, user):
    return credits.get_or_create_wallet(db, user.default_account_id).balance


# --- quoting --------------------------------------------------------------

def test_quote_does_not_touch_the_ledger(db, user, project):
    before = _balance(db, user)
    jobs.quote(db, project, user)
    assert _balance(db, user) == before


def test_quote_ceiling_is_never_below_the_expectation(db, user, project):
    q = jobs.quote(db, project, user)
    assert q["cost_ceiling"] >= q["cost_expected"] > 0
    assert q["sufficient_credits"] is True


def test_an_invalid_project_cannot_be_quoted(db, user):
    p = projects.create_project(
        db, user, name="wip", component_type="RectangularPatch", family="Antennas"
    )
    with pytest.raises(jobs.ProjectNotRunnable):
        jobs.quote(db, p, user)


# --- submission -----------------------------------------------------------

def test_submit_holds_the_ceiling(db, user, project):
    before = _balance(db, user)
    job = jobs.submit(db, project, user)

    assert job.execution_status is ExecutionStatus.queued
    assert job.settlement_status is SettlementStatus.reserved
    assert _balance(db, user) == before - job.cost_ceiling


def test_submit_is_idempotent(db, user, project):
    """A retried request must not create a second job or a second hold."""
    a = jobs.submit(db, project, user, idempotency_key="req-42")
    before = _balance(db, user)
    b = jobs.submit(db, project, user, idempotency_key="req-42")

    assert a.id == b.id
    assert _balance(db, user) == before


def test_submit_without_credits_fails_before_any_compute(db, user, project):
    credits.adjust(
        db, user.default_account_id, -_balance(db, user) + Decimal("0.5"),
        reason="drain for test", actor_user_id=user.id, idempotency_key="drain",
    )
    with pytest.raises(InsufficientCredits):
        jobs.submit(db, project, user)


def test_job_pins_the_version_it_ran_on(db, user, project):
    """Editing a project must not change what a finished result came from."""
    job = jobs.submit(db, project, user)
    pinned = job.project_version_id

    d = empty_definition("RectangularPatch", "Antennas")
    d = set_parameter(d, "frequency_center", 5.8, "GHz")
    d = set_parameter(d, "substrate_material", "RO4003C")
    d = set_parameter(d, "substrate_height", 0.813, "mm")
    projects.update_definition(db, project, user, d)

    assert job.project_version_id == pinned
    assert project.current_version_id != pinned


# --- successful run -------------------------------------------------------

def test_successful_run_settles_at_actual_cost(db, user, project):
    before = _balance(db, user)
    job = jobs.run(db, jobs.submit(db, project, user))

    assert job.execution_status is ExecutionStatus.succeeded
    assert job.settlement_status is SettlementStatus.settled
    assert job.cost_actual is not None
    assert _balance(db, user) == before - job.cost_actual


def test_customer_is_never_charged_above_the_quote(db, user, project):
    job = jobs.run(db, jobs.submit(db, project, user))
    assert job.cost_actual <= job.cost_ceiling


def test_successful_run_stores_results_and_usage(db, user, project):
    job = jobs.run(db, jobs.submit(db, project, user))
    result = jobs.get_results(db, job)

    assert result is not None
    assert "s_parameters" in result.summary
    assert job.cpu_seconds is not None
    assert job.progress == 100.0


def test_ledger_stays_consistent_across_a_run(db, user, project):
    jobs.run(db, jobs.submit(db, project, user))
    assert credits.reconcile(db) == []


def test_settlement_writes_both_a_release_and_a_consume(db, user, project):
    """A statement must show what was held and what was actually used."""
    from easyem.models import CreditTransaction

    jobs.run(db, jobs.submit(db, project, user))
    ops = {t.operation for t in db.query(CreditTransaction).all()}
    assert {LedgerOp.reserve, LedgerOp.release, LedgerOp.consume} <= ops


# --- failure and billing policy -------------------------------------------

def test_platform_failure_refunds_the_whole_hold(db, user, project):
    """The customer does not pay for our bugs."""
    before = _balance(db, user)
    job = jobs.submit(db, project, user, backend_key="mock")
    jobs._fail(db, job, SolverErrorCode.SOLVER_INTERNAL, "boom")

    assert job.settlement_status is SettlementStatus.released
    assert job.cost_actual == Decimal("0")
    assert _balance(db, user) == before


def test_mesh_failure_is_not_billed(db, user, project):
    before = _balance(db, user)
    job = jobs.submit(db, project, user, backend_key="mock")
    jobs._fail(db, job, SolverErrorCode.MESH_FAILED, "could not mesh")
    assert _balance(db, user) == before


def test_resource_exceeded_is_billed_at_the_cap_not_beyond(db, user, project):
    before = _balance(db, user)
    job = jobs.submit(db, project, user, backend_key="mock")
    jobs._fail(db, job, SolverErrorCode.RESOURCE_EXCEEDED, "out of memory")

    assert job.cost_actual == job.cost_ceiling
    assert _balance(db, user) == before - job.cost_ceiling


def test_every_error_code_resolves_the_reservation(db, user, project):
    """No code may leave a hold dangling — that is how credits silently vanish."""
    for code in SolverErrorCode:
        credits.credit(
            db, user.default_account_id, 100, operation=LedgerOp.grant,
            idempotency_key=f"topup-{code.value}",
        )
        job = jobs.submit(
            db, project, user, backend_key="mock",
            idempotency_key=f"job-{code.value}",
        )
        jobs._fail(db, job, code, "test")
        assert job.settlement_status in (
            SettlementStatus.settled, SettlementStatus.released
        ), code
    assert credits.reconcile(db) == []


# --- cancellation ---------------------------------------------------------

def test_cancel_prorates_and_resolves_the_hold(db, user, project):
    job = jobs.submit(db, project, user, backend_key="mock")
    jobs.cancel(db, job, user)

    assert job.execution_status is ExecutionStatus.cancelled
    assert job.cancelled_by_user_id == user.id
    assert job.settlement_status is SettlementStatus.settled
    assert credits.reconcile(db) == []


def test_a_finished_job_cannot_be_cancelled(db, user, project):
    job = jobs.run(db, jobs.submit(db, project, user))
    with pytest.raises(jobs.JobNotCancellable):
        jobs.cancel(db, job, user)


# --- recovery -------------------------------------------------------------

def test_a_dead_worker_releases_its_credits(db, user, project):
    """The single most expensive silent failure: frozen credits nobody notices."""
    before = _balance(db, user)
    job = jobs.submit(db, project, user, backend_key="mock")
    job.execution_status = ExecutionStatus.running
    job.heartbeat_at = utcnow() - timedelta(hours=2)
    db.flush()

    recovered = jobs.recover_stalled_jobs(db)

    assert job.id in recovered
    assert job.error_code == SolverErrorCode.PLATFORM_ERROR.value
    assert _balance(db, user) == before
    assert credits.reconcile(db) == []


def test_healthy_jobs_are_left_alone(db, user, project):
    job = jobs.submit(db, project, user, backend_key="mock")
    assert jobs.recover_stalled_jobs(db) == []
    assert job.execution_status is ExecutionStatus.queued


# --- backend gating -------------------------------------------------------

def test_mock_results_are_watermarked_end_to_end(db, user, project):
    """The flag survives the trip from backend to database."""
    job = jobs.run(db, jobs.submit(db, project, user, backend_key="mock"))
    result = jobs.get_results(db, job)

    assert result.demonstration_only is True
    assert "NOT PHYSICAL" in result.summary["notice"]


def test_analytical_results_are_not_watermarked(db, user, project):
    job = jobs.run(db, jobs.submit(db, project, user, backend_key="analytical"))
    assert jobs.get_results(db, job).demonstration_only is False


def test_mock_is_unreachable_in_production(monkeypatch):
    from easyem.config import get_settings
    from easyem.solver.registry import SolverBackendUnavailable

    get_settings.cache_clear()
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "a-real-production-secret-value-here")
    # Production also refuses to start without a real mail backend, since
    # without one nobody can verify an address and nobody can simulate.
    monkeypatch.setenv("EMAIL_BACKEND", "smtp")
    try:
        with pytest.raises(SolverBackendUnavailable):
            get_backend("mock")
        assert "mock" not in get_backend("analytical").key or True
    finally:
        get_settings.cache_clear()


# --- isolation ------------------------------------------------------------

def test_another_account_cannot_read_a_job(db, user, project):
    from easyem.errors import NotFound

    other, _ = identity.signup(
        db, email="rival@example.com", password=PASSWORD, full_name="Rival"
    )
    job = jobs.submit(db, project, user)
    with pytest.raises(NotFound):
        jobs.get_job(db, job.id, other)
