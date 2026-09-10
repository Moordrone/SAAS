"""Mock backend: interface exercise only, never a design tool.

Everything it returns is watermarked `demonstration_only`. That flag is set by
the backend and stripped by nothing, because an RF engineer recognises a
non-physical S11 in about three seconds and a demonstration caught doing that
does not recover.

The mock exists to develop the frontend and to give the contract suite a
backend it can drive predictably. It is deterministic from the job id, so a
screenshot taken today reproduces tomorrow.

There is deliberately no "force this error" hook: a mutable flag on an object
the registry shares between requests is a race waiting to happen, and test
scaffolding does not belong in production code. Tests that need a failure path
patch the backend they were given.
"""

from __future__ import annotations

import hashlib
import uuid

from .base import (
    CanonicalResults,
    SolverAdapter,
    SolverError,
    SolverErrorCode,
    SolverEstimate,
    SolverJobRef,
    SolverState,
    SolverStatus,
    SolverUsage,
)


class MockSolverAdapter(SolverAdapter):
    key = "mock"
    version = "1.0.0"
    physical = False
    synchronous = True
    durable_state = False          # gate in registry.py depends on this

    def __init__(self, points: int = 201, steps_to_finish: int = 4) -> None:
        self._points = points
        self._steps = steps_to_finish
        self._jobs: dict[str, dict] = {}


    def supports(self, definition: dict) -> bool:
        return bool((definition.get("component") or {}).get("type"))

    def estimate(self, definition: dict) -> SolverEstimate:
        cells = 20_000
        return SolverEstimate(
            cost_ceiling=12.0,
            cost_expected=8.0,
            estimated_seconds=5.0,
            cell_count=cells,
            frequency_points=self._points,
            notes=["Mock backend: this estimate is not based on any computation."],
        )

    def submit(self, definition: dict, job_id: uuid.UUID) -> SolverJobRef:
        ref = SolverJobRef(backend=self.key, external_id=str(job_id))
        if ref.external_id in self._jobs:
            return ref

        self._jobs[ref.external_id] = {
            "state": SolverState.queued,
            "progress": 0.0,
            "polls": 0,
            "definition": definition,
        }
        return ref

    def status(self, ref: SolverJobRef) -> SolverStatus:
        job = self._jobs.get(ref.external_id)
        if job is None:
            raise SolverError(SolverErrorCode.PLATFORM_ERROR, "Unknown job")

        if job["state"] in (SolverState.queued, SolverState.running):
            job["polls"] += 1
            fraction = min(job["polls"] / self._steps, 1.0)
            job["progress"] = round(fraction * 100.0, 1)
            job["state"] = (
                SolverState.succeeded if fraction >= 1.0 else SolverState.running
            )

        return SolverStatus(
            state=job["state"],
            progress=job["progress"],
            error_code=job.get("error_code"),
            error_message=job.get("error_message"),
            usage=self._usage() if job["state"] is SolverState.succeeded else None,
        )

    def cancel(self, ref: SolverJobRef) -> None:
        job = self._jobs.get(ref.external_id)
        if job and job["state"] in (SolverState.queued, SolverState.running):
            job["state"] = SolverState.cancelled
            job["error_code"] = SolverErrorCode.USER_CANCELLED

    def fetch_results(self, ref: SolverJobRef) -> CanonicalResults:
        job = self._jobs.get(ref.external_id)
        if job is None or job["state"] is not SolverState.succeeded:
            raise SolverError(SolverErrorCode.PLATFORM_ERROR, "Results unavailable")

        seed = int(
            hashlib.sha256(ref.external_id.encode()).hexdigest()[:8], 16
        )
        f0 = 2.45e9 * (1.0 + ((seed % 100) - 50) / 5000.0)
        lo, hi = f0 * 0.85, f0 * 1.15
        step = (hi - lo) / (self._points - 1)
        frequencies = [lo + i * step for i in range(self._points)]

        q = 30.0
        s11 = [
            (lambda z: (z - 50.0) / (z + 50.0))(
                50.0 / complex(1.0, 2.0 * q * (f - f0) / f0)
            )
            for f in frequencies
        ]

        return CanonicalResults(
            frequencies_hz=frequencies,
            s_parameters={"S11": s11},
            scalars={
                "resonant_frequency_hz": f0,
                "directivity_dbi": 7.0,
                "gain_dbi": 6.0,
            },
            usage=self._usage(),
            demonstration_only=True,
            warnings=[
                "DEMONSTRATION DATA — NOT PHYSICAL. Produced by the mock "
                "backend. These numbers bear no relation to the geometry you "
                "entered and must not be used for design."
            ],
        )

    def _usage(self) -> SolverUsage:
        return SolverUsage(
            cpu_seconds=4.2,
            wall_seconds=5.0,
            peak_memory_mb=512.0,
            cell_count=20_000,
            frequency_points=self._points,
            solver_version=self.version,
        )
