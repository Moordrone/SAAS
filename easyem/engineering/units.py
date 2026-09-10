"""Strict unit handling.

Everything is stored in SI. Display units are a presentation concern and never
touch the database. Mixing GHz and mm inside the stored document is how a
factor-of-1000 error reaches production and is only noticed by a customer.

A Quantity always knows its dimension, so `2.45 GHz` cannot be assigned to a
length parameter: the conversion table is keyed by dimension, not by string.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal

from .errors import UnitError


class Dimension(enum.StrEnum):
    frequency = "frequency"
    length = "length"
    angle = "angle"
    conductivity = "conductivity"
    impedance = "impedance"
    power_ratio = "power_ratio"      # dB, dimensionless ratios in log scale
    dimensionless = "dimensionless"
    time = "time"


# unit -> (dimension, factor to SI). Only exact, unambiguous factors here.
_UNITS: dict[str, tuple[Dimension, float]] = {
    # frequency, SI: Hz
    "Hz": (Dimension.frequency, 1.0),
    "kHz": (Dimension.frequency, 1e3),
    "MHz": (Dimension.frequency, 1e6),
    "GHz": (Dimension.frequency, 1e9),
    "THz": (Dimension.frequency, 1e12),
    # length, SI: m
    "m": (Dimension.length, 1.0),
    "cm": (Dimension.length, 1e-2),
    "mm": (Dimension.length, 1e-3),
    "um": (Dimension.length, 1e-6),
    "nm": (Dimension.length, 1e-9),
    "mil": (Dimension.length, 2.54e-5),
    "inch": (Dimension.length, 2.54e-2),
    # angle, SI: rad
    "rad": (Dimension.angle, 1.0),
    "deg": (Dimension.angle, 0.017453292519943295),
    # conductivity, SI: S/m
    "S/m": (Dimension.conductivity, 1.0),
    "MS/m": (Dimension.conductivity, 1e6),
    # impedance, SI: ohm
    "ohm": (Dimension.impedance, 1.0),
    # time, SI: s
    "s": (Dimension.time, 1.0),
    "ms": (Dimension.time, 1e-3),
    "us": (Dimension.time, 1e-6),
    "ns": (Dimension.time, 1e-9),
    "ps": (Dimension.time, 1e-12),
    # ratios
    "dB": (Dimension.power_ratio, 1.0),
    "1": (Dimension.dimensionless, 1.0),
    "": (Dimension.dimensionless, 1.0),
}

SI_UNIT: dict[Dimension, str] = {
    Dimension.frequency: "Hz",
    Dimension.length: "m",
    Dimension.angle: "rad",
    Dimension.conductivity: "S/m",
    Dimension.impedance: "ohm",
    Dimension.time: "s",
    Dimension.power_ratio: "dB",
    Dimension.dimensionless: "1",
}


def dimension_of(unit: str) -> Dimension:
    try:
        return _UNITS[unit][0]
    except KeyError:
        raise UnitError(f"Unknown unit {unit!r}") from None


def is_known(unit: str) -> bool:
    return unit in _UNITS


@dataclass(frozen=True)
class Quantity:
    """A number with a unit. `si_value` is the single source of truth."""

    value: float
    unit: str

    def __post_init__(self) -> None:
        if not is_known(self.unit):
            raise UnitError(f"Unknown unit {self.unit!r}")

    @property
    def dimension(self) -> Dimension:
        return _UNITS[self.unit][0]

    @property
    def si_value(self) -> float:
        return self.value * _UNITS[self.unit][1]

    def to(self, unit: str) -> Quantity:
        if not is_known(unit):
            raise UnitError(f"Unknown unit {unit!r}")
        target_dim, factor = _UNITS[unit]
        if target_dim is not self.dimension:
            raise UnitError(
                f"Cannot convert {self.dimension.value} ({self.unit}) "
                f"to {target_dim.value} ({unit})"
            )
        return Quantity(self.si_value / factor, unit)

    def to_si(self) -> Quantity:
        return Quantity(self.si_value, SI_UNIT[self.dimension])

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.value:g} {self.unit}"


def to_si(value: float | int | str | Decimal, unit: str) -> float:
    """Convenience: convert a raw value+unit pair to its SI magnitude."""
    return Quantity(float(value), unit).si_value


def humanise(si_value: float, dimension: Dimension) -> Quantity:
    """Pick a readable display unit. Presentation only — never stored.

    Chooses the largest prefix that keeps the magnitude at or above 1, so
    2.45e9 Hz renders as 2.45 GHz rather than 2450000000 Hz.
    """
    ladders: dict[Dimension, list[str]] = {
        Dimension.frequency: ["THz", "GHz", "MHz", "kHz", "Hz"],
        Dimension.length: ["m", "cm", "mm", "um", "nm"],
        Dimension.time: ["s", "ms", "us", "ns", "ps"],
    }
    ladder = ladders.get(dimension)
    if not ladder:
        return Quantity(si_value, SI_UNIT[dimension])

    for unit in ladder:
        candidate = Quantity(si_value, SI_UNIT[dimension]).to(unit)
        if abs(candidate.value) >= 1:
            return candidate
    return Quantity(si_value, ladder[-1])
