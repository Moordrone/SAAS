"""Generate a standalone openEMS script from a Project JSON definition.

This module is the actual value of the openEMS integration. Anyone can install
openEMS; what an engineer spends an afternoon on is writing the geometry, mesh
and port script for each new design. That translation is what EasyEM sells.

**Licensing.** openEMS is GPL v3. The generated script runs as a separate
process and communicates through files — nothing from openEMS is imported into
this codebase, and nothing in this codebase is linked into it. That is the
standard arm's-length boundary. It is also why `openems/` contains no openEMS
import anywhere: the separation is structural, not a matter of discipline.
Have a lawyer confirm the position before selling, not after.

The generated script writes `results.json` and exits. It is deliberately plain,
readable Python: a customer can download it, read it, and run it themselves.
That is a feature, not a leak — it is what makes the results checkable, and
checkability is what sells a simulation tool to sceptical engineers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ...engineering.analytical import microstrip, patch
from ...engineering.materials import get_substrate
from ...engineering.units import Quantity, is_known
from .mesh import MeshPlan, plan_mesh

SUPPORTED = {"RectangularPatch", "MicrostripLine"}


class UnsupportedGeometry(Exception):
    pass


def _si(definition: dict, name: str, default=None):
    raw = (definition.get("parameters") or {}).get(name)
    if raw is None:
        return default
    value, unit = raw.get("value"), raw.get("unit", "")
    if isinstance(value, str):
        return value
    return Quantity(float(value), unit).si_value if is_known(unit) else default


@dataclass
class GeneratedRun:
    """A script plus the metadata needed to price and interpret it."""

    script: str
    plan: MeshPlan
    geometry: dict
    component: str


# --------------------------------------------------------------------------- #

def build(definition: dict, *, accuracy: str = "standard") -> GeneratedRun:
    component = (definition.get("component") or {}).get("type")
    if component not in SUPPORTED:
        raise UnsupportedGeometry(
            f"openEMS backend has no geometry template for {component!r}"
        )

    substrate = get_substrate(str(_si(definition, "substrate_material")))
    f0 = _si(definition, "frequency_center")
    height = _si(definition, "substrate_height")

    mesh_block = (definition.get("mesh") or {})
    accuracy = mesh_block.get("accuracy", accuracy)

    if component == "RectangularPatch":
        return _patch_run(definition, substrate, f0, height, accuracy)
    return _line_run(definition, substrate, f0, height, accuracy)


# --------------------------------------------------------------------------- #
# rectangular patch
# --------------------------------------------------------------------------- #

def _patch_run(definition, substrate, f0, height, accuracy) -> GeneratedRun:
    feed_z = _si(definition, "feed_impedance", 50.0) or 50.0

    # The analytical model supplies the starting dimensions and, crucially, the
    # Q factor that sets how long the run has to be. Using the engine to seed
    # the full-wave run is the whole point of having built it first.
    model = patch.design(
        frequency_hz=f0,
        epsilon_r=substrate.epsilon_r,
        height_m=height,
        feed_impedance_ohm=feed_z,
        loss_tangent=substrate.loss_tangent,
    )

    width = _si(definition, "patch_width") or model.width_m
    length = _si(definition, "patch_length") or model.length_m
    inset = model.inset_feed_offset_m or 0.0
    # Feed sits on the centre line, offset from the radiating edge by the inset.
    feed_y = -(length / 2.0 - inset)

    sub_w = model.ground_plane_width_m
    sub_l = model.ground_plane_length_m

    q = 0.707 / model.bandwidth_fraction if model.bandwidth_fraction > 0 else 100.0
    plan = plan_mesh(
        f0_hz=f0,
        bandwidth_fraction=0.6,
        epsilon_r=substrate.epsilon_r,
        structure_extent_m=(sub_w, sub_l, height),
        substrate_height_m=height,
        accuracy=accuracy,
        quality_factor=q,
    )

    geometry = {
        "patch_width_mm": width * 1e3,
        "patch_length_mm": length * 1e3,
        "substrate_width_mm": sub_w * 1e3,
        "substrate_length_mm": sub_l * 1e3,
        "substrate_thickness_mm": height * 1e3,
        "feed_y_mm": feed_y * 1e3,
        "feed_impedance_ohm": feed_z,
        "epsilon_r": substrate.epsilon_r,
        "loss_tangent": substrate.loss_tangent,
        "inset_mm": inset * 1e3,
    }

    context = {
        **geometry,
        "f0": f0,
        "fc": plan.fc_hz,
        "timesteps": plan.timesteps,
        "resolution_mm": plan.resolution_m * 1e3,
        "edge_resolution_mm": plan.metal_edge_resolution_m * 1e3,
        "substrate_cells": plan.substrate_cells,
        "frequency_points": plan.frequency_points,
        "domain_mm": [round(d * 1e3, 3) for d in plan.domain_m],
    }
    return GeneratedRun(
        script=_render(_PATCH_TEMPLATE, context),
        plan=plan,
        geometry=geometry,
        component="RectangularPatch",
    )


# --------------------------------------------------------------------------- #
# microstrip line
# --------------------------------------------------------------------------- #

def _line_run(definition, substrate, f0, height, accuracy) -> GeneratedRun:
    target = _si(definition, "target_impedance", 50.0) or 50.0
    electrical = _si(definition, "electrical_length")
    electrical_deg = (
        electrical * 180.0 / 3.141592653589793 if electrical is not None else 90.0
    )

    model = microstrip.design(
        target_impedance_ohm=target,
        epsilon_r=substrate.epsilon_r,
        height_m=height,
        frequency_hz=f0,
        electrical_length_deg=electrical_deg,
    )
    trace_w = _si(definition, "trace_width") or model.width_m
    length = model.physical_length_m or model.guided_wavelength_m / 4.0

    # Ports need feed line either side, and the substrate must be wide enough
    # that the ground return is not squeezed.
    port_length = model.guided_wavelength_m / 4.0
    total_length = length + 2.0 * port_length
    sub_w = max(8.0 * trace_w, 6.0 * height)

    plan = plan_mesh(
        f0_hz=f0,
        bandwidth_fraction=0.6,
        epsilon_r=substrate.epsilon_r,
        structure_extent_m=(sub_w, total_length, height),
        substrate_height_m=height,
        accuracy=accuracy,
        quality_factor=20.0,  # a matched line settles fast
    )

    geometry = {
        "trace_width_mm": trace_w * 1e3,
        "line_length_mm": length * 1e3,
        "port_length_mm": port_length * 1e3,
        "total_length_mm": total_length * 1e3,
        "substrate_width_mm": sub_w * 1e3,
        "substrate_thickness_mm": height * 1e3,
        "epsilon_r": substrate.epsilon_r,
        "loss_tangent": substrate.loss_tangent,
        "target_impedance_ohm": target,
    }
    context = {
        **geometry,
        "f0": f0,
        "fc": plan.fc_hz,
        "timesteps": plan.timesteps,
        "resolution_mm": plan.resolution_m * 1e3,
        "substrate_cells": plan.substrate_cells,
        "frequency_points": plan.frequency_points,
    }
    return GeneratedRun(
        script=_render(_LINE_TEMPLATE, context),
        plan=plan,
        geometry=geometry,
        component="MicrostripLine",
    )


# --------------------------------------------------------------------------- #

def _render(template: str, context: dict) -> str:
    """Substitute @@name@@ placeholders.

    Deliberately not str.format or f-strings: the templates are full of braces
    and brackets, and escaping every one of them makes the physics unreadable.
    """
    out = template
    for key, value in context.items():
        rendered = json.dumps(value) if isinstance(value, list) else repr(value)
        out = out.replace(f"@@{key}@@", rendered)
    if "@@" in out:
        leftover = out[out.index("@@"): out.index("@@") + 40]
        raise ValueError(f"Unsubstituted placeholder in template: {leftover!r}")
    return out


_HEADER = '''\
"""openEMS run generated by EasyEM. Standalone: read it, edit it, run it.

