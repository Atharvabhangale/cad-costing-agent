"""Component classification: manufactured, purchased, standard, excluded.

The thing being defended here is that an assembly stops being a bag of
identical machined parts without the application starting to invent make/buy
decisions. **No CAD format states whether a component is made or bought**, so
most of these tests are about what is *not* claimed: that a name never decides
a classification, that a purchased part never goes near the machining pipeline,
that a component nobody priced is not zero, and that the system's suggestion
survives beside the engineer's correction rather than under it.

The arithmetic is stated rather than observed. Every unit cost below is a test
value typed in as a user would type it — none of them is a market price, and
nothing in this application looks one up.
"""

from functools import lru_cache
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.assembly_costing import (
    CLASSIFICATION_SCOPE_NOTE,
    ComponentEstimate,
    roll_up,
)
from cad_costing_agent.cad_import import decompose_assembly, detect_format
from cad_costing_agent.component_classification import (
    COST_FROM_ESTIMATE,
    COST_USER_ENTERED,
    COSTED,
    CURRENCY_MISMATCH,
    EXCLUDED,
    EXCLUDED_FROM_TOTAL,
    MANUFACTURED,
    MATERIAL_FROM_CAD,
    MATERIAL_USER_SELECTED,
    NEEDS_COSTING_INPUT,
    NO_CURRENCY,
    NO_GEOMETRY,
    NO_UNIT_COST,
    NOT_A_COMPONENT,
    PURCHASED,
    STANDARD,
    SYSTEM_SUGGESTED,
    UNRESOLVED,
    UNRESOLVED_CLASSIFICATION,
    USER_SELECTED,
    ClassificationFeedback,
    ComponentCostingProfile,
    build_profiles,
    classification_feedback,
    suggest_classification,
)
from cad_costing_agent.main import app
from tests import assembly_fixtures

client = TestClient(app)

REAL_MODELS = Path(__file__).resolve().parent.parent / "cadmodels"

# Test values, entered the way a user enters them. Not prices.
BEARING_UNIT_COST = 250.0
BOLT_UNIT_COST = 12.5


@lru_cache(maxsize=None)
def fixture(name: str) -> bytes:
    return getattr(assembly_fixtures, name)()


@lru_cache(maxsize=None)
def assembly_of(name: str):
    adapter, _ = detect_format(f"{name}.step", fixture(name))
    return decompose_assembly(fixture(name), f"{name}.step", adapter)


def definition_id(assembly, name: str) -> str:
    return next(d.definition_id for d in assembly.definitions if d.name == name)


def profiles_for(assembly, decisions=None):
    return build_profiles(assembly, decisions=decisions or {})


def line_named(summary, name: str):
    return next(line for line in summary.lines if line.name == name)


def estimates_for(assembly, *names, per_unit=100.0, currency="INR"):
    """Stand-in per-part figures, as the unchanged pipeline would hand them
    over. The roll-up never asks how they were made."""
    return {
        definition_id(assembly, name): ComponentEstimate(
            definition_id=definition_id(assembly, name),
            estimate_id=f"est-{name}",
            currency=currency,
            per_unit_cost=per_unit,
            material_per_unit=per_unit * 0.6,
            operation_per_unit=per_unit * 0.4,
        )
        for name in names
    }


# --- nothing in a CAD file states make/buy ----------------------------------


def test_the_suggestion_reads_geometry_and_nothing_else():
    """Two components differing only in name, part number and material get the
    same suggestion. This is the whole honesty claim of the module, so it is
    asserted behaviourally rather than trusted."""
    assembly = assembly_of("pump_assembly")
    housing = next(d for d in assembly.definitions if d.name == "HOUSING")

    bolt_like = housing.model_copy(
        update={"name": "M8 BOLT", "part_number": "DIN-933", "material": "Stainless"}
    )
    bearing_like = housing.model_copy(
        update={"name": "6204-2RS BEARING", "part_number": "SKF-6204", "material": None}
    )

    assert suggest_classification(bolt_like).classification == MANUFACTURED
    assert suggest_classification(bearing_like).classification == MANUFACTURED
    assert suggest_classification(housing).classification == MANUFACTURED


def test_the_suggestion_function_cannot_see_a_name_or_a_material():
    """Structural, because behaviour alone would pass for a rule that reads a
    name today and starts matching on it tomorrow. The function is unparsed on
    its own so the module's prose about bolts and bearings cannot match."""
    import ast

    import cad_costing_agent.component_classification as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "suggest_classification"
    )
    body = function.body
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body.pop(0)
    code = ast.unparse(function).lower()

    for forbidden in ("name", "part_number", "material", "volume", "colour", "color", "density"):
        assert forbidden not in code, forbidden
    # It may look at exactly these two things, and does.
    assert "is_assembly" in code
    assert "geometry_status" in code


def test_a_component_with_no_geometry_is_unresolved_not_purchased():
    """The absence of geometry is the absence of evidence, not evidence that a
    part is bought."""
    assembly = assembly_of("pump_assembly")
    housing = next(d for d in assembly.definitions if d.name == "HOUSING")
    absent = housing.model_copy(update={"geometry_status": "no_geometry_in_file"})

    suggestion = suggest_classification(absent)
    assert suggestion.classification == UNRESOLVED
    assert "absence of evidence" in suggestion.caveat


def test_a_sub_assembly_gets_no_suggestion_at_all():
    assembly = assembly_of("pump_assembly")
    cover_assy = next(d for d in assembly.definitions if d.name == "COVER_ASSY")

    assert suggest_classification(cover_assy) is None
    profile = next(
        p for p in profiles_for(assembly) if p.definition_id == cover_assy.definition_id
    )
    assert profile.classification is None
    assert profile.classification_source == "unclassified"


def test_the_suggestion_is_low_confidence_and_says_what_it_is_not():
    assembly = assembly_of("pump_assembly")
    profile = next(p for p in profiles_for(assembly) if p.name == "HOUSING")

    assert profile.system_confidence == "low"
    assert "No CAD format states make/buy" in profile.system_caveat
    assert "not evidence that the part is made rather than bought" in profile.system_caveat


# --- manufactured -----------------------------------------------------------


