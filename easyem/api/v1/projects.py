"""Project and engineering-catalogue endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DbSession

from ...engineering import components as catalog
from ...engineering import materials as material_lib
from ...engineering.errors import EngineeringError
from ...engineering.validation import estimate, validate
from ...errors import AppError
from ...models import Project, User
from ...projects import service as projects
from ..deps import current_user, get_db

router = APIRouter(tags=["projects"])


class EngineeringErrorResponse(AppError):
    status, code, title = 400, "engineering_error", "Engineering error"


# --- schemas --------------------------------------------------------------

class ProjectCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    component_type: str
    family: str = ""
    description: str | None = None


class DefinitionIn(BaseModel):
    definition: dict
    change_summary: str | None = Field(default=None, max_length=500)


class ProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    status: str
    account_id: uuid.UUID
    current_version_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, p: Project) -> ProjectOut:
        return cls(
            id=p.id, name=p.name, description=p.description, status=str(p.status),
            account_id=p.account_id, current_version_id=p.current_version_id,
            created_at=p.created_at, updated_at=p.updated_at,
        )


class VersionOut(BaseModel):
    id: uuid.UUID
    version_number: int
    schema_version: str
    content_hash: str
    is_valid: bool
    definition: dict
    validation_report: dict | None
    change_summary: str | None
    created_at: datetime


def _version_out(v) -> VersionOut:
    return VersionOut(
        id=v.id, version_number=v.version_number, schema_version=v.schema_version,
        content_hash=v.content_hash, is_valid=v.is_valid, definition=v.definition,
        validation_report=v.validation_report, change_summary=v.change_summary,
        created_at=v.created_at,
    )


# --- catalogue ------------------------------------------------------------

@router.get("/catalog/components")
def list_components() -> dict:
    return {
        "families": catalog.families(),
        "components": [c.as_dict() for c in catalog.list_components()],
    }


@router.get("/catalog/components/{key}")
def get_component(key: str) -> dict:
    try:
        return catalog.get_component(key).as_dict()
    except EngineeringError as exc:
        raise EngineeringErrorResponse(str(exc)) from exc


@router.get("/catalog/materials")
def list_materials() -> dict:
    return {
        "substrates": [
            {
                "key": s.key, "name": s.name, "epsilon_r": s.epsilon_r,
                "loss_tangent": s.loss_tangent,
                "reference_frequency_hz": s.reference_frequency_hz,
                "manufacturer": s.manufacturer,
                "standard_thicknesses_m": list(s.standard_thicknesses_m),
                "notes": s.notes,
            }
            for s in material_lib.list_substrates()
        ],
        "conductors": [
            {"key": c.key, "name": c.name, "conductivity_s_per_m": c.conductivity_s_per_m}
            for c in material_lib.CONDUCTORS.values()
        ],
    }


# --- projects -------------------------------------------------------------

@router.get("/projects", response_model=list[ProjectOut])
def list_projects(
    include_archived: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> list[ProjectOut]:
    rows = projects.list_projects(
        db, user, include_archived=include_archived, limit=limit
    )
    return [ProjectOut.of(p) for p in rows]


@router.post("/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreateIn,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> ProjectOut:
    try:
        catalog.get_component(payload.component_type)
    except EngineeringError as exc:
        raise EngineeringErrorResponse(str(exc)) from exc

    project = projects.create_project(
        db, user,
        name=payload.name,
        component_type=payload.component_type,
        family=payload.family or catalog.get_component(payload.component_type).family,
        description=payload.description,
    )
    return ProjectOut.of(project)


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: uuid.UUID,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> ProjectOut:
    return ProjectOut.of(projects.get_project(db, project_id, user))


@router.get("/projects/{project_id}/versions", response_model=list[VersionOut])
def list_versions(
    project_id: uuid.UUID,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> list[VersionOut]:
    project = projects.get_project(db, project_id, user)
    return [_version_out(v) for v in projects.list_versions(db, project)]


@router.put("/projects/{project_id}/definition", response_model=VersionOut)
def update_definition(
    project_id: uuid.UUID,
    payload: DefinitionIn,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> VersionOut:
    project = projects.get_project(db, project_id, user)
    version = projects.update_definition(
        db, project, user, payload.definition, change_summary=payload.change_summary
    )
    return _version_out(version)


@router.post("/projects/{project_id}/duplicate", response_model=ProjectOut, status_code=201)
def duplicate_project(
    project_id: uuid.UUID,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> ProjectOut:
    project = projects.get_project(db, project_id, user)
    return ProjectOut.of(projects.duplicate_project(db, project, user))


@router.post("/projects/{project_id}/versions/{number}/revert", response_model=VersionOut)
def revert(
    project_id: uuid.UUID,
    number: int,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> VersionOut:
    project = projects.get_project(db, project_id, user)
    return _version_out(projects.revert_to_version(db, project, user, number))


@router.delete("/projects/{project_id}", response_model=ProjectOut)
def delete_project(
    project_id: uuid.UUID,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> ProjectOut:
    project = projects.get_project(db, project_id, user)
    return ProjectOut.of(projects.delete_project(db, project, user))


# --- engineering ----------------------------------------------------------

@router.post("/projects/{project_id}/validate")
def validate_project(
    project_id: uuid.UUID,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    project = projects.get_project(db, project_id, user)
    version = projects.get_current_version(db, project)
    if version is None:
        return {"valid": False, "missing": [], "issues": []}
    return validate(version.definition).as_dict()


@router.post("/projects/{project_id}/estimate")
def estimate_project(
    project_id: uuid.UUID,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    """Analytical estimate. Milliseconds, real physics, stated validity limits.

    This is not a stand-in for a full-wave solve. It is the answer a customer
    gets before deciding whether the full solve is worth the credits.
    """
    project = projects.get_project(db, project_id, user)
    version = projects.get_current_version(db, project)
    if version is None:
        raise EngineeringErrorResponse("Project has no definition yet")
    try:
        return estimate(version.definition)
    except EngineeringError as exc:
        raise EngineeringErrorResponse(str(exc)) from exc
