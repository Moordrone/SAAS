"""Validation and analytical estimation of a project definition.

The rule the whole product rests on: the copilot proposes, the engine decides.
Every value that reaches a solver has passed through `validate`, whether a human
typed it or a language model suggested it.

Validation answers three questions, in order:
  1. Is the document structurally well-formed?
  2. Is every required parameter present, and can the rest be derived?
  3. Is the result physically sensible?
"""

from __future__ import annotations

from typing import Any

from .analytical import microstrip, patch
from .components import ComponentSchema, Provenance, get_component
from .errors import Issue, UnknownComponent, UnknownMaterial, ValidationResult
from .materials import get_substrate
from .units import Quantity, is_known

SCHEMA_VERSION = "1.0.0"


def _read_parameter(raw: Any, spec) -> tuple[float | str | None, Issue | None]:
    """Pull an SI magnitude (or a plain string) out of one parameter entry."""
    path = f"parameters.{spec.name}"

    if not isinstance(raw, dict):
        return None, Issue(
            path, "malformed_parameter",
            "Expected an object with 'value' and 'unit'.",
        )

    value = raw.get("value")
    if value is None:
        return None, Issue(path, "missing_value", "No value supplied.")

    # Choice parameters (materials) are strings, not quantities.
    if spec.choices:
        if not isinstance(value, str):
            return None, Issue(path, "expected_choice", "Expected a text value.")
        if value.upper() not in {c.upper() for c in spec.choices}:
            return None, Issue(
                path, "invalid_choice",
                f"{value!r} is not one of: {', '.join(spec.choices)}.",
                suggestion=spec.choices[0],
            )
        return value.upper(), None

    unit = raw.get("unit", "")
    if not is_known(unit):
        return None, Issue(path, "unknown_unit", f"Unknown unit {unit!r}.")

    try:
        quantity = Quantity(float(value), unit)
    except (TypeError, ValueError) as exc:
        return None, Issue(path, "invalid_value", str(exc))

    if quantity.dimension is not spec.dimension:
        return None, Issue(
            path, "wrong_dimension",
            f"{spec.label} is a {spec.dimension.value}, but {unit!r} is a "
            f"{quantity.dimension.value}.",
        )

    return quantity.si_value, None


def validate(definition: dict) -> ValidationResult:
    """Check a project definition. Never raises for user error — it reports."""
    result = ValidationResult()

    version = definition.get("schema_version")
    if version != SCHEMA_VERSION:
        result.issues.append(
            Issue(
                "schema_version", "unsupported_schema_version",
                f"Expected schema version {SCHEMA_VERSION}, got {version!r}.",
            )
        )
        return result

    component = definition.get("component") or {}
    key = component.get("type")
    if not key:
        result.issues.append(
            Issue("component.type", "missing_component", "No component selected.")
        )
        return result

    try:
        schema = get_component(key)
    except UnknownComponent as exc:
        result.issues.append(Issue("component.type", "unknown_component", str(exc)))
        return result

    parameters = definition.get("parameters") or {}
    resolved: dict[str, Any] = {}

    for spec in schema.parameters:
        raw = parameters.get(spec.name)

        if raw is None:
            if spec.required and not spec.derivable and spec.default_si is None:
                result.missing.append(spec.name)
            elif spec.default_si is not None:
                resolved[spec.name] = spec.default_si
            continue

        value, issue = _read_parameter(raw, spec)
        if issue is not None:
            result.issues.append(issue)
            continue

        if isinstance(value, float) and not spec.in_bounds(value):
            result.issues.append(
                Issue(
                    f"parameters.{spec.name}", "out_of_bounds",
                    f"{spec.label} must be between {spec.min_si} and "
                    f"{spec.max_si} {spec.dimension.value} units (SI).",
                )
            )
            continue

        resolved[spec.name] = value

    # Unknown parameters are a signal that the client or the copilot is out of
    # date with the schema. Warn rather than reject: silently dropping them
    # would hide the drift.
    known = set(schema.by_name)
    for name in parameters:
        if name not in known:
            result.issues.append(
                Issue(
                    f"parameters.{name}", "unknown_parameter",
                    f"{name!r} is not a parameter of {schema.name}. It will be "
                    "ignored.",
                    severity="warning",
                )
            )

    if result.missing or result.errors:
        return result

    _check_physics(schema, resolved, result)
    return result