def test_a_manufactured_component_takes_its_cost_from_the_existing_pipeline():
    assembly = assembly_of("pump_assembly")
    summary = roll_up(assembly, estimates_for(assembly, "HOUSING", "COVER", "PIN"))

    housing = line_named(summary, "HOUSING")
    assert housing.classification == MANUFACTURED
    assert housing.unit_cost_source == COST_FROM_ESTIMATE
    assert housing.estimate_id == "est-HOUSING"
    assert housing.costing_status == COSTED
    assert housing.extended_cost == pytest.approx(200.0)  # 100 x 2 occurrences


def test_the_default_behaviour_is_exactly_what_it_was_before_classification():
    """No profiles given: every measurable component is manufactured, which is
    the pre-Step-26 behaviour — and every line says the classification came
    from the system rather than from a person."""
    assembly = assembly_of("pump_assembly")
    summary = roll_up(assembly, estimates_for(assembly, "HOUSING", "COVER", "PIN"))

    assert summary.is_complete is True
    assert summary.total_cost == pytest.approx(500.0)  # (2 + 1 + 2) x 100
    assert summary.manufactured_cost == pytest.approx(500.0)
    for line in summary.lines:
        if line.is_assembly:
            continue
        assert line.classification_source == SYSTEM_SUGGESTED
        assert line.user_classification is None


def test_a_sub_assembly_is_still_not_costed_on_top_of_its_children():
    assembly = assembly_of("pump_assembly")
    summary = roll_up(assembly, estimates_for(assembly, "HOUSING", "COVER", "PIN"))

    cover_assy = line_named(summary, "COVER_ASSY")
    assert cover_assy.costing_status == NOT_A_COMPONENT
    assert cover_assy.classification is None
    assert cover_assy.extended_cost is None


# --- purchased --------------------------------------------------------------


def test_a_purchased_component_uses_the_stated_unit_cost():
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")
    profiles = profiles_for(
        assembly,
        {
            pin: {
                "user_classification": PURCHASED,
                "unit_cost": BEARING_UNIT_COST,
                "currency": "INR",
                "supplier": "Test Supplier",
                "supplier_part_number": "TS-6204",
            }
        },
    )
    summary = roll_up(
        assembly, estimates_for(assembly, "HOUSING", "COVER"), profiles=profiles
    )

    line = line_named(summary, "PIN")
    assert line.classification == PURCHASED
    assert line.unit_cost_source == COST_USER_ENTERED
    assert line.per_unit_cost == BEARING_UNIT_COST
    assert line.extended_cost == pytest.approx(BEARING_UNIT_COST * 2)
    assert line.supplier == "Test Supplier"
    assert line.supplier_part_number == "TS-6204"
    assert summary.purchased_cost == pytest.approx(500.0)


def test_a_purchased_component_never_enters_the_manufacturing_path():
    """It contributes what the user stated, and nothing about material,
    operations or setups. Estimates offered for it are simply not consulted."""
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")
    # An estimate exists for the pin — and must be ignored, because a pin that
    # is bought is not machined here whatever the geometry would allow.
    estimates = estimates_for(assembly, "HOUSING", "COVER", "PIN")
    profiles = profiles_for(
        assembly,
        {pin: {"user_classification": PURCHASED, "unit_cost": BEARING_UNIT_COST,
               "currency": "INR"}},
    )
    summary = roll_up(assembly, estimates, profiles=profiles)

    line = line_named(summary, "PIN")
    assert line.estimate_id is None
    assert line.material_extended is None
    assert line.operation_extended is None
    assert line.per_unit_cost == BEARING_UNIT_COST
    # And the manufactured breakdown covers only the components that are made.
    assert summary.material_cost == pytest.approx(180.0)  # (2 + 1) x 60
    assert summary.operation_cost == pytest.approx(120.0)


def test_a_purchased_component_with_no_price_needs_input_and_is_not_zero():
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")
    profiles = profiles_for(assembly, {pin: {"user_classification": PURCHASED}})
    summary = roll_up(
        assembly, estimates_for(assembly, "HOUSING", "COVER"), profiles=profiles
    )

    line = line_named(summary, "PIN")
    assert line.costing_status == NEEDS_COSTING_INPUT
    assert line.reason == NO_UNIT_COST
    assert line.extended_cost is None
    assert "It is not zero." in line.reason_detail
    assert summary.is_complete is False
    assert summary.needs_input_definition_count == 1


def test_a_price_with_no_currency_says_that_rather_than_no_price():
    """Found in the browser: the currency field showed INR but its value was
    never captured, so an entered price came back as "no unit cost" and sent
    someone to re-type a number that was already there. An amount with no
    currency is a distinct problem and now says so."""
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")
    profiles = profiles_for(
        assembly, {pin: {"user_classification": PURCHASED, "unit_cost": BEARING_UNIT_COST}}
    )
    summary = roll_up(assembly, {}, profiles=profiles)

    line = line_named(summary, "PIN")
    assert line.reason == NO_CURRENCY
    assert "An amount with no currency is not a cost" in line.reason_detail
    assert line.extended_cost is None


def test_the_browser_stores_the_currency_it_displays():
    """The bug above, guarded at its source: the default shown in the currency
    field has to be written into the profile, not only rendered."""
    source = (STATIC / "assembly.js").read_text(encoding="utf-8")

    assert "function assemblyCurrency()" in source
    assert "profile.currency = assemblyCurrency();" in source
    assert "profile.currency || assemblyCurrency()" in source


def test_the_changed_label_is_computed_from_the_live_answer():
    """Also found in the browser: the row read the server-computed
    `classification_changed`, which was calculated when the assembly was
    decomposed, so a row said "you confirmed it" about a classification the
    user had just changed."""
    import re

    source = (STATIC / "assembly.js").read_text(encoding="utf-8")

    assert "profile.user_classification !== profile.system_classification" in source
    assert not re.search(r"changed\s*=\s*profile\.classification_changed", source)


def test_long_text_gets_its_own_row_rather_than_a_squeezed_column():
    """The assembly panel is a narrow column. A sixth column of prose beside
    five numeric ones produced twenty-line rows and a horizontally scrolling
    page, so the reason and the costing inputs are full-width sub-rows."""
    source = (STATIC / "assembly.js").read_text(encoding="utf-8")

    assert 'class="reason-row"' in source
    assert 'class="inputs-row"' in source
    assert 'colspan="5"' in source  # the roll-up table
    assert 'colspan="3"' in source  # the classification table


# --- standard ---------------------------------------------------------------


