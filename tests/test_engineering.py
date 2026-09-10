"""Engineering Engine tests.

The analytical models are checked against published worked examples, not
against themselves. A model that only agrees with its own output is a model
nobody can trust.
"""


import pytest

from easyem.engineering.analytical import microstrip, patch
from easyem.engineering.components import list_components
from easyem.engineering.errors import UnitError, UnknownMaterial
from easyem.engineering.materials import get_substrate, nearest_standard_thickness
from easyem.engineering.units import Dimension, Quantity, humanise
from easyem.engineering.validation import estimate, validate
from easyem.projects.schema import empty_definition, set_parameter

# --- units ----------------------------------------------------------------

def test_conversion_to_si():
    assert Quantity(2.45, "GHz").si_value == pytest.approx(2.45e9)
    assert Quantity(1.6, "mm").si_value == pytest.approx(1.6e-3)
    assert Quantity(62, "mil").si_value == pytest.approx(62 * 2.54e-5)


def test_cannot_convert_across_dimensions():
    """A frequency is not a length, however plausible the number looks."""
    with pytest.raises(UnitError):
        Quantity(2.45, "GHz").to("mm")


def test_unknown_unit_is_rejected_at_construction(): 
    with pytest.raises(UnitError):
        Quantity(1.0, "furlongs")


def test_round_trip_is_lossless():
    q = Quantity(2.45, "GHz")
    assert q.to("MHz").to("GHz").value == pytest.approx(2.45)


def test_humanise_picks_a_readable_unit():
    assert humanise(2.45e9, Dimension.frequency).unit == "GHz"
    assert humanise(1.6e-3, Dimension.length).unit == "mm"
    assert humanise(3.2e-2, Dimension.length).unit == "cm"


# --- materials ------------------------------------------------------------

def test_substrate_lookup_is_case_insensitive():
    assert get_substrate("fr4").epsilon_r == pytest.approx(4.4)


def test_unknown_substrate_lists_the_alternatives():
    with pytest.raises(UnknownMaterial) as exc:
        get_substrate("unobtanium")
    assert "RO4003C" in str(exc.value)


def test_thickness_snaps_to_a_stocked_value():
    fr4 = get_substrate("FR4")
    assert nearest_standard_thickness(fr4, 1.5e-3) == pytest.approx(1.6e-3)


# --- microstrip -----------------------------------------------------------

def test_50_ohm_on_fr4_matches_the_published_width():
    """50 ohm on FR-4, h = 1.6 mm is ~3.0-3.1 mm in every reference table."""
    r = microstrip.design(
        target_impedance_ohm=50, epsilon_r=4.4, height_m=1.6e-3, frequency_hz=2.45e9
    )
    assert 2.9e-3 < r.width_m < 3.2e-3
    assert 3.2 < r.epsilon_eff < 3.5


def test_synthesis_and_analysis_agree():
    """If the two directions disagree, one of them is wrong."""
    for z0 in (25, 50, 75, 100):
        w = microstrip.synthesise_width(z0, 3.38, 0.508e-3)
        back, _ = microstrip.characteristic_impedance(3.38, w, 0.508e-3)
        assert back == pytest.approx(z0, rel=1e-3)


def test_wider_traces_have_lower_impedance():
    z_narrow, _ = microstrip.characteristic_impedance(4.4, 1e-3, 1.6e-3)
    z_wide, _ = microstrip.characteristic_impedance(4.4, 6e-3, 1.6e-3)
    assert z_wide < z_narrow


def test_effective_permittivity_lies_between_air_and_substrate():
    e_eff = microstrip.effective_permittivity(4.4, 3e-3, 1.6e-3)
    assert 1.0 < e_eff < 4.4


def test_quarter_wave_length_is_a_quarter_of_lambda_g():
    r = microstrip.design(
        target_impedance_ohm=70.7, epsilon_r=2.2, height_m=0.787e-3,
        frequency_hz=10e9, electrical_length_deg=90,
    )
    assert r.physical_length_m == pytest.approx(r.guided_wavelength_m / 4)


def test_extreme_aspect_ratio_is_flagged_not_hidden():
    r = microstrip.design(
        target_impedance_ohm=200, epsilon_r=9.8, height_m=5e-3, frequency_hz=1e9
    )
    assert any("W/h" in w or "dispersion" in w for w in r.warnings)


# --- patch ----------------------------------------------------------------

