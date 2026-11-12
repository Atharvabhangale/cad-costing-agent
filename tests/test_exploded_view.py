"""The exploded view: deterministic, hierarchy-aware, and display-only.

Two things are being defended.

The first is that the arrangement is *good*: groups separate, sub-assemblies
move as units, a component sitting on the assembly centre still gets a
direction, and turning the slider back to zero restores the file's own layout
exactly rather than approximately.

The second matters more. An exploded position is a **viewing preference**, and
this suite exists largely to prove it can never become anything else — that no
volume, mass, feature, time, setup or cost can be reached from it, that it is
never persisted, and that the module which computes it could not import the
costing engine even by accident.
"""

import ast
import math
from functools import lru_cache
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_costing_agent.cad_assembly import read_step_assembly
from cad_costing_agent.cad_import import decompose_assembly, detect_format
from cad_costing_agent.cad_mesh import build_assembly_mesh
from cad_costing_agent.exploded_view import (
    DISPLACEMENT_FRACTION,
    METHOD,
    MINIMUM_SPREAD,
    VISUAL_ONLY_NOTE,
    ExplodedViewState,
    build_exploded_view,
    _spiral_direction,
)
from cad_costing_agent.main import app
from tests import assembly_fixtures
from tests.step_fixtures import plain_block

client = TestClient(app)

REAL_MODELS = Path(__file__).resolve().parent.parent / "cadmodels"


@lru_cache(maxsize=None)
def fixture(name: str) -> bytes:
    return getattr(assembly_fixtures, name)()


@lru_cache(maxsize=None)
def assembly_of(name: str):
    data = fixture(name)
    return decompose_assembly(data, f"{name}.step", detect_format(f"{name}.step", data)[0])


@lru_cache(maxsize=None)
def exploded(name: str) -> ExplodedViewState:
    return build_exploded_view(assembly_of(name))


def translations(state: ExplodedViewState, factor: float) -> dict:
    return {
        placement.occurrence_id: placement.translation_at(factor)
        for placement in state.component_transforms
    }


def separation(state: ExplodedViewState, first: str, second: str, factor: float) -> float:
    positions = translations(state, factor)
    return math.dist(positions[first], positions[second])


# --- zero is the assembly as built -----------------------------------------


def test_zero_explosion_is_exactly_the_original_placement():
    """Not approximately: the displayed position is rebuilt from the stored CAD
    placement each time, so returning to zero cannot leave a drift behind."""
    state = exploded("pump_assembly")

    for placement in state.component_transforms:
        assert placement.translation_at(0.0) == tuple(placement.original_translation)


def test_the_original_placement_is_never_written_over():
    state = exploded("pump_assembly")
    assembly = assembly_of("pump_assembly")

    stated = {
        occurrence.occurrence_id: tuple(occurrence.transform.world[9:])
        for occurrence in assembly.occurrences
    }
    for placement in state.component_transforms:
        assert tuple(placement.original_translation) == stated[placement.occurrence_id]


def test_computing_an_exploded_view_does_not_mutate_the_assembly():
    assembly = assembly_of("pump_assembly")
    before = assembly.model_dump()

    build_exploded_view(assembly, factor=1.0)
    build_exploded_view(assembly, factor=0.5)

    assert assembly.model_dump() == before


# --- determinism ------------------------------------------------------------


def test_the_same_assembly_always_explodes_the_same_way():
    first = build_exploded_view(assembly_of("pump_assembly"))
    second = build_exploded_view(assembly_of("pump_assembly"))

    assert first.model_dump() == second.model_dump()


def test_a_component_on_the_assembly_centre_gets_a_deterministic_direction():
    """It would otherwise stay buried inside the others. The fallback spreads
    co-located groups over a sphere by the golden angle, which is reproducible
    and does not clump."""
    first = [_spiral_direction(index, 8) for index in range(8)]
    second = [_spiral_direction(index, 8) for index in range(8)]

    assert first == second
    for direction in first:
        assert math.isclose(math.dist((0, 0, 0), direction), 1.0, rel_tol=1e-9)
    # And they are genuinely different directions, not all one axis.
    assert len({tuple(round(value, 6) for value in d) for d in first}) == 8


def test_the_fallback_is_used_and_labelled_when_a_group_sits_on_the_centre():
    """A single group is, by definition, at its own centre."""
    state = build_exploded_view(assembly_of("deep_assembly"))

    assert state.group_count == 1
    assert {p.direction_source for p in state.component_transforms if p.group_occurrence_id} == {
        "spiral_fallback"
    }


# --- separation grows -------------------------------------------------------