def test_a_standard_component_uses_a_configured_unit_cost():
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")
    profiles = profiles_for(
        assembly,
        {pin: {"user_classification": STANDARD, "unit_cost": BOLT_UNIT_COST,
               "currency": "INR"}},
    )
    summary = roll_up(
        assembly, estimates_for(assembly, "HOUSING", "COVER"), profiles=profiles
    )

    line = line_named(summary, "PIN")
    assert line.classification == STANDARD
    assert line.extended_cost == pytest.approx(BOLT_UNIT_COST * 2)
    assert summary.standard_cost == pytest.approx(25.0)
    assert summary.purchased_cost == pytest.approx(0.0)


def test_no_standards_database_is_consulted():
    """A standard item's cost comes from the user and nowhere else. Nothing
    here has a catalogue, and the note says so on every response."""
    assembly = assembly_of("pump_assembly")
    summary = roll_up(assembly, {})

    assert CLASSIFICATION_SCOPE_NOTE in summary.notes
    assert "no price is looked up" in CLASSIFICATION_SCOPE_NOTE


# --- excluded ---------------------------------------------------------------


def test_an_excluded_component_contributes_nothing_and_says_why():
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")
    profiles = profiles_for(assembly, {pin: {"user_classification": EXCLUDED}})
    summary = roll_up(
        assembly, estimates_for(assembly, "HOUSING", "COVER"), profiles=profiles
    )

    line = line_named(summary, "PIN")
    assert line.excluded is True
    assert line.costing_status == EXCLUDED_FROM_TOTAL
    assert line.extended_cost == 0.0
    assert line.exclusion_reason == "user_selected"
    assert "Reason: user_selected" in line.reason_detail
    assert summary.excluded_cost == 0.0
    assert summary.excluded_definition_count == 1


def test_an_exclusion_does_not_make_the_total_incomplete():
    """A deliberate omission is not a gap. This is the one legitimate zero
    here, and it is legitimate precisely because a person authorized it."""
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")
    profiles = profiles_for(
        assembly, {pin: {"user_classification": EXCLUDED, "exclusion_reason": "shipping only"}}
    )
    summary = roll_up(
        assembly, estimates_for(assembly, "HOUSING", "COVER"), profiles=profiles
    )

    assert summary.is_complete is True
    assert summary.total_cost == pytest.approx(300.0)  # housing x2 + cover x1
    assert "deliberately excluded" in summary.completeness_note
    assert line_named(summary, "PIN").exclusion_reason == "shipping only"


def test_an_excluded_component_keeps_its_place_in_the_assembly():
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")
    profiles = profiles_for(assembly, {pin: {"user_classification": EXCLUDED}})
    summary = roll_up(assembly, {}, profiles=profiles)

    line = line_named(summary, "PIN")
    assert line.quantity == 2  # still counted, still placed
    assert summary.occurrence_count == 6


# --- unresolved -------------------------------------------------------------


def test_an_unresolved_component_needs_input_rather_than_costing_nothing():
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")
    profiles = profiles_for(assembly, {pin: {"user_classification": UNRESOLVED}})
    summary = roll_up(
        assembly, estimates_for(assembly, "HOUSING", "COVER"), profiles=profiles
    )

    line = line_named(summary, "PIN")
    assert line.costing_status == NEEDS_COSTING_INPUT
    assert line.reason == UNRESOLVED_CLASSIFICATION
    assert line.extended_cost is None
    assert "not counted as zero" in line.reason_detail
    assert summary.is_complete is False
    assert summary.unresolved_definition_count == 1


def test_a_component_with_no_geometry_reports_the_geometry_reason():
    """"Unresolved" is the classification; "the file carries no geometry" is
    the more useful thing to tell someone about why."""
    assembly = assembly_of("pump_assembly")
    housing = next(d for d in assembly.definitions if d.name == "HOUSING")
    stripped = assembly.model_copy(
        update={
            "definitions": [
                d.model_copy(update={"geometry_status": "no_geometry_in_file", "geometry": None})
                if d.definition_id == housing.definition_id
                else d
                for d in assembly.definitions
            ]
        }
    )
    summary = roll_up(stripped, {})

    line = line_named(summary, "HOUSING")
    assert line.classification == UNRESOLVED
    assert line.reason == NO_GEOMETRY
    assert "classify it as purchased or standard" in line.reason_detail


def test_an_incomplete_total_says_how_many_components_need_input():
    assembly = assembly_of("pump_assembly")
    summary = roll_up(assembly, estimates_for(assembly, "HOUSING"))

    assert summary.needs_input_definition_count == 2
    assert "need costing input" in summary.completeness_note
    assert summary.is_complete is False


# --- the user is authoritative ----------------------------------------------


def test_a_user_classification_wins_and_the_suggestion_survives_beside_it():
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")
    profiles = profiles_for(
        assembly,
        {pin: {"user_classification": PURCHASED, "unit_cost": BEARING_UNIT_COST,
               "currency": "INR", "classification_reason": "catalogue bearing"}},
    )
    profile = next(p for p in profiles if p.definition_id == pin)

    assert profile.system_classification == MANUFACTURED  # untouched
    assert profile.user_classification == PURCHASED
    assert profile.classification == PURCHASED
    assert profile.classification_source == USER_SELECTED
    assert profile.classification_changed is True

    summary = roll_up(assembly, {}, profiles=profiles)
    line = line_named(summary, "PIN")
    assert line.system_classification == MANUFACTURED
    assert line.user_classification == PURCHASED
    assert line.classification_changed is True


def test_agreeing_with_the_system_is_not_a_change():
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")
    profiles = profiles_for(assembly, {pin: {"user_classification": MANUFACTURED}})
    profile = next(p for p in profiles if p.definition_id == pin)

    assert profile.classification_source == USER_SELECTED
    assert profile.classification_changed is False


def test_a_request_cannot_rewrite_the_suggestion_or_the_quantity():
    """The decision payload owns only the user's own fields. A round trip
    through a browser must not be able to restate what the system proposed or
    how many of a component the file contains."""
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")
    profiles = profiles_for(
        assembly,
        {
            pin: {
                "user_classification": PURCHASED,
                "system_classification": PURCHASED,
                "system_confidence": "high",
                "quantity": 99,
                "name": "NOT A PIN",
            }
        },
    )
    profile = next(p for p in profiles if p.definition_id == pin)

    assert profile.system_classification == MANUFACTURED
    assert profile.system_confidence == "low"
    assert profile.quantity == 2
    assert profile.name == "PIN"


