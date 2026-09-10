"""Simulation orchestration.

The loop the whole product turns on:

    estimate  ->  reserve(ceiling)  ->  run  ->  settle(actual)
                                             ->  release   (failed)
                                             ->  expire    (worker died)

Two rules are not negotiable:

  * The ceiling shown before pressing Run is the maximum the customer can be
    charged. A solver that overruns its own estimate is the platform's problem,
    not the customer's.
  * Whether a failed run is billed is decided by its `error_code` against a
    published table, not by whoever writes the next exception message.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from ..credits import service as credits
from ..db import utcnow
from ..errors import AppError, NotFound
from ..models import (
    CreditReservation,
    ExecutionStatus,
    JobEvent,
    Project,
    ProjectVersion,
    SettlementStatus,
    SimulationJob,
    SimulationResult,
    User,
)
from ..projects import service as projects
from ..solver.base import (
    BILLING_FOR_ERROR,
    BillingPolicy,
    SolverErrorCode,
    SolverJobRef,
    SolverState,
)
from ..solver.registry import get_backend

HEARTBEAT_GRACE = timedelta(minutes=5)


class ProjectNotRunnable(AppError):
    status, code, title = 422, "project_not_runnable", "Project is not runnable"


class JobNotCancellable(AppError):
    status, code, title = 409, "job_not_cancellable", "Job is already finished"


def _log(
    db: DbSession, job: SimulationJob, event_type: str, *,
    to_status: str | None = None, detail: str | None = None,
) -> None:
    db.add(
        JobEvent(
            job_id=job.id,
            event_type=event_type,
            from_status=str(job.execution_status),
            to_status=to_status,
            detail=detail,
            created_at=utcnow(),
        )
    )


# --------------------------------------------------------------------------- #
# quoting
# --------------------------------------------------------------------------- #

def quote(
    db: DbSession, project: Project, user: User, *, backend_key: str | None = None
) -> dict:
    """What this run will cost, before committing a single credit."""
    version = projects.get_current_version(db, project)
    if version is None or not version.is_valid:
        raise ProjectNotRunnable(
            "The project must validate before it can be simulated."
        )

    backend = get_backend(backend_key)
    if not backend.supports(version.definition):
        raise ProjectNotRunnable(
            f"The {backend.key} backend cannot solve this component."
        )

    estimate = backend.estimate(version.definition)
    wallet = credits.get_or_create_wallet(db, project.account_id)

    return {
        "backend": backend.key,
        "backend_version": backend.version,
        "cost_ceiling": float(estimate.cost_ceiling),
        "cost_expected": float(estimate.cost_expected),
        "estimated_seconds": estimate.estimated_seconds,
        "frequency_points": estimate.frequency_points,
        "cell_count": estimate.cell_count,
        "available_credits": float(wallet.balance),
        "sufficient_credits": wallet.balance >= Decimal(str(estimate.cost_ceiling)),
        "content_hash": version.content_hash,
        "demonstration_only": not backend.physical,
        "notes": estimate.notes,
    }


# --------------------------------------------------------------------------- #
# submission
# --------------------------------------------------------------------------- #

def submit(
    db: DbSession,
    project: Project,
    user: User,
    *,
    backend_key: str | None = None,
    idempotency_key: str | None = None,
) -> SimulationJob:
    version = projects.get_current_version(db, project)
    if version is None or not version.is_valid:
        raise ProjectNotRunnable(
            "The project must validate before it can be simulated."
        )

    key = idempotency_key or f"job:{project.id}:{version.id}:{uuid.uuid4()}"

    existing = db.scalar(
        select(SimulationJob).where(SimulationJob.idempotency_key == key)
    )
    if existing is not None:
        return existing  # replayed submission

    backend = get_backend(backend_key)
    if not backend.supports(version.definition):
        raise ProjectNotRunnable(
            f"The {backend.key} backend cannot solve this component."
        )

    estimate = backend.estimate(version.definition)
    job_id = uuid.uuid4()

    # Hold the ceiling, not the expectation. This raises InsufficientCredits
    # before any compute is spent, which is the right place to fail.
    reservation = credits.reserve(
        db,
        project.account_id,
        Decimal(str(estimate.cost_ceiling)),
        reference_type="simulation_job",
        reference_id=job_id,
    )

    job = SimulationJob(
        id=job_id,
        account_id=project.account_id,
        project_id=project.id,
        project_version_id=version.id,
        submitted_by_user_id=user.id,
        execution_status=ExecutionStatus.queued,
        settlement_status=SettlementStatus.reserved,
        backend=backend.key,
        backend_version=backend.version,
        content_hash=version.content_hash,
        idempotency_key=key,
        cost_ceiling=Decimal(str(estimate.cost_ceiling)),
        reservation_id=reservation.id,
        created_at=utcnow(),
        heartbeat_at=utcnow(),
    )
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        # Two concurrent requests carried the same key. Neither saw the
        # other's row because they are in separate transactions, so both got
        # past the check above and both inserted. The UNIQUE constraint
        # settles it — and the loser must receive the winner's job, not a 500.
        db.rollback()
        existing = db.scalar(
            select(SimulationJob).where(SimulationJob.idempotency_key == key)
        )
        if existing is not None:
            return existing
        raise

    _log(db, job, "submitted", to_status=str(ExecutionStatus.queued))
    db.flush()
    return job


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #

def run(db: DbSession, job: SimulationJob, *, worker_id: str = "inline") -> SimulationJob:
    """Drive a job to completion synchronously.

    Kept for tests and for backends that finish in milliseconds. Production
    goes through `jobs.worker`, which commits between phases: running a
    minutes-long solve inside one transaction holds a lock on the wallet for
    the duration and serialises every other run on the account.
    """
    if job.is_terminal:
        return job

    backend = get_backend(job.backend)
    version = db.get(ProjectVersion, job.project_version_id)

    job.execution_status = ExecutionStatus.running
    job.started_at = utcnow()
    job.worker_id = worker_id
    job.heartbeat_at = utcnow()
    job.attempt += 1
    _log(db, job, "started", to_status=str(ExecutionStatus.running))
    db.flush()

    ref = backend.submit(version.definition, job.id)
    job.external_job_id = ref.external_id

    status = backend.status(ref)
    polls = 0
    while status.state in (SolverState.queued, SolverState.running) and polls < 200:
        job.progress = status.progress
        job.heartbeat_at = utcnow()
        status = backend.status(ref)
        polls += 1

    if status.state is SolverState.succeeded:
        return _complete(db, job, backend, ref, status)
    if status.state is SolverState.cancelled:
        return _fail(db, job, SolverErrorCode.USER_CANCELLED, "Cancelled")
    return _fail(
        db, job,
        status.error_code or SolverErrorCode.SOLVER_INTERNAL,
        status.error_message or "Solver reported failure",
    )


def _complete(db, job, backend, ref: SolverJobRef, status) -> SimulationJob:
    results = backend.fetch_results(ref)
    usage = results.usage or status.usage

    job.execution_status = ExecutionStatus.succeeded
    job.progress = 100.0
    job.finished_at = utcnow()
    if usage is not None:
        job.cpu_seconds = usage.cpu_seconds
        job.peak_memory_mb = usage.peak_memory_mb
        job.cell_count = usage.cell_count
        job.backend_version = usage.solver_version

    db.add(
        SimulationResult(
            job_id=job.id,
            summary=results.as_dict(),
            artifacts=results.artifacts or None,
            demonstration_only=results.demonstration_only,
            created_at=utcnow(),
        )
    )

    actual = _actual_cost(job, usage)
    _settle(db, job, actual)
    _log(db, job, "succeeded", to_status=str(ExecutionStatus.succeeded))
    db.flush()
    return job


def _fail(db, job, code: SolverErrorCode, message: str) -> SimulationJob:
    job.execution_status = (
        ExecutionStatus.cancelled
        if code is SolverErrorCode.USER_CANCELLED
        else ExecutionStatus.timed_out
        if code is SolverErrorCode.TIMEOUT
        else ExecutionStatus.failed
    )
    job.finished_at = utcnow()
    job.error_code = code.value
    job.error_message = message[:1000]

    policy = BILLING_FOR_ERROR.get(code, BillingPolicy.none)
    if policy is BillingPolicy.none:
        _release(db, job, reason=code.value)
    elif policy is BillingPolicy.capped:
        _settle(db, job, job.cost_ceiling)
    elif policy is BillingPolicy.prorated:
        _settle(db, job, _prorated_cost(job))
    else:  # actual
        _settle(db, job, _actual_cost(job, None))

    _log(db, job, "failed", to_status=str(job.execution_status), detail=code.value)
    db.flush()
    return job


def _actual_cost(job: SimulationJob, usage) -> Decimal:
    """Charge measured consumption, never above the quoted ceiling.

    The ceiling is a promise made before the customer pressed Run; a solver that
    exceeded its own estimate does not get to renegotiate it afterwards.
    """
    if usage is None or usage.cpu_seconds is None:
        return job.cost_ceiling
    cost = Decimal(str(max(0.1, usage.cpu_seconds * 0.5)))
    return min(cost, job.cost_ceiling)


def _prorated_cost(job: SimulationJob) -> Decimal:
    if not job.cpu_seconds:
        return Decimal("0")
    return min(Decimal(str(job.cpu_seconds * 0.5)), job.cost_ceiling)


def _settle(db: DbSession, job: SimulationJob, actual: Decimal) -> None:
    reservation = db.get(CreditReservation, job.reservation_id)
    if reservation is None:
        return
    credits.settle(db, reservation, actual)
    job.cost_actual = min(actual, job.cost_ceiling)
    job.settlement_status = SettlementStatus.settled


def _release(db: DbSession, job: SimulationJob, *, reason: str) -> None:
    reservation = db.get(CreditReservation, job.reservation_id)
    if reservation is None:
        return
    credits.release(db, reservation, reason=reason)
    job.cost_actual = Decimal("0")
    job.settlement_status = SettlementStatus.released


# --------------------------------------------------------------------------- #
# cancellation and recovery
# --------------------------------------------------------------------------- #

def cancel(db: DbSession, job: SimulationJob, user: User) -> SimulationJob:
    if job.is_terminal:
        raise JobNotCancellable(f"Job is already {job.execution_status}")

    backend = get_backend(job.backend)
    if job.external_job_id:
        backend.cancel(SolverJobRef(job.backend, job.external_job_id))

    job.cancelled_at = utcnow()
    job.cancelled_by_user_id = user.id
    return _fail(db, job, SolverErrorCode.USER_CANCELLED, "Cancelled by user")


def recover_stalled_jobs(db: DbSession, *, limit: int = 100) -> list[uuid.UUID]:
    """A worker killed mid-job leaves a job RUNNING and credits frozen.

    Without this the customer's usable balance quietly shrinks and they never
    find out why. Run it on a schedule alongside the credit reaper.
    """
    cutoff = utcnow() - HEARTBEAT_GRACE
    stalled = db.scalars(
        select(SimulationJob)
        .where(
            SimulationJob.execution_status.in_(
                [ExecutionStatus.queued, ExecutionStatus.running]
            ),
            SimulationJob.heartbeat_at < cutoff,
        )
        .limit(limit)
    ).all()

    recovered = []
    for job in stalled:
        _fail(
            db, job, SolverErrorCode.PLATFORM_ERROR,
            "Worker stopped reporting; job abandoned and credits released.",
        )
        recovered.append(job.id)
    return recovered


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #

def get_job(db: DbSession, job_id: uuid.UUID, user: User) -> SimulationJob:
    job = db.get(SimulationJob, job_id)
    if job is None:
        raise NotFound("Job not found")
    project = db.get(Project, job.project_id)
    projects.get_project(db, project.id, user)  # raises NotFound without access
    return job


def get_results(db: DbSession, job: SimulationJob) -> SimulationResult | None:
    return db.scalar(
        select(SimulationResult).where(SimulationResult.job_id == job.id)
    )


def list_jobs(
    db: DbSession, user: User, *, project_id: uuid.UUID | None = None, limit: int = 50
) -> list[SimulationJob]:
    stmt = (
        select(SimulationJob)
        .where(SimulationJob.account_id == user.default_account_id)
        .order_by(SimulationJob.created_at.desc())
        .limit(limit)
    )
    if project_id is not None:
        stmt = stmt.where(SimulationJob.project_id == project_id)
    return list(db.scalars(stmt).all())
