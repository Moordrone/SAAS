"""Component registry: what a component is, what it needs, and what is legal.

This is the part of EasyEM that a language model must never be allowed to
improvise. The registry states which parameters exist, their dimension, their
physical bounds and their defaults. The copilot may *propose* a value; the
registry decides whether it is admissible.

Adding a component means adding a schema here, not touching the API or the UI.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from .errors import UnknownComponent
from .units import Dimension


class Provenance(enum.StrEnum):
    """Where a value came from. Drives the UI and the level of trust."""

    user = "user"                        # the engineer typed it
    ai_suggested = "ai_suggested"        # proposed by the copilot, unconfirmed
    default = "default"                  # registry default
    material_database = "material_database"
    computed = "computed"                # derived by an analytical model


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    label: str
    dimension: Dimension
    required: bool = True
    default_si: float | None = None
    min_si: float | None = None
    max_si: float | None = None
    display_unit: str = ""
    description: str = ""
    # Parameters the engine can derive rather than ask for.
    derivable: bool = False
    choices: tuple[str, ...] = ()

    def in_bounds(self, si_value: float) -> bool:
        if self.min_si is not None and si_value < self.min_si:
            return False
        if self.max_si is not None and si_value > self.max_si:
            return False
        return True

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "dimension": self.dimension.value,
            "required": self.required,
            "derivable": self.derivable,
            "default_si": self.default_si,
            "min_si": self.min_si,
            "max_si": self.max_si,
            "display_unit": self.display_unit,
            "description": self.description,
            "choices": list(self.choices),
        }


@dataclass(frozen=True)
class ComponentSchema:
    key: str
    family: str
    name: str
    description: str
    parameters: tuple[ParameterSpec, ...]
    outputs: tuple[str, ...] = ()
    supports_analytical: bool = False
    #: Backends with a geometry template for this component.
    full_wave_backends: tuple[str, ...] = ()

    @property
    def by_name(self) -> dict[str, ParameterSpec]:
        return {p.name: p for p in self.parameters}

    def required_names(self) -> list[str]:
        return [p.name for p in self.parameters if p.required and not p.derivable]

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "family": self.family,
            "name": self.name,
            "description": self.description,
            "supports_analytical": self.supports_analytical,
            "full_wave_backends": list(self.full_wave_backends),
            "outputs": list(self.outputs),
            "parameters": [p.as_dict() for p in self.parameters],
        }


# Bounds that apply almost everywhere. Kept as named constants so a reviewer can
# argue with the number rather than hunt for it.
FREQ_MIN, FREQ_MAX = 1e6, 1e12            # 1 MHz .. 1 THz
LENGTH_MIN, LENGTH_MAX = 1e-9, 10.0       # 1 nm .. 10 m
EPS_MIN, EPS_MAX = 1.0, 100.0


_FREQUENCY = ParameterSpec(
    name="frequency_center",
    label="Centre frequency",
    dimension=Dimension.frequency,
    min_si=FREQ_MIN,
    max_si=FREQ_MAX,
    display_unit="GHz",
    description="Design frequency the component is tuned to.",
)

_SUBSTRATE = ParameterSpec(
    name="substrate_material",
    label="Substrate",
    dimension=Dimension.dimensionless,
    display_unit="",
    description="Substrate key from the material library.",
    choices=("FR4", "RO4003C", "RO3003", "RT5880", "ALUMINA", "AIR"),
)

_HEIGHT = ParameterSpec(
    name="substrate_height",
    label="Substrate thickness",
    dimension=Dimension.length,
    min_si=1e-6,
    max_si=0.05,
    display_unit="mm",
    description="Dielectric thickness between the trace and the ground plane.",
)


RECTANGULAR_PATCH = ComponentSchema(
    key="RectangularPatch",
    family="Antennas",
    name="Rectangular microstrip patch",
    description=(
        "Single-layer rectangular patch fed against a ground plane. The "
        "workhorse of printed antennas: cheap, flat, narrowband."
    ),
    supports_analytical=True,
    full_wave_backends=("openems",),
    outputs=("s_parameters", "radiation_pattern", "input_impedance", "gain"),
    parameters=(
        _FREQUENCY,
        _SUBSTRATE,
        _HEIGHT,
        ParameterSpec(
            name="feed_impedance",
            label="Feed impedance",
            dimension=Dimension.impedance,
            required=False,
            default_si=50.0,
            min_si=1.0,
            max_si=1000.0,
            display_unit="ohm",
            description="System impedance the feed should match.",
        ),
        ParameterSpec(
            name="patch_width",
            label="Patch width",
            dimension=Dimension.length,
            required=True,
            derivable=True,
            min_si=LENGTH_MIN,
            max_si=LENGTH_MAX,
            display_unit="mm",
            description="Derived from the frequency and substrate if not given.",
        ),
        ParameterSpec(
            name="patch_length",
            label="Patch length",
            dimension=Dimension.length,
            required=True,
            derivable=True,
            min_si=LENGTH_MIN,
            max_si=LENGTH_MAX,
            display_unit="mm",
            description="Sets the resonant frequency. Derived if not given.",
        ),
    ),
)


MICROSTRIP_LINE = ComponentSchema(
    key="MicrostripLine",
    family="Transmission lines",
    name="Microstrip transmission line",
    description="A trace over a ground plane, sized to a target impedance.",
    supports_analytical=True,
    full_wave_backends=("openems",),
    outputs=("s_parameters", "impedance", "effective_permittivity"),
    parameters=(
        _FREQUENCY,
        _SUBSTRATE,
        _HEIGHT,
        ParameterSpec(
            name="target_impedance",
            label="Target impedance",
            dimension=Dimension.impedance,
            default_si=50.0,
            min_si=1.0,
            max_si=1000.0,
            display_unit="ohm",
            description="Characteristic impedance the line should present.",
        ),
        ParameterSpec(
            name="trace_width",
            label="Trace width",
            dimension=Dimension.length,
            required=True,
            derivable=True,
            min_si=LENGTH_MIN,
            max_si=LENGTH_MAX,
            display_unit="mm",
            description="Derived from the target impedance if not given.",
        ),
        ParameterSpec(
            name="electrical_length",
            label="Electrical length",
            dimension=Dimension.angle,
            required=False,
            min_si=0.0,
            max_si=100.0,
            display_unit="deg",
            description="Optional. 90 deg gives a quarter-wave transformer.",
        ),
    ),
)


_REGISTRY: dict[str, ComponentSchema] = {
    RECTANGULAR_PATCH.key: RECTANGULAR_PATCH,
    MICROSTRIP_LINE.key: MICROSTRIP_LINE,
}


def get_component(key: str) -> ComponentSchema:
    try:
        return _REGISTRY[key]
    except KeyError:
        raise UnknownComponent(
            f"Unknown component {key!r}. Known: {', '.join(sorted(_REGISTRY))}"
        ) from None


def list_components() -> list[ComponentSchema]:
    return sorted(_REGISTRY.values(), key=lambda c: (c.family, c.name))


def families() -> list[str]:
    return sorted({c.family for c in _REGISTRY.values()})