def test_an_unknown_classification_is_refused():
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")

    with pytest.raises(ValueError, match="Unknown classification"):
        profiles_for(assembly, {pin: {"user_classification": "outsourced"}})


# --- material ---------------------------------------------------------------


def test_a_material_the_file_states_is_preserved_with_its_provenance():
    assembly = assembly_of("pump_assembly")
    housing = next(d for d in assembly.definitions if d.name == "HOUSING")
    stated = assembly.model_copy(
        update={
            "definitions": [
                d.model_copy(update={"material": "EN8"})
                if d.definition_id == housing.definition_id
                else d
                for d in assembly.definitions
            ]
        }
    )
    profile = next(p for p in build_profiles(stated) if p.name == "HOUSING")

    assert profile.material == "EN8"
    assert profile.material_source == MATERIAL_FROM_CAD


def test_a_user_material_is_marked_as_chosen_rather_than_read():
    assembly = assembly_of("pump_assembly")
    housing = definition_id(assembly, "HOUSING")
    profiles = profiles_for(assembly, {housing: {"material": "Aluminium"}})
    profile = next(p for p in profiles if p.definition_id == housing)

    assert profile.material == "Aluminium"
    assert profile.material_source == MATERIAL_USER_SELECTED


def test_a_purchased_cost_is_never_derived_from_a_material_or_a_weight():
    """The unit cost is exactly what was entered, whatever the component
    weighs and whatever it is made of."""
    assembly = assembly_of("pump_assembly")
    housing = definition_id(assembly, "HOUSING")
    profiles = profiles_for(
        assembly,
        {housing: {"user_classification": PURCHASED, "unit_cost": BEARING_UNIT_COST,
                   "currency": "INR", "material": "Tungsten"}},
    )
    summary = roll_up(assembly, estimates_for(assembly, "HOUSING"), profiles=profiles)

    assert line_named(summary, "HOUSING").per_unit_cost == BEARING_UNIT_COST


# --- quantity ---------------------------------------------------------------


def test_occurrence_quantity_and_production_quantity_are_multiplied_once():
    """Four of something in an assembly built ten times is forty, reached by
    one multiplication rather than two."""
    assembly = assembly_of("pump_assembly")
    summary = roll_up(
        assembly,
        estimates_for(assembly, "HOUSING", "COVER", "PIN"),
        assembly_quantity=10,
    )

    pin = line_named(summary, "PIN")
    assert pin.quantity == 2  # per assembly, unchanged
    assert pin.assembly_quantity == 10
    assert pin.total_quantity == 20
    assert pin.extended_cost == pytest.approx(200.0)  # one assembly
    assert pin.production_extended_cost == pytest.approx(2000.0)  # ten of them
    assert summary.total_cost == pytest.approx(500.0)
    assert summary.production_total_cost == pytest.approx(5000.0)


def test_the_default_production_quantity_changes_nothing():
    assembly = assembly_of("pump_assembly")
    estimates = estimates_for(assembly, "HOUSING", "COVER", "PIN")

    one = roll_up(assembly, estimates)
    explicit = roll_up(assembly, estimates, assembly_quantity=1)

    assert one.model_dump() == explicit.model_dump()
    assert one.production_total_cost == one.total_cost


def test_a_production_quantity_of_zero_is_refused():
    assembly = assembly_of("pump_assembly")

    with pytest.raises(ValueError, match="at least 1"):
        roll_up(assembly, {}, assembly_quantity=0)


def test_a_definition_stays_one_reusable_geometry_however_many_are_built():
    assembly = assembly_of("pump_assembly")
    summary = roll_up(
        assembly, estimates_for(assembly, "HOUSING", "COVER", "PIN"), assembly_quantity=10
    )

    assert summary.part_definition_count == 3
    assert summary.definition_count == 5
    assert len([line for line in summary.lines if line.name == "PIN"]) == 1


# --- the roll-up ------------------------------------------------------------


def test_the_total_is_the_sum_of_the_three_contributing_kinds():
    assembly = assembly_of("pump_assembly")
    cover = definition_id(assembly, "COVER")
    pin = definition_id(assembly, "PIN")
    profiles = profiles_for(
        assembly,
        {
            cover: {"user_classification": PURCHASED, "unit_cost": BEARING_UNIT_COST,
                    "currency": "INR"},
            pin: {"user_classification": STANDARD, "unit_cost": BOLT_UNIT_COST,
                  "currency": "INR"},
        },
    )
    summary = roll_up(assembly, estimates_for(assembly, "HOUSING"), profiles=profiles)

    assert summary.manufactured_cost == pytest.approx(200.0)  # 100 x 2
    assert summary.purchased_cost == pytest.approx(250.0)  # 250 x 1
    assert summary.standard_cost == pytest.approx(25.0)  # 12.5 x 2
    assert summary.excluded_cost == 0.0
    assert summary.total_cost == pytest.approx(475.0)
    assert summary.is_complete is True


def test_each_contribution_is_visible_on_its_own():
    assembly = assembly_of("pump_assembly")
    summary = roll_up(assembly, estimates_for(assembly, "HOUSING", "COVER", "PIN"))

    assert summary.manufactured_cost is not None
    assert summary.purchased_cost is not None
    assert summary.standard_cost is not None
    assert summary.classification_counts == {MANUFACTURED: 3}


def test_nothing_is_costed_when_nothing_can_be():
    assembly = assembly_of("pump_assembly")
    summary = roll_up(assembly, {})

    assert summary.total_cost is None
    assert summary.production_total_cost is None
    assert "Nothing is reported as zero." in summary.completeness_note


# --- currency ---------------------------------------------------------------


def test_a_second_currency_is_reported_rather_than_converted():
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")
    profiles = profiles_for(
        assembly,
        {pin: {"user_classification": PURCHASED, "unit_cost": 30.0, "currency": "EUR"}},
    )
    summary = roll_up(
        assembly, estimates_for(assembly, "HOUSING", "COVER"), profiles=profiles
    )

    line = line_named(summary, "PIN")
    assert line.reason == CURRENCY_MISMATCH
    assert line.currency == "EUR"
    assert line.extended_cost is None
    assert summary.is_aggregatable is False
    assert summary.currencies_present == ["EUR", "INR"]
    assert "not directly aggregatable" in summary.currency_note
    assert "Nothing is converted" in summary.currency_note
    assert summary.is_complete is False


