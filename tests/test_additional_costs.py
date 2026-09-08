import pytest
from pydantic import ValidationError

from cad_costing_agent.additional_costs import (
    AdditionalCostInputs,
    calculate_additional_costs,
)
from cad_costing_agent.operation import ManufacturingOperation
from cad_costing_agent.part import Part
from cad_costing_agent.part_cost import PartCostEstimateRequest, calculate_part_cost

# ---------------------------------------------------------------------------
# Worked example, chosen so every figure is exact and hand-checkable.
#
#   Part:      Steel @ 0.80/kg, mass 2.5 kg, quantity 4
#     material_cost         = 2.5 * 0.80            =    2.00 per part
#     total_material_cost   = 2.00 * 4              =    8.00
#
#   Operation: machine 1200/hr, labor 600/hr, setup 30 min, cycle 3 min, qty 4
#     run hours             = 3 * 4 / 60            =    0.2 h
#     machine_cost          = 1200 * 0.2            =  240.00
#     setup_cost            = 1200 * (30 / 60)      =  600.00
#     labor_cost            = 600 * 0.2             =  120.00
#     total_operation_cost                          =  960.00
#
#   direct_cost_base        = 8.00 + 960.00         =  968.00
#     overhead  10%         = 968.00 * 0.10         =   96.80
#     scrap      5%         = 968.00 * 0.05         =   48.40
#     tooling               = flat                  =  250.00
#     total_additional_cost                         =  395.20
#
#   total_part_cost         = 968.00 + 395.20       = 1363.20
# ---------------------------------------------------------------------------
WORKED_PART = dict(part_id="P100", part_name="Flange", material="Steel", mass_kg=2.5, quantity=4)
WORKED_OPERATION = dict(
    operation_id="OP1",
    operation_name="Milling",
    process_type="Milling",
    machine_rate_per_hour=1200.0,
    labor_rate_per_hour=600.0,
    setup_time_minutes=30.0,
    cycle_time_minutes=3.0,
    quantity=4,
)
EXPECTED_MATERIAL_COST = 8.00
EXPECTED_OPERATION_COST = 960.00
EXPECTED_DIRECT_COST_BASE = 968.00
EXPECTED_OVERHEAD_COST = 96.80
EXPECTED_SCRAP_COST = 48.40
EXPECTED_TOOLING_COST = 250.00
EXPECTED_TOTAL_ADDITIONAL_COST = 395.20
EXPECTED_TOTAL_PART_COST = 1363.20


def worked_request(**additional) -> PartCostEstimateRequest:
    return PartCostEstimateRequest(
        part=Part(**WORKED_PART),
        operations=[ManufacturingOperation(**WORKED_OPERATION)],
        additional_costs=AdditionalCostInputs(**additional),
    )


# --- component calculations -------------------------------------------------


def test_overhead_calculation():
    result = calculate_additional_costs(1000.0, AdditionalCostInputs(overhead_rate_percent=12.5))

    assert result.overhead_cost == pytest.approx(125.0)
    assert result.scrap_cost == 0
    assert result.tooling_cost == 0
    assert result.total_additional_cost == pytest.approx(125.0)


def test_scrap_calculation():
    result = calculate_additional_costs(1000.0, AdditionalCostInputs(scrap_rate_percent=7.5))

    assert result.scrap_cost == pytest.approx(75.0)
    assert result.overhead_cost == 0
    assert result.total_additional_cost == pytest.approx(75.0)


def test_tooling_calculation_is_a_flat_batch_amount():
    result = calculate_additional_costs(1000.0, AdditionalCostInputs(tooling_cost_per_batch=430.0))

    assert result.tooling_cost == pytest.approx(430.0)
    assert result.overhead_cost == 0
    assert result.scrap_cost == 0
    assert result.total_additional_cost == pytest.approx(430.0)


