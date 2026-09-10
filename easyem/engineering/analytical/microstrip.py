"""Microstrip line: analysis and synthesis.

Analysis uses Hammerstad's quasi-static closed forms; synthesis uses Wheeler's
inverse. Both are standard textbook models (Hammerstad 1975; Wheeler 1977, as
presented in Pozar, *Microwave Engineering*, 4th ed., §3.8).

Accuracy is roughly 1-2 % for 0.05 < W/h < 20 and epsilon_r < 16 at frequencies
where the quasi-TEM assumption holds. That is good enough to size a line before
committing minutes of full-wave solver time, and it is honest: these are real
approximations, not invented numbers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

C0 = 299_792_458.0  # speed of light in vacuum, m/s
ETA0 = 376.730313668  # impedance of free space, ohm


@dataclass
class MicrostripResult:
    width_m: float
    height_m: float
    epsilon_r: float
    epsilon_eff: float
    impedance_ohm: float
    guided_wavelength_m: float
    electrical_length_deg: float | None = None
    physical_length_m: float | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {
            "width_m": self.width_m,
            "height_m": self.height_m,
            "epsilon_r": self.epsilon_r,
            "epsilon_eff": self.epsilon_eff,
            "impedance_ohm": self.impedance_ohm,
            "guided_wavelength_m": self.guided_wavelength_m,
        }
        if self.physical_length_m is not None:
            d["physical_length_m"] = self.physical_length_m
            d["electrical_length_deg"] = self.electrical_length_deg
        if self.warnings:
            d["warnings"] = self.warnings
        return d


def effective_permittivity(epsilon_r: float, width_m: float, height_m: float) -> float:
    """Hammerstad's quasi-static effective permittivity.

    The field is partly in the substrate and partly in the air above it, so the
    line sees something between 1 and epsilon_r. Wide lines concentrate more
    field in the dielectric and approach epsilon_r.
    """
    u = width_m / height_m
    a = (epsilon_r + 1.0) / 2.0
    b = (epsilon_r - 1.0) / 2.0

    if u >= 1.0:
        return a + b * (1.0 + 12.0 / u) ** -0.5
    return a + b * ((1.0 + 12.0 / u) ** -0.5 + 0.04 * (1.0 - u) ** 2)


def characteristic_impedance(
    epsilon_r: float, width_m: float, height_m: float
) -> tuple[float, float]:
    """Return (Z0 in ohm, epsilon_eff)."""
    if width_m <= 0 or height_m <= 0:
        raise ValueError("Width and height must be positive")

    u = width_m / height_m
    e_eff = effective_permittivity(epsilon_r, width_m, height_m)

    if u <= 1.0:
        z0 = (60.0 / math.sqrt(e_eff)) * math.log(8.0 / u + u / 4.0)
    else:
        z0 = (120.0 * math.pi / math.sqrt(e_eff)) / (
            u + 1.393 + 0.667 * math.log(u + 1.444)
        )
    return z0, e_eff


def synthesise_width(
    target_impedance_ohm: float, epsilon_r: float, height_m: float
) -> float:
    """Wheeler's inverse: the width that gives a target Z0.

    Two branches, selected by which one produces a self-consistent W/h. The
    result is refined by a short bisection against the forward model, because
    the closed form alone drifts near the branch boundary.
    """
    if target_impedance_ohm <= 0:
        raise ValueError("Target impedance must be positive")

    z0 = target_impedance_ohm
    a = z0 / 60.0 * math.sqrt((epsilon_r + 1.0) / 2.0) + (
        (epsilon_r - 1.0) / (epsilon_r + 1.0)
    ) * (0.23 + 0.11 / epsilon_r)
    b = 377.0 * math.pi / (2.0 * z0 * math.sqrt(epsilon_r))

    u_narrow = 8.0 * math.exp(a) / (math.exp(2.0 * a) - 2.0)
    if u_narrow < 2.0:
        u = u_narrow
    else:
        u = (2.0 / math.pi) * (
            b
            - 1.0
            - math.log(2.0 * b - 1.0)
            + ((epsilon_r - 1.0) / (2.0 * epsilon_r))
            * (math.log(b - 1.0) + 0.39 - 0.61 / epsilon_r)
        )

    u = _refine_width_ratio(u, z0, epsilon_r)
    return u * height_m


def _refine_width_ratio(u_guess: float, z0_target: float, epsilon_r: float) -> float:
    """Bisect the forward model so synthesis and analysis agree to <0.1 %."""
    lo, hi = max(u_guess * 0.2, 1e-4), u_guess * 5.0

    def z_of(u: float) -> float:
        return characteristic_impedance(epsilon_r, u, 1.0)[0]

    # Z0 decreases monotonically with width.
    if z_of(lo) < z0_target or z_of(hi) > z0_target:
        return u_guess  # target outside the bracket; keep the closed form

    for _ in range(80):
        mid = math.sqrt(lo * hi)
        if z_of(mid) > z0_target:
            lo = mid
        else:
            hi = mid
    return math.sqrt(lo * hi)


def design(
    *,
    target_impedance_ohm: float,
    epsilon_r: float,
    height_m: float,
    frequency_hz: float,
    electrical_length_deg: float | None = None,
) -> MicrostripResult:
    """Size a microstrip line, optionally to a given electrical length."""
    width = synthesise_width(target_impedance_ohm, epsilon_r, height_m)
    z0, e_eff = characteristic_impedance(epsilon_r, width, height_m)

    lambda_g = C0 / (frequency_hz * math.sqrt(e_eff))
    physical_length = None
    if electrical_length_deg is not None:
        physical_length = lambda_g * electrical_length_deg / 360.0

    warnings: list[str] = []
    u = width / height_m
    if not 0.05 <= u <= 20.0:
        warnings.append(
            f"W/h = {u:.3f} is outside the 0.05-20 range where this model is "
            "accurate; treat the result as indicative only."
        )
    if epsilon_r > 16.0:
        warnings.append(
            f"epsilon_r = {epsilon_r:g} exceeds the validated range of the "
            "Hammerstad model."
        )
    # Dispersion is ignored by the quasi-static form; flag where it starts to bite.
    f_cutoff = C0 / (4.0 * height_m * math.sqrt(epsilon_r - 1.0)) if epsilon_r > 1 else math.inf
    if frequency_hz > 0.5 * f_cutoff:
        warnings.append(
            "Frequency is high enough for dispersion to matter; the quasi-static "
            "result will read optimistic. Confirm with a full-wave run."
        )

    return MicrostripResult(
        width_m=width,
        height_m=height_m,
        epsilon_r=epsilon_r,
        epsilon_eff=e_eff,
        impedance_ohm=z0,
        guided_wavelength_m=lambda_g,
        electrical_length_deg=electrical_length_deg,
        physical_length_m=physical_length,
        warnings=warnings,
    )


def analyse(
    *, width_m: float, height_m: float, epsilon_r: float, frequency_hz: float
) -> MicrostripResult:
    """Forward direction: given a physical line, what does it do?"""
    z0, e_eff = characteristic_impedance(epsilon_r, width_m, height_m)
    return MicrostripResult(
        width_m=width_m,
        height_m=height_m,
        epsilon_r=epsilon_r,
        epsilon_eff=e_eff,
        impedance_ohm=z0,
        guided_wavelength_m=C0 / (frequency_hz * math.sqrt(e_eff)),
    )