def test_one_currency_throughout_is_aggregatable():
    assembly = assembly_of("pump_assembly")
    summary = roll_up(assembly, estimates_for(assembly, "HOUSING", "COVER", "PIN"))

    assert summary.is_aggregatable is True
    assert summary.currencies_present == ["INR"]
    assert summary.currency_note == ""


def test_inr_remains_the_currency_a_roll_up_is_denominated_in():
    assembly = assembly_of("pump_assembly")
    summary = roll_up(assembly, estimates_for(assembly, "HOUSING"))

    assert summary.currency == "INR"


# --- feedback ---------------------------------------------------------------


def test_feedback_pairs_the_suggestion_with_the_correction():
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")
    profiles = profiles_for(
        assembly,
        {pin: {"user_classification": PURCHASED, "unit_cost": BEARING_UNIT_COST,
               "currency": "INR", "supplier": "Test Supplier",
               "classification_reason": "catalogue bearing"}},
    )
    records = classification_feedback(profiles)
    record = next(r for r in records if r.definition_id == pin)

    assert record.system_classification == MANUFACTURED
    assert record.user_classification == PURCHASED
    assert record.final_classification == PURCHASED
    assert record.classification_changed is True
    assert record.reason == "catalogue bearing"
    assert record.unit_cost == BEARING_UNIT_COST
    assert record.unit_cost_source == COST_USER_ENTERED
    assert record.supplier == "Test Supplier"
    assert record.recorded_at is not None


def test_feedback_is_written_only_where_a_suggestion_existed():
    assembly = assembly_of("pump_assembly")
    records = classification_feedback(profiles_for(assembly))

    # Three parts got a suggestion; the two sub-assemblies did not.
    assert len(records) == 3
    assert {r.component_name for r in records} == {"HOUSING", "COVER", "PIN"}


def test_a_recorded_correction_never_changes_a_later_suggestion():
    """Nothing is trained on this. The suggestion rule is a fixed two-branch
    test, and running it again after a correction gives the same answer."""
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")
    corrected = profiles_for(assembly, {pin: {"user_classification": PURCHASED}})
    classification_feedback(corrected)

    again = profiles_for(assembly)
    assert next(p for p in again if p.definition_id == pin).system_classification == MANUFACTURED
    assert next(p for p in again if p.definition_id == pin).user_classification is None


def test_feedback_records_are_frozen():
    record = ClassificationFeedback(definition_id="d1", system_classification=MANUFACTURED)

    with pytest.raises(Exception):
        record.definition_id = "d2"


# --- serialization ----------------------------------------------------------


def test_the_resolved_classification_is_serialized_rather_than_recomputed():
    """The browser must not have to reimplement the resolution order."""
    profile = ComponentCostingProfile(
        definition_id="d1", name="X", system_classification=MANUFACTURED,
        user_classification=PURCHASED,
    )
    payload = profile.model_dump(mode="json")

    assert payload["classification"] == PURCHASED
    assert payload["system_classification"] == MANUFACTURED
    assert payload["classification_source"] == USER_SELECTED
    assert payload["classification_changed"] is True


def test_the_roll_up_serializes_deterministically():
    assembly = assembly_of("pump_assembly")
    cover = definition_id(assembly, "COVER")
    decisions = {
        cover: {"user_classification": PURCHASED, "unit_cost": BEARING_UNIT_COST,
                "currency": "INR"}
    }
    estimates = estimates_for(assembly, "HOUSING", "PIN")

    first = roll_up(
        assembly, estimates, profiles=profiles_for(assembly, decisions), assembly_quantity=4
    ).model_dump(mode="json")
    second = roll_up(
        assembly, estimates, profiles=profiles_for(assembly, decisions), assembly_quantity=4
    ).model_dump(mode="json")

    assert first == second


# --- the endpoint -----------------------------------------------------------


def cost_assembly(components=None, plans=None, **extra):
    data = {"material": "Steel", "source_unit": "mm", "rate_set_id": "poc-machine-shop"}
    data.update(extra)
    if components is not None:
        import json

        data["components"] = json.dumps(components)
    if plans is not None:
        import json

        data["component_plans"] = json.dumps(plans)
    return client.post(
        "/cost/assembly",
        files={"file": ("pump.step", fixture("pump_assembly"), "application/octet-stream")},
        data=data,
    )


def test_the_decompose_endpoint_returns_suggested_profiles():
    response = client.post(
        "/cad/assembly?measure=true",
        files={"file": ("pump.step", fixture("pump_assembly"), "application/octet-stream")},
    )

    assert response.status_code == 200
    profiles = response.json()["component_costing"]
    assert len(profiles) == 5
    housing = next(p for p in profiles if p["name"] == "HOUSING")
    assert housing["classification"] == MANUFACTURED
    assert housing["classification_source"] == SYSTEM_SUGGESTED
    assert housing["system_confidence"] == "low"
    assert housing["user_classification"] is None


def test_the_endpoint_costs_a_mixed_assembly_through_the_real_pipeline():
    assembly = assembly_of("pump_assembly")
    response = cost_assembly(
        [
            {"definition_id": definition_id(assembly, "COVER"),
             "user_classification": PURCHASED, "unit_cost": BEARING_UNIT_COST,
             "currency": "INR", "supplier": "Test Supplier"},
            {"definition_id": definition_id(assembly, "PIN"),
             "user_classification": STANDARD, "unit_cost": BOLT_UNIT_COST,
             "currency": "INR"},
        ]
    )

    assert response.status_code == 200
    summary = response.json()
    assert summary["purchased_cost"] == pytest.approx(BEARING_UNIT_COST)
    assert summary["standard_cost"] == pytest.approx(BOLT_UNIT_COST * 2)
    assert summary["manufactured_cost"] > 0  # the housing, through the real engine
    assert summary["currency"] == "INR"

    # Every definition was accounted for, but the housing has committed no
    # manufacturing time, so the total is partial and says which kind of partial.
    assert summary["is_complete"] is False
    assert summary["unplanned_definition_count"] == 1
    assert "manufacturing plans incomplete" in summary["completeness_note"]

    # With the plans committed the same call is complete, and larger by exactly
    # what making the housing costs. Both halves run on `poc-default`, which
    # prices every process these plans contain — `poc-machine-shop` prices
    # drilling and milling only, and the pump's components turn.
    decisions = [
        {"definition_id": definition_id(assembly, "COVER"),
         "user_classification": PURCHASED, "unit_cost": BEARING_UNIT_COST, "currency": "INR"},
        {"definition_id": definition_id(assembly, "PIN"),
         "user_classification": STANDARD, "unit_cost": BOLT_UNIT_COST, "currency": "INR"},
    ]
    unplanned = cost_assembly(decisions, rate_set_id="poc-default").json()
    committed = cost_assembly(
        decisions,
        rate_set_id="poc-default",
        plans=assembly_fixtures.committed_plans(
            client, rate_set_id="poc-default", components=decisions
        ),
    ).json()

    assert unplanned["is_complete"] is False
    assert committed["is_complete"] is True
    assert committed["unplanned_definition_count"] == 0
    assert committed["operation_cost"] > 0
    assert unplanned["operation_cost"] == 0
    assert committed["total_cost"] > unplanned["total_cost"]
    # Material is untouched by any of this: the same components, the same rates.
    assert committed["material_cost"] == pytest.approx(unplanned["material_cost"])


