"""Canonical form and content hashing of a project definition.

Two definitions that describe the same physical problem must produce the same
hash, whatever order their keys arrived in or which display unit the client
happened to use. That is what makes the hash worth anything: it is a statement
about the physics, not about the JSON.

Canonicalisation therefore does three things:
  * converts every quantity to SI, dropping the display unit
  * sorts keys
  * drops annotations that carry no physical meaning (provenance, labels)
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..engineering.units import Quantity, is_known

SCHEMA_VERSION = "1.0.0"

# Fields that describe how a value was obtained, not what it is. They belong in
# the stored document — the UI needs them — but not in the identity of the
# problem: a length is the same length whether a human or a model proposed it.
_NON_PHYSICAL_KEYS = {"provenance", "source", "status", "confidence", "label", "note"}


def canonicalise(definition: dict) -> dict:
    """Return the SI-normalised, annotation-free form of a definition."""
    component = definition.get("component") or {}
    parameters = definition.get("parameters") or {}

    canon_params: dict[str, Any] = {}
    for name, raw in sorted(parameters.items()):
        if not isinstance(raw, dict):
            canon_params[name] = raw
            continue

        value = raw.get("value")
        unit = raw.get("unit", "")

        if isinstance(value, (int, float)) and is_known(unit):
            quantity = Quantity(float(value), unit)
            canon_params[name] = {
                "value": _round_significant(quantity.si_value),
                "unit": quantity.to_si().unit,
            }
        else:
            canon_params[name] = {
                k: v for k, v in sorted(raw.items()) if k not in _NON_PHYSICAL_KEYS
            }

    canon: dict[str, Any] = {
        "schema_version": definition.get("schema_version"),
        "component": {
            "family": component.get("family"),
            "type": component.get("type"),
        },
        "parameters": canon_params,
    }

    for optional in ("mesh", "solver", "outputs"):
        if definition.get(optional) is not None:
            canon[optional] = _sort_deep(definition[optional])

    return canon


def _round_significant(value: float, digits: int = 12) -> float:
    """Absorb float noise so 2.45 GHz and 2450 MHz hash identically.

    Without this, 2.45e9 and 2450.0 * 1e6 differ in the last bits and produce
    two hashes for one design.
    """
    if value == 0.0:
        return 0.0
    return float(f"%.{digits}g" % value)


def _sort_deep(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sort_deep(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [_sort_deep(v) for v in obj]
    return obj


def content_hash(definition: dict) -> str:
    """Stable identity of a physical problem, prefixed with its algorithm."""
    payload = json.dumps(
        canonicalise(definition),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def empty_definition(component_type: str, family: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "component": {"family": family, "type": component_type},
        "parameters": {},
    }


def set_parameter(
    definition: dict,
    name: str,
    value: float | str,
    unit: str = "",
    *,
    provenance: str = "user",
) -> dict:
    """Return a copy with one parameter set. Definitions are never mutated."""
    updated = json.loads(json.dumps(definition))
    updated.setdefault("parameters", {})[name] = {
        "value": value,
        "unit": unit,
        "provenance": provenance,
    }
    return updated
