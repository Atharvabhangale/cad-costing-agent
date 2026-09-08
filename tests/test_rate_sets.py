import pytest
from pydantic import ValidationError

from cad_costing_agent.additional_costs import AdditionalCostInputs
from cad_costing_agent.operation import ManufacturingOperation
from cad_costing_agent.part import Part
from cad_costing_agent.part_cost import PartCostEstimateRequest, calculate_part_cost
from cad_costing_agent.rate_sets import (
    SEED_RATE_SETS,
    MaterialRate,
    OperationRate,
    OperationRequirement,
    RateSet,
    resolve_material,
    resolve_operation,
)

_SEED_BY_IDENTITY = {
    (rate_set.rate_set_id, rate_set.version): rate_set for rate_set in SEED_RATE_SETS
}


def seed_rate_set(rate_set_id: str, version: str) -> RateSet:
    """A rate set straight from the seed data, with no database involved.

    Rate sets now live in SQLite and are resolved by the repository, but these
    are pure domain tests: they inject a known rate set directly rather than
    standing up storage.
    """
    return _SEED_BY_IDENTITY[(rate_set_id, version)]

# The Step 8 worked example, now resolved through a rate set:
#   Steel 0.80/kg, mass 2.5 kg, qty 4          -> material   =    8.00
#   Milling 1200/hr machine, 600/hr labor,
#     setup 30 min, cycle 3 min, qty 4         -> operations =  960.00
#   direct base                                             =  968.00
#   overhead 10% / scrap 5% / tooling 250                    =  395.20
#   TOTAL                                                    = 1363.20
WORKED_PART = dict(part_id="P100", part_name="Flange", material="Steel", mass_kg=2.5, quantity=4)
WORKED_REQUIREMENT = dict(
    operation_id="OP1",
    operation_name="Milling",
    process_type="Milling",
    setup_time_minutes=30.0,
    cycle_time_minutes=3.0,
    quantity=4,
)
EXPECTED_TOTAL_PART_COST = 1363.20


# --- model ------------------------------------------------------------------


def test_valid_rate_set():
    rate_set = RateSet(
        rate_set_id="my-shop",
        name="My Shop",
        version="1.2.3",
        description="test",
        materials={"steel": MaterialRate(name="Steel", cost_per_kg=1.0, density_kg_m3=7850)},
        operations={"milling": OperationRate(machine_rate_per_hour=100.0, labor_rate_per_hour=50.0)},
    )

    assert rate_set.rate_set_id == "my-shop"
    assert rate_set.version == "1.2.3"
    assert rate_set.is_illustrative is True
    assert rate_set.additional_costs.overhead_rate_percent == 0.0


@pytest.mark.parametrize("rate_set_id", ["", "Has Spaces", "UPPER", "trailing-", "under_score"])
def test_invalid_rate_set_id_rejected(rate_set_id):
    with pytest.raises(ValidationError):
        RateSet(rate_set_id=rate_set_id, name="X", version="1.0.0")


@pytest.mark.parametrize("version", ["", "1", "1.0", "v1.0.0", "1.0.0-beta", "one.two.three"])
def test_invalid_version_rejected(version):
    with pytest.raises(ValidationError):
        RateSet(rate_set_id="ok", name="X", version=version)


def test_missing_required_identifiers_rejected():
    with pytest.raises(ValidationError):
        RateSet(name="X", version="1.0.0")
    with pytest.raises(ValidationError):
        RateSet(rate_set_id="ok", version="1.0.0")
    with pytest.raises(ValidationError):
        RateSet(rate_set_id="ok", name="X")


@pytest.mark.parametrize(
    "overrides",
    [{"cost_per_kg": -1.0}, {"cost_per_kg": 0.0}, {"density_kg_m3": -1.0}, {"density_kg_m3": 0.0}],
)
def test_negative_material_rates_rejected(overrides):
    base = dict(name="Steel", cost_per_kg=1.0, density_kg_m3=7850.0)
    with pytest.raises(ValidationError):
        MaterialRate(**{**base, **overrides})