def test_changing_a_component_to_purchased_removes_its_manufacturing_cost():
    """The verification the step asks for: the same file, the same rates, one
    classification changed, and the manufacturing path genuinely gone."""
    assembly = assembly_of("pump_assembly")
    housing = definition_id(assembly, "HOUSING")

    made = cost_assembly().json()
    bought = cost_assembly(
        [{"definition_id": housing, "user_classification": PURCHASED,
          "unit_cost": BEARING_UNIT_COST, "currency": "INR"}]
    ).json()

    made_line = next(line for line in made["lines"] if line["name"] == "HOUSING")
    bought_line = next(line for line in bought["lines"] if line["name"] == "HOUSING")

    assert made_line["unit_cost_source"] == COST_FROM_ESTIMATE
    assert made_line["material_extended"] > 0
    assert bought_line["unit_cost_source"] == COST_USER_ENTERED
    assert bought_line["material_extended"] is None
    assert bought_line["operation_extended"] is None
    assert bought_line["estimate_id"] is None
    assert bought_line["extended_cost"] == pytest.approx(BEARING_UNIT_COST * 2)
    # And the housing's material no longer reaches the assembly's material cost.
    assert bought["material_cost"] < made["material_cost"]


def test_the_endpoint_multiplies_a_production_quantity_exactly_once():
    one = cost_assembly().json()
    ten = cost_assembly(assembly_quantity=10).json()

    assert ten["total_cost"] == pytest.approx(one["total_cost"])
    assert ten["production_total_cost"] == pytest.approx(one["total_cost"] * 10)
    pin = next(line for line in ten["lines"] if line["name"] == "PIN")
    assert pin["quantity"] == 2
    assert pin["total_quantity"] == 20


def test_the_endpoint_refuses_an_unknown_classification():
    assembly = assembly_of("pump_assembly")
    response = cost_assembly(
        [{"definition_id": definition_id(assembly, "PIN"),
          "user_classification": "outsourced"}]
    )

    assert response.status_code == 400
    assert "Unknown classification" in response.json()["detail"]


def test_the_endpoint_refuses_a_decision_about_a_component_that_is_not_there():
    response = cost_assembly([{"definition_id": "no-such-component",
                               "user_classification": PURCHASED}])

    assert response.status_code == 404
    assert "no-such-component" in response.json()["detail"]


def test_the_endpoint_refuses_malformed_decisions_rather_than_half_applying_them():
    response = client.post(
        "/cost/assembly",
        files={"file": ("pump.step", fixture("pump_assembly"), "application/octet-stream")},
        data={"material": "Steel", "components": "{not json"},
    )

    assert response.status_code == 400
    assert "not valid JSON" in response.json()["detail"]


# --- persistence and history ------------------------------------------------


def stored_estimates():
    return client.get("/estimates").json()["items"]


def test_the_classification_decisions_are_stored_on_the_estimate():
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")
    cost_assembly(
        [{"definition_id": pin, "user_classification": PURCHASED,
          "unit_cost": BEARING_UNIT_COST, "currency": "INR",
          "supplier": "Test Supplier", "supplier_part_number": "TS-6204"}]
    )

    stored = client.get(f"/estimates/{stored_estimates()[0]['estimate_id']}").json()
    decisions = stored["request_snapshot"]["component_costing"]
    pin_decision = next(d for d in decisions if d["definition_id"] == pin)

    assert pin_decision["system_classification"] == MANUFACTURED
    assert pin_decision["user_classification"] == PURCHASED
    assert pin_decision["classification"] == PURCHASED
    assert pin_decision["unit_cost"] == BEARING_UNIT_COST
    assert pin_decision["supplier_part_number"] == "TS-6204"
    assert pin_decision["quantity"] == 2
    # And the contributions are in the result snapshot.
    line = next(
        line for line in stored["result_snapshot"]["lines"] if line["definition_id"] == pin
    )
    assert line["classification"] == PURCHASED
    assert line["extended_cost"] == pytest.approx(BEARING_UNIT_COST * 2)


def test_the_feedback_pairs_travel_with_the_estimate():
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")
    cost_assembly(
        [{"definition_id": pin, "user_classification": PURCHASED,
          "unit_cost": BEARING_UNIT_COST, "currency": "INR",
          "classification_reason": "catalogue bearing"}]
    )

    stored = client.get(f"/estimates/{stored_estimates()[0]['estimate_id']}").json()
    feedback = stored["result_snapshot"]["classification_feedback"]
    record = next(r for r in feedback if r["definition_id"] == pin)

    assert record["system_classification"] == MANUFACTURED
    assert record["user_classification"] == PURCHASED
    assert record["classification_changed"] is True
    assert record["reason"] == "catalogue bearing"
    assert record["unit_cost"] == BEARING_UNIT_COST


