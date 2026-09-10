"""Simulation worker.

This is the fix for the worst defect in the audit: the whole simulation used to
run inside the HTTP request's transaction, holding a row lock on the wallet from
`reserve` until the response. Two runs on one account serialised completely —
167 seconds of blocking with openEMS — and the connection pool drained.

The correction is not "move `run` elsewhere". It is a different transaction
shape:

    HTTP request      reserve credits, queue the job, commit.  Milliseconds.
    Worker, claim     one short transaction, atomically.
    Worker, execute   no transaction held; progress committed per heartbeat.
    Worker, settle    one short transaction.

Nothing holds a lock while a solver runs. That is the whole point.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from ..db import SessionLocal, utcnow
from ..models import (
    ExecutionStatus,
    ProjectVersion,
    SimulationJob,
)
from ..solver.base import SolverErrorCode, SolverState
from ..solver.registry import get_backend
from . import service as jobs

logger = logging.getLogger("easyem.worker")

HEARTBEAT_INTERVAL = timedelta(seconds=20)
POLL_INTERVAL_SECONDS = 2.0
IDLE_SLEEP_SECONDS = 1.0


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


# --------------------------------------------------------------------------- #
# claiming
# --------------------------------------------------------------------------- #

def claim_next_job(db: DbSession, worker_id: str) -> SimulationJob | None:
    """Take one queued job, atomically, in its own short transaction.

    The guarded UPDATE is what makes this safe with several workers: whoever
    lands the write first flips the status, and everyone else gets rowcount 0
    and moves on. It works identically on PostgreSQL and SQLite, unlike
    FOR UPDATE SKIP LOCKED.

    Highest priority first — the Professional plan promises it — then oldest,
    so a low-priority job cannot starve behind a steady stream of urgent ones
    from the same account.
    """
    candidate = db.scalar(
        select(SimulationJob.id)
        .where(SimulationJob.execution_status == ExecutionStatus.queued)
        .order_by(
            SimulationJob.priority.desc(),
            SimulationJob.created_at.asc(),
        )
        .limit(1)
    )
    if candidate is None:
        return None

    now = utcnow()
    result = db.execute(
        update(SimulationJob)
        .where(
            SimulationJob.id == candidate,
            # The guard: another worker may have taken it since the SELECT.
            SimulationJob.execution_status == ExecutionStatus.queued,
        )
        .values(
            execution_status=ExecutionStatus.running,
            worker_id=worker_id,
            heartbeat_at=now,
            started_at=now,
            attempt=SimulationJob.attempt + 1,
        )
    )
    if (result.rowcount or 0) == 0:
        return None  # lost the race, fine

    db.commit()

    # The bulk UPDATE went round the ORM, so anything already in the identity
    # map still holds the pre-claim values. Without this refresh the worker
    # reads worker_id=None on the job it just claimed — and the reaper would
    # then see a running job with no owner.
    job = db.get(SimulationJob, candidate)
    if job is not None:
        db.refresh(job)
    return job


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #

def execute(db: DbSession, job: SimulationJob) -> SimulationJob:
    """Run a claimed job. Holds no transaction across the solve.

    Progress is committed on each heartbeat, so a stalled job is visible to the
    reaper and to the customer, and a worker that dies leaves a job whose last
    heartbeat says when it stopped.
    """
    backend = get_backend(job.backend)
    version = db.get(ProjectVersion, job.project_version_id)
    if version is None:
        return jobs._fail(
            db, job, SolverErrorCode.PLATFORM_ERROR, "Project version missing"
        )

    try:
        ref = backend.submit(version.definition, job.id)
    except Exception as exc:
        logger.exception("submit_failed", extra={"job_id": str(job.id)})
        return jobs._fail(db, job, SolverErrorCode.PLATFORM_ERROR, str(exc))

    job.external_job_id = ref.external_id
    db.commit()

    deadline = time.monotonic() + job.timeout_seconds
    last_beat = 0.0

    while True:
        try:
            status = backend.status(ref)
        except Exception as exc:
            logger.exception("status_failed", extra={"job_id": str(job.id)})
            return jobs._fail(db, job, SolverErrorCode.PLATFORM_ERROR, str(exc))

        if status.state not in (SolverState.queued, SolverState.running):
            break

        if time.monotonic() > deadline:
            backend.cancel(ref)
            return jobs._fail(
                db, job, SolverErrorCode.TIMEOUT,
                f"Exceeded {job.timeout_seconds}s",
            )

        now = time.monotonic()
        if now - last_beat > HEARTBEAT_INTERVAL.total_seconds():
            job.progress = status.progress
            job.heartbeat_at = utcnow()
            db.commit()          # short transaction, nothing held
            last_beat = now

        # A synchronous backend finishes inside submit(); polling it in a tight
        # loop would spin.
        if not backend.synchronous:
            time.sleep(POLL_INTERVAL_SECONDS)

    if status.state is SolverState.succeeded:
        job = jobs._complete(db, job, backend, ref, status)
    elif status.state is SolverState.cancelled:
        job = jobs._fail(db, job, SolverErrorCode.USER_CANCELLED, "Cancelled")
    else:
        job = jobs._fail(
            db, job,
            status.error_code or SolverErrorCode.SOLVER_INTERNAL,
            status.error_message or "Solver reported failure",
        )

    db.commit()
    _notify(db, job)
    return job


def _notify(db: DbSession, job: SimulationJob) -> None:
    """Tell the customer their run finished. Never let this break the job."""
    from ..config import get_settings
    from ..models import Project, User
    from ..notifications import send
    from ..notifications.templates import simulation_finished

    try:
        user = db.get(User, job.submitted_by_user_id)
        project = db.get(Project, job.project_id)
        if user is None or project is None or user.deleted_at:
            return
        send(
            simulation_finished(
                to=user.email,
                name=user.full_name or user.email,
                project=project.name,
                job_id=str(job.id),
                base_url=get_settings().app_base_url,
                charged=str(job.cost_actual or 0),
                succeeded=job.execution_status is ExecutionStatus.succeeded,
                error=job.error_message,
            )
        )
    except Exception:
        logger.exception("notify_failed", extra={"job_id": str(job.id)})


def process_one(worker_id: str | None = None) -> uuid.UUID | None:
    """Claim and run a single job. The unit the loop and the tests share."""
    worker_id = worker_id or worker_identity()
    db = SessionLocal()
    try:
        job = claim_next_job(db, worker_id)
        if job is None:
            return None
        logger.info(
            "job_claimed",
            extra={"job_id": str(job.id), "backend": job.backend,
                   "worker_id": worker_id},
        )
        execute(db, job)
        logger.info(
            "job_finished",
            extra={"job_id": str(job.id),
                   "status": str(job.execution_status),
                   "charged": str(job.cost_actual)},
        )
        return job.id
    except Exception:
        db.rollback()
        logger.exception("worker_error")
        return None
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# maintenance and loop
# --------------------------------------------------------------------------- #

def run_maintenance() -> dict:
    """Reap abandoned work. Both of these existed and nothing called them,
    which meant a killed worker froze a customer's credits indefinitely."""
    from ..api.ratelimit import limiter
    from ..credits import service as credits
    from ..identity import service as identity

    db = SessionLocal()
    try:
        stalled = jobs.recover_stalled_jobs(db)
        expired = credits.expire_stale_reservations(db)
        divergences = credits.reconcile(db)
        purged = identity.purge_expired_sessions(db)
        limiter.prune()
        db.commit()

        if divergences:
            # The ledger cache disagrees with its own history. This should
            # page someone: it means money is being counted wrong.
            logger.error("ledger_divergence", extra={"wallets": len(divergences)})
        if stalled or expired:
            logger.warning(
                "maintenance_recovered",
                extra={"stalled_jobs": len(stalled),
                       "expired_reservations": len(expired)},
            )
        return {
            "stalled_jobs": len(stalled),
            "expired_reservations": len(expired),
            "ledger_divergences": len(divergences),
            "purged_sessions": purged,
        }
    finally:
        db.close()


class Worker:
    def __init__(self, *, maintenance_every: int = 60) -> None:
        self.worker_id = worker_identity()
        self.running = True
        self.maintenance_every = maintenance_every
        self._last_maintenance = 0.0

    def stop(self, *_args) -> None:
        """Finish the job in hand, then exit. Killing mid-solve would leave a
        hold the reaper has to clean up minutes later."""
        logger.info("worker_stopping", extra={"worker_id": self.worker_id})
        self.running = False

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        logger.info("worker_started", extra={"worker_id": self.worker_id})

        while self.running:
            if time.monotonic() - self._last_maintenance > self.maintenance_every:
                run_maintenance()
                self._last_maintenance = time.monotonic()

            if process_one(self.worker_id) is None:
                time.sleep(IDLE_SLEEP_SECONDS)

        logger.info("worker_stopped", extra={"worker_id": self.worker_id})


def main() -> None:  # pragma: no cover - entrypoint
    from .. import logs
    from ..config import get_settings

    settings = get_settings()
    logs.configure(json_output=settings.environment not in ("dev", "test"))
    Worker().run()


if __name__ == "__main__":  # pragma: no cover
    main()
