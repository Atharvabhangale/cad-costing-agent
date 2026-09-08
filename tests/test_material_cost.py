import pytest
from pydantic import ValidationError

from cad_costing_agent.costing import calculate_material_cost
from cad_costing_agent.part import Part


def test_material_cost_calculation():
    part = Part(part_id="P1", part_name="Bracket", material="Steel", mass_kg=2.0, quantity=1)
    result = calculate_material_cost(part)

    assert result.material_cost_per_kg == 0.80
    assert result.material_cost == pytest.approx(1.60)
    assert result.total_material_cost == pytest.approx(1.60)
    assert result.is_illustrative_rate is True


def test_quantity_multiplication():
    part = Part(part_id="P2", part_name="Bracket", material="Aluminium", mass_kg=1.5, quantity=10)
    result = calculate_material_cost(part)

    assert result.material_cost == pytest.approx(1.5 * 2.20)
    assert result.total_material_cost == pytest.approx(1.5 * 2.20 * 10)


def test_unknown_material():
    part = Part(part_id="P3", part_name="Bracket", material="Unobtainium", mass_kg=1.0, quantity=1)

    with pytest.raises(ValueError, match="Unknown material"):
        calculate_material_cost(part)


def test_negative_mass_rejected():
    with pytest.raises(ValidationError):
        Part(part_id="P4", part_name="Bracket", material="Steel", mass_kg=-1.0, quantity=1)


def test_negative_quantity_rejected():
    with pytest.raises(ValidationError):
        Part(part_id="P5", part_name="Bracket", material="Steel", mass_kg=1.0, quantity=-1)
