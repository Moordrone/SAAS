"""Every backend runs the same suite. That is the whole point.

Add a backend to `all_backends()` and it is immediately held to the same
behavioural contract as the others. EMG-TLM is integrated the day it goes green
here — which turns phase 9 from a gamble into an acceptance test.
"""

import pytest

from easyem.solver.registry import all_backends


def pytest_generate_tests(metafunc):
    if "backend" in metafunc.fixturenames:
        backends = all_backends()
        metafunc.parametrize(
            "backend", backends, ids=[b.key for b in backends], scope="function"
        )


@pytest.fixture()
def valid_definition():
    return {
        "schema_version": "1.0.0",
        "component": {"family": "Antennas", "type": "RectangularPatch"},
        "parameters": {
            "frequency_center": {"value": 2.45, "unit": "GHz", "provenance": "user"},
            "substrate_material": {"value": "RO4003C", "provenance": "user"},
            "substrate_height": {"value": 0.813, "unit": "mm", "provenance": "user"},
        },
    }


def drive_to_completion(backend, ref, max_polls: int = 50):
    """Poll until terminal. Backends differ in how many polls they need."""
    from easyem.solver.base import SolverState

    terminal = {SolverState.succeeded, SolverState.failed, SolverState.cancelled}
    status = backend.status(ref)
    polls = 0
    while status.state not in terminal and polls < max_polls:
        status = backend.status(ref)
        polls += 1
    return status
