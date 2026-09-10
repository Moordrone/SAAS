"""Analytical solver backend.

Wraps the Engineering Engine's closed-form models into the solver contract and
turns them into frequency sweeps. The S-parameters are derived from physics —
a resonant circuit for the patch, an ABCD matrix for the line — not invented.

This backend exists for three reasons:

  1. It gives the contract a second real implementation, so the interface is not
     quietly shaped around the mock's conveniences.
  2. It makes the product genuinely useful before EMG-TLM is ready. A first-pass
     dimensioning in milliseconds sells a subscription on its own.
  3. It makes demonstrations honest. An approximate answer grounded in physics
     can be checked against a full-wave run; a plausible fabrication is
     contradicted by one, in front of someone who will notice.

Accuracy is stated, not implied: roughly 5 % on patch resonance for thin
substrates, 1-2 % on microstrip impedance. Outside the validated range the
results carry warnings.
"""

from __future__ import annotations

import cmath
import math
import uuid

from ..engineering.analytical import microstrip, patch
from ..engineering.materials import get_substrate
from ..engineering.validation import validate
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

C0 = 299_792_458.0
SUPPORTED = {"RectangularPatch", "MicrostripLine"}


def _read(definition: dict, name: str, default=None):
    raw = (definition.get("parameters") or {}).get(name)
    if raw is None:
        return default
    value, unit = raw.get("value"), raw.get("unit", "")
    if isinstance(value, str):
        return value
    from ..engineering.units import Quantity, is_known

    return Quantity(float(value), unit).si_value if is_known(unit) else default


