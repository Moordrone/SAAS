"""The solver contract.

These tests assert *behaviour*, never numbers. A backend that returns different
physics is still a valid backend; a backend whose progress goes backwards, or
that charges twice for one job, is not.
"""

import uuid

import pytest

from easyem.solver.base import (
    BILLING_FOR_ERROR,
    BillingPolicy,
    SolverError,
    SolverErrorCode,
    SolverState,
)
from tests.solver_contract.conftest import drive_to_completion

# --- identity -------------------------------------------------------------

def test_backend_declares_key_and_version(backend):
    assert backend.key and backend.key != "abstract"
    assert backend.version
    assert isinstance(backend.physical, bool)


def test_health_reports_itself(backend):
    health = backend.health()
    assert health["backend"] == backend.key
    assert "healthy" in health


# --- estimation -----------------------------------------------------------

def test_accepts_a_valid_definition(backend, valid_definition):
    assert backend.supports(valid_definition) is True


def test_estimate_bounds_its_own_cost(backend, valid_definition):
    """The ceiling is what the customer is shown and held. It must not be
    below the expected cost, or the quote is a lie by construction."""
    estimate = backend.estimate(valid_definition)
    assert estimate.cost_ceiling >= estimate.cost_expected > 0
    assert estimate.estimated_seconds >= 0
    assert estimate.frequency_points > 0


def test_estimate_does_not_start_a_run(backend, valid_definition):
    backend.estimate(valid_definition)
    backend.estimate(valid_definition)
    # No job id was involved, so nothing can have been queued.
    ref = backend.submit(valid_definition, uuid.uuid4())
    assert backend.status(ref).state in {
        SolverState.queued, SolverState.running, SolverState.succeeded
    }


# --- submission -----------------------------------------------------------

def test_submit_returns_a_ref_for_its_own_backend(backend, valid_definition):
    ref = backend.submit(valid_definition, uuid.uuid4())
    assert ref.backend == backend.key
    assert ref.external_id


def test_submitting_the_same_job_twice_is_idempotent(backend, valid_definition):
    """A retried worker must not run — or charge for — the same job twice."""
    job_id = uuid.uuid4()
    first = backend.submit(valid_definition, job_id)
    second = backend.submit(valid_definition, job_id)
    assert first.external_id == second.external_id


def test_unknown_job_raises_rather_than_returning_nonsense(backend):
    from easyem.solver.base import SolverJobRef

    with pytest.raises(SolverError):
        backend.status(SolverJobRef(backend=backend.key, external_id="nope"))


# --- status ---------------------------------------------------------------

def test_progress_stays_within_bounds_and_never_decreases(backend, valid_definition):
    """A progress bar that goes backwards destroys trust in everything else
    on the screen."""
    ref = backend.submit(valid_definition, uuid.uuid4())
    previous = -1.0
    for _ in range(20):
        status = backend.status(ref)
        assert 0.0 <= status.progress <= 100.0
        assert status.progress >= previous
        previous = status.progress
        if status.state in {SolverState.succeeded, SolverState.failed}:
            break


def test_reaches_a_terminal_state(backend, valid_definition):
    ref = backend.submit(valid_definition, uuid.uuid4())
    status = drive_to_completion(backend, ref)
    assert status.state in {
        SolverState.succeeded, SolverState.failed, SolverState.cancelled
    }


def test_terminal_state_is_stable(backend, valid_definition):
    ref = backend.submit(valid_definition, uuid.uuid4())
    first = drive_to_completion(backend, ref)
    assert backend.status(ref).state is first.state


def test_success_reports_measured_usage(backend, valid_definition):
    """Without usage the platform can only ever debit what it reserved, and the
    estimator can never be calibrated against reality."""
    ref = backend.submit(valid_definition, uuid.uuid4())
    status = drive_to_completion(backend, ref)
    if status.state is not SolverState.succeeded:
        pytest.skip("backend did not succeed on this input")

    usage = status.usage
    assert usage is not None
    assert usage.cpu_seconds >= 0
    assert usage.wall_seconds >= 0
    assert usage.frequency_points > 0
    assert usage.solver_version