def test_a_later_classification_does_not_alter_an_earlier_estimate():
    """Historical immutability, the same rule every other estimate follows: a
    later decision produces a new estimate, never an edit to an old one."""
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")

    cost_assembly()  # everything manufactured
    first_id = stored_estimates()[0]["estimate_id"]
    first = client.get(f"/estimates/{first_id}").json()

    cost_assembly(
        [{"definition_id": pin, "user_classification": PURCHASED,
          "unit_cost": BEARING_UNIT_COST, "currency": "INR"}]
    )

    unchanged = client.get(f"/estimates/{first_id}").json()
    assert unchanged["result_snapshot"] == first["result_snapshot"]
    assert unchanged["request_snapshot"] == first["request_snapshot"]
    assert len(stored_estimates()) == 2
    assert stored_estimates()[0]["estimate_id"] != first_id


def test_the_stored_totals_follow_the_production_quantity():
    cost_assembly(assembly_quantity=10)
    stored = client.get(f"/estimates/{stored_estimates()[0]['estimate_id']}").json()

    assert stored["quantity"] == 10
    assert stored["total_part_cost"] == pytest.approx(
        stored["result_snapshot"]["production_total_cost"]
    )


# --- nothing else changed ---------------------------------------------------


def test_classification_does_not_reach_the_exploded_view():
    """Step 25's arrangement is computed from placements alone and must stay
    that way: a classification is a costing decision, not a position."""
    response = client.post(
        "/cad/assembly?measure=true&with_mesh=true",
        files={"file": ("pump.step", fixture("pump_assembly"), "application/octet-stream")},
    )
    body = response.json()

    assert body["exploded_view"]["enabled"] is True
    serialized = str(body["exploded_view"])
    for word in ("manufactured", "purchased", "classification", "unit_cost"):
        assert word not in serialized


