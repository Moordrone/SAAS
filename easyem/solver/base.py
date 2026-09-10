"""The solver contract.

Everything the SaaS knows about solving lives in this file. The rest of the
product talks to `SolverAdapter` and never to a solver.

The contract is written against *two* implementations from the start — `mock`
and `analytical` — because an interface written against one implementation takes
the shape of that implementation. The mock is instant, deterministic, unbounded
in memory and has no failure modes; EMG-TLM will be slow, resource-hungry and
will fail in a dozen ways. A contract shaped only by the mock would not survive
meeting it.

Every backend must pass `tests/solver_contract/`. That suite is the acceptance
criterion for adding a new one.
"""

from __future__ import annotations

import enum
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class SolverErrorCode(enum.StrEnum):
    """A closed set. Open-ended error strings cannot drive a billing policy.

    Whether a customer is charged for a failed run is a commercial decision, and
    it has to be decided per cause rather than left to whoever writes the next
    exception message.
    """

    INVALID_GEOMETRY = "INVALID_GEOMETRY"
    UNSUPPORTED_COMPONENT = "UNSUPPORTED_COMPONENT"
    MESH_FAILED = "MESH_FAILED"
    NON_CONVERGED = "NON_CONVERGED"
    RESOURCE_EXCEEDED = "RESOURCE_EXCEEDED"
    TIMEOUT = "TIMEOUT"
    USER_CANCELLED = "USER_CANCELLED"
    SOLVER_INTERNAL = "SOLVER_INTERNAL"
    PLATFORM_ERROR = "PLATFORM_ERROR"


class BillingPolicy(enum.StrEnum):
    none = "none"                 # release the whole hold
    actual = "actual"             # charge measured consumption
    prorated = "prorated"         # charge CPU actually burned
    capped = "capped"             # charge the full quote, results partial


# Published in the customer documentation. Answering disputes before they happen
# is cheaper than answering them afterwards.
BILLING_FOR_ERROR: dict[SolverErrorCode, BillingPolicy] = {
    SolverErrorCode.INVALID_GEOMETRY: BillingPolicy.none,
    SolverErrorCode.UNSUPPORTED_COMPONENT: BillingPolicy.none,
    SolverErrorCode.MESH_FAILED: BillingPolicy.none,
    SolverErrorCode.NON_CONVERGED: BillingPolicy.actual,
    SolverErrorCode.RESOURCE_EXCEEDED: BillingPolicy.capped,
    SolverErrorCode.TIMEOUT: BillingPolicy.none,
    SolverErrorCode.USER_CANCELLED: BillingPolicy.prorated,
    SolverErrorCode.SOLVER_INTERNAL: BillingPolicy.none,
    SolverErrorCode.PLATFORM_ERROR: BillingPolicy.none,
}


class SolverState(enum.StrEnum):
    """Execution only. Money lives in a separate field — see models/jobs.py."""

    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class SolverError(Exception):
    def __init__(self, code: SolverErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")


@dataclass
class SolverEstimate:
    """What the run will cost, before committing to it.

    `cost_ceiling` is the number shown to the customer and the amount held. The
    customer is never charged above it, so a backend that cannot bound its own
    cost must return a generous ceiling rather than an optimistic one.
    """

    cost_ceiling: float
    cost_expected: float
    estimated_seconds: float
    cell_count: int
    frequency_points: int
    notes: list[str] = field(default_factory=list)


@dataclass
class SolverUsage:
    """Measured consumption, reported back so settlement can charge the truth.

    Without this the platform can only ever debit the amount it reserved, and
    the estimator can never be calibrated against reality.
    """

    cpu_seconds: float
    wall_seconds: float
    peak_memory_mb: float
    cell_count: int
    frequency_points: int
    solver_version: str


@dataclass
class SolverStatus:
    state: SolverState
    progress: float = 0.0                      # 0..100, never decreasing
    message: str | None = None
    error_code: SolverErrorCode | None = None
    error_message: str | None = None
    usage: SolverUsage | None = None


@dataclass
class CanonicalResults:
    """Solver-independent results. EMG-TLM output is translated into this.

    Large payloads (field volumes, meshes) are referenced by URI, never inlined:
    a field volume can run to hundreds of megabytes and has no business in a
    JSON response or a database row.
    """

    frequencies_hz: list[float]
    s_parameters: dict[str, list[complex]]
    input_impedance_ohm: list[complex] | None = None
    radiation_pattern: dict | None = None
    scalars: dict = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)   # name -> URI
    usage: SolverUsage | None = None
    # Set by any backend whose numbers are not physical. The API refuses to
    # strip it, so demonstration data cannot be mistaken for a result.
    demonstration_only: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        def unpack(values):
            return [{"re": v.real, "im": v.imag} for v in values]

        payload: dict = {
            "frequencies_hz": self.frequencies_hz,
            "s_parameters": {k: unpack(v) for k, v in self.s_parameters.items()},
            "s_parameters_db": {
                k: [_to_db(v) for v in values]
                for k, values in self.s_parameters.items()
            },
            "scalars": self.scalars,
            "artifacts": self.artifacts,
            "warnings": list(self.warnings),
        }
        if self.input_impedance_ohm is not None:
            payload["input_impedance_ohm"] = unpack(self.input_impedance_ohm)
        if self.radiation_pattern is not None:
            payload["radiation_pattern"] = self.radiation_pattern
        if self.demonstration_only:
            payload["demonstration_only"] = True
            payload["notice"] = (
                "DEMONSTRATION DATA — NOT PHYSICAL. Generated by the mock "
                "backend for interface testing. Do not use for design."
            )
        return payload


def _to_db(value: complex) -> float:
    magnitude = abs(value)
    return -300.0 if magnitude <= 1e-15 else 20.0 * __import__("math").log10(magnitude)


@dataclass
class SolverJobRef:
    """Opaque handle. The platform stores it and asks nothing about its shape."""

    backend: str
    external_id: str


class SolverAdapter(ABC):
    """The only surface between EasyEM and a solver."""

    key: str = "abstract"
    version: str = "0.0.0"
    #: False means the backend produces non-physical numbers and must never be
    #: reachable by a customer in production.
    physical: bool = True

    #: True means `submit` returns only once the run is finished, so state
    #: cannot outlive the call and durability is moot. Anything that takes
    #: longer than a request must be False *and* durable — see below.
    synchronous: bool = True

    #: True means job state survives process restarts and is readable by any
    #: worker. Mandatory for asynchronous backends: a run that takes minutes
    #: will meet a deploy, and process-local state loses the job and freezes
    #: the customer's credits until the reaper finds it.
    durable_state: bool = False

    @abstractmethod
    def supports(self, definition: dict) -> bool:
        """Can this backend solve that problem at all?"""

    @abstractmethod
    def estimate(self, definition: dict) -> SolverEstimate:
        """Cost and size, before anything is committed."""

    @abstractmethod
    def submit(self, definition: dict, job_id: uuid.UUID) -> SolverJobRef:
        """Start a run. Submitting the same job_id twice must not run it twice."""

    @abstractmethod
    def status(self, ref: SolverJobRef) -> SolverStatus:
        """Poll. Progress must be within 0-100 and must never decrease."""

    @abstractmethod
    def cancel(self, ref: SolverJobRef) -> None:
        """Stop a run. Must be safe to call on an already-finished job."""

    @abstractmethod
    def fetch_results(self, ref: SolverJobRef) -> CanonicalResults:
        """Only valid once status is `succeeded`."""

    def health(self) -> dict:
        return {"backend": self.key, "version": self.version, "healthy": True}