def test_patch_matches_the_balanis_worked_example():
    """Balanis, Antenna Theory 4th ed., §14.2: er=2.2, h=0.1588 cm, f=10 GHz
    gives W = 1.186 cm and L = 0.906 cm."""
    p = patch.design(frequency_hz=10e9, epsilon_r=2.2, height_m=0.1588e-2)
    assert p.width_m == pytest.approx(1.186e-2, rel=0.01)
    assert p.length_m == pytest.approx(0.906e-2, rel=0.01)


def test_patch_resonates_where_it_was_designed_to():
    """Design then analyse: the round trip must return the target frequency."""
    for f_target in (900e6, 2.45e9, 5.8e9):
        p = patch.design(frequency_hz=f_target, epsilon_r=4.4, height_m=1.6e-3)
        f_back = patch.resonant_frequency(
            length_m=p.length_m, width_m=p.width_m, epsilon_r=4.4, height_m=1.6e-3
        )
        assert f_back == pytest.approx(f_target, rel=1e-6)


def test_patch_directivity_is_in_the_published_range():
    """A patch is 6-9 dBi. Quoting the single-slot value would read ~3 dB low."""
    p = patch.design(frequency_hz=10e9, epsilon_r=2.2, height_m=0.1588e-2)
    assert 6.0 <= p.directivity_dbi <= 9.0


def test_higher_permittivity_narrows_bandwidth():
    thin = patch.design(frequency_hz=2.45e9, epsilon_r=2.2, height_m=1.6e-3)
    thick = patch.design(frequency_hz=2.45e9, epsilon_r=9.8, height_m=1.6e-3)
    assert thick.bandwidth_fraction < thin.bandwidth_fraction


def test_thicker_substrate_widens_bandwidth():
    thin = patch.design(frequency_hz=2.45e9, epsilon_r=4.4, height_m=0.8e-3)
    thick = patch.design(frequency_hz=2.45e9, epsilon_r=4.4, height_m=3.2e-3)
    assert thick.bandwidth_fraction > thin.bandwidth_fraction


def test_lossy_substrate_lowers_efficiency():
    lossless = patch.design(frequency_hz=2.45e9, epsilon_r=4.4, height_m=1.6e-3)
    lossy = patch.design(
        frequency_hz=2.45e9, epsilon_r=4.4, height_m=1.6e-3, loss_tangent=0.02
    )
    assert lossy.radiation_efficiency_estimate < lossless.radiation_efficiency_estimate


def test_inset_feed_lands_inside_the_patch():
    p = patch.design(frequency_hz=2.45e9, epsilon_r=4.4, height_m=1.6e-3)
    assert p.inset_feed_offset_m is not None
    assert 0 < p.inset_feed_offset_m < p.length_m / 2


def test_inset_is_snapped_to_a_manufacturable_grid():
    """An arbitrary-precision inset gives a perfect match and an S11 of minus
    infinity. No fabricated antenna does that, and quoting it would poison
    trust in every other number on the page."""
    from easyem.engineering.analytical.patch import INSET_RESOLUTION_M

    p = patch.design(frequency_hz=2.45e9, epsilon_r=3.38, height_m=0.813e-3)
    steps = p.inset_feed_offset_m / INSET_RESOLUTION_M
    assert abs(steps - round(steps)) < 1e-9

    # The achieved resistance is close to 50 ohm but never exactly 50.
    assert p.inset_resistance_ohm != 50.0
    assert 45.0 < p.inset_resistance_ohm < 55.0


def test_return_loss_is_finite():
    """Guards against the perfect-match artefact returning."""
    import math

    p = patch.design(frequency_hz=2.45e9, epsilon_r=4.4, height_m=1.6e-3)
    gamma = abs((p.inset_resistance_ohm - 50) / (p.inset_resistance_ohm + 50))
    assert gamma > 0
    assert -80 < 20 * math.log10(gamma) < -10


def test_unmodelled_feed_reactance_is_disclosed():
    """A stated limitation is worth more than an invented correction factor."""
    p = patch.design(frequency_hz=2.45e9, epsilon_r=3.38, height_m=0.813e-3)
    assert any("reactance" in w.lower() for w in p.warnings)


def test_thick_substrate_carries_a_warning():
    """h/lambda0 > 0.05 breaks the thin-substrate assumption. Say so."""
    p = patch.design(frequency_hz=10e9, epsilon_r=2.2, height_m=3e-3)
    assert any("h/lambda0" in w for w in p.warnings)


