"""Simulation endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from ...jobs import service as jobs
from ...models import SimulationJob, User
from ...projects import service as projects
from ...solver.registry import available
from ..deps import current_user, get_db, verified_user

router = APIRouter(tags=["simulations"])


class SubmitIn(BaseModel):
    backend: str | None = None


class JobOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    project_version_id: uuid.UUID
    execution_status: str
    settlement_status: str
    backend: str
    progress: float
    cost_ceiling: float
    cost_actual: float | None
    error_code: str | None
    error_message: str | None
    cpu_seconds: float | None
    content_hash: str
    created_at: datetime
    finished_at: datetime | None

    @classmethod
    def of(cls, j: SimulationJob) -> JobOut:
        return cls(
            id=j.id, project_id=j.project_id,
            project_version_id=j.project_version_id,
            execution_status=str(j.execution_status),
            settlement_status=str(j.settlement_status),
            backend=j.backend, progress=j.progress,
            cost_ceiling=float(j.cost_ceiling),
            cost_actual=float(j.cost_actual) if j.cost_actual is not None else None,
            error_code=j.error_code, error_message=j.error_message,
            cpu_seconds=j.cpu_seconds, content_hash=j.content_hash,
            created_at=j.created_at, finished_at=j.finished_at,
        )


@router.get("/solver/backends")
def solver_backends() -> dict:
    return {"backends": available(), "default": "analytical"}


@router.post("/projects/{project_id}/simulations/quote")
def quote(
    project_id: uuid.UUID,
    payload: SubmitIn | None = None,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    """The price before committing. This number is a ceiling, not an estimate."""
    project = projects.get_project(db, project_id, user)
    return jobs.quote(db, project, user, backend_key=(payload.backend if payload else None))


@router.post(
    "/projects/{project_id}/simulations",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
)
def submit(
    project_id: uuid.UUID,
    payload: SubmitIn | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(verified_user),
    db: DbSession = Depends(get_db),
) -> JobOut:
    """Reserve credits and queue a run. Returns immediately.

    Poll `GET /v1/simulations/{id}` for progress. Requires a verified email:
    creating an account is free, spending is not.
    """
    project = projects.get_project(db, project_id, user)
    job = jobs.submit(
        db, project, user,
        backend_key=(payload.backend if payload else None),
        idempotency_key=idempotency_key,
    )
    # Returns as soon as the credits are held and the job is queued. A worker
    # picks it up. Running the solve here would hold this request's
    # transaction — and its lock on the wallet — for the whole simulation,
    # serialising every other run on the account behind it.
    return JobOut.of(job)


@router.get("/simulations", response_model=list[JobOut])
def list_jobs(
    project_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> list[JobOut]:
    return [
        JobOut.of(j)
        for j in jobs.list_jobs(db, user, project_id=project_id, limit=limit)
    ]


@router.get("/simulations/{job_id}", response_model=JobOut)
def get_job(
    job_id: uuid.UUID,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> JobOut:
    return JobOut.of(jobs.get_job(db, job_id, user))


@router.get("/simulations/{job_id}/results")
def get_results(
    job_id: uuid.UUID,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    job = jobs.get_job(db, job_id, user)
    result = jobs.get_results(db, job)
    if result is None:
        return {
            "available": False,
            "execution_status": str(job.execution_status),
            "error_code": job.error_code,
        }
    return {
        "available": True,
        "job_id": str(job.id),
        "backend": job.backend,
        "backend_version": job.backend_version,
        "content_hash": job.content_hash,
        "demonstration_only": result.demonstration_only,
        "results": result.summary,
    }


@router.post("/simulations/{job_id}/cancel", response_model=JobOut)
def cancel(
    job_id: uuid.UUID,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> JobOut:
    job = jobs.get_job(db, job_id, user)
    return JobOut.of(jobs.cancel(db, job, user))