def test_increasing_the_factor_increases_separation():
    state = exploded("pump_assembly")
    groups = sorted({p.group_occurrence_id for p in state.component_transforms if p.group_occurrence_id})
    first = next(p for p in state.component_transforms if p.group_occurrence_id == groups[0])
    second = next(p for p in state.component_transforms if p.group_occurrence_id == groups[1])

    distances = [
        separation(state, first.occurrence_id, second.occurrence_id, factor)
        for factor in (0.0, 0.25, 0.5, 0.75, 1.0)
    ]
    assert distances == sorted(distances)
    assert distances[-1] > distances[0]


def test_every_group_travels_the_same_distance():
    """The formula is `original + direction x distance x factor`: the direction
    varies, the distance does not, so no component is arbitrarily flung
    further than another."""
    state = exploded("pump_assembly")

    travelled = {
        round(math.dist((0, 0, 0), placement.offset), 6)
        for placement in state.component_transforms
        if placement.group_occurrence_id
    }
    assert len(travelled) == 1
    assert travelled.pop() == pytest.approx(state.distance)


def test_the_distance_is_taken_from_how_far_apart_the_groups_already_are():
    """So a 10 mm insert and a 2 m frame explode by comparable *proportions*
    rather than by the same number of millimetres."""
    state = exploded("pump_assembly")

    centres = {}
    for placement in state.component_transforms:
        if placement.group_occurrence_id:
            centres.setdefault(placement.group_occurrence_id, []).append(
                placement.original_translation
            )
    means = [
        [sum(axis) / len(values) for axis in zip(*values)] for values in centres.values()
    ]
    lows = [min(mean[axis] for mean in means) for axis in range(3)]
    highs = [max(mean[axis] for mean in means) for axis in range(3)]
    spread = math.dist(lows, highs)

    assert state.distance == pytest.approx(max(spread, MINIMUM_SPREAD) * DISPLACEMENT_FRACTION)
    assert state.distance > 0


# --- hierarchy --------------------------------------------------------------


def test_a_sub_assemblys_parts_keep_their_positions_relative_to_each_other():
    """The requirement that stops an explosion from being a scattering: the
    cover and its two pins move as one rigid group."""
    state = exploded("pump_assembly")
    assembly = assembly_of("pump_assembly")

    inside = [
        occurrence.occurrence_id
        for occurrence in assembly.occurrences
        if occurrence.parent_occurrence_id is not None
    ]
    assert len(inside) == 3  # COVER and two PINs

    for factor in (0.0, 0.5, 1.0):
        positions = translations(state, factor)
        # Flat, not a list of tuples: pytest.approx refuses nested sequences.
        offsets = [
            positions[inside[index]][axis] - positions[inside[0]][axis]
            for index in range(len(inside))
            for axis in range(3)
        ]
        if factor == 0.0:
            reference = offsets
        assert offsets == pytest.approx(reference)


def test_members_of_one_group_share_one_offset():
    state = exploded("pump_assembly")
    by_group = {}
    for placement in state.component_transforms:
        if placement.group_occurrence_id:
            by_group.setdefault(placement.group_occurrence_id, set()).add(tuple(placement.offset))

    for offsets in by_group.values():
        assert len(offsets) == 1


def test_the_explosion_happens_at_the_first_branching_depth():
    """A chain of single children is skipped: separating an only child from its
    parent moves the whole assembly and shows nothing."""
    pump = exploded("pump_assembly")
    deep = build_exploded_view(assembly_of("deep_assembly"))

    assert pump.explode_depth == 1  # three siblings at the top
    assert pump.group_count == 3
    # L0 -> L1 -> L2 -> L3 -> BLOCK is a single chain at every level.
    assert deep.explode_depth == 1
    assert deep.group_count == 1


def test_a_flat_assembly_explodes_every_part():
    state = build_exploded_view(assembly_of("flat_assembly"))

    assert state.explode_depth == 1
    assert state.group_count == 3


def test_occurrences_above_the_exploding_depth_do_not_move():
    """Their group is the whole assembly, so displacing them would displace
    everything and separate nothing."""
    state = exploded("pump_assembly")
    for placement in state.component_transforms:
        if placement.group_occurrence_id is None:
            assert placement.offset == [0.0, 0.0, 0.0]
            assert placement.direction_source == "none"


# --- quantities and instancing ----------------------------------------------


def test_explosion_creates_no_components_and_changes_no_quantities():
    assembly = assembly_of("pump_assembly")
    state = exploded("pump_assembly")

    assert len(state.component_transforms) == len(assembly.occurrences)
    assert {p.occurrence_id for p in state.component_transforms} == {
        o.occurrence_id for o in assembly.occurrences
    }
    quantities = {d.name: d.quantity for d in assembly.definitions if not d.is_assembly}
    assert quantities == {"HOUSING": 2, "COVER": 1, "PIN": 2}


