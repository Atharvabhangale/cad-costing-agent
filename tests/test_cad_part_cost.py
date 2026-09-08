import pytest

from cad_costing_agent.cad_geometry import BoundingBoxDimensions, GeometryProperties
from cad_costing_agent.cad_part_cost import calculate_cad_part_cost
from cad_costing_agent.materials import MATERIALS
from cad_costing_agent.operation import ManufacturingOperation

STEEL_DENSITY_KG_M3 = MATERIALS["steel"].density_kg_m3
STEEL_COST_PER_KG = MATERIALS["steel"].cost_per_kg

CUBE_GEOMETRY = GeometryProperties(
    source_format="stl",
    volume=6000.0,  # 10x20x30 mm
    surface_area=2200.0,
    bounding_box=BoundingBoxDimensions(x=10.0, y=20.0, z=30.0),
)


def make_operation(operation_id: str, quantity: int) -> ManufacturingOperation:
    return ManufacturingOperation(
        operation_id=operation_id,
        operation_name="Cutting",
        process_type="Cutting",
        machine_rate_per_hour=1800.0,
        labor_rate_per_hour=500.0,
        setup_time_minutes=20.0,
        cycle_time_minutes=2.0,
        quantity=quantity,
    )


def test_cad_part_cost_material_only():
    result = calculate_cad_part_cost(
        geometry=CUBE_GEOMETRY,
        material_name="Steel",
        source_unit="mm",
        part_id="P001",
        part_name="Disc",
        quantity=1,
        operations=[],
    )

    expected_mass_kg = 6000.0 * (0.001**3) * STEEL_DENSITY_KG_M3
    assert result.mass.mass_kg == pytest.approx(expected_mass_kg)
    assert result.part_cost.material.mass_kg == pytest.approx(expected_mass_kg)
    assert result.part_cost.total_operation_cost == 0
    assert result.part_cost.total_part_cost == pytest.approx(result.part_cost.total_material_cost)


def test_cad_part_cost_with_operations():
    result = calculate_cad_part_cost(
        geometry=CUBE_GEOMETRY,
        material_name="Steel",
        source_unit="mm",
        part_id="P001",
        part_name="Disc",
        quantity=10,
        operations=[make_operation("OP1", quantity=10)],
    )

    assert len(result.part_cost.operations) == 1
    assert result.part_cost.total_part_cost == pytest.approx(
        result.part_cost.total_material_cost + result.part_cost.total_operation_cost
    )


def test_cad_derived_mass_is_per_part_not_multiplied_by_quantity():
    quantity_1_result = calculate_cad_part_cost(
        geometry=CUBE_GEOMETRY,
        material_name="Steel",
        source_unit="mm",
        part_id="P001",
        part_name="Disc",
        quantity=1,
    )
    quantity_10_result = calculate_cad_part_cost(
        geometry=CUBE_GEOMETRY,
        material_name="Steel",
        source_unit="mm",
        part_id="P001",
        part_name="Disc",
        quantity=10,
    )

    # Mass per part is identical regardless of quantity.
    assert quantity_1_result.mass.mass_kg == pytest.approx(quantity_10_result.mass.mass_kg)
    # But total material cost scales by quantity, applied only inside material costing.
    assert quantity_10_result.part_cost.total_material_cost == pytest.approx(
        quantity_1_result.part_cost.total_material_cost * 10
    )


def test_cad_part_cost_unknown_material_rejected():
    with pytest.raises(ValueError, match="Unknown material"):
        calculate_cad_part_cost(
            geometry=CUBE_GEOMETRY,
            material_name="Unobtainium",
            source_unit="mm",
            part_id="P001",
            part_name="Disc",
            quantity=1,
        )


def test_cad_part_cost_operation_quantity_mismatch_rejected():
    with pytest.raises(ValueError, match="must match part quantity"):
        calculate_cad_part_cost(
            geometry=CUBE_GEOMETRY,
            material_name="Steel",
            source_unit="mm",
            part_id="P001",
            part_name="Disc",
            quantity=10,
            operations=[make_operation("OP1", quantity=1)],
        )
