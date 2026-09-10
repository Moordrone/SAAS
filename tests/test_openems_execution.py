"""Execute the generated openEMS script against a stand-in openEMS.

openEMS is not installed in CI, so a fake package mimicking its API is put on
the path and the generated script is run as a real subprocess. This is the one
thing the other openEMS tests cannot do: prove the script runs end to end,
writes results.json in the shape the adapter reads, and reports a crash the
customer can act on. It does not prove openEMS itself accepts the geometry —
only a real install does that — but it closes the gap between "the script is
valid Python" and "the script runs".
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from easyem.solver.base import SolverJobRef, SolverState
from easyem.solver.openems import build
from easyem.solver.openems.adapter import OpenEMSAdapter

PATCH = {
    "schema_version": "1.0.0",
    "component": {"family": "Antennas", "type": "RectangularPatch"},
    "parameters": {
        "frequency_center": {"value": 2.45, "unit": "GHz"},
        "substrate_material": {"value": "RO4003C"},
        "substrate_height": {"value": 0.813, "unit": "mm"},
    },
}

FAKE_CSXCAD = '''
class _Box:
    def AddBox(self, **k): pass
class _Grid:
    def SetDeltaUnit(self, *a): pass
    def AddLine(self, *a): pass
    def SmoothMeshLines(self, *a): pass
class ContinuousStructure:
    def GetGrid(self): return _Grid()
    def AddMaterial(self, *a, **k): return _Box()
    def AddMetal(self, *a, **k): return _Box()
'''

FAKE_OPENEMS = '''
import numpy as np
class _Port:
    def __init__(self):
        n = 401
        self.uf_ref = np.linspace(0.9, 0.1, n) + 0j
        self.uf_inc = np.ones(n) + 0j
        self.uf_tot = np.full(n, 50.0) + 0j
        self.if_tot = np.ones(n) + 0j
    def CalcPort(self, *a, **k): pass
class _NF:
    def CalcNF2FF(self, *a, **k):
        class R:
            E_norm = [np.ones((91, 2))]
            Dmax = [6.6]; Prad = [0.01]
        return R()
class openEMS:
    def __init__(self, **k): pass
    def SetGaussExcite(self, *a): pass
    def SetBoundaryCond(self, *a): pass
    def SetCSX(self, *a): pass
    def AddEdges2Grid(self, **k): pass
    def AddLumpedPort(self, *a, **k): return _Port()
    def CreateNF2FFBox(self, *a, **k): return _NF()
    def Run(self, *a, **k): pass
'''

FAKE_CONSTANTS = "C0 = 299792458.0\nEPS0 = 8.8541878128e-12\n"


@pytest.fixture()
def fake_openems(tmp_path):
    """A directory holding a stand-in openEMS + CSXCAD, for PYTHONPATH."""
    root = tmp_path / "fake"
    (root / "CSXCAD").mkdir(parents=True)
    (root / "openEMS").mkdir(parents=True)
    (root / "CSXCAD" / "__init__.py").write_text(FAKE_CSXCAD)
    (root / "openEMS" / "__init__.py").write_text(FAKE_OPENEMS)
    (root / "openEMS" / "physical_constants.py").write_text(FAKE_CONSTANTS)
    return root


def _run(script: str, out_dir: Path, fake_root: Path):
    script_path = out_dir / "run.py"
    script_path.write_text(script)
    env = {"PYTHONPATH": str(fake_root)}
    import os
    env = {**os.environ, **env}
    return subprocess.run(
        [sys.executable, str(script_path), str(out_dir)],
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_generated_patch_script_runs_end_to_end(tmp_path, fake_openems):
    out = tmp_path / "run"
    out.mkdir()
    result = _run(build(PATCH).script, out, fake_openems)

    assert result.returncode == 0, result.stderr
    results = json.loads((out / "results.json").read_text())
    assert results["solver"] == "openEMS"
    assert len(results["s_parameters"]["S11"]) == 401
    assert "radiation_pattern" in results
    assert json.loads((out / "progress.json").read_text())["progress"] == 100.0


def test_helper_functions_are_defined_before_they_are_called(tmp_path, fake_openems):
    """progress() is called near the top of the run; if it were defined at the
    bottom, as it first was, the script would NameError before openEMS even
    started. This test pins the ordering."""
    script = build(PATCH).script
    out = tmp_path / "order"
    out.mkdir()
    result = _run(script, out, fake_openems)
    assert "NameError" not in result.stderr
    assert result.returncode == 0


def test_a_solver_crash_is_written_where_the_adapter_can_read_it(tmp_path):
    """A crash inside openEMS must leave a reason on disk, not just die on
    stderr, or the customer gets a bare 'internal error'."""
    crash_root = tmp_path / "crash-oe"
    (crash_root / "CSXCAD").mkdir(parents=True)
    (crash_root / "openEMS").mkdir(parents=True)
    (crash_root / "CSXCAD" / "__init__.py").write_text(FAKE_CSXCAD)
    (crash_root / "openEMS" / "physical_constants.py").write_text(FAKE_CONSTANTS)
    (crash_root / "openEMS" / "__init__.py").write_text(textwrap.dedent('''
        class openEMS:
            def __init__(self, **k): pass
            def SetGaussExcite(self, *a): pass
            def SetBoundaryCond(self, *a): pass
            def SetCSX(self, *a): pass
            def AddEdges2Grid(self, **k): pass
            def AddLumpedPort(self, *a, **k): return object()
            def CreateNF2FFBox(self, *a, **k): return object()
            def Run(self, *a, **k):
                raise RuntimeError("mesh generation failed: geometry self-intersects")
    '''))

    out = tmp_path / "crash"
    out.mkdir()
    result = _run(build(PATCH).script, out, crash_root)

    assert result.returncode == 1
    assert not (out / "results.json").exists()
    error = json.loads((out / "error.json").read_text())
    assert "self-intersects" in error["error"]

    # And the adapter surfaces that reason rather than a generic message.
    adapter = OpenEMSAdapter(root=tmp_path)
    (tmp_path / str(out.name)).mkdir(exist_ok=True)
    # place the crash artefacts where the adapter expects them
    jid = "crashjob"
    jdir = tmp_path / jid
    jdir.mkdir()
    (jdir / "error.json").write_text((out / "error.json").read_text())
    import time
    (jdir / "state.json").write_text(json.dumps({
        "state": "running", "pid": 999999,
        "started_at": time.time(), "timeout_seconds": 3600,
    }))
    status = adapter.status(SolverJobRef("openems", jid))
    assert status.state is SolverState.failed
    assert "self-intersects" in status.error_message
