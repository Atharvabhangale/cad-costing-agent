import pytest

from cad_costing_agent.cad_geometry import extract_geometry_from_bytes
from cad_costing_agent.cad_mass import calculate_mass_from_geometry
from cad_costing_agent.cad_part_cost import calculate_cad_part_cost
from cad_costing_agent.materials import MATERIALS
from cad_costing_agent.operation import ManufacturingOperation

STEEL_DENSITY_KG_M3 = MATERIALS["steel"].density_kg_m3

# 10 x 20 x 30 mm box -> 6000 mm^3 -> 6e-6 m^3
EXPECTED_VOLUME_M3 = 6000.0 * (0.001**3)
EXPECTED_MASS_KG = EXPECTED_VOLUME_M3 * STEEL_DENSITY_KG_M3


def test_step_derived_mass_matches_expected_value(step_box_bytes):
    geometry = extract_geometry_from_bytes(step_box_bytes(10.0, 20.0, 30.0), "step")

    result = calculate_mass_from_geometry(geometry, "Steel", "mm")

    assert result.volume_m3 == pytest.approx(EXPECTED_VOLUME_M3)
    assert result.mass_kg == pytest.approx(EXPECTED_MASS_KG)
    assert result.mass_kg == pytest.approx(0.0471, abs=1e-4)
    assert result.detected_unit == "mm"


def test_step_and_stl_of_same_box_give_the_same_mass(step_box_bytes):
    """The mass pipeline must not care which CAD format the geometry came from."""
    import trimesh

    stl_bytes = trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")

    step_geometry = extract_geometry_from_bytes(step_box_bytes(10.0, 20.0, 30.0), "step")
    stl_geometry = extract_geometry_from_bytes(stl_bytes, "stl")

    step_mass = calculate_mass_from_geometry(step_geometry, "Steel", "mm")
    stl_mass = calculate_mass_from_geometry(stl_geometry, "Steel", "mm")

    assert step_mass.mass_kg == pytest.approx(stl_mass.mass_kg)


def test_conflicting_source_unit_rejected_for_step(step_box_bytes):
    """STEP is already normalized to mm, so 'inch' would mis-scale mass by 25.4**3."""
    geometry = extract_geometry_from_bytes(step_box_bytes(), "step")

    with pytest.raises(ValueError, match="conflicts with the unit detected"):
        calculate_mass_from_geometry(geometry, "Steel", "inch")


def test_matching_source_unit_accepted_for_step(step_box_bytes):
    geometry = extract_geometry_from_bytes(step_box_bytes(), "step")

    result = calculate_mass_from_geometry(geometry, "Steel", "mm")

    assert result.source_unit.value == "mm"


def test_stl_still_accepts_any_declared_unit():
    """STL has no detected unit, so the caller's unit is still taken at face value."""
    import trimesh

    stl_bytes = trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")
    geometry = extract_geometry_from_bytes(stl_bytes, "stl")

    assert geometry.detected_unit is None

    inch_result = calculate_mass_from_geometry(geometry, "Steel", "inch")
    mm_result = calculate_mass_from_geometry(geometry, "Steel", "mm")

    assert inch_result.mass_kg == pytest.approx(mm_result.mass_kg * (0.0254 / 0.001) ** 3)


def test_inch_authored_step_flows_through_as_millimetres(step_box_bytes):
    """A 1-inch steel cube weighs 25.4**3 mm^3 worth of steel, not 1 mm^3 worth."""
    geometry = extract_geometry_from_bytes(step_box_bytes(25.4, 25.4, 25.4, unit="INCH"), "step")

    result = calculate_mass_from_geometry(geometry, "Steel", "mm")

    expected_mass_kg = (25.4**3) * (0.001**3) * STEEL_DENSITY_KG_M3
    assert result.mass_kg == pytest.approx(expected_mass_kg)


def test_step_derived_mass_flows_into_existing_part_costing(step_box_bytes):
    """STEP geometry reaches the existing costing pipeline with no STEP-specific logic."""
    geometry = extract_geometry_from_bytes(step_box_bytes(10.0, 20.0, 30.0), "step")

    result = calculate_cad_part_cost(
        geometry=geometry,
        material_name="Steel",
        source_unit="mm",
        part_id="P001",
        part_name="Disc",
        quantity=10,
        operations=[
            ManufacturingOperation(
                operation_id="OP1",
                operation_name="Cutting",
                process_type="Cutting",
                machine_rate_per_hour=1800.0,
                labor_rate_per_hour=500.0,
                setup_time_minutes=20.0,
                cycle_time_minutes=2.0,
                quantity=10,
            )
        ],
    )

    assert result.geometry.source_format == "step"
    assert result.mass.mass_kg == pytest.approx(EXPECTED_MASS_KG)
    # Material cost still comes from the existing, unmodified formula.
    assert result.part_cost.total_material_cost == pytest.approx(
        EXPECTED_MASS_KG * MATERIALS["steel"].cost_per_kg * 10
    )
    assert result.part_cost.total_part_cost == pytest.approx(
        result.part_cost.total_material_cost + result.part_cost.total_operation_cost
    )


def test_step_derived_mass_is_per_part_not_scaled_by_quantity(step_box_bytes):
    geometry = extract_geometry_from_bytes(step_box_bytes(10.0, 20.0, 30.0), "step")

    single = calculate_cad_part_cost(
        geometry=geometry,
        material_name="Steel",
        source_unit="mm",
        part_id="P001",
        part_name="Disc",
        quantity=1,
    )
    bulk = calculate_cad_part_cost(
        geometry=geometry,
        material_name="Steel",
        source_unit="mm",
        part_id="P001",
        part_name="Disc",
        quantity=10,
    )

    assert single.mass.mass_kg == pytest.approx(bulk.mass.mass_kg)
    assert bulk.part_cost.total_material_cost == pytest.approx(
        single.part_cost.total_material_cost * 10
    )
