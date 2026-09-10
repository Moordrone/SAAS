"""Backend selection.

The single place that decides which solver a request may reach. The rule that
matters: a backend whose numbers are not physical is unreachable by a customer
in production, whatever the request asks for.
"""

from __future__ import annotations

from ..config import get_settings
from ..errors import AppError
from .analytical import AnalyticalSolverAdapter
from .base import SolverAdapter
from .mock import MockSolverAdapter
from .openems import OpenEMSAdapter, openems_available

DEFAULT_BACKEND = "analytical"


class SolverBackendUnavailable(AppError):
    status, code, title = 400, "solver_backend_unavailable", "Solver unavailable"


_BACKENDS: dict[str, SolverAdapter] = {}


def _registry() -> dict[str, SolverAdapter]:
    if not _BACKENDS:
        _BACKENDS["analytical"] = AnalyticalSolverAdapter()
        _BACKENDS["mock"] = MockSolverAdapter()
        # openEMS only registers when it is actually installed. Offering a
        # backend that cannot run is worse than not offering it: the customer
        # discovers the gap after their credits are held.
        if openems_available():
            _BACKENDS["openems"] = OpenEMSAdapter()
        # EMG-TLM registers here once it passes tests/solver_contract/.
    return _BACKENDS


def get_backend(key: str | None = None, *, is_admin: bool = False) -> SolverAdapter:
    key = key or DEFAULT_BACKEND
    backend = _registry().get(key)
    if backend is None:
        raise SolverBackendUnavailable(
            f"Unknown solver backend {key!r}. Available: "
            f"{', '.join(sorted(available(is_admin=is_admin)))}"
        )

    settings = get_settings()
    if not backend.physical and settings.environment == "production" and not is_admin:
        raise SolverBackendUnavailable(
            f"The {key!r} backend produces demonstration data and is not "
            "available in production."
        )
    return backend


def available(*, is_admin: bool = False) -> list[str]:
    settings = get_settings()
    return [
        key
        for key, backend in _registry().items()
        if backend.physical
        or is_admin
        or settings.environment != "production"
    ]


def all_backends() -> list[SolverAdapter]:
    """Every registered backend. Used by the contract suite."""
    return list(_registry().values())