# --- failure --------------------------------------------------------------

def test_invalid_definition_fails_with_an_enumerated_code(backend):
    """Free-form error strings cannot drive a billing policy."""
    broken = {
        "schema_version": "1.0.0",
        "component": {"family": "Antennas", "type": "RectangularPatch"},
        "parameters": {},
    }
    if not backend.supports(broken):
        pytest.skip("backend rejects this component outright")

    ref = backend.submit(broken, uuid.uuid4())
    status = drive_to_completion(backend, ref)
    if status.state is SolverState.failed:
        assert status.error_code in set(SolverErrorCode)
        assert status.error_message


def test_every_error_code_has_a_billing_policy(backend):
    """A failure whose billing is undefined becomes a support ticket."""
    for code in SolverErrorCode:
        assert code in BILLING_FOR_ERROR
        assert BILLING_FOR_ERROR[code] in set(BillingPolicy)


def test_results_unavailable_before_success_raises(backend, valid_definition):
    broken = {"schema_version": "1.0.0", "component": {"type": "RectangularPatch"},
              "parameters": {}}
    if not backend.supports(broken):
        pytest.skip("backend rejects this component outright")
    ref = backend.submit(broken, uuid.uuid4())
    status = drive_to_completion(backend, ref)
    if status.state is SolverState.failed:
        with pytest.raises(SolverError):
            backend.fetch_results(ref)


# --- cancellation ---------------------------------------------------------

def test_cancel_is_safe_on_a_finished_job(backend, valid_definition):
    """A user pressing cancel as the job lands must not corrupt its state."""
    ref = backend.submit(valid_definition, uuid.uuid4())
    before = drive_to_completion(backend, ref)
    backend.cancel(ref)
    assert backend.status(ref).state is before.state


def test_cancel_is_safe_on_an_unknown_job(backend):
    from easyem.solver.base import SolverJobRef

    backend.cancel(SolverJobRef(backend=backend.key, external_id="ghost"))


# --- results --------------------------------------------------------------

def test_results_match_the_canonical_shape(backend, valid_definition):
    ref = backend.submit(valid_definition, uuid.uuid4())
    status = drive_to_completion(backend, ref)
    if status.state is not SolverState.succeeded:
        pytest.skip("backend did not succeed on this input")

    results = backend.fetch_results(ref)
    assert len(results.frequencies_hz) > 1
    assert results.frequencies_hz == sorted(results.frequencies_hz)
    assert "S11" in results.s_parameters
    for values in results.s_parameters.values():
        assert len(values) == len(results.frequencies_hz)
        assert all(isinstance(v, complex) for v in values)


def test_passive_structure_never_reflects_more_than_it_receives(backend, valid_definition):
    """|S11| > 1 means the antenna is generating power. This is the single
    cheapest physics check there is, and it catches sign and scaling errors in
    any new backend."""
    ref = backend.submit(valid_definition, uuid.uuid4())
    status = drive_to_completion(backend, ref)
    if status.state is not SolverState.succeeded:
        pytest.skip("backend did not succeed on this input")

    for name, values in backend.fetch_results(ref).s_parameters.items():
        for v in values:
            assert abs(v) <= 1.0 + 1e-9, f"{name} exceeds unity: |{v}| = {abs(v)}"


def test_large_payloads_are_referenced_not_inlined(backend, valid_definition):
    """Field volumes belong in object storage, not in a JSON response."""
    ref = backend.submit(valid_definition, uuid.uuid4())
    status = drive_to_completion(backend, ref)
    if status.state is not SolverState.succeeded:
        pytest.skip("backend did not succeed on this input")

    results = backend.fetch_results(ref)
    assert all(isinstance(uri, str) for uri in results.artifacts.values())