def test_component_features_are_unaffected_by_classification():
    assembly = assembly_of("pump_assembly")
    response = client.post(
        "/cad/assembly/features",
        files={"file": ("pump.step", fixture("pump_assembly"), "application/octet-stream")},
        data={"component_id": definition_id(assembly, "HOUSING")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["component_id"] == "HOUSING"
    assert "classification" not in body


def test_the_viewer_mesh_is_unchanged_by_classification():
    response = client.post(
        "/cad/assembly?measure=true&with_mesh=true",
        files={"file": ("pump.step", fixture("pump_assembly"), "application/octet-stream")},
    )
    mesh = response.json()["mesh"]

    assert len(mesh["definitions"]) == 3
    assert len(mesh["instances"]) == 5


def test_an_excluded_component_is_still_drawn_and_still_selectable():
    """Excluding a component from the cost is a costing decision. It is still
    in the file, still in the tree, and still in the picture."""
    response = client.post(
        "/cad/assembly?measure=true&with_mesh=true",
        files={"file": ("pump.step", fixture("pump_assembly"), "application/octet-stream")},
    )
    body = response.json()
    assembly = assembly_of("pump_assembly")
    pin = definition_id(assembly, "PIN")

    summary = roll_up(
        assembly, {}, profiles=profiles_for(assembly, {pin: {"user_classification": EXCLUDED}})
    )
    assert line_named(summary, "PIN").excluded is True
    assert any(i["definition_id"] == pin for i in body["mesh"]["instances"])


# --- the browser decides nothing about a classification ---------------------


STATIC = Path(__file__).resolve().parent.parent / "src" / "cad_costing_agent" / "static"


def test_the_panel_offers_every_classification_and_invents_none():
    source = (STATIC / "assembly.js").read_text(encoding="utf-8")

    for value in ("manufactured", "purchased", "standard", "excluded", "unresolved"):
        assert f'"{value}"' in source or f"{value}:" in source


def test_the_browser_never_writes_over_the_system_suggestion():
    """It may set `user_classification`; writing `system_classification` would
    destroy exactly the pair this step exists to keep."""
    import re

    source = (STATIC / "assembly.js").read_text(encoding="utf-8")

    assert re.search(r"\.user_classification\s*=(?!=)", source)
    assert not re.search(r"\.system_classification\s*=(?!=)", source)
    assert not re.search(r"\.system_confidence\s*=(?!=)", source)
    assert not re.search(r"\.classification_changed\s*=(?!=)", source)


def test_the_panel_reads_only_fields_the_api_actually_declares():
    """The Step 25 blank-panel bug: a renamed field threw inside a template
    literal and the panel rendered empty with no error."""
    import re

    from cad_costing_agent.assembly_costing import ComponentCostLine

    source = (STATIC / "assembly.js").read_text(encoding="utf-8")
    declared = set(ComponentCostingProfile.model_fields) | {
        "classification",
        "classification_source",
        "classification_changed",
    }
    for field in re.findall(r"\bprofile\.([a-z_]+)", source):
        assert field in declared, field

    line_fields = set(ComponentCostLine.model_fields)
    for field in re.findall(r"\bline\.([a-z_]+)", source):
        assert field in line_fields, field


def test_the_classification_card_is_in_the_page():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'id="component-costing-card"' in html
    assert 'id="component-detail"' in html
    assert 'id="assembly_quantity"' in html
    assert "No CAD format states make/buy" in html


# --- the real assembly ------------------------------------------------------


@pytest.mark.skipif(
    not (REAL_MODELS / "Figure 3.stp").is_file(), reason="the real assembly is not present"
)
def test_the_real_assembly_classifies_every_component_it_can_name():
    """36 part definitions, none of which the file carries geometry for. Every
    one is `unresolved` with the reason, and none is quietly zero."""
    from cad_costing_agent.cad_assembly import read_step_assembly

    data = (REAL_MODELS / "Figure 3.stp").read_bytes()
    adapter, _ = detect_format("Figure 3.stp", data)
    assembly = decompose_assembly(
        data, "Figure 3.stp", adapter, structure=read_step_assembly(data)
    )
    profiles = build_profiles(assembly)
    summary = roll_up(assembly, {}, profiles=profiles)

    assert len(profiles) == len(assembly.definitions)
    parts = [p for p in profiles if not p.is_assembly]
    assert all(p.classification == UNRESOLVED for p in parts)
    assert summary.total_cost is None
    assert all(line.extended_cost is None for line in summary.lines)
    assert "Nothing is reported as zero." in summary.completeness_note


# --- the estimate an assembly roll-up is saved as ----------------------------


def test_cost_assembly_returns_the_estimate_it_saved():
    """The roll-up has always been persisted; now the caller is told where.

    Without the id in the response the browser had nothing to point a quotation
    at, so the Quote screen fell back to the `/cost/cad-part` estimate — for an
    assembly, the same file costed as one lump of material.
    """
    summary = cost_assembly().json()

    assert summary["estimate_id"], "the roll-up must name the estimate it was saved as"

    stored = client.get(f"/estimates/{summary['estimate_id']}")
    assert stored.status_code == 200
    detail = stored.json()
    assert detail["source"] == "cost/assembly"
    # `total_part_cost` is rounded to two decimals by the money policy;
    # `production_total_cost` is a plain float and serializes exact. Agreement
    # is to the rounding, which is what "the same figure" means here.
    assert detail["total_part_cost"] == pytest.approx(
        summary["production_total_cost"], abs=0.005
    )


def test_an_unplanned_assembly_is_not_quotable():
    """The state that produced a plausible, badly wrong number.

    Every component costed, coverage complete, arithmetic exact — and nothing
    charged for making anything, because no manufacturing time was committed.
    The figure is real, so it must not reach a customer document.
    """
    summary = cost_assembly().json()

    assert summary["unplanned_definition_count"] > 0
    assert summary["is_complete"] is False

    quotation = client.get(f"/estimates/{summary['estimate_id']}/quotation").json()
    assert quotation["available"] is False
    codes = [blocker["code"] for blocker in quotation["blockers"]]
    assert "assembly_manufacturing_incomplete" in codes
    # The engineering view is never withheld: the person fixing it needs it.
    engineering = client.get(
        f"/estimates/{summary['estimate_id']}/quotation?view=engineering"
    ).json()
    assert engineering["blockers"], "the reason must travel with the refusal"


def test_the_assembly_estimate_quotes_as_an_assembly():
    """A quotation made from a reviewed assembly is an assembly document."""
    summary = cost_assembly(
        rate_set_id="poc-default",
        plans=assembly_fixtures.committed_plans(client, rate_set_id="poc-default"),
    ).json()
    assert summary["is_complete"] is True

    quotation = client.get(f"/estimates/{summary['estimate_id']}/quotation?view=customer")
    assert quotation.status_code == 200
    document = quotation.json()

    assert document["available"] is True, document["blockers"]
    assert document["subject"]["kind"] == "assembly"
    assert document["assembly"] is not None, "the component section must be present"
    assert document["assembly"]["assembly_name"] == summary["assembly_name"]
    assert document["totals"]["total"] == pytest.approx(
        summary["production_total_cost"], abs=0.005
    )


def test_the_part_estimate_for_the_same_file_is_a_different_document():
    """Two questions, two estimates. Neither is the other's answer."""
    roll_up = cost_assembly().json()

    part = client.post(
        "/cost/cad-part",
        files={"file": ("pump.step", fixture("pump_assembly"), "application/octet-stream")},
        data={
            "material": "Steel", "part_id": "P-1", "part_name": "Whole file",
            "quantity": 1, "source_unit": "mm", "rate_set_id": "poc-machine-shop",
        },
    )
    assert part.status_code == 200, part.text
    part_id = part.json()["part_cost"]["estimate_id"]

    assert part_id != roll_up["estimate_id"]
    assert client.get(f"/estimates/{part_id}/quotation").json()["subject"]["kind"] == "part"


def test_carrying_an_estimate_id_changed_no_figure():
    """Two identical roll-ups differ only in the estimate they were saved as."""
    first = cost_assembly().json()
    second = cost_assembly().json()

    assert first["estimate_id"] != second["estimate_id"], "each roll-up is its own estimate"
    for field in (
        "total_cost", "production_total_cost", "material_cost",
        "operation_cost", "additional_cost", "assembly_quantity",
    ):
        assert first[field] == second[field]
    assert [line["extended_cost"] for line in first["lines"]] == [
        line["extended_cost"] for line in second["lines"]
    ]


def test_assembly_quantity_still_multiplies_exactly_once():
    """Per-assembly figures stay per-assembly; only the production total scales."""
    one = cost_assembly(assembly_quantity=1).json()
    ten = cost_assembly(assembly_quantity=10).json()

    assert one["total_cost"] == pytest.approx(ten["total_cost"])
    assert ten["production_total_cost"] == pytest.approx(one["production_total_cost"] * 10)
    for a, b in zip(one["lines"], ten["lines"]):
        assert a["per_unit_cost"] == b["per_unit_cost"]
        assert a["extended_cost"] == b["extended_cost"]
        if not a["is_assembly"]:
            assert b["total_quantity"] == a["total_quantity"] * 10


def test_the_quotation_total_is_the_stored_assembly_estimate():
    """The document presents the estimate; it must not recompute it.

    Three figures have to agree: what the roll-up returned, what the estimate
    stored, and what the quotation prints. A disagreement anywhere means a
    document can quote a number the estimate never held.
    """
    summary = cost_assembly(
        assembly_quantity=3,
        rate_set_id="poc-default",
        plans=assembly_fixtures.committed_plans(client, rate_set_id="poc-default"),
    ).json()
    estimate_id = summary["estimate_id"]

    stored = client.get(f"/estimates/{estimate_id}").json()
    document = client.get(f"/estimates/{estimate_id}/quotation").json()

    assert document["available"] is True, document["blockers"]
    assert document["totals"]["total"] == pytest.approx(stored["total_part_cost"], abs=0.005)
    assert document["totals"]["total"] == pytest.approx(
        summary["production_total_cost"], abs=0.005
    )


def test_a_part_estimate_for_the_same_file_leaves_the_assembly_estimate_alone():
    """Two estimates coexist. Neither edits the other, in either order.

    An estimate is immutable once written, so this is really a check that
    `/cost/cad-part` writes a *new* record rather than touching the roll-up's —
    which is what the browser then has to choose between.
    """
    roll_up = cost_assembly().json()
    before = client.get(f"/estimates/{roll_up['estimate_id']}").json()

    part = client.post(
        "/cost/cad-part",
        files={"file": ("pump.step", fixture("pump_assembly"), "application/octet-stream")},
        data={
            "material": "Steel",
            "part_id": "P-1",
            "part_name": "Whole file",
            "quantity": 10,
            "source_unit": "mm",
            "rate_set_id": "poc-machine-shop",
        },
    )
    assert part.status_code == 200, part.text

    after = client.get(f"/estimates/{roll_up['estimate_id']}").json()
    assert after == before, "the roll-up estimate is immutable and untouched"
    assert (
        client.get(f"/estimates/{roll_up['estimate_id']}/quotation").json()["subject"]["kind"]
        == "assembly"
    )