@pytest.mark.parametrize(
    "overrides",
    [{"machine_rate_per_hour": -1.0}, {"machine_rate_per_hour": 0.0}, {"labor_rate_per_hour": -1.0}],
)
def test_negative_operation_rates_rejected(overrides):
    base = dict(machine_rate_per_hour=100.0, labor_rate_per_hour=50.0)
    with pytest.raises(ValidationError):
        OperationRate(**{**base, **overrides})


@pytest.mark.parametrize(
    "additional",
    [
        {"overhead_rate_percent": -1.0},
        {"scrap_rate_percent": 100.0},
        {"scrap_rate_percent": -1.0},
        {"tooling_cost_per_batch": -1.0},
    ],
)
def test_invalid_percentages_rejected_inside_a_rate_set(additional):
    """A rate set reuses AdditionalCostInputs, so its bounds apply here too."""
    with pytest.raises(ValidationError):
        RateSet(
            rate_set_id="ok",
            name="X",
            version="1.0.0",
            additional_costs=AdditionalCostInputs(**additional),
        )


def test_rate_set_is_frozen():
    rate_set = seed_rate_set("poc-default", "1.0.0")

    with pytest.raises(ValidationError):
        rate_set.version = "9.9.9"


def test_reference_carries_identity_and_illustrative_flag():
    reference = seed_rate_set("poc-machine-shop", "1.0.0").reference()

    assert reference.rate_set_id == "poc-machine-shop"
    assert reference.version == "1.0.0"
    assert reference.is_illustrative is True


# --- seed data --------------------------------------------------------------
#
# Storage-backed lookup (unknown id, unknown version, latest-version
# resolution, listing) now lives in the repository and is covered by
# tests/test_persistence.py. What stays here is the seed data itself and the
# pure resolution/costing behaviour built on it.


def test_seed_data_carries_the_expected_illustrative_values():
    rate_set = seed_rate_set("poc-default", "1.0.0")

    assert rate_set.rate_set_id == "poc-default"
    assert "steel" in rate_set.materials
    assert rate_set.materials["steel"].cost_per_kg == 0.80
    assert rate_set.is_illustrative is True


def test_version_identity_is_preserved():
    """Two versions of one id are distinct rate inputs, and each stays pinned."""
    v1 = seed_rate_set("poc-machine-shop", "1.0.0")
    v2 = seed_rate_set("poc-machine-shop", "2.0.0")

    assert v1.rate_set_id == v2.rate_set_id
    assert v1.version != v2.version
    assert v1.materials["steel"].cost_per_kg != v2.materials["steel"].cost_per_kg
    assert v1.operations["milling"].machine_rate_per_hour == 1200.0
    assert v2.operations["milling"].machine_rate_per_hour == 1400.0


def test_every_seed_rate_set_is_marked_illustrative():
    assert SEED_RATE_SETS
    assert all(rate_set.is_illustrative for rate_set in SEED_RATE_SETS)


def test_seed_identities_are_unique():
    identities = [(rate_set.rate_set_id, rate_set.version) for rate_set in SEED_RATE_SETS]

    assert len(identities) == len(set(identities))


# --- resolution -------------------------------------------------------------


def test_resolve_material_builds_the_existing_material_model():
    material = resolve_material(seed_rate_set("poc-default", "1.0.0"), "Steel")

    assert material.name == "Steel"
    assert material.cost_per_kg == 0.80
    assert material.density_kg_m3 == 7850
    assert material.illustrative is True


def test_resolve_material_is_case_insensitive():
    assert resolve_material(seed_rate_set("poc-default", "1.0.0"), "  sTeEl ").cost_per_kg == 0.80


def test_resolve_material_rejects_a_material_the_rate_set_does_not_price():
    with pytest.raises(ValueError, match="no rate for material"):
        resolve_material(seed_rate_set("poc-default", "1.0.0"), "Unobtainium")


def test_resolve_operation_fills_rates_from_the_rate_set():
    operation = resolve_operation(
        seed_rate_set("poc-machine-shop", "1.0.0"), OperationRequirement(**WORKED_REQUIREMENT)
    )

    assert isinstance(operation, ManufacturingOperation)
    assert operation.machine_rate_per_hour == 1200.0
    assert operation.labor_rate_per_hour == 600.0
    # Part-specific fields pass through untouched.
    assert operation.setup_time_minutes == 30.0
    assert operation.cycle_time_minutes == 3.0
    assert operation.quantity == 4