Requires openEMS (GPL v3) and CSXCAD. EasyEM invokes this file as a separate
process and reads results.json; nothing here is imported into EasyEM.

    python3 run.py <output_directory>
"""

import json
import os
import sys

import numpy as np
from CSXCAD import ContinuousStructure
from openEMS import openEMS
from openEMS.physical_constants import C0, EPS0

OUT = sys.argv[1] if len(sys.argv) > 1 else "."
SIM = os.path.join(OUT, "sim")
UNIT = 1e-3  # all dimensions below are in millimetres


def write(payload):
    payload["solver"] = "openEMS"
    with open(os.path.join(OUT, "results.json"), "w") as fh:
        json.dump(payload, fh)


def progress(fraction, message=""):
    """Coarse progress for the platform to poll. openEMS reports to stdout;
    this file is what EasyEM watches. Defined here, before any call to it."""
    with open(os.path.join(OUT, "progress.json"), "w") as fh:
        json.dump({"progress": round(float(fraction) * 100, 1),
                   "message": message}, fh)


def fail(message):
    """Record a crash the platform can read, then exit non-zero.

    Without this a solver exception dies on stderr and the adapter can only
    report a generic SOLVER_INTERNAL. Writing the reason lets the customer see
    what actually went wrong."""
    import traceback
    with open(os.path.join(OUT, "error.json"), "w") as fh:
        json.dump({"error": str(message),
                   "traceback": traceback.format_exc()}, fh)
    sys.exit(1)
'''

_FOOTER = ''''''


_PATCH_TEMPLATE = _HEADER + '''
try:

    f0 = @@f0@@
    fc = @@fc@@

    patch_w = @@patch_width_mm@@
    patch_l = @@patch_length_mm@@
    sub_w = @@substrate_width_mm@@
    sub_l = @@substrate_length_mm@@
    sub_h = @@substrate_thickness_mm@@
    eps_r = @@epsilon_r@@
    tan_d = @@loss_tangent@@
    feed_y = @@feed_y_mm@@
    feed_R = @@feed_impedance_ohm@@

    res = @@resolution_mm@@
    edge_res = @@edge_resolution_mm@@
    sub_cells = @@substrate_cells@@

    # Dielectric loss enters openEMS as a conductivity at the design frequency.
    kappa = tan_d * 2 * np.pi * f0 * EPS0 * eps_r

    FDTD = openEMS(NrTS=@@timesteps@@, EndCriteria=1e-4)
    FDTD.SetGaussExcite(f0, fc)
    # MUR absorbing boundaries, with the ground plane backing the structure.
    FDTD.SetBoundaryCond(["MUR"] * 6)

    CSX = ContinuousStructure()
    FDTD.SetCSX(CSX)
    mesh = CSX.GetGrid()
    mesh.SetDeltaUnit(UNIT)

    # --- substrate ------------------------------------------------------------
    substrate = CSX.AddMaterial("substrate", epsilon=eps_r, kappa=kappa)
    substrate.AddBox(
        priority=0,
        start=[-sub_w / 2, -sub_l / 2, 0],
        stop=[sub_w / 2, sub_l / 2, sub_h],
    )
    # Explicit lines through the substrate: the fringing field sets the resonant
    # length, so under-resolving the thickness moves the answer.
    mesh.AddLine("z", np.linspace(0, sub_h, sub_cells + 1))

    # --- ground plane ---------------------------------------------------------
    gnd = CSX.AddMetal("gnd")
    gnd.AddBox(
        priority=10,
        start=[-sub_w / 2, -sub_l / 2, 0],
        stop=[sub_w / 2, sub_l / 2, 0],
    )

    # --- patch ----------------------------------------------------------------
    patch = CSX.AddMetal("patch")
    patch.AddBox(
        priority=10,
        start=[-patch_w / 2, -patch_l / 2, sub_h],
        stop=[patch_w / 2, patch_l / 2, sub_h],
    )
    # Thirds rule on the metal edges: the current density is singular there, and a
    # symmetric line straddling the edge resolves it badly.
    FDTD.AddEdges2Grid(dirs="xy", properties=patch, metal_edge_res=edge_res)
    FDTD.AddEdges2Grid(dirs="xy", properties=gnd)

    # --- feed -----------------------------------------------------------------
    port = FDTD.AddLumpedPort(
        1, feed_R,
        [0, feed_y, 0], [0, feed_y, sub_h],
        "z", 1.0, priority=5, edges2grid="xy",
    )

    # --- air box and smoothing -----------------------------------------------
    lambda0_mm = C0 / f0 / UNIT
    margin = lambda0_mm / 4
    mesh.AddLine("x", [-sub_w / 2 - margin, sub_w / 2 + margin])
    mesh.AddLine("y", [-sub_l / 2 - margin, sub_l / 2 + margin])
    mesh.AddLine("z", [-margin, sub_h + margin])
    mesh.SmoothMeshLines("all", res, 1.4)

    nf2ff = FDTD.CreateNF2FFBox()

    progress(0.05, "meshing")
    FDTD.Run(SIM, verbose=0, cleanup=True)
    progress(0.85, "post-processing")

    freq = np.linspace(max(1e6, f0 - fc), f0 + fc, @@frequency_points@@)
    port.CalcPort(SIM, freq)

    s11 = port.uf_ref / port.uf_inc
    z_in = port.uf_tot / port.if_tot

    # Radiation pattern at the frequency of best match, not at the nominal design
    # frequency: a fabricated patch resonates where it resonates.
    idx = int(np.argmin(np.abs(s11)))
    f_res = float(freq[idx])

    theta = np.arange(-90, 91, 2.0)
    nf = nf2ff.CalcNF2FF(SIM, f_res, theta, [0.0, 90.0], center=[0, 0, sub_h / 2])
    e_norm = nf.E_norm[0]
    peak = float(np.max(e_norm)) or 1.0
    e_plane = 20 * np.log10(np.maximum(e_norm[:, 0] / peak, 1e-5))
    h_plane = 20 * np.log10(np.maximum(e_norm[:, 1] / peak, 1e-5))

    write({
        "frequencies_hz": freq.tolist(),
        "s_parameters": {
            "S11": [{"re": float(v.real), "im": float(v.imag)} for v in s11]
        },
        "input_impedance_ohm": [
            {"re": float(v.real), "im": float(v.imag)} for v in z_in
        ],
        "radiation_pattern": {
            "theta_deg": theta.tolist(),
            "e_plane_db": e_plane.tolist(),
            "h_plane_db": h_plane.tolist(),
            "peak_directivity_dbi": float(10 * np.log10(nf.Dmax[0])),
        },
        "scalars": {
            "resonant_frequency_hz": f_res,
            "return_loss_db": float(20 * np.log10(abs(s11[idx]))),
            "directivity_dbi": float(10 * np.log10(nf.Dmax[0])),
            "radiated_power_w": float(nf.Prad[0]),
            "patch_width_m": patch_w * UNIT,
            "patch_length_m": patch_l * UNIT,
        },
    })
    progress(1.0, "done")

except Exception as _exc:
    fail(_exc)
''' + _FOOTER


_LINE_TEMPLATE = _HEADER + '''
try:

    f0 = @@f0@@
    fc = @@fc@@

    trace_w = @@trace_width_mm@@
    line_l = @@line_length_mm@@
    port_l = @@port_length_mm@@
    total_l = @@total_length_mm@@
    sub_w = @@substrate_width_mm@@
    sub_h = @@substrate_thickness_mm@@
    eps_r = @@epsilon_r@@
    tan_d = @@loss_tangent@@
    z_ref = @@target_impedance_ohm@@

    res = @@resolution_mm@@
    sub_cells = @@substrate_cells@@
    kappa = tan_d * 2 * np.pi * f0 * EPS0 * eps_r

    FDTD = openEMS(NrTS=@@timesteps@@, EndCriteria=1e-5)
    FDTD.SetGaussExcite(f0, fc)
    # PEC on the y faces so the ports terminate cleanly; MUR elsewhere.
    FDTD.SetBoundaryCond(["PML_8", "PML_8", "MUR", "MUR", "PEC", "MUR"])

    CSX = ContinuousStructure()
    FDTD.SetCSX(CSX)
    mesh = CSX.GetGrid()
    mesh.SetDeltaUnit(UNIT)

    substrate = CSX.AddMaterial("substrate", epsilon=eps_r, kappa=kappa)
    substrate.AddBox(
        priority=0,
        start=[-sub_w / 2, -total_l / 2, 0],
        stop=[sub_w / 2, total_l / 2, sub_h],
    )
    mesh.AddLine("z", np.linspace(0, sub_h, sub_cells + 1))

    # Ground plane is the z=0 PEC boundary, so no explicit box is needed.
    mesh.AddLine("x", [-sub_w / 2, sub_w / 2])
    mesh.AddLine("y", [-total_l / 2, total_l / 2])

    # Two microstrip ports, fed inward from each end.
    port_in = FDTD.AddMSLPort(
        1, CSX.AddMetal("msl1"),
        [-trace_w / 2, -total_l / 2, sub_h], [trace_w / 2, -total_l / 2 + port_l, sub_h],
        "y", "z", excite=-1, FeedShift=10 * res, MeasPlaneShift=port_l / 3,
        priority=10,
    )
    port_out = FDTD.AddMSLPort(
        2, CSX.AddMetal("msl2"),
        [-trace_w / 2, total_l / 2, sub_h], [trace_w / 2, total_l / 2 - port_l, sub_h],
        "y", "z", MeasPlaneShift=port_l / 3, priority=10,
    )

    line = CSX.AddMetal("line")
    line.AddBox(
        priority=10,
        start=[-trace_w / 2, -total_l / 2 + port_l, sub_h],
        stop=[trace_w / 2, total_l / 2 - port_l, sub_h],
    )
    FDTD.AddEdges2Grid(dirs="xy", properties=line, metal_edge_res=res / 2)

    lambda0_mm = C0 / f0 / UNIT
    mesh.AddLine("z", [sub_h + lambda0_mm / 4])
    mesh.SmoothMeshLines("all", res, 1.4)

    progress(0.05, "meshing")
    FDTD.Run(SIM, verbose=0, cleanup=True)
    progress(0.85, "post-processing")

    freq = np.linspace(max(1e6, f0 - fc), f0 + fc, @@frequency_points@@)
    for p in (port_in, port_out):
        p.CalcPort(SIM, freq, ref_impedance=z_ref)

    s11 = port_in.uf_ref / port_in.uf_inc
    s21 = port_out.uf_ref / port_in.uf_inc

    write({
        "frequencies_hz": freq.tolist(),
        "s_parameters": {
            "S11": [{"re": float(v.real), "im": float(v.imag)} for v in s11],
            "S21": [{"re": float(v.real), "im": float(v.imag)} for v in s21],
        },
        "scalars": {
            "characteristic_impedance_ohm": float(np.real(port_in.ZL_ref)),
            "trace_width_m": trace_w * UNIT,
            "physical_length_m": line_l * UNIT,
            "insertion_loss_db": float(-20 * np.log10(abs(s21[len(s21) // 2]))),
        },
    })
    progress(1.0, "done")

except Exception as _exc:
    fail(_exc)
''' + _FOOTER