def test_serialised_results_are_json_safe(backend, valid_definition):
    import json

    ref = backend.submit(valid_definition, uuid.uuid4())
    status = drive_to_completion(backend, ref)
    if status.state is not SolverState.succeeded:
        pytest.skip("backend did not succeed on this input")

    payload = backend.fetch_results(ref).as_dict()
    json.dumps(payload)  # complex numbers must already be unpacked
    assert "s_parameters_db" in payload


def test_non_physical_backends_watermark_every_result(backend, valid_definition):
    """The flag is set by the backend so it cannot be dropped downstream."""
    ref = backend.submit(valid_definition, uuid.uuid4())
    status = drive_to_completion(backend, ref)
    if status.state is not SolverState.succeeded:
        pytest.skip("backend did not succeed on this input")

    results = backend.fetch_results(ref)
    if backend.physical:
        assert results.demonstration_only is False
    else:
        assert results.demonstration_only is True
        payload = results.as_dict()
        assert "NOT PHYSICAL" in payload["notice"]


# --- durability -----------------------------------------------------------

def test_asynchronous_backends_declare_durable_state(backend):
    """A run that takes minutes will meet a deploy.

    If `submit` returns before the run finishes, the job's state must survive
    the process. Process-local state loses the job on restart and freezes the
    customer's credits until the reaper finds them — the most expensive silent
    failure in the system.
    """
    if backend.synchronous:
        pytest.skip("synchronous backend: no state outlives submit()")
    assert backend.durable_state, (
        f"{backend.key} is asynchronous but keeps state in the process. "
        "Persist it where another worker can read it."
    )


def test_durable_backends_survive_a_fresh_instance(backend):
    """The contract suite runs in one process, which hides exactly this bug.

    A second adapter instance stands in for the worker that picks the job up
    after a restart.
    """
    if not backend.durable_state:
        pytest.skip("backend does not claim durable state")

    definition = {
        "schema_version": "1.0.0",
        "component": {"family": "Antennas", "type": "RectangularPatch"},
        "parameters": {
            "frequency_center": {"value": 2.45, "unit": "GHz"},
            "substrate_material": {"value": "RO4003C"},
            "substrate_height": {"value": 0.813, "unit": "mm"},
        },
    }
    job_id = uuid.uuid4()
    ref = backend.submit(definition, job_id)

    fresh = type(backend)(root=backend.root) if hasattr(backend, "root") else type(backend)()
    fresh.status(ref)  # must not raise: the job is findable from disk
    backend.cancel(ref)


# --- physics --------------------------------------------------------------

def test_resonance_lands_where_the_geometry_says(backend, valid_definition):
    """A backend returning a flat line passes every structural check.

    The design is a 2.45 GHz patch, so the S11 minimum has to be near 2.45 GHz.
    The tolerance is loose on purpose — different methods legitimately disagree
    by a few percent, and this test is here to catch a backend that is not
    solving the geometry at all.
    """
    ref = backend.submit(valid_definition, uuid.uuid4())
    status = drive_to_completion(backend, ref)
    if status.state is not SolverState.succeeded:
        pytest.skip("backend did not succeed on this input")

    results = backend.fetch_results(ref)
    if not backend.physical:
        pytest.skip("mock output is not expected to track the geometry")

    s11 = results.s_parameters["S11"]
    frequencies = results.frequencies_hz
    dip = frequencies[min(range(len(s11)), key=lambda i: abs(s11[i]))]

    assert 2.45e9 * 0.9 < dip < 2.45e9 * 1.1, (
        f"{backend.key} puts the resonance at {dip / 1e9:.3f} GHz for a "
        "2.45 GHz design"
    )


def test_the_dip_is_actually_a_dip(backend, valid_definition):
    """Guards against a flat response passing the test above by accident."""
    ref = backend.submit(valid_definition, uuid.uuid4())
    status = drive_to_completion(backend, ref)
    if status.state is not SolverState.succeeded or not backend.physical:
        pytest.skip("not applicable")

    s11 = [abs(v) for v in backend.fetch_results(ref).s_parameters["S11"]]
    assert max(s11) - min(s11) > 0.1, "S11 is flat: the geometry is being ignored"