def test_resolve_operation_rejects_an_unpriced_process_type():
    requirement = OperationRequirement(**{**WORKED_REQUIREMENT, "process_type": "Electroplating"})

    with pytest.raises(ValueError, match="no rate for process type"):
        resolve_operation(seed_rate_set("poc-machine-shop", "1.0.0"), requirement)


@pytest.mark.parametrize(
    "overrides",
    [
        {"cycle_time_minutes": -1.0},
        {"cycle_time_minutes": 0.0},
        {"setup_time_minutes": -1.0},
        {"quantity": 0},
        {"operation_id": ""},
    ],
)
def test_operation_requirement_validation_matches_the_operation_model(overrides):
    with pytest.raises(ValidationError):
        OperationRequirement(**{**WORKED_REQUIREMENT, **overrides})


# --- cost integration -------------------------------------------------------


def test_worked_example_through_a_rate_set():
    result = calculate_part_cost(
        PartCostEstimateRequest(
            part=Part(**WORKED_PART),
            operation_requirements=[OperationRequirement(**WORKED_REQUIREMENT)],
            rate_set_id="poc-machine-shop",
            rate_set_version="1.0.0",
        ),
        rate_set=seed_rate_set("poc-machine-shop", "1.0.0"),
    )

    assert result.total_material_cost == pytest.approx(8.00)
    assert result.total_operation_cost == pytest.approx(960.00)
    assert result.direct_cost_base == pytest.approx(968.00)
    assert result.additional_costs.overhead_cost == pytest.approx(96.80)
    assert result.additional_costs.scrap_cost == pytest.approx(48.40)
    assert result.additional_costs.tooling_cost == pytest.approx(250.00)
    assert result.total_part_cost == pytest.approx(EXPECTED_TOTAL_PART_COST)


def test_provenance_appears_in_the_result():
    result = calculate_part_cost(
        PartCostEstimateRequest(
            part=Part(**WORKED_PART),
            operation_requirements=[OperationRequirement(**WORKED_REQUIREMENT)],
            rate_set_id="poc-machine-shop",
            rate_set_version="1.0.0",
        ),
        rate_set=seed_rate_set("poc-machine-shop", "1.0.0"),
    )

    assert result.rate_set is not None
    assert result.rate_set.rate_set_id == "poc-machine-shop"
    assert result.rate_set.name == "POC Machine Shop"
    assert result.rate_set.version == "1.0.0"
    assert result.rate_set.is_illustrative is True
    assert result.rate_precedence


def test_resolved_rates_are_visible_in_the_breakdown():
    """The rates actually used are readable from the existing result shape."""
    result = calculate_part_cost(
        PartCostEstimateRequest(
            part=Part(**WORKED_PART),
            operation_requirements=[OperationRequirement(**WORKED_REQUIREMENT)],
            rate_set_id="poc-machine-shop",
            rate_set_version="1.0.0",
        ),
        rate_set=seed_rate_set("poc-machine-shop", "1.0.0"),
    )

    assert result.material.material_cost_per_kg == 0.80
    assert result.operations[0].machine_rate_per_hour == 1200.0
    assert result.operations[0].labor_rate_per_hour == 600.0
    assert result.additional_costs.overhead_rate_percent == 10.0
    assert result.additional_costs.scrap_rate_percent == 5.0
    assert result.additional_costs.tooling_cost_per_batch == 250.0


def test_a_different_version_produces_different_numbers():
    def total_for(version: str) -> float:
        return calculate_part_cost(
            PartCostEstimateRequest(
                part=Part(**WORKED_PART),
                operation_requirements=[OperationRequirement(**WORKED_REQUIREMENT)],
                rate_set_id="poc-machine-shop",
                rate_set_version=version,
            ),
            rate_set=seed_rate_set("poc-machine-shop", version),
        ).total_part_cost

    assert total_for("1.0.0") != total_for("2.0.0")


