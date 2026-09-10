"""Project lifecycle: create, edit, version, duplicate, archive.

Versions are append-only. Editing a project writes a new version rather than
mutating the last one, so a simulation launched an hour ago still points at
exactly the inputs it ran on. A new version is only written when the physics
actually changed — repeated saves of an unchanged design would otherwise bury
the real history under noise.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ..db import utcnow
from ..engineering.validation import validate
from ..errors import Forbidden, NotFound
from ..models import Membership, Project, ProjectStatus, ProjectVersion, Role, User
from .schema import SCHEMA_VERSION, content_hash, empty_definition

_EDITOR_ROLES = (Role.owner, Role.admin, Role.member)


def _assert_access(
    db: DbSession, project: Project, user: User, *allowed: Role
) -> Membership:
    membership = db.scalar(
        select(Membership).where(
            Membership.account_id == project.account_id,
            Membership.user_id == user.id,
        )
    )
    # A project the caller cannot see must 404, not 403: a 403 confirms the id
    # exists, which is itself a small leak.
    if membership is None:
        raise NotFound("Project not found")
    if allowed and membership.role not in allowed:
        raise Forbidden(f"Requires one of: {', '.join(r.value for r in allowed)}")
    return membership


def get_project(db: DbSession, project_id: uuid.UUID, user: User) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.deleted_at is not None:
        raise NotFound("Project not found")
    _assert_access(db, project, user)
    return project


def list_projects(
    db: DbSession,
    user: User,
    *,
    account_id: uuid.UUID | None = None,
    include_archived: bool = False,
    limit: int = 50,
) -> list[Project]:
    account_id = account_id or user.default_account_id
    stmt = (
        select(Project)
        .where(Project.account_id == account_id, Project.deleted_at.is_(None))
        .order_by(Project.updated_at.desc())
        .limit(limit)
    )
    if not include_archived:
        stmt = stmt.where(Project.status != ProjectStatus.archived)
    return list(db.scalars(stmt).all())


def create_project(
    db: DbSession,
    user: User,
    *,
    name: str,
    component_type: str,
    family: str,
    description: str | None = None,
    account_id: uuid.UUID | None = None,
    definition: dict | None = None,
) -> Project:
    account_id = account_id or user.default_account_id

    membership = db.scalar(
        select(Membership).where(
            Membership.account_id == account_id, Membership.user_id == user.id
        )
    )
    if membership is None or membership.role not in _EDITOR_ROLES:
        raise Forbidden("Cannot create a project in this account")

    project = Project(
        account_id=account_id,
        created_by_user_id=user.id,
        name=name.strip(),
        description=description,
        status=ProjectStatus.draft,
    )
    db.add(project)
    db.flush()

    _write_version(
        db,
        project,
        definition or empty_definition(component_type, family),
        user_id=user.id,
        change_summary="Project created",
    )
    return project


def update_definition(
    db: DbSession,
    project: Project,
    user: User,
    definition: dict,
    *,
    change_summary: str | None = None,
) -> ProjectVersion:
    """Write a new version if the physics changed; otherwise return the current one."""
    _assert_access(db, project, user, *_EDITOR_ROLES)

    current = get_current_version(db, project)
    if current is not None and current.content_hash == content_hash(definition):
        return current  # nothing physical changed

    version = _write_version(
        db, project, definition, user_id=user.id, change_summary=change_summary
    )
    if project.status is ProjectStatus.draft and version.is_valid:
        project.status = ProjectStatus.active
    project.updated_at = utcnow()
    db.flush()
    return version


def _write_version(
    db: DbSession,
    project: Project,
    definition: dict,
    *,
    user_id: uuid.UUID,
    change_summary: str | None,
) -> ProjectVersion:
    next_number = (
        db.scalar(
            select(func.coalesce(func.max(ProjectVersion.version_number), 0)).where(
                ProjectVersion.project_id == project.id
            )
        )
        or 0
    ) + 1

    report = validate(definition)
    version = ProjectVersion(
        project_id=project.id,
        version_number=next_number,
        definition=definition,
        schema_version=definition.get("schema_version", SCHEMA_VERSION),
        content_hash=content_hash(definition),
        is_valid=report.is_valid,
        validation_report=report.as_dict(),
        change_summary=change_summary,
        created_by_user_id=user_id,
        created_at=utcnow(),
    )
    db.add(version)
    db.flush()

    project.current_version_id = version.id
    db.flush()
    return version


def get_current_version(db: DbSession, project: Project) -> ProjectVersion | None:
    if project.current_version_id is None:
        return None
    return db.get(ProjectVersion, project.current_version_id)


def list_versions(db: DbSession, project: Project) -> list[ProjectVersion]:
    return list(
        db.scalars(
            select(ProjectVersion)
            .where(ProjectVersion.project_id == project.id)
            .order_by(ProjectVersion.version_number.desc())
        ).all()
    )


def revert_to_version(
    db: DbSession, project: Project, user: User, version_number: int
) -> ProjectVersion:
    """Reverting writes a *new* version holding the old content.

    History is never rewritten: the record shows that a revert happened, which
    is more useful than a history that pretends the mistake never occurred.
    """
    _assert_access(db, project, user, *_EDITOR_ROLES)

    target = db.scalar(
        select(ProjectVersion).where(
            ProjectVersion.project_id == project.id,
            ProjectVersion.version_number == version_number,
        )
    )
    if target is None:
        raise NotFound(f"Version {version_number} not found")

    return _write_version(
        db,
        project,
        target.definition,
        user_id=user.id,
        change_summary=f"Reverted to version {version_number}",
    )


def duplicate_project(
    db: DbSession, project: Project, user: User, *, name: str | None = None
) -> Project:
    _assert_access(db, project, user)
    current = get_current_version(db, project)

    return create_project(
        db,
        user,
        name=name or f"{project.name} (copy)",
        component_type=(current.definition.get("component") or {}).get("type", ""),
        family=(current.definition.get("component") or {}).get("family", ""),
        description=project.description,
        account_id=project.account_id,
        definition=current.definition if current else None,
    )


def archive_project(db: DbSession, project: Project, user: User) -> Project:
    _assert_access(db, project, user, Role.owner, Role.admin, Role.member)
    project.status = ProjectStatus.archived
    project.archived_at = utcnow()
    db.flush()
    return project


def delete_project(db: DbSession, project: Project, user: User) -> Project:
    """Soft delete. Invoices and audit entries must not be orphaned."""
    _assert_access(db, project, user, Role.owner, Role.admin)
    project.status = ProjectStatus.deleted
    project.deleted_at = utcnow()
    db.flush()
    return project
