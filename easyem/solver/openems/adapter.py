"""openEMS solver backend.

Two things distinguish this adapter from the two already in the repo, and both
are deliberate preparation for EMG-TLM:

**State lives on disk, not in a dict.** The analytical and mock backends keep
job state in a process-local dictionary, which is fine because they finish in
milliseconds. A real time-domain run takes minutes to hours, across restarts and
multiple workers, so its state must survive both. Each job owns a directory
holding the script, the process id, the progress file and the results. Any
worker can pick up any job by reading it.

**The solver is a subprocess, never an import.** openEMS is GPL v3. Running it
as a separate process communicating through files is the standard arm's-length
boundary; linking its library into this codebase would put the licence question
squarely in play. There is no openEMS import anywhere in this package, and that
is a structural guarantee rather than a matter of remembering.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ...engineering.errors import UnitError, UnknownMaterial
from ..base import (
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
from .mesh import credits_for
from .script import UnsupportedGeometry, build

DEFAULT_ROOT = Path(os.environ.get("EASYEM_RUN_ROOT", "/var/lib/easyem/runs"))


def openems_available() -> bool:
    """Is openEMS importable by the interpreter that will run the script?

    Checked in a subprocess so a broken openEMS install cannot take down the
    API process on import.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import openEMS, CSXCAD"],
            capture_output=True,
            timeout=20,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class OpenEMSAdapter(SolverAdapter):
    key = "openems"
    version = "0.36"
    physical = True
    synchronous = False
    durable_state = True

    def __init__(self, root: Path | str | None = None, accuracy: str = "standard"):
        self.root = Path(root) if root else DEFAULT_ROOT
        self.accuracy = accuracy

    # -- run directory -----------------------------------------------------

    def _dir(self, job_id: str) -> Path:
        return self.root / job_id

    def _read_json(self, job_id: str, name: str) -> dict | None:
        path = self._dir(job_id) / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _write_json(self, job_id: str, name: str, payload: dict) -> None:
        path = self._dir(job_id) / name
        # Write-then-rename: a poller must never read a half-written file.
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)

    # -- contract ----------------------------------------------------------

    def supports(self, definition: dict) -> bool:
        """Only geometry and material problems mean "unsupported".

        Catching everything here turns any bug — a typo, a unit error, a
        missing material — into a polite "this backend cannot do that", so the
        customer sees a refusal and nobody ever learns there is a bug.
        """
        try:
            build(definition, accuracy=self.accuracy)
            return True
        except (UnsupportedGeometry, UnknownMaterial, UnitError, KeyError):
            return False

    def estimate(self, definition: dict) -> SolverEstimate:
        try:
            run = build(definition, accuracy=self.accuracy)
        except UnsupportedGeometry as exc:
            raise SolverError(SolverErrorCode.UNSUPPORTED_COMPONENT, str(exc)) from exc

        if run.plan.truncated:
            raise SolverError(
                SolverErrorCode.RESOURCE_EXCEEDED,
                "This design needs more timesteps than the platform allows: a "
                "very thin substrate at this frequency forces a tiny Courant "
                "step. The run would stop before the structure settles, so the "
                "spectrum would be wrong rather than merely imprecise. Use a "
                "thicker substrate, or narrow the frequency span.",
            )

        expected, ceiling = credits_for(run.plan)
        return SolverEstimate(
            cost_ceiling=ceiling,
            cost_expected=expected,
            estimated_seconds=run.plan.estimated_seconds,
            cell_count=run.plan.cell_count,
            frequency_points=run.plan.frequency_points,
            notes=[
                f"{run.plan.cell_count:,} cells, {run.plan.timesteps:,} timesteps, "
                f"~{run.plan.estimated_memory_mb:.0f} MB.",
                "Full-wave FDTD. The ceiling carries margin because mesh "
                "smoothing adds lines and a structure that fails to settle runs "
                "to its step limit; you are charged actual consumption.",
            ],
        )

    def submit(self, definition: dict, job_id: uuid.UUID) -> SolverJobRef:
        ref = SolverJobRef(backend=self.key, external_id=str(job_id))
        run_dir = self._dir(ref.external_id)

        if (run_dir / "state.json").exists():
            return ref  # idempotent resubmission

        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            run = build(definition, accuracy=self.accuracy)
        except UnsupportedGeometry as exc:
            self._write_json(
                ref.external_id, "state.json",
                {
                    "state": SolverState.failed.value,
                    "error_code": SolverErrorCode.UNSUPPORTED_COMPONENT.value,
                    "error_message": str(exc),
                },
            )
            return ref

        (run_dir / "run.py").write_text(run.script)
        self._write_json(
            ref.external_id, "plan.json",
            {**run.plan.as_dict(), "geometry": run.geometry,
             "component": run.component},
        )

        started = time.time()
        process = subprocess.Popen(
            [sys.executable, str(run_dir / "run.py"), str(run_dir)],
            cwd=str(run_dir),
            stdout=(run_dir / "stdout.log").open("w"),
            stderr=(run_dir / "stderr.log").open("w"),
            # Own process group, so cancelling kills openEMS and not just the
            # Python wrapper that launched it.
            start_new_session=True,
        )
        self._write_json(
            ref.external_id, "state.json",
            {
                "state": SolverState.running.value,
                "pid": process.pid,
                "started_at": started,
                "timeout_seconds": max(600, run.plan.estimated_seconds * 6),
            },
        )
        return ref

    def status(self, ref: SolverJobRef) -> SolverStatus:
        state = self._read_json(ref.external_id, "state.json")
        if state is None:
            raise SolverError(
                SolverErrorCode.PLATFORM_ERROR, f"Unknown job {ref.external_id}"
            )

        recorded = SolverState(state["state"])
        if recorded is not SolverState.running:
            return self._terminal_status(ref, state)

        pid = state.get("pid")
        elapsed = time.time() - state.get("started_at", time.time())

        if elapsed > state.get("timeout_seconds", 3600):
            self._kill(pid)
            return self._finish(
                ref, SolverState.failed, SolverErrorCode.TIMEOUT,
                f"Exceeded {state['timeout_seconds']:.0f}s", elapsed,
            )

        if self._alive(pid):
            progress = (self._read_json(ref.external_id, "progress.json") or {})
            return SolverStatus(
                state=SolverState.running,
                progress=float(progress.get("progress", 0.0)),
                message=progress.get("message"),
            )

        # Process gone. Results present means success; absent means it died.
        if (self._dir(ref.external_id) / "results.json").exists():
            return self._finish(ref, SolverState.succeeded, None, None, elapsed)

        # The script writes error.json with the actual exception before exiting.
        # Prefer it: "geometry self-intersects" is worth infinitely more to the
        # customer than a generic internal error scraped from stderr.
        error = self._read_json(ref.external_id, "error.json")
        message = error.get("error") if error else self._tail_stderr(ref.external_id)
        return self._finish(
            ref, SolverState.failed, SolverErrorCode.SOLVER_INTERNAL,
            message, elapsed,
        )

    def cancel(self, ref: SolverJobRef) -> None:
        state = self._read_json(ref.external_id, "state.json")
        if state is None or state["state"] != SolverState.running.value:
            return  # safe on finished and on unknown jobs
        self._kill(state.get("pid"))
        self._finish(
            ref, SolverState.cancelled, SolverErrorCode.USER_CANCELLED,
            "Cancelled by user",
            time.time() - state.get("started_at", time.time()),
        )

    def fetch_results(self, ref: SolverJobRef) -> CanonicalResults:
        payload = self._read_json(ref.external_id, "results.json")
        if payload is None:
            raise SolverError(SolverErrorCode.PLATFORM_ERROR, "Results unavailable")

        state = self._read_json(ref.external_id, "state.json") or {}
        plan = self._read_json(ref.external_id, "plan.json") or {}

        def to_complex(values):
            return [complex(v["re"], v["im"]) for v in values]

        return CanonicalResults(
            frequencies_hz=payload["frequencies_hz"],
            s_parameters={
                k: to_complex(v) for k, v in payload["s_parameters"].items()
            },
            input_impedance_ohm=(
                to_complex(payload["input_impedance_ohm"])
                if "input_impedance_ohm" in payload
                else None
            ),
            radiation_pattern=payload.get("radiation_pattern"),
            scalars=payload.get("scalars", {}),
            artifacts=self._artifacts(ref.external_id),
            usage=SolverUsage(
                cpu_seconds=state.get("cpu_seconds", 0.0),
                wall_seconds=state.get("wall_seconds", 0.0),
                peak_memory_mb=plan.get("estimated_memory_mb", 0.0),
                cell_count=plan.get("cell_count", 0),
                frequency_points=len(payload["frequencies_hz"]),
                solver_version=self.version,
            ),
            demonstration_only=False,
            warnings=[
                "Full-wave FDTD on a Cartesian grid. Curved geometry is "
                "staircased; results near sharp curvature read optimistic.",
                "Mesh convergence has not been verified. Re-run at higher "
                "accuracy and compare before committing to fabrication.",
            ],
        )

    def health(self) -> dict:
        return {
            "backend": self.key,
            "version": self.version,
            "healthy": openems_available(),
            "run_root": str(self.root),
        }

    # -- helpers -----------------------------------------------------------

    def _terminal_status(self, ref, state) -> SolverStatus:
        recorded = SolverState(state["state"])
        code = state.get("error_code")
        return SolverStatus(
            state=recorded,
            progress=100.0 if recorded is SolverState.succeeded else state.get(
                "progress", 0.0
            ),
            error_code=SolverErrorCode(code) if code else None,
            error_message=state.get("error_message"),
            usage=self._usage(ref, state) if recorded is SolverState.succeeded else None,
        )

    def _finish(self, ref, state, code, message, elapsed) -> SolverStatus:
        payload = {
            "state": state.value,
            "wall_seconds": elapsed,
            "cpu_seconds": elapsed,  # single-process run
            "progress": 100.0 if state is SolverState.succeeded else 0.0,
        }
        if code is not None:
            payload["error_code"] = code.value
            payload["error_message"] = (message or "")[:1000]
        self._write_json(ref.external_id, "state.json", payload)
        return self._terminal_status(ref, payload)

    def _usage(self, ref, state) -> SolverUsage:
        plan = self._read_json(ref.external_id, "plan.json") or {}
        return SolverUsage(
            cpu_seconds=state.get("cpu_seconds", 0.0),
            wall_seconds=state.get("wall_seconds", 0.0),
            peak_memory_mb=plan.get("estimated_memory_mb", 0.0),
            cell_count=plan.get("cell_count", 0),
            frequency_points=plan.get("frequency_points", 0),
            solver_version=self.version,
        )

    def _artifacts(self, job_id: str) -> dict[str, str]:
        """URIs, never inlined payloads. The script alone is worth exposing:
        a customer who can read and re-run it can verify the result."""
        run_dir = self._dir(job_id)
        return {
            name: f"file://{run_dir / name}"
            for name in ("run.py", "stdout.log", "results.json")
            if (run_dir / name).exists()
        }

    @staticmethod
    def _alive(pid: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    @staticmethod
    def _kill(pid: int | None) -> None:
        if not pid:
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            time.sleep(0.2)
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    def _tail_stderr(self, job_id: str, lines: int = 20) -> str:
        path = self._dir(job_id) / "stderr.log"
        if not path.exists():
            return "Solver exited without producing results."
        try:
            return "\n".join(path.read_text().splitlines()[-lines:])[:1000]
        except OSError:
            return "Solver exited without producing results."

    def cleanup(self, ref: SolverJobRef) -> None:
        """Remove a run directory once its results are in object storage."""
        shutil.rmtree(self._dir(ref.external_id), ignore_errors=True)