def test_multiple_operations_resolve_their_own_process_rates():
    result = calculate_part_cost(
        PartCostEstimateRequest(
            part=Part(**WORKED_PART),
            operation_requirements=[
                OperationRequirement(**WORKED_REQUIREMENT),
                OperationRequirement(
                    operation_id="OP2",
                    operation_name="Drilling",
                    process_type="Drilling",
                    setup_time_minutes=15.0,
                    cycle_time_minutes=1.0,
                    quantity=4,
                ),
            ],
            rate_set_id="poc-machine-shop",
            rate_set_version="1.0.0",
        ),
        rate_set=seed_rate_set("poc-machine-shop", "1.0.0"),
    )

    assert len(result.operations) == 2
    assert result.operations[0].machine_rate_per_hour == 1200.0  # milling
    assert result.operations[1].machine_rate_per_hour == 1800.0  # drilling
    assert result.total_operation_cost == pytest.approx(
        sum(operation.total_operation_cost for operation in result.operations)
    )


def test_operation_quantity_semantics_are_unchanged_for_requirements():
    request = PartCostEstimateRequest(
        part=Part(**WORKED_PART),  # quantity 4
        operation_requirements=[OperationRequirement(**{**WORKED_REQUIREMENT, "quantity": 2})],
        rate_set_id="poc-machine-shop",
        rate_set_version="1.0.0",
    )

    with pytest.raises(ValueError, match="must match part quantity"):
        calculate_part_cost(request, rate_set=seed_rate_set("poc-machine-shop", "1.0.0"))


def test_additional_cost_semantics_are_unchanged_under_a_rate_set():
    result = calculate_part_cost(
        PartCostEstimateRequest(
            part=Part(**WORKED_PART),
            operation_requirements=[OperationRequirement(**WORKED_REQUIREMENT)],
            rate_set_id="poc-machine-shop",
            rate_set_version="1.0.0",
        ),
        rate_set=seed_rate_set("poc-machine-shop", "1.0.0"),
    )

    # Percentages still ride on direct_cost_base only, and tooling is still flat.
    assert result.additional_costs.overhead_cost == pytest.approx(result.direct_cost_base * 0.10)
    assert result.additional_costs.scrap_cost == pytest.approx(result.direct_cost_base * 0.05)
    assert result.additional_costs.tooling_cost == pytest.approx(250.0)
    assert result.total_part_cost == pytest.approx(
        result.direct_cost_base + result.total_additional_cost
    )


def test_estimate_is_deterministic_for_identical_inputs():
    def run():
        return calculate_part_cost(
            PartCostEstimateRequest(
                part=Part(**WORKED_PART),
                operation_requirements=[OperationRequirement(**WORKED_REQUIREMENT)],
                rate_set_id="poc-machine-shop",
                rate_set_version="1.0.0",
            ),
            rate_set=seed_rate_set("poc-machine-shop", "1.0.0"),
        ).model_dump()

    assert run() == run()


# --- precedence and backward compatibility ----------------------------------


def test_explicit_additional_costs_override_the_rate_set():
    result = calculate_part_cost(
        PartCostEstimateRequest(
            part=Part(**WORKED_PART),
            operation_requirements=[OperationRequirement(**WORKED_REQUIREMENT)],
            rate_set_id="poc-machine-shop",
            rate_set_version="1.0.0",
            additional_costs=AdditionalCostInputs(overhead_rate_percent=1.0),
        ),
        rate_set=seed_rate_set("poc-machine-shop", "1.0.0"),
    )

    assert result.additional_costs.overhead_rate_percent == 1.0
    assert result.additional_costs.scrap_cost == 0
    assert result.additional_costs.tooling_cost == 0


def test_explicitly_rated_operations_are_never_overridden_by_the_rate_set():
    explicit = ManufacturingOperation(
        operation_id="OP1",
        operation_name="Milling",
        process_type="Milling",
        machine_rate_per_hour=999.0,
        labor_rate_per_hour=111.0,
        setup_time_minutes=30.0,
        cycle_time_minutes=3.0,
        quantity=4,
    )

    result = calculate_part_cost(
        PartCostEstimateRequest(
            part=Part(**WORKED_PART),
            operations=[explicit],
            rate_set_id="poc-machine-shop",
            rate_set_version="1.0.0",
        ),
        rate_set=seed_rate_set("poc-machine-shop", "1.0.0"),
    )

    assert result.operations[0].machine_rate_per_hour == 999.0
    assert result.operations[0].labor_rate_per_hour == 111.0