def test_impossible_geometry_is_refused():
    with pytest.raises(ValueError):
        patch.design(frequency_hz=30e9, epsilon_r=2.2, height_m=20e-3)


# --- validation -----------------------------------------------------------

def _patch_definition(**overrides):
    d = empty_definition("RectangularPatch", "Antennas")
    d = set_parameter(d, "frequency_center", overrides.get("f", 2.45), "GHz")
    d = set_parameter(d, "substrate_material", overrides.get("mat", "RO4003C"))
    d = set_parameter(d, "substrate_height", overrides.get("h", 0.813), "mm")
    return d


def test_valid_definition_passes():
    assert validate(_patch_definition()).is_valid


def test_missing_parameters_are_named():
    d = empty_definition("RectangularPatch", "Antennas")
    result = validate(d)
    assert not result.is_valid
    assert "frequency_center" in result.missing


def test_wrong_dimension_is_caught():
    """Assigning a length to a frequency is the classic paste error."""
    d = _patch_definition()
    d = set_parameter(d, "frequency_center", 2.45, "mm")
    result = validate(d)
    assert not result.is_valid
    assert any(i.code == "wrong_dimension" for i in result.errors)


def test_out_of_bounds_frequency_is_caught():
    d = _patch_definition(f=0.0001)  # 100 kHz, below the 1 MHz floor
    assert any(i.code == "out_of_bounds" for i in validate(d).errors)


def test_unknown_schema_version_is_refused():
    d = _patch_definition()
    d["schema_version"] = "0.9.0"
    result = validate(d)
    assert not result.is_valid
    assert any(i.code == "unsupported_schema_version" for i in result.issues)


def test_unknown_parameter_warns_but_does_not_block():
    d = _patch_definition()
    d = set_parameter(d, "sparkle_factor", 3, "1")
    result = validate(d)
    assert result.is_valid
    assert any(i.code == "unknown_parameter" for i in result.warnings)


def test_fr4_above_3ghz_warns():
    result = validate(_patch_definition(f=5.8, mat="FR4"))
    assert result.is_valid  # a warning must not block the engineer
    assert any(i.code == "fr4_above_3ghz" for i in result.warnings)


def test_absurdly_thick_substrate_is_an_error_not_a_warning():
    result = validate(_patch_definition(f=10, h=5))  # 5 mm at 10 GHz
    assert not result.is_valid
    assert any(i.code == "substrate_too_thick" for i in result.errors)


def test_invalid_material_choice_suggests_alternatives():
    d = _patch_definition(mat="cardboard")
    result = validate(d)
    assert any(i.code == "invalid_choice" for i in result.errors)


# --- estimation -----------------------------------------------------------

def test_estimate_returns_derived_dimensions():
    out = estimate(_patch_definition())
    assert out["ok"] is True
    assert out["model"] == "analytical"
    derived = out["derived_parameters"]
    assert derived["patch_width"]["provenance"] == "computed"
    assert derived["patch_width"]["unit"] == "m"
    # RO4003C (er=3.38) at 2.45 GHz: W = c/(2f)*sqrt(2/(er+1)) = 41.3 mm
    assert derived["patch_width"]["value"] == pytest.approx(0.0413, rel=0.01)


def test_estimate_refuses_an_invalid_project():
    out = estimate(empty_definition("RectangularPatch", "Antennas"))
    assert out["ok"] is False
    assert out["validation"]["missing"]


def test_estimate_works_for_microstrip():
    d = empty_definition("MicrostripLine", "Transmission lines")
    d = set_parameter(d, "frequency_center", 10, "GHz")
    d = set_parameter(d, "substrate_material", "RT5880")
    d = set_parameter(d, "substrate_height", 0.787, "mm")
    d = set_parameter(d, "target_impedance", 50, "ohm")

    out = estimate(d)
    assert out["ok"] is True
    assert out["results"]["impedance_ohm"] == pytest.approx(50, rel=1e-3)


def test_every_component_claiming_analytical_support_has_an_estimator():
    """Stops the registry and the estimator from drifting apart."""
    for schema in list_components():
        if not schema.supports_analytical:
            continue
        d = empty_definition(schema.key, schema.family)
        d = set_parameter(d, "frequency_center", 2.45, "GHz")
        d = set_parameter(d, "substrate_material", "RO4003C")
        d = set_parameter(d, "substrate_height", 0.813, "mm")
        out = estimate(d)
        assert out.get("ok") is True, f"{schema.key} has no working estimator"
