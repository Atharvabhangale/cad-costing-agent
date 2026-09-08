import pytest
from pydantic import ValidationError

from cad_costing_agent.operation import ManufacturingOperation
from cad_costing_agent.part import Part
from cad_costing_agent.part_cost import PartCostEstimateRequest, calculate_part_cost

# Illustrative POC example: Disc, quantity 10, with Cutting/Facing/Drilling operations
PART = dict(part_id="P001", part_name="Disc", material="Steel", mass_kg=2.0, quantity=10)


def make_operation(operation_id: str, operation_name: str, quantity: int = 10) -> dict:
    return dict(
        operation_id=operation_id,
        operation_name=operation_name,
        process_type=operation_name,
        machine_rate_per_hour=1800.0,
        labor_rate_per_hour=500.0,
        setup_time_minutes=20.0,
        cycle_time_minutes=2.0,
        quantity=quantity,
    )


def test_part_with_material_only():
    request = PartCostEstimateRequest(part=Part(**PART), operations=[])
    result = calculate_part_cost(request)

    assert result.operations == []
    assert result.total_operation_cost == 0
    assert result.total_material_cost == pytest.approx(2.0 * 0.80 * 10)
    assert result.total_part_cost == pytest.approx(result.total_material_cost)


def test_part_with_one_operation():
    request = PartCostEstimateRequest(
        part=Part(**PART),
        operations=[ManufacturingOperation(**make_operation("OP1", "Cutting"))],
    )
    result = calculate_part_cost(request)

    assert len(result.operations) == 1
    assert result.total_operation_cost == pytest.approx(result.operations[0].total_operation_cost)


def test_part_with_multiple_operations():
    request = PartCostEstimateRequest(
        part=Part(**PART),
        operations=[
            ManufacturingOperation(**make_operation("OP1", "Cutting")),
            ManufacturingOperation(**make_operation("OP2", "Facing")),
            ManufacturingOperation(**make_operation("OP3", "Drilling")),
        ],
    )
    result = calculate_part_cost(request)

    assert len(result.operations) == 3
    assert [op.operation_name for op in result.operations] == ["Cutting", "Facing", "Drilling"]


def test_operation_costs_sum_correctly():
    request = PartCostEstimateRequest(
        part=Part(**PART),
        operations=[
            ManufacturingOperation(**make_operation("OP1", "Cutting")),
            ManufacturingOperation(**make_operation("OP2", "Facing")),
            ManufacturingOperation(**make_operation("OP3", "Drilling")),
        ],
    )
    result = calculate_part_cost(request)

    expected_total_operation_cost = sum(op.total_operation_cost for op in result.operations)
    assert result.total_operation_cost == pytest.approx(expected_total_operation_cost)


def test_total_part_cost():
    request = PartCostEstimateRequest(
        part=Part(**PART),
        operations=[
            ManufacturingOperation(**make_operation("OP1", "Cutting")),
            ManufacturingOperation(**make_operation("OP2", "Facing")),
        ],
    )
    result = calculate_part_cost(request)

    assert result.total_part_cost == pytest.approx(
        result.total_material_cost + result.total_operation_cost
    )


def test_operation_quantity_must_match_part_quantity():
    request = PartCostEstimateRequest(
        part=Part(**PART),  # quantity=10
        operations=[ManufacturingOperation(**make_operation("OP1", "Cutting", quantity=5))],
    )

    with pytest.raises(ValueError, match="must match part quantity"):
        calculate_part_cost(request)


def test_invalid_part_input_rejected():
    with pytest.raises(ValidationError):
        Part(**{**PART, "mass_kg": -2.0})


def test_invalid_operation_input_rejected():
    with pytest.raises(ValidationError):
        ManufacturingOperation(**{**make_operation("OP1", "Cutting"), "cycle_time_minutes": -2.0})
