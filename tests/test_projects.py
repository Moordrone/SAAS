"""Project versioning, canonical hashing and access isolation."""

import pytest

from easyem.errors import NotFound
from easyem.identity import service as identity
from easyem.projects import service as projects
from easyem.projects.schema import (
    canonicalise,
    content_hash,
    empty_definition,
    set_parameter,
)

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def user(db):
    u, _ = identity.signup(
        db, email="eng@example.com", password=PASSWORD, full_name="Eng"
    )
    return u


@pytest.fixture()
def other_user(db):
    u, _ = identity.signup(
        db, email="rival@example.com", password=PASSWORD, full_name="Rival"
    )
    return u


def _definition(freq_value=2.45, freq_unit="GHz"):
    d = empty_definition("RectangularPatch", "Antennas")
    d = set_parameter(d, "frequency_center", freq_value, freq_unit)
    d = set_parameter(d, "substrate_material", "RO4003C")
    d = set_parameter(d, "substrate_height", 0.813, "mm")
    return d


# --- canonical hashing ----------------------------------------------------

def test_same_physics_in_different_units_hashes_the_same():
    """2.45 GHz and 2450 MHz are the same design. The hash must agree."""
    assert content_hash(_definition(2.45, "GHz")) == content_hash(
        _definition(2450, "MHz")
    )


def test_key_order_does_not_change_the_hash():
    a = _definition()
    b = {k: a[k] for k in reversed(list(a))}
    assert content_hash(a) == content_hash(b)


def test_provenance_does_not_change_the_hash():
    """A length is the same length whether a human or a model proposed it."""
    a = _definition()
    b = set_parameter(a, "frequency_center", 2.45, "GHz", provenance="ai_suggested")
    assert content_hash(a) == content_hash(b)


def test_different_physics_hashes_differently():
    assert content_hash(_definition(2.45)) != content_hash(_definition(5.8))


def test_canonical_form_is_stored_in_si():
    canon = canonicalise(_definition(2.45, "GHz"))
    assert canon["parameters"]["frequency_center"] == {"value": 2.45e9, "unit": "Hz"}


def test_hash_is_prefixed_with_its_algorithm():
    assert content_hash(_definition()).startswith("sha256:")


# --- lifecycle ------------------------------------------------------------

def test_create_project_writes_version_one(db, user):
    p = projects.create_project(
        db, user, name="2.4 GHz patch", component_type="RectangularPatch",
        family="Antennas",
    )
    versions = projects.list_versions(db, p)
    assert len(versions) == 1
    assert versions[0].version_number == 1
    assert p.current_version_id == versions[0].id


def test_editing_writes_a_new_version(db, user):
    p = projects.create_project(
        db, user, name="patch", component_type="RectangularPatch", family="Antennas"
    )
    projects.update_definition(db, p, user, _definition(), change_summary="Set 2.45 GHz")
    projects.update_definition(db, p, user, _definition(5.8), change_summary="Retune")

    versions = projects.list_versions(db, p)
    assert [v.version_number for v in versions] == [3, 2, 1]


def test_saving_an_unchanged_design_does_not_create_a_version(db, user):
    """Repeated saves of identical physics would bury the real history."""
    p = projects.create_project(
        db, user, name="patch", component_type="RectangularPatch", family="Antennas"
    )
    v = projects.update_definition(db, p, user, _definition())
    again = projects.update_definition(db, p, user, _definition())

    assert again.id == v.id
    assert len(projects.list_versions(db, p)) == 2


def test_same_design_in_other_units_is_not_a_new_version(db, user):
    p = projects.create_project(
        db, user, name="patch", component_type="RectangularPatch", family="Antennas"
    )
    v = projects.update_definition(db, p, user, _definition(2.45, "GHz"))
    same = projects.update_definition(db, p, user, _definition(2450, "MHz"))
    assert same.id == v.id


def test_versions_record_their_validation_state(db, user):
    p = projects.create_project(
        db, user, name="patch", component_type="RectangularPatch", family="Antennas"
    )
    first = projects.list_versions(db, p)[0]
    assert first.is_valid is False           # nothing filled in yet
    assert first.validation_report["missing"]

    complete = projects.update_definition(db, p, user, _definition())
    assert complete.is_valid is True


def test_project_becomes_active_once_it_validates(db, user):
    p = projects.create_project(
        db, user, name="patch", component_type="RectangularPatch", family="Antennas"
    )
    assert str(p.status) == "draft"
    projects.update_definition(db, p, user, _definition())
    assert str(p.status) == "active"


def test_revert_writes_a_new_version_rather_than_rewriting_history(db, user):
    p = projects.create_project(
        db, user, name="patch", component_type="RectangularPatch", family="Antennas"
    )
    projects.update_definition(db, p, user, _definition(2.45))
    projects.update_definition(db, p, user, _definition(5.8))

    reverted = projects.revert_to_version(db, p, user, 2)

    assert reverted.version_number == 4
    assert reverted.content_hash == content_hash(_definition(2.45))
    assert len(projects.list_versions(db, p)) == 4  # nothing was deleted


def test_duplicate_copies_the_current_definition(db, user):
    p = projects.create_project(
        db, user, name="patch", component_type="RectangularPatch", family="Antennas"
    )
    projects.update_definition(db, p, user, _definition())
    copy = projects.duplicate_project(db, p, user)

    assert copy.id != p.id
    assert copy.name == "patch (copy)"
    original = projects.get_current_version(db, p)
    duplicated = projects.get_current_version(db, copy)
    assert duplicated.content_hash == original.content_hash


def test_deleted_project_disappears_from_listings(db, user):
    p = projects.create_project(
        db, user, name="patch", component_type="RectangularPatch", family="Antennas"
    )
    projects.delete_project(db, p, user)
    assert projects.list_projects(db, user) == []
    with pytest.raises(NotFound):
        projects.get_project(db, p.id, user)


def test_archived_project_is_hidden_but_retrievable(db, user):
    p = projects.create_project(
        db, user, name="patch", component_type="RectangularPatch", family="Antennas"
    )
    projects.archive_project(db, p, user)
    assert projects.list_projects(db, user) == []
    assert len(projects.list_projects(db, user, include_archived=True)) == 1


# --- isolation ------------------------------------------------------------

def test_one_account_cannot_read_another_accounts_project(db, user, other_user):
    """The single most important authorisation test in the product."""
    p = projects.create_project(
        db, user, name="secret design", component_type="RectangularPatch",
        family="Antennas",
    )
    with pytest.raises(NotFound):
        projects.get_project(db, p.id, other_user)


def test_missing_access_reports_not_found_not_forbidden(db, user, other_user):
    """403 would confirm the id exists. 404 tells them nothing."""
    p = projects.create_project(
        db, user, name="secret", component_type="RectangularPatch", family="Antennas"
    )
    with pytest.raises(NotFound):
        projects.update_definition(db, p, other_user, _definition())


def test_listing_is_scoped_to_the_callers_account(db, user, other_user):
    projects.create_project(
        db, user, name="mine", component_type="RectangularPatch", family="Antennas"
    )
    assert projects.list_projects(db, other_user) == []