class AnalyticalSolverAdapter(SolverAdapter):
    key = "analytical"
    version = "1.0.0"
    physical = True
    synchronous = True
    durable_state = False

    def __init__(self, points: int = 201, span_fraction: float = 0.3) -> None:
        self._points = points
        self._span = span_fraction
        self._jobs: dict[str, dict] = {}

    # -- contract ----------------------------------------------------------

    def supports(self, definition: dict) -> bool:
        return (definition.get("component") or {}).get("type") in SUPPORTED

    def estimate(self, definition: dict) -> SolverEstimate:
        if not self.supports(definition):
            raise SolverError(
                SolverErrorCode.UNSUPPORTED_COMPONENT,
                f"{(definition.get('component') or {}).get('type')} has no "
                "analytical model.",
            )
        return SolverEstimate(
            # Closed forms are cheap and bounded: the ceiling is the real cost.
            cost_ceiling=1.0,
            cost_expected=1.0,
            estimated_seconds=0.05,
            cell_count=0,
            frequency_points=self._points,
            notes=[
                "Analytical model: milliseconds, approximate, with stated "
                "validity limits. Not a substitute for a full-wave solve."
            ],
        )

    def submit(self, definition: dict, job_id: uuid.UUID) -> SolverJobRef:
        ref = SolverJobRef(backend=self.key, external_id=str(job_id))

        if ref.external_id in self._jobs:
            return ref  # idempotent resubmission

        report = validate(definition)
        if not report.is_valid:
            self._jobs[ref.external_id] = {
                "state": SolverState.failed,
                "error_code": SolverErrorCode.INVALID_GEOMETRY,
                "error_message": "; ".join(
                    i.message for i in report.errors
                ) or f"Missing: {', '.join(report.missing)}",
                "progress": 0.0,
            }
            return ref

        try:
            results = self._solve(definition)
        except SolverError as exc:
            self._jobs[ref.external_id] = {
                "state": SolverState.failed,
                "error_code": exc.code,
                "error_message": exc.message,
                "progress": 0.0,
            }
            return ref
        except (ValueError, ZeroDivisionError) as exc:
            self._jobs[ref.external_id] = {
                "state": SolverState.failed,
                "error_code": SolverErrorCode.SOLVER_INTERNAL,
                "error_message": str(exc),
                "progress": 0.0,
            }
            return ref

        self._jobs[ref.external_id] = {
            "state": SolverState.succeeded,
            "progress": 100.0,
            "results": results,
        }
        return ref

    def status(self, ref: SolverJobRef) -> SolverStatus:
        job = self._jobs.get(ref.external_id)
        if job is None:
            raise SolverError(
                SolverErrorCode.PLATFORM_ERROR, f"Unknown job {ref.external_id}"
            )
        results = job.get("results")
        return SolverStatus(
            state=job["state"],
            progress=job["progress"],
            error_code=job.get("error_code"),
            error_message=job.get("error_message"),
            usage=results.usage if results else None,
        )

    def cancel(self, ref: SolverJobRef) -> None:
        job = self._jobs.get(ref.external_id)
        # Safe on an already-finished job: a terminal state is not overwritten.
        if job and job["state"] in (SolverState.queued, SolverState.running):
            job["state"] = SolverState.cancelled
            job["error_code"] = SolverErrorCode.USER_CANCELLED

    def fetch_results(self, ref: SolverJobRef) -> CanonicalResults:
        job = self._jobs.get(ref.external_id)
        if job is None or job["state"] is not SolverState.succeeded:
            raise SolverError(
                SolverErrorCode.PLATFORM_ERROR, "Results are not available"
            )
        return job["results"]

    # -- physics -----------------------------------------------------------

    def _solve(self, definition: dict) -> CanonicalResults:
        component = definition["component"]["type"]
        f0 = _read(definition, "frequency_center")
        substrate = get_substrate(str(_read(definition, "substrate_material")))
        height = _read(definition, "substrate_height")

        lo = f0 * (1.0 - self._span / 2.0)
        hi = f0 * (1.0 + self._span / 2.0)
        step = (hi - lo) / (self._points - 1)
        frequencies = [lo + i * step for i in range(self._points)]

        if component == "RectangularPatch":
            return self._patch_sweep(definition, substrate, height, f0, frequencies)
        return self._line_sweep(definition, substrate, height, f0, frequencies)

    def _patch_sweep(
        self, definition, substrate, height, f0, frequencies
    ) -> CanonicalResults:
        """Patch as a parallel RLC near resonance.

        Near f0 the input impedance of a resonant antenna follows
        Z(f) = R / (1 + j*2Q*(f-f0)/f0), with Q from the estimated bandwidth.
        The resulting S11 dip sits where the geometry says it should and has the
        width the substrate implies — both checkable against a full-wave run.
        """
        z_ref = _read(definition, "feed_impedance", 50.0) or 50.0
        model = patch.design(
            frequency_hz=f0,
            epsilon_r=substrate.epsilon_r,
            height_m=height,
            feed_impedance_ohm=z_ref,
            loss_tangent=substrate.loss_tangent,
        )

        # Use the resistance the manufacturable inset actually presents, not
        # the feed impedance. Assuming a perfect match produces S11 = 0 exactly,
        # which is not a result — it is an artefact of the idealisation.
        r_res = model.inset_resistance_ohm
        q = 0.707 / model.bandwidth_fraction if model.bandwidth_fraction > 0 else 100.0

        s11: list[complex] = []
        z_in: list[complex] = []
        for f in frequencies:
            detune = 2.0 * q * (f - f0) / f0
            z = r_res / complex(1.0, detune)
            z_in.append(z)
            s11.append((z - z_ref) / (z + z_ref))

        return CanonicalResults(
            frequencies_hz=frequencies,
            s_parameters={"S11": s11},
            input_impedance_ohm=z_in,
            radiation_pattern=self._patch_pattern(model, f0),
            scalars={
                "resonant_frequency_hz": f0,
                "quality_factor": q,
                "bandwidth_fraction": model.bandwidth_fraction,
                "directivity_dbi": model.directivity_dbi,
                "radiation_efficiency": model.radiation_efficiency_estimate,
                "gain_dbi": model.directivity_dbi
                + 10.0 * math.log10(max(model.radiation_efficiency_estimate, 1e-6)),
                "patch_width_m": model.width_m,
                "patch_length_m": model.length_m,
                "inset_feed_offset_m": model.inset_feed_offset_m,
                "inset_resistance_ohm": model.inset_resistance_ohm,
                "return_loss_db": 20.0
                * math.log10(
                    abs((model.inset_resistance_ohm - z_ref)
                        / (model.inset_resistance_ohm + z_ref))
                    or 1e-6
                ),
            },
            usage=SolverUsage(
                cpu_seconds=0.02,
                wall_seconds=0.02,
                peak_memory_mb=8.0,
                cell_count=0,
                frequency_points=len(frequencies),
                solver_version=self.version,
            ),
            warnings=model.warnings
            + [
                "Cavity-model approximation. Resonance is typically within 5 % "
                "for thin substrates; confirm with a full-wave run before "
                "committing to fabrication."
            ],
        )

    def _patch_pattern(self, model, f0: float) -> dict:
        """E- and H-plane cuts from the two-slot aperture model."""
        lambda0 = C0 / f0
        k0 = 2.0 * math.pi / lambda0
        thetas = [math.radians(t) for t in range(-90, 91, 2)]

        e_plane, h_plane = [], []
        for theta in thetas:
            st, ct = math.sin(theta), math.cos(theta)

            # E-plane: two slots spaced by the effective length.
            arg = k0 * model.length_m / 2.0 * st
            e_val = abs(math.cos(arg)) * abs(ct) if abs(ct) > 1e-9 else 0.0
            e_plane.append(e_val)

            # H-plane: aperture of width W.
            x = k0 * model.width_m / 2.0 * st
            h_val = abs(math.sin(x) / x) if abs(x) > 1e-9 else 1.0
            h_plane.append(h_val * abs(ct))

        def normalise_db(values):
            peak = max(values) or 1.0
            return [
                round(20.0 * math.log10(max(v / peak, 1e-5)), 3) for v in values
            ]

        return {
            "theta_deg": [round(math.degrees(t), 1) for t in thetas],
            "e_plane_db": normalise_db(e_plane),
            "h_plane_db": normalise_db(h_plane),
            "peak_directivity_dbi": model.directivity_dbi,
        }

    def _line_sweep(
        self, definition, substrate, height, f0, frequencies
    ) -> CanonicalResults:
        """Transmission line by its ABCD matrix, converted to S-parameters."""
        z_ref = 50.0
        target = _read(definition, "target_impedance", 50.0) or 50.0
        electrical = _read(definition, "electrical_length")
        electrical_deg = math.degrees(electrical) if electrical is not None else 90.0

        model = microstrip.design(
            target_impedance_ohm=target,
            epsilon_r=substrate.epsilon_r,
            height_m=height,
            frequency_hz=f0,
            electrical_length_deg=electrical_deg,
        )
        length = model.physical_length_m or (model.guided_wavelength_m / 4.0)
        z_line = model.impedance_ohm

        s11: list[complex] = []
        s21: list[complex] = []
        for f in frequencies:
            beta = 2.0 * math.pi * f * math.sqrt(model.epsilon_eff) / C0
            theta = beta * length
            # Loss from the dielectric, in nepers per metre.
            alpha = (
                math.pi
                * f
                * math.sqrt(model.epsilon_eff)
                * substrate.loss_tangent
                / C0
            )
            gamma_l = complex(alpha * length, theta)

            ratio = z_line / z_ref
            denom = 2.0 * cmath.cosh(gamma_l) + (ratio + 1.0 / ratio) * cmath.sinh(
                gamma_l
            )
            s11.append((ratio - 1.0 / ratio) * cmath.sinh(gamma_l) / denom)
            s21.append(2.0 / denom)

        return CanonicalResults(
            frequencies_hz=frequencies,
            s_parameters={"S11": s11, "S21": s21, "S12": s21, "S22": s11},
            scalars={
                "characteristic_impedance_ohm": z_line,
                "epsilon_eff": model.epsilon_eff,
                "trace_width_m": model.width_m,
                "physical_length_m": length,
                "electrical_length_deg": electrical_deg,
                "guided_wavelength_m": model.guided_wavelength_m,
            },
            usage=SolverUsage(
                cpu_seconds=0.01,
                wall_seconds=0.01,
                peak_memory_mb=4.0,
                cell_count=0,
                frequency_points=len(frequencies),
                solver_version=self.version,
            ),
            warnings=model.warnings
            + ["Quasi-static model: dispersion and radiation loss are ignored."],
        )