def test_all_additional_costs_combined():
    result = calculate_additional_costs(
        1000.0,
        AdditionalCostInputs(
            overhead_rate_percent=10.0, scrap_rate_percent=5.0, tooling_cost_per_batch=200.0
        ),
    )

    assert result.overhead_cost == pytest.approx(100.0)
    assert result.scrap_cost == pytest.approx(50.0)
    assert result.tooling_cost == pytest.approx(200.0)
    assert result.total_additional_cost == pytest.approx(350.0)


def test_percentages_do_not_compound_on_each_other():
    """Both percentages use the same base, so order and presence are independent."""
    overhead_only = calculate_additional_costs(1000.0, AdditionalCostInputs(overhead_rate_percent=10.0))
    scrap_only = calculate_additional_costs(1000.0, AdditionalCostInputs(scrap_rate_percent=5.0))
    both = calculate_additional_costs(
        1000.0, AdditionalCostInputs(overhead_rate_percent=10.0, scrap_rate_percent=5.0)
    )

    assert both.overhead_cost == pytest.approx(overhead_only.overhead_cost)
    assert both.scrap_cost == pytest.approx(scrap_only.scrap_cost)
    assert both.total_additional_cost == pytest.approx(
        overhead_only.total_additional_cost + scrap_only.total_additional_cost
    )


def test_tooling_is_excluded_from_the_percentage_base():
    without_tooling = calculate_additional_costs(
        1000.0, AdditionalCostInputs(overhead_rate_percent=10.0)
    )
    with_tooling = calculate_additional_costs(
        1000.0, AdditionalCostInputs(overhead_rate_percent=10.0, tooling_cost_per_batch=500.0)
    )

    # Adding tooling must not increase the overhead charged.
    assert with_tooling.overhead_cost == pytest.approx(without_tooling.overhead_cost)


def test_zero_valued_additional_costs_add_nothing():
    result = calculate_additional_costs(1000.0, AdditionalCostInputs())

    assert result.overhead_cost == 0
    assert result.scrap_cost == 0
    assert result.tooling_cost == 0
    assert result.total_additional_cost == 0


def test_result_exposes_formulas_and_assumptions():
    result = calculate_additional_costs(1000.0, AdditionalCostInputs(overhead_rate_percent=10.0))

    assert any("overhead_cost =" in formula for formula in result.formulas)
    assert any("scrap_cost =" in formula for formula in result.formulas)
    assert any("tooling_cost =" in formula for formula in result.formulas)
    assert result.assumptions


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"overhead_rate_percent": -1.0},
        {"scrap_rate_percent": -1.0},
        {"tooling_cost_per_batch": -1.0},
    ],
)
def test_negative_values_rejected(overrides):
    with pytest.raises(ValidationError):
        AdditionalCostInputs(**overrides)


@pytest.mark.parametrize("scrap_rate_percent", [100.0, 100.1, 250.0])
def test_scrap_rate_of_100_percent_or_more_rejected(scrap_rate_percent):
    """At 100% scrap no good parts are produced, so a part cost is meaningless."""
    with pytest.raises(ValidationError):
        AdditionalCostInputs(scrap_rate_percent=scrap_rate_percent)


def test_scrap_rate_just_below_100_percent_accepted():
    assert AdditionalCostInputs(scrap_rate_percent=99.9).scrap_rate_percent == 99.9


def test_absurd_overhead_rate_rejected():
    """Guards against a fraction being passed where a percent was meant."""
    with pytest.raises(ValidationError):
        AdditionalCostInputs(overhead_rate_percent=1001.0)


def test_negative_cost_base_rejected():
    with pytest.raises(ValueError, match="must not be negative"):
        calculate_additional_costs(-1.0, AdditionalCostInputs())


# --- integration with the part-level estimate -------------------------------


