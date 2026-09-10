"""Substrate and conductor library.

Values are datasheet figures at a stated reference frequency. Permittivity and
loss tangent both drift with frequency and temperature, so `reference_frequency`
is carried explicitly: quoting FR-4 as "4.4" without saying at what frequency is
how a design misses its band.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import UnknownMaterial


@dataclass(frozen=True)
class Substrate:
    key: str
    name: str
    epsilon_r: float
    loss_tangent: float
    reference_frequency_hz: float
    manufacturer: str | None = None
    # Datasheet thicknesses in metres, for suggesting a plausible default.
    standard_thicknesses_m: tuple[float, ...] = ()
    notes: str | None = None


@dataclass(frozen=True)
class Conductor:
    key: str
    name: str
    conductivity_s_per_m: float


SUBSTRATES: dict[str, Substrate] = {
    "FR4": Substrate(
        key="FR4",
        name="FR-4 (generic)",
        epsilon_r=4.4,
        loss_tangent=0.02,
        reference_frequency_hz=1e9,
        standard_thicknesses_m=(0.2e-3, 0.4e-3, 0.8e-3, 1.6e-3, 2.4e-3),
        notes=(
            "Cheap and ubiquitous, but epsilon_r varies by several percent "
            "between batches and loss rises steeply above ~3 GHz."
        ),
    ),
    "RO4003C": Substrate(
        key="RO4003C",
        name="Rogers RO4003C",
        epsilon_r=3.38,
        loss_tangent=0.0027,
        reference_frequency_hz=10e9,
        manufacturer="Rogers Corporation",
        standard_thicknesses_m=(0.203e-3, 0.305e-3, 0.508e-3, 0.813e-3, 1.524e-3),
    ),
    "RO3003": Substrate(
        key="RO3003",
        name="Rogers RO3003",
        epsilon_r=3.00,
        loss_tangent=0.0010,
        reference_frequency_hz=10e9,
        manufacturer="Rogers Corporation",
        standard_thicknesses_m=(0.127e-3, 0.254e-3, 0.508e-3, 0.762e-3, 1.524e-3),
    ),
    "RT5880": Substrate(
        key="RT5880",
        name="Rogers RT/duroid 5880",
        epsilon_r=2.20,
        loss_tangent=0.0009,
        reference_frequency_hz=10e9,
        manufacturer="Rogers Corporation",
        standard_thicknesses_m=(0.127e-3, 0.254e-3, 0.508e-3, 0.787e-3, 1.575e-3),
        notes="Low loss and very stable; the usual choice above 10 GHz.",
    ),
    "ALUMINA": Substrate(
        key="ALUMINA",
        name="Alumina 99.6%",
        epsilon_r=9.8,
        loss_tangent=0.0001,
        reference_frequency_hz=10e9,
        standard_thicknesses_m=(0.254e-3, 0.508e-3, 0.635e-3),
    ),
    "AIR": Substrate(
        key="AIR",
        name="Air",
        epsilon_r=1.0006,
        loss_tangent=0.0,
        reference_frequency_hz=1e9,
    ),
}

CONDUCTORS: dict[str, Conductor] = {
    "COPPER": Conductor("COPPER", "Copper (annealed)", 5.8e7),
    "SILVER": Conductor("SILVER", "Silver", 6.3e7),
    "GOLD": Conductor("GOLD", "Gold", 4.1e7),
    "ALUMINIUM": Conductor("ALUMINIUM", "Aluminium", 3.77e7),
    "PEC": Conductor("PEC", "Perfect electric conductor", float("inf")),
}


def get_substrate(key: str) -> Substrate:
    try:
        return SUBSTRATES[key.upper()]
    except KeyError:
        raise UnknownMaterial(
            f"Unknown substrate {key!r}. Known: {', '.join(sorted(SUBSTRATES))}"
        ) from None


def get_conductor(key: str) -> Conductor:
    try:
        return CONDUCTORS[key.upper()]
    except KeyError:
        raise UnknownMaterial(
            f"Unknown conductor {key!r}. Known: {', '.join(sorted(CONDUCTORS))}"
        ) from None


def nearest_standard_thickness(substrate: Substrate, target_m: float) -> float | None:
    """Snap a computed thickness onto something a fabricator actually stocks."""
    if not substrate.standard_thicknesses_m:
        return None
    return min(substrate.standard_thicknesses_m, key=lambda t: abs(t - target_m))


def list_substrates() -> list[Substrate]:
    return sorted(SUBSTRATES.values(), key=lambda s: s.epsilon_r)