def _check_physics(
    schema: ComponentSchema, values: dict, result: ValidationResult
) -> None:
    """Plausibility rules that no schema constraint can express."""
    material_key = values.get("substrate_material")
    height = values.get("substrate_height")
    frequency = values.get("frequency_center")

    if not (material_key and height and frequency):
        return

    try:
        substrate = get_substrate(str(material_key))
    except UnknownMaterial as exc:
        result.issues.append(
            Issue("parameters.substrate_material", "unknown_material", str(exc))
        )
        return

    wavelength = 299_792_458.0 / frequency
    ratio = height / wavelength

    if ratio > 0.1:
        result.issues.append(
            Issue(
                "parameters.substrate_height", "substrate_too_thick",
                f"The substrate is {ratio:.1%} of a free-space wavelength. Above "
                "about 10 % the structure stops behaving like a printed circuit "
                "and analytical models break down entirely.",
            )
        )
    elif ratio > 0.05:
        result.issues.append(
            Issue(
                "parameters.substrate_height", "thick_substrate",
                f"h/lambda0 = {ratio:.3f}. Surface waves and feed inductance will "
                "shift the real resonance; the analytical estimate will read "
                "optimistic.",
                severity="warning",
                suggestion="Use a thinner substrate or verify with a full-wave run.",
            )
        )

    if frequency > 5.0 * substrate.reference_frequency_hz:
        result.issues.append(
            Issue(
                "parameters.substrate_material", "material_out_of_band",
                f"{substrate.name} is characterised at "
                f"{substrate.reference_frequency_hz / 1e9:g} GHz but you are "
                f"designing at {frequency / 1e9:g} GHz. Permittivity and loss "
                "both drift with frequency.",
                severity="warning",
            )
        )

    if substrate.key == "FR4" and frequency > 3e9:
        result.issues.append(
            Issue(
                "parameters.substrate_material", "fr4_above_3ghz",
                "FR-4 loss rises steeply above ~3 GHz and its permittivity varies "
                "between batches. Fine for a prototype, risky for a product.",
                severity="warning",
                suggestion="RO4003C",
            )
        )


def estimate(definition: dict) -> dict:
    """Run the analytical model and return derived dimensions and performance.

    This is what lets a customer get a real answer in milliseconds. It is an
    approximation with stated validity limits, not a substitute for a full-wave
    solve — and the warnings it carries say so.
    """
    validation = validate(definition)
    if not validation.is_valid:
        return {"ok": False, "validation": validation.as_dict()}

    schema = get_component(definition["component"]["type"])
    values: dict[str, Any] = {}
    for spec in schema.parameters:
        raw = (definition.get("parameters") or {}).get(spec.name)
        if raw is None:
            if spec.default_si is not None:
                values[spec.name] = spec.default_si
            continue
        value, issue = _read_parameter(raw, spec)
        if issue is None:
            values[spec.name] = value

    substrate = get_substrate(str(values["substrate_material"]))
    frequency = values["frequency_center"]
    height = values["substrate_height"]

    if schema.key == "RectangularPatch":
        model = patch.design(
            frequency_hz=frequency,
            epsilon_r=substrate.epsilon_r,
            height_m=height,
            feed_impedance_ohm=values.get("feed_impedance", 50.0),
            loss_tangent=substrate.loss_tangent,
        )
        derived = {
            "patch_width": model.width_m,
            "patch_length": model.length_m,
        }
        payload = model.as_dict()

    elif schema.key == "MicrostripLine":
        electrical_length = values.get("electrical_length")
        model = microstrip.design(
            target_impedance_ohm=values.get("target_impedance", 50.0),
            epsilon_r=substrate.epsilon_r,
            height_m=height,
            frequency_hz=frequency,
            electrical_length_deg=(
                electrical_length * 180.0 / 3.141592653589793
                if electrical_length is not None
                else None
            ),
        )
        derived = {"trace_width": model.width_m}
        payload = model.as_dict()

    else:  # pragma: no cover - registry and estimator kept in step by a test
        return {
            "ok": False,
            "error": f"No analytical model for {schema.key}",
        }

    return {
        "ok": True,
        "model": "analytical",
        "component": schema.key,
        "substrate": {
            "key": substrate.key,
            "name": substrate.name,
            "epsilon_r": substrate.epsilon_r,
            "loss_tangent": substrate.loss_tangent,
        },
        "derived_parameters": {
            name: {
                "value": value,
                "unit": "m",
                "provenance": Provenance.computed.value,
            }
            for name, value in derived.items()
        },
        "results": payload,
        "validation": validation.as_dict(),
    }
