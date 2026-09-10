"""openEMS backend.

Nothing in this package imports openEMS. The solver runs as a subprocess and
communicates through files, which keeps the GPL v3 boundary at arm's length.
"""

from .adapter import OpenEMSAdapter, openems_available
from .mesh import MeshPlan, credits_for, plan_mesh
from .script import UnsupportedGeometry, build

__all__ = [
    "OpenEMSAdapter", "openems_available",
    "MeshPlan", "plan_mesh", "credits_for",
    "build", "UnsupportedGeometry",
]
