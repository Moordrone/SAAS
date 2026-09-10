"""openEMS backend tests.

openEMS itself is not installed in CI, so these test what can be tested without
it — which is most of the value. The script generation, the mesh plan and the
cost model are the work; running FDTD is openEMS's job, not ours.

The one thing these cannot catch is whether openEMS accepts the generated
script. That needs a real install, and it is the first thing to verify on a
machine that has one.
"""

import ast
import json
import uuid
from pathlib import Path

import pytest

from easyem.projects.schema import empty_definition, set_parameter
from easyem.solver.base import SolverErrorCode, SolverState
from easyem.solver.openems import OpenEMSAdapter, build, credits_for, plan_mesh
from easyem.solver.openems.script import UnsupportedGeometry


def patch_def(f=2.45, mat="RO4003C", h=0.813):
    d = empty_definition("RectangularPatch", "Antennas")
    d = set_parameter(d, "frequency_center", f, "GHz")
    d = set_parameter(d, "substrate_material", mat)
    d = set_parameter(d, "substrate_height", h, "mm")
    return d


def line_def():
    d = empty_definition("MicrostripLine", "Transmission lines")
    d = set_parameter(d, "frequency_center", 10, "GHz")
    d = set_parameter(d, "substrate_material", "RT5880")
    d = set_parameter(d, "substrate_height", 0.787, "mm")
    d = set_parameter(d, "target_impedance", 50, "ohm")
    return d


# --- mesh planning --------------------------------------------------------

def test_mesh_stays_in_the_range_a_patch_actually_needs():
    """openEMS patch tutorials land at 100k-500k cells and run in minutes.

    This test exists because the first version of plan_mesh applied the
    substrate cell size to the whole domain, producing 79 million cells and a
    28,000-credit quote for a job that costs about 8.
    """
    run = build(patch_def())
    assert 30_000 < run.plan.cell_count < 800_000
    assert 10 < run.plan.estimated_seconds < 1200


def test_air_is_not_meshed_at_substrate_resolution():
    """The global cell must follow the wavelength, not the substrate."""
    thin = build(patch_def(h=0.127)).plan
    thick = build(patch_def(h=1.575)).plan
    # A 12x thinner substrate must not produce a 12x denser global grid.
    assert thin.cells_x < thick.cells_x * 2


def test_thin_substrates_cost_more_timesteps_not_more_cells():
    """Courant is limited by the smallest cell, which is the substrate z cell.
    Missing this understates runtime and breaches the quoted ceiling."""
    thin = build(patch_def(h=0.127)).plan
    thick = build(patch_def(h=1.575)).plan
    assert thin.timesteps > thick.timesteps


def test_higher_accuracy_costs_more():
    draft = build(patch_def(), accuracy="draft").plan
    high = build(patch_def(), accuracy="high").plan
    assert high.cell_count > draft.cell_count
    assert high.estimated_seconds > draft.estimated_seconds


def test_memory_estimate_includes_process_overhead():
    """Sizing a worker from field storage alone gets it OOM-killed, which then
    bills the customer for a crash we caused."""
    plan = build(patch_def()).plan
    assert plan.estimated_memory_mb > 100


def test_ceiling_leaves_margin_over_the_expectation():
    expected, ceiling = credits_for(build(patch_def()).plan)
    assert ceiling >= expected * 2
    assert expected >= 1.0


def test_timesteps_are_bounded():
    plan = plan_mesh(
        f0_hz=1e9, bandwidth_fraction=0.6, epsilon_r=10.0,
        structure_extent_m=(0.1, 0.1, 0.001), substrate_height_m=0.001,
        quality_factor=100_000,  # absurd, to hit the cap
    )
    assert plan.timesteps <= 200_000


# --- script generation ----------------------------------------------------

def test_generated_patch_script_is_valid_python():
    ast.parse(build(patch_def()).script)


def test_generated_line_script_is_valid_python():
    ast.parse(build(line_def()).script)


def test_no_placeholder_survives_generation():
    for definition in (patch_def(), line_def()):
        assert "@@" not in build(definition).script


def test_script_carries_the_computed_geometry():
    run = build(patch_def())
    script = run.script
    # 41.34 x 33.10 mm for RO4003C at 2.45 GHz.
    assert f"{run.geometry['patch_width_mm']!r}" in script
    assert f"{run.geometry['patch_length_mm']!r}" in script


def test_script_meshes_the_substrate_explicitly():
    """The fringing field sets the resonant length; under-resolving the
    thickness moves the answer."""
    script = build(patch_def()).script
    assert 'mesh.AddLine("z", np.linspace(0, sub_h' in script


def test_script_applies_metal_edge_refinement():
    """Current density is singular at a metal edge. Without the thirds rule the
    resonance lands in the wrong place."""
    assert "metal_edge_res" in build(patch_def()).script


def test_script_writes_json_and_progress():
    script = build(patch_def()).script
    assert "results.json" in script
    assert "progress.json" in script


def test_line_script_defines_two_ports():
    """S21 needs a second port. One port gives reflection only."""
    script = build(line_def()).script
    assert script.count("AddMSLPort") == 2