def test_worked_example_end_to_end():
    result = calculate_part_cost(
        worked_request(
            overhead_rate_percent=10.0, scrap_rate_percent=5.0, tooling_cost_per_batch=250.0
        )
    )

    assert result.total_material_cost == pytest.approx(EXPECTED_MATERIAL_COST)
    assert result.total_operation_cost == pytest.approx(EXPECTED_OPERATION_COST)
    assert result.direct_cost_base == pytest.approx(EXPECTED_DIRECT_COST_BASE)
    assert result.additional_costs.overhead_cost == pytest.approx(EXPECTED_OVERHEAD_COST)
    assert result.additional_costs.scrap_cost == pytest.approx(EXPECTED_SCRAP_COST)
    assert result.additional_costs.tooling_cost == pytest.approx(EXPECTED_TOOLING_COST)
    assert result.total_additional_cost == pytest.approx(EXPECTED_TOTAL_ADDITIONAL_COST)
    assert result.total_part_cost == pytest.approx(EXPECTED_TOTAL_PART_COST)


def test_total_equals_sum_of_exposed_components():
    result = calculate_part_cost(
        worked_request(
            overhead_rate_percent=10.0, scrap_rate_percent=5.0, tooling_cost_per_batch=250.0
        )
    )

    assert result.total_part_cost == pytest.approx(
        result.total_material_cost
        + result.total_operation_cost
        + result.additional_costs.overhead_cost
        + result.additional_costs.scrap_cost
        + result.additional_costs.tooling_cost
    )


def test_omitting_additional_costs_leaves_the_estimate_unchanged():
    """Backwards compatibility: no additional inputs means the pre-Step-8 total."""
    without = calculate_part_cost(
        PartCostEstimateRequest(
            part=Part(**WORKED_PART), operations=[ManufacturingOperation(**WORKED_OPERATION)]
        )
    )

    assert without.total_additional_cost == 0
    assert without.total_part_cost == pytest.approx(EXPECTED_DIRECT_COST_BASE)
    assert without.total_part_cost == pytest.approx(
        without.total_material_cost + without.total_operation_cost
    )


def test_material_only_part_still_works_with_additional_costs():
    result = calculate_part_cost(
        PartCostEstimateRequest(
            part=Part(**WORKED_PART),
            operations=[],
            additional_costs=AdditionalCostInputs(overhead_rate_percent=10.0),
        )
    )

    assert result.total_operation_cost == 0
    assert result.direct_cost_base == pytest.approx(EXPECTED_MATERIAL_COST)
    assert result.additional_costs.overhead_cost == pytest.approx(0.80)
    assert result.total_part_cost == pytest.approx(8.80)


def test_additional_costs_are_batch_level_and_not_rescaled_by_quantity():
    """Percentages ride on an already batch-scaled base; tooling is flat per batch."""
    additional = dict(
        overhead_rate_percent=10.0, scrap_rate_percent=5.0, tooling_cost_per_batch=250.0
    )

    single = calculate_part_cost(
        PartCostEstimateRequest(
            part=Part(**{**WORKED_PART, "quantity": 1}),
            operations=[ManufacturingOperation(**{**WORKED_OPERATION, "quantity": 1})],
            additional_costs=AdditionalCostInputs(**additional),
        )
    )
    batch = calculate_part_cost(
        PartCostEstimateRequest(
            part=Part(**{**WORKED_PART, "quantity": 4}),
            operations=[ManufacturingOperation(**{**WORKED_OPERATION, "quantity": 4})],
            additional_costs=AdditionalCostInputs(**additional),
        )
    )

    # Tooling is a one-time batch cost: identical regardless of quantity.
    assert batch.additional_costs.tooling_cost == pytest.approx(
        single.additional_costs.tooling_cost
    )
    # Percentage components track the base exactly — no extra quantity factor.
    assert batch.additional_costs.overhead_cost == pytest.approx(
        batch.direct_cost_base * 0.10
    )
    assert single.additional_costs.overhead_cost == pytest.approx(
        single.direct_cost_base * 0.10
    )