def test_two_placements_of_one_definition_explode_independently():
    """They are different occurrences in different groups, and each keeps its
    own placement — but they still share one geometry."""
    state = exploded("pump_assembly")
    assembly = assembly_of("pump_assembly")
    housing_id = next(d.definition_id for d in assembly.definitions if d.name == "HOUSING")

    housings = [p for p in state.component_transforms if p.definition_id == housing_id]
    assert len(housings) == 2
    assert housings[0].offset != housings[1].offset

    mesh = build_assembly_mesh(read_step_assembly(fixture("pump_assembly")))
    assert len([d for d in mesh.definitions if d.name == "HOUSING"]) == 1


# --- the state model --------------------------------------------------------


def test_a_part_has_no_exploded_view():
    part = decompose_assembly(
        plain_block(), "block.step", detect_format("block.step", plain_block())[0]
    )
    state = build_exploded_view(part)

    assert state.enabled is False
    assert state.component_transforms == []
    assert "nothing to explode" in state.notes[0]


def test_the_arrangement_is_labelled_as_visual_only():
    """It must never read as an engineering disassembly sequence."""
    state = exploded("pump_assembly")

    assert state.method == METHOD
    assert state.note == VISUAL_ONLY_NOTE
    assert "not an assembly or disassembly sequence" in state.note
    assert "manufacturing decision" in state.note


def test_the_endpoint_returns_the_arrangement_beside_the_structure():
    response = client.post(
        "/cad/assembly",
        files={"file": ("pump.step", fixture("pump_assembly"), "application/octet-stream")},
        params={"with_mesh": "true"},
    )

    assert response.status_code == 200
    body = response.json()
    state = body["exploded_view"]
    assert state["enabled"] is True
    assert state["factor"] == 0.0
    assert len(state["component_transforms"]) == 6
    assert "not an assembly or disassembly sequence" in state["note"]
    # The mesh carries the CAD placement; the exploded view carries the offset.
    # Neither is the other.
    assert len(body["mesh"]["instances"]) == 5


def test_a_single_part_upload_gets_no_exploded_view():
    response = client.post(
        "/cad/assembly",
        files={"file": ("block.step", plain_block(), "application/octet-stream")},
    )

    assert response.status_code == 200
    assert response.json()["exploded_view"] is None


# --- engineering data is unreachable from here ------------------------------


def test_the_module_imports_nothing_from_this_application():
    """The strongest available statement that an exploded position cannot reach
    a mass, a feature or a cost: there is no path from here to any of them."""
    import cad_costing_agent.exploded_view as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not any(name.startswith("cad_costing_agent") for name in imported), imported


def test_nothing_in_the_application_consumes_an_exploded_position():
    """The other direction: the arrangement is produced for the viewer and read
    by nothing that calculates."""
    import cad_costing_agent as package

    root = Path(package.__file__).parent
    consumers = []
    for path in root.rglob("*.py"):
        if path.name in {"exploded_view.py", "main.py"}:
            continue
        if "exploded_view" in path.read_text(encoding="utf-8"):
            consumers.append(path.name)

    assert consumers == [], consumers


def test_geometry_mass_and_cost_are_identical_at_every_explosion_factor():
    """The same assembly at 0% and 100% must have identical engineering data.

    Nothing in the costing path takes an explosion factor, so this asserts the
    consequence: the numbers do not move because the picture does.
    """
    data = fixture("pump_assembly")
    form = {"material": "Steel", "source_unit": "mm", "rate_set_id": "poc-machine-shop"}

    def cost():
        return client.post(
            "/cost/assembly",
            files={"file": ("pump.step", data, "application/octet-stream")},
            data=form,
        ).json()

    baseline = cost()
    state = exploded("pump_assembly")

    # Explode it as hard as the viewer ever will, then cost it again.
    fully = translations(state, 1.0)
    assert any(
        fully[p.occurrence_id] != tuple(p.original_translation)
        for p in state.component_transforms
        if p.group_occurrence_id
    )

    after = cost()
    assert after["total_cost"] == baseline["total_cost"]
    assert [line["extended_cost"] for line in after["lines"]] == [
        line["extended_cost"] for line in baseline["lines"]
    ]


def test_component_geometry_is_untouched_by_the_arrangement():
    assembly = assembly_of("pump_assembly")
    volumes = {
        definition.name: definition.geometry.volume
        for definition in assembly.definitions
        if definition.geometry is not None
    }

    build_exploded_view(assembly, factor=1.0)

    assert {
        definition.name: definition.geometry.volume
        for definition in assembly.definitions
        if definition.geometry is not None
    } == volumes
    assert volumes["HOUSING"] == pytest.approx(assembly_fixtures.HOUSING_VOLUME)