def test_unsupported_component_is_refused_not_guessed():
    d = empty_definition("HornAntenna", "Antennas")
    d = set_parameter(d, "frequency_center", 10, "GHz")
    with pytest.raises(UnsupportedGeometry):
        build(d)


# --- licence boundary -----------------------------------------------------

def test_no_module_in_this_package_imports_openems():
    """openEMS is GPL v3. The subprocess-and-files boundary is what keeps this
    codebase out of the licence question, and it must be structural rather than
    a matter of remembering."""
    package = Path("easyem/solver/openems")
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not name.startswith(("openEMS", "CSXCAD")), (
                    f"{path.name} imports {name}: this links GPL code into the "
                    "product instead of running it as a separate process."
                )


def test_generated_script_is_readable_by_a_customer():
    """A customer who can read and re-run the script can verify the result.
    Checkability is what sells a simulation tool to sceptical engineers."""
    script = build(patch_def()).script
    assert script.startswith('"""openEMS run generated by EasyEM')
    assert "python3 run.py" in script


# --- adapter --------------------------------------------------------------

@pytest.fixture()
def adapter(tmp_path):
    return OpenEMSAdapter(root=tmp_path)


def test_adapter_supports_what_it_can_generate(adapter):
    assert adapter.supports(patch_def()) is True
    assert adapter.supports(line_def()) is True
    assert adapter.supports(empty_definition("HornAntenna", "Antennas")) is False


def test_estimate_reports_the_plan(adapter):
    estimate = adapter.estimate(patch_def())
    assert estimate.cost_ceiling > estimate.cost_expected > 0
    assert estimate.cell_count > 0
    assert any("cells" in note for note in estimate.notes)


def test_unsupported_geometry_raises_an_enumerated_code(adapter):
    from easyem.solver.base import SolverError

    with pytest.raises(SolverError) as exc:
        adapter.estimate(empty_definition("HornAntenna", "Antennas"))
    assert exc.value.code is SolverErrorCode.UNSUPPORTED_COMPONENT


def test_state_lives_on_disk_not_in_memory(adapter, tmp_path):
    """A run takes minutes and must survive a restart and a different worker.

    This is the fix for the flaw in the analytical backend, and it is the
    pattern EMG-TLM should follow.
    """
    job_id = uuid.uuid4()
    run_dir = tmp_path / str(job_id)
    run_dir.mkdir()
    (run_dir / "state.json").write_text(json.dumps({
        "state": "succeeded", "wall_seconds": 42.0, "cpu_seconds": 42.0,
    }))
    (run_dir / "plan.json").write_text(json.dumps({
        "cell_count": 1000, "frequency_points": 401, "estimated_memory_mb": 160,
    }))

    # A completely fresh adapter instance reads the same job.
    fresh = OpenEMSAdapter(root=tmp_path)
    status = fresh.status(uuid_ref(job_id))
    assert status.state is SolverState.succeeded
    assert status.usage.wall_seconds == 42.0


def test_unsupported_submit_records_a_failure_it_can_report(adapter):
    job_id = uuid.uuid4()
    ref = adapter.submit(empty_definition("HornAntenna", "Antennas"), job_id)
    status = adapter.status(ref)
    assert status.state is SolverState.failed
    assert status.error_code is SolverErrorCode.UNSUPPORTED_COMPONENT


def test_unknown_job_raises(adapter):
    from easyem.solver.base import SolverError, SolverJobRef

    with pytest.raises(SolverError):
        adapter.status(SolverJobRef("openems", "no-such-job"))


def test_cancel_is_safe_on_an_unknown_job(adapter):
    from easyem.solver.base import SolverJobRef

    adapter.cancel(SolverJobRef("openems", "ghost"))


def test_health_reports_whether_openems_is_installed(adapter):
    health = adapter.health()
    assert health["backend"] == "openems"
    assert isinstance(health["healthy"], bool)


def test_backend_is_not_registered_when_openems_is_absent():
    """Offering a backend that cannot run is worse than not offering it: the
    customer finds out after their credits are held."""
    from easyem.solver.openems import openems_available
    from easyem.solver.registry import available

    if not openems_available():
        assert "openems" not in available()


def uuid_ref(job_id):
    from easyem.solver.base import SolverJobRef

    return SolverJobRef("openems", str(job_id))


def test_a_run_that_would_be_truncated_is_refused_not_priced():
    """Very thin substrates force a tiny Courant step. Past the timestep cap the
    run stops before the structure settles, so the spectrum is wrong rather than
    imprecise — and quoting a price for that sells a result nobody should trust.

    Found by running the getting-started notebook and reading the output table:
    0.127 mm and 0.254 mm both sat exactly on 200,000 timesteps.
    """
    from easyem.solver.base import SolverError

    assert build(patch_def(h=0.127)).plan.truncated is True
    assert build(patch_def(h=0.813)).plan.truncated is False

    adapter = OpenEMSAdapter(root=Path("/tmp"))
    with pytest.raises(SolverError) as exc:
        adapter.estimate(patch_def(h=0.127))
    assert exc.value.code is SolverErrorCode.RESOURCE_EXCEEDED
