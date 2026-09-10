"""Mesh planning and cost estimation for a structured-grid time-domain solver.

Written for openEMS but deliberately method-agnostic: FDTD and TLM share the
same cost structure — a Cartesian grid, a Courant-limited timestep, and a run
length set by how long the structure rings. EMG-TLM will reuse this module
rather than reimplement it.

Why this matters commercially: the platform must quote a ceiling *before* the
run starts, and must never charge above it. A frequency-domain solver with
adaptive meshing cannot be estimated honestly; a structured time-domain solver
can, because cell count follows from geometry and resolution alone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

C0 = 299_792_458.0

#: Cells per wavelength in the densest dielectric. 20 is the usual working
#: value; below ~15 numerical dispersion starts shifting resonances.
DEFAULT_CELLS_PER_WAVELENGTH = {"draft": 12, "standard": 20, "high": 30}

#: Cells through the substrate thickness. Too few and the fringing field —
#: which sets the resonant length — is badly resolved.
SUBSTRATE_CELLS = {"draft": 3, "standard": 4, "high": 6}

#: Absorbing boundaries need roughly a quarter wavelength of air around the
#: structure, or reflections contaminate the result.
BOUNDARY_MARGIN_WAVELENGTHS = 0.25

#: Measured openEMS throughput on one modern core, in millions of cell-updates
#: per second. Conservative on purpose: an estimate that reads low produces a
#: ceiling the run then exceeds, which is exactly the promise we must not break.
CELL_UPDATES_PER_SECOND = 25e6

#: Hard ceiling on run length. Beyond this the job is too expensive to be worth
#: running at all; the caller is told rather than given a truncated result.
MAX_TIMESTEPS = 200_000

#: Measured openEMS footprint per cell: six field components plus material
#: coefficients and index arrays.
BYTES_PER_CELL = 100

#: Interpreter, numpy and openEMS load before a single cell is allocated.
BASE_PROCESS_MB = 150


@dataclass
class MeshPlan:
    """Everything needed to size, price and generate a run."""

    resolution_m: float
    cells_x: int
    cells_y: int
    cells_z: int
    timesteps: int
    domain_m: tuple[float, float, float]
    substrate_cells: int
    metal_edge_resolution_m: float
    frequency_points: int
    f0_hz: float
    fc_hz: float
    #: True when the run hits MAX_TIMESTEPS before the structure settles.
    truncated: bool = False

    @property
    def cell_count(self) -> int:
        return self.cells_x * self.cells_y * self.cells_z

    @property
    def cell_updates(self) -> float:
        return float(self.cell_count) * self.timesteps

    @property
    def estimated_seconds(self) -> float:
        return self.cell_updates / CELL_UPDATES_PER_SECOND

    @property
    def estimated_memory_mb(self) -> float:
        """Six field components plus per-cell material coefficients, and the
        interpreter's own footprint.

        Counting only the fields (24 bytes/cell) understates openEMS by roughly
        4x. That matters for scheduling: a worker sized from the field count
        alone gets OOM-killed, which then bills the customer for a crash the
        platform caused."""
        return BASE_PROCESS_MB + self.cell_count * BYTES_PER_CELL / 1e6

    def as_dict(self) -> dict:
        return {
            "resolution_m": self.resolution_m,
            "cells": [self.cells_x, self.cells_y, self.cells_z],
            "cell_count": self.cell_count,
            "timesteps": self.timesteps,
            "estimated_seconds": round(self.estimated_seconds, 1),
            "estimated_memory_mb": round(self.estimated_memory_mb, 1),
            "domain_mm": [round(d * 1e3, 2) for d in self.domain_m],
            "truncated": self.truncated,
        }


def plan_mesh(
    *,
    f0_hz: float,
    bandwidth_fraction: float,
    epsilon_r: float,
    structure_extent_m: tuple[float, float, float],
    substrate_height_m: float,
    accuracy: str = "standard",
    quality_factor: float = 100.0,
    frequency_points: int = 401,
) -> MeshPlan:
    """Size the grid and the run length for one simulation.

    `quality_factor` comes from the analytical model, which already estimated
    the bandwidth. A high-Q resonator rings for a long time and needs a longer
    run — using a fixed timestep count would either waste compute on broadband
    structures or truncate narrowband ones before they settle.
    """
    cpw = DEFAULT_CELLS_PER_WAVELENGTH.get(accuracy, 20)
    sub_cells = SUBSTRATE_CELLS.get(accuracy, 4)

    fc_hz = f0_hz * bandwidth_fraction / 2.0
    f_max = f0_hz + fc_hz

    # Global cell size, set by the shortest wavelength — which lives in the
    # substrate, so the dielectric enters here even though most of the domain
    # is air.
    lambda_min = C0 / (f_max * math.sqrt(epsilon_r))
    resolution = lambda_min / cpw

    # The substrate is refined *locally*, in z only. Applying its cell size to
    # the whole domain would be catastrophic: a 0.8 mm substrate would force
    # 0.2 mm cells through 100 mm of air, turning a 100-second run into a
    # 60-hour one and quoting the customer several thousand credits for it.
    substrate_cell_z = substrate_height_m / sub_cells

    lambda0 = C0 / f0_hz
    margin = BOUNDARY_MARGIN_WAVELENGTHS * lambda0
    domain = tuple(extent + 2.0 * margin for extent in structure_extent_m)

    nx = max(8, int(math.ceil(domain[0] / resolution)))
    ny = max(8, int(math.ceil(domain[1] / resolution)))
    # Base z lines plus the extra ones threaded through the substrate.
    nz = max(8, int(math.ceil(domain[2] / resolution))) + sub_cells

    # Metal-edge refinement and smoothing transitions add lines around the
    # structure. Measured against openEMS runs this lands near 30 %.
    refinement = 1.3
    cells = [
        int(nx * refinement),
        int(ny * refinement),
        int(nz * refinement),
    ]

    # Courant is limited by the *smallest* cell, which is the substrate z cell.
    # This is why thin substrates are expensive in time-domain solvers, and it
    # has to be in the estimate or the ceiling will be breached.
    inv = (
        1.0 / resolution**2
        + 1.0 / resolution**2
        + 1.0 / substrate_cell_z**2
    )
    dt = 1.0 / (C0 * math.sqrt(inv))

    # A resonator settles in roughly Q/(pi*f0) seconds; 2x for margin.
    settle_seconds = 2.0 * quality_factor / (math.pi * f0_hz)
    required_timesteps = int(math.ceil(settle_seconds / dt))
    timesteps = max(3_000, min(required_timesteps, MAX_TIMESTEPS))

    # When the cap binds, the run stops before the structure has settled and
    # the spectrum is truncated — the answer is wrong, not merely imprecise.
    # Silently clamping would quote a price for a result nobody should trust.
    truncated = required_timesteps > MAX_TIMESTEPS

    return MeshPlan(
        resolution_m=resolution,
        cells_x=cells[0],
        cells_y=cells[1],
        cells_z=cells[2],
        timesteps=timesteps,
        domain_m=domain,  # type: ignore[arg-type]
        substrate_cells=sub_cells,
        metal_edge_resolution_m=resolution / 2.0,
        frequency_points=frequency_points,
        f0_hz=f0_hz,
        fc_hz=fc_hz,
        truncated=truncated,
    )


def credits_for(plan: MeshPlan, *, rate_per_core_second: float = 0.05) -> tuple[float, float]:
    """Return (expected, ceiling) in credits.

    The ceiling carries a 2.5x margin because FDTD runtime is not perfectly
    predictable: mesh smoothing adds lines, and a structure that fails to settle
    runs to its step limit. The customer sees the ceiling, so it must be one the
    run cannot exceed — a ceiling that is breached is worse than an expensive one.
    """
    expected = max(1.0, plan.estimated_seconds * rate_per_core_second)
    return round(expected, 2), round(expected * 2.5, 2)