def test_features_are_unaffected_by_the_arrangement():
    from cad_costing_agent.cad_geometry import read_step_shape
    from cad_costing_agent.features import recognize_component_features
    from tests.step_fixtures import block_with_through_hole

    shape = read_step_shape(block_with_through_hole())
    before = recognize_component_features(shape, component_id="P").model_dump(
        exclude={"elapsed_ms"}
    )
    build_exploded_view(assembly_of("pump_assembly"), factor=1.0)
    after = recognize_component_features(shape, component_id="P").model_dump(
        exclude={"elapsed_ms"}
    )

    assert before == after


def test_an_exploded_position_is_never_persisted():
    """A slider position is a viewing preference, not CAD truth. A historical
    estimate has to keep the placements the file states."""
    data = fixture("pump_assembly")
    client.post(
        "/cost/assembly",
        files={"file": ("pump.step", data, "application/octet-stream")},
        data={"material": "Steel", "source_unit": "mm", "rate_set_id": "poc-machine-shop"},
    )
    listing = client.get("/estimates").json()
    stored = client.get(f"/estimates/{listing['items'][0]['estimate_id']}").json()

    text = repr(stored)
    for forbidden in ("exploded", "explode", "explosion"):
        assert forbidden not in text.lower()

    # And the placements stored are the CAD ones.
    state = exploded("pump_assembly")
    snapshot = stored["request_snapshot"]["assembly"]
    stated = {
        occurrence["occurrence_id"]: tuple(occurrence["transform"]["world"][9:])
        for occurrence in snapshot["occurrences"]
    }
    for placement in state.component_transforms:
        assert stated[placement.occurrence_id] == tuple(placement.original_translation)


def test_the_browser_never_sends_an_exploded_position_back():
    static = Path(__file__).resolve().parent.parent / "src" / "cad_costing_agent" / "static"
    for name in ("assembly.js", "app.js"):
        source = (static / name).read_text(encoding="utf-8")
        for line in source.splitlines():
            if "body.append" in line or "explode" in line.lower():
                assert not ("body.append" in line and "explode" in line.lower()), line


# --- the real assembly ------------------------------------------------------


@pytest.mark.skipif(
    not (REAL_MODELS / "Figure 3.stp").is_file(), reason="the real assembly is not present"
)
def test_the_real_assembly_explodes_at_its_branching_depth():
    """Figure 3's top two levels are single children, so a naive explosion at
    depth 1 would move all 102 occurrences together and separate nothing."""
    data = (REAL_MODELS / "Figure 3.stp").read_bytes()
    assembly = decompose_assembly(
        data, "Figure 3.stp", detect_format("Figure 3.stp", data)[0], measure_components=False
    )
    state = build_exploded_view(assembly)

    assert state.explode_depth == 3
    assert state.group_count == 83
    assert len(state.component_transforms) == 102
    # The two occurrences above the branching depth stay where they are.
    assert sum(1 for p in state.component_transforms if p.offset == [0.0, 0.0, 0.0]) == 2


# --- the panel reads fields that actually exist ------------------------------


def test_the_panel_reads_only_fields_the_assembly_mesh_actually_has():
    """Step 25 replaced the mesh's `components` with `definitions` + `instances`,
    and one reference to the old name survived in the bill-of-materials row
    builder. It threw inside a template literal, so the panel simply rendered
    blank — no error, no missing-field message, just nothing. This catches the
    next one.
    """
    import re

    from cad_costing_agent.cad_mesh import AssemblyMesh

    static = Path(__file__).resolve().parent.parent / "src" / "cad_costing_agent" / "static"
    source = (static / "assembly.js").read_text(encoding="utf-8")

    known = set(AssemblyMesh.model_fields) | {"instance_count"}
    for field in re.findall(r"\bresult\.mesh\.([a-z_]+)", source):
        assert field in known, f"assembly.js reads mesh.{field}, which AssemblyMesh does not have"


def test_the_panel_reads_only_fields_the_exploded_view_actually_has():
    import re

    from cad_costing_agent.exploded_view import ExplodedViewState

    static = Path(__file__).resolve().parent.parent / "src" / "cad_costing_agent" / "static"
    source = (static / "assembly.js").read_text(encoding="utf-8")

    known = set(ExplodedViewState.model_fields)
    for field in re.findall(r"\bexploded(?:_view)?\.([a-z_]+)", source):
        assert field in known, f"assembly.js reads exploded view field '{field}', which is absent"