def test_explicit_and_resolved_operations_can_be_combined():
    explicit = ManufacturingOperation(
        operation_id="OP-EXPLICIT",
        operation_name="Grinding",
        process_type="Grinding",
        machine_rate_per_hour=900.0,
        labor_rate_per_hour=400.0,
        setup_time_minutes=10.0,
        cycle_time_minutes=1.0,
        quantity=4,
    )

    result = calculate_part_cost(
        PartCostEstimateRequest(
            part=Part(**WORKED_PART),
            operations=[explicit],
            operation_requirements=[OperationRequirement(**WORKED_REQUIREMENT)],
            rate_set_id="poc-machine-shop",
            rate_set_version="1.0.0",
        ),
        rate_set=seed_rate_set("poc-machine-shop", "1.0.0"),
    )

    assert [operation.operation_id for operation in result.operations] == ["OP-EXPLICIT", "OP1"]
    assert result.operations[0].machine_rate_per_hour == 900.0
    assert result.operations[1].machine_rate_per_hour == 1200.0


def test_no_rate_set_preserves_previous_behaviour_exactly():
    """The Step 8 worked example, unchanged, with explicit rates and no rate set."""
    result = calculate_part_cost(
        PartCostEstimateRequest(
            part=Part(**WORKED_PART),
            operations=[
                ManufacturingOperation(
                    operation_id="OP1",
                    operation_name="Milling",
                    process_type="Milling",
                    machine_rate_per_hour=1200.0,
                    labor_rate_per_hour=600.0,
                    setup_time_minutes=30.0,
                    cycle_time_minutes=3.0,
                    quantity=4,
                )
            ],
            additional_costs=AdditionalCostInputs(
                overhead_rate_percent=10.0, scrap_rate_percent=5.0, tooling_cost_per_batch=250.0
            ),
        )
    )

    assert result.rate_set is None
    assert result.total_part_cost == pytest.approx(EXPECTED_TOTAL_PART_COST)


def test_omitting_everything_still_yields_zero_additional_costs():
    result = calculate_part_cost(PartCostEstimateRequest(part=Part(**WORKED_PART)))

    assert result.rate_set is None
    assert result.total_additional_cost == 0
    assert result.total_part_cost == pytest.approx(8.00)


def test_selecting_a_rate_set_without_supplying_one_is_rejected():
    """Lookup lives in the repository, so the calculator refuses to guess."""
    with pytest.raises(ValueError, match="no resolved rate set was supplied"):
        calculate_part_cost(
            PartCostEstimateRequest(part=Part(**WORKED_PART), rate_set_id="poc-default")
        )


def test_injected_rate_set_must_match_the_requested_identity():
    with pytest.raises(ValueError, match="does not match the requested"):
        calculate_part_cost(
            PartCostEstimateRequest(part=Part(**WORKED_PART), rate_set_id="poc-default"),
            rate_set=seed_rate_set("poc-machine-shop", "1.0.0"),
        )


def test_injected_rate_set_must_match_the_requested_version():
    with pytest.raises(ValueError, match="version .* does not match the requested"):
        calculate_part_cost(
            PartCostEstimateRequest(
                part=Part(**WORKED_PART),
                rate_set_id="poc-machine-shop",
                rate_set_version="2.0.0",
            ),
            rate_set=seed_rate_set("poc-machine-shop", "1.0.0"),
        )


def test_operation_requirements_without_a_rate_set_rejected():
    with pytest.raises(ValueError, match="must be given"):
        calculate_part_cost(
            PartCostEstimateRequest(
                part=Part(**WORKED_PART),
                operation_requirements=[OperationRequirement(**WORKED_REQUIREMENT)],
            )
        )


def test_version_without_an_id_rejected():
    with pytest.raises(ValueError, match="without a rate_set_id"):
        calculate_part_cost(
            PartCostEstimateRequest(part=Part(**WORKED_PART), rate_set_version="1.0.0")
        )


def test_rate_set_material_not_priced_is_rejected():
    with pytest.raises(ValueError, match="no rate for material"):
        calculate_part_cost(
            PartCostEstimateRequest(
                part=Part(**{**WORKED_PART, "material": "Unobtainium"}),
                rate_set_id="poc-default",
            ),
            rate_set=seed_rate_set("poc-default", "1.0.0"),
        )
