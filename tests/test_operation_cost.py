import pytest
from pydantic import ValidationError

from cad_costing_agent.operation import ManufacturingOperation
from cad_costing_agent.operation_costing import calculate_operation_cost

# Illustrative POC example: CNC Drilling
CNC_DRILLING = dict(
    operation_id="OP1",
    operation_name="CNC Drilling",
    process_type="Drilling",
    machine_rate_per_hour=1800.0,
    labor_rate_per_hour=500.0,
    setup_time_minutes=20.0,
    cycle_time_minutes=2.0,
    quantity=10,
)


def test_machine_cost():
    op = ManufacturingOperation(**CNC_DRILLING)
    result = calculate_operation_cost(op)

    assert result.machine_cost == pytest.approx(1800.0 * (2.0 * 10 / 60))


def test_setup_cost():
    op = ManufacturingOperation(**CNC_DRILLING)
    result = calculate_operation_cost(op)

    assert result.setup_cost == pytest.approx(1800.0 * (20.0 / 60))


def test_labor_cost():
    op = ManufacturingOperation(**CNC_DRILLING)
    result = calculate_operation_cost(op)

    assert result.labor_cost == pytest.approx(500.0 * (2.0 * 10 / 60))


def test_total_operation_cost():
    op = ManufacturingOperation(**CNC_DRILLING)
    result = calculate_operation_cost(op)

    assert result.total_operation_cost == pytest.approx(
        result.machine_cost + result.setup_cost + result.labor_cost
    )
    assert result.is_illustrative_rate is True


def test_quantity_multiplication():
    single = ManufacturingOperation(**{**CNC_DRILLING, "quantity": 1})
    tenfold = ManufacturingOperation(**{**CNC_DRILLING, "quantity": 10})

    single_result = calculate_operation_cost(single)
    tenfold_result = calculate_operation_cost(tenfold)

    assert tenfold_result.machine_cost == pytest.approx(single_result.machine_cost * 10)
    assert tenfold_result.labor_cost == pytest.approx(single_result.labor_cost * 10)
    # setup cost is a one-time cost, independent of quantity
    assert tenfold_result.setup_cost == pytest.approx(single_result.setup_cost)


@pytest.mark.parametrize(
    "overrides",
    [
        {"machine_rate_per_hour": -1800.0},
        {"labor_rate_per_hour": -500.0},
        {"setup_time_minutes": -20.0},
        {"cycle_time_minutes": -2.0},
        {"cycle_time_minutes": 0.0},
        {"quantity": -10},
        {"quantity": 0},
    ],
)
def test_invalid_negative_values_rejected(overrides):
    with pytest.raises(ValidationError):
        ManufacturingOperation(**{**CNC_DRILLING, **overrides})
