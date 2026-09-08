"""Repairing faceted geometry — and, more importantly, refusing to.

This system produces monetary quotations, so a wrong volume is worse than no
volume. The single repair implemented here is the one case where nothing has to
be chosen: a triangulated surface missing one facet leaves a boundary of exactly
three edges over three vertices that are already present, and exactly one
triangle can fill it, in an orientation the unmatched directed edges fix.

Everything else is refused. Most of these tests are therefore about what does
*not* happen: a four-edge hole offers a choice of diagonal, an open sheet is
supposed to be open, and a tolerance is never widened just to make a model
close.
"""

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient

from cad_costing_agent.cad_faceted import (
    REPAIR_METHOD,
    analyse_faceted_volume,
    restore_missing_facets,
)
from cad_costing_agent.cad_geometry import extract_geometry_from_bytes
from cad_costing_agent.main import app
from tests.faceted_fixtures import faceted_plain_block, faceted_step_from_mesh
from tests.step_fixtures import ALL_FIXTURES
from tests.test_faceted_geometry import CUBE_TRIANGLES, CUBE_VERTICES

client = TestClient(app)


def mesh(vertices, triangles, **kwargs) -> bytes:
    return faceted_step_from_mesh(
        np.asarray(vertices, dtype=float), np.asarray(triangles, dtype=int), **kwargs
    )


def cube_missing(*facets) -> bytes:
    return mesh(CUBE_VERTICES * 10.0, np.delete(CUBE_TRIANGLES, list(facets), axis=0))


# --- the one repair that is justified ---------------------------------------


def test_a_single_missing_facet_is_restored_exactly():
    """Three boundary edges over three existing vertices admit one triangle."""
    topology = analyse_faceted_volume(cube_missing(5))

    assert topology.restored_facet_count == 1
    assert topology.is_watertight
    assert topology.volume == pytest.approx(1000.0, rel=1e-12)


def test_the_restored_volume_matches_the_intact_model_exactly():
    repaired = extract_geometry_from_bytes(cube_missing(5), ".step")
    intact = extract_geometry_from_bytes(mesh(CUBE_VERTICES * 10.0, CUBE_TRIANGLES), ".step")

    assert repaired.volume == pytest.approx(intact.volume, rel=1e-12)
    assert repaired.surface_area == pytest.approx(intact.surface_area, rel=1e-12)


@pytest.mark.parametrize("facet", range(12))
def test_any_single_missing_facet_of_a_cube_is_recovered(facet):
    """Not a lucky orientation: every facet of the cube, one at a time."""
    topology = analyse_faceted_volume(cube_missing(facet))

    assert topology.restored_facet_count == 1
    assert topology.is_watertight
    assert topology.volume == pytest.approx(1000.0, rel=1e-12)


def two_cubes(missing_a=(), missing_b=()):
    """Two cubes far enough apart to share nothing, each optionally holed."""
    vertices = np.vstack([CUBE_VERTICES * 10.0, CUBE_VERTICES * 10.0 + np.array([50.0, 0.0, 0.0])])
    first = np.delete(CUBE_TRIANGLES, list(missing_a), axis=0)
    second = np.delete(CUBE_TRIANGLES, list(missing_b), axis=0) + 8
    return mesh(vertices, np.vstack([first, second]))


def test_separate_missing_facets_are_each_restored():
    """Holes that share no vertex are independent, and all are repaired."""
    topology = analyse_faceted_volume(two_cubes(missing_a=(5,), missing_b=(2,)))

    assert topology.restored_facet_count == 2
    assert topology.is_watertight
    assert topology.volume == pytest.approx(2000.0, rel=1e-12)


def test_two_holes_sharing_a_vertex_are_left_alone():
    """A conservative limit, and the safe direction to fail in.

    Boundary chains are traced through shared vertices, so two triangular
    holes that touch at a corner form one five-vertex chain rather than two
    three-vertex ones. That is no longer the uniquely-determined case, so
    neither is filled.
    """
    touching = analyse_faceted_volume(cube_missing(0, 5))

    assert touching.restored_facet_count == 0
    assert not touching.is_watertight


def test_the_repair_is_reported_rather_than_hidden():
    geometry = extract_geometry_from_bytes(cube_missing(5), ".step")

    assert geometry.repair_applied is True
    assert geometry.repair_method == REPAIR_METHOD
    assert geometry.repaired_facet_count == 1
    assert geometry.volume_confidence == "repaired"
    assert geometry.repair_tolerance_mm is not None


def test_sound_geometry_is_left_alone_and_says_so():
    geometry = extract_geometry_from_bytes(faceted_plain_block(), ".step")

    assert geometry.repair_applied is False
    assert geometry.repair_method is None
    assert geometry.repaired_facet_count == 0
    assert geometry.volume_confidence == "high"


def test_restoring_a_facet_needs_no_tolerance_at_all():
    """It is topology: existing vertices, existing edges, one possible triangle."""
    holed = np.delete(CUBE_TRIANGLES, 5, axis=0)

    repaired, count = restore_missing_facets(holed)

    assert count == 1
    assert len(repaired) == 12
    # The restored triangle uses only vertices that were already present.
    assert set(repaired[-1].tolist()) <= set(holed.reshape(-1).tolist())


# --- what is deliberately not repaired --------------------------------------


def test_a_two_facet_hole_is_not_filled_because_the_diagonal_would_be_a_choice():
    topology = analyse_faceted_volume(cube_missing(4, 5))

    assert topology.restored_facet_count == 0
    assert not topology.is_watertight


def test_a_genuine_open_sheet_stays_open():
    """A surface that is meant to be open must not be turned into a solid."""
    sheet = mesh(
        [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [10.0, 10.0, 0.0], [0.0, 10.0, 0.0]],
        [[0, 1, 2], [0, 2, 3]],
    )

    topology = analyse_faceted_volume(sheet)

    assert topology.restored_facet_count == 0
    assert not topology.is_watertight
    assert topology.components[0].boundary_edge_count == 4


def test_a_large_missing_face_is_not_reconstructed():
    """Filling a many-edged hole would mean inventing a surface."""
    # An open-topped box: the whole top face is absent, an eight-edge boundary.
    open_top = np.array([t for i, t in enumerate(CUBE_TRIANGLES) if i not in (2, 3)])

    topology = analyse_faceted_volume(mesh(CUBE_VERTICES * 10.0, open_top))

    assert topology.restored_facet_count == 0
    assert not topology.is_watertight


def test_a_t_junction_is_not_silently_repaired():
    """T-junctions are a known gap, and they fail closed rather than open.

    Two coplanar strips meeting along an edge that one side splits and the
    other does not. Nothing here can be filled without subdividing an existing
    triangle, which is not attempted, so the mesh stays open.
    """
    vertices = [
        [0.0, 0.0, 0.0], [2.0, 0.0, 0.0], [2.0, 1.0, 0.0], [0.0, 1.0, 0.0],
        [1.0, 0.0, 0.0],  # mid-edge vertex present on one side only
        [0.0, -1.0, 0.0], [1.0, -1.0, 0.0], [2.0, -1.0, 0.0],
    ]
    triangles = [
        [0, 1, 2], [0, 2, 3],          # upper strip, undivided edge 0-1
        [5, 6, 4], [5, 4, 0],          # lower strip, split at the mid vertex
        [6, 7, 1], [6, 1, 4],
    ]

    topology = analyse_faceted_volume(mesh(vertices, triangles))

    assert topology.restored_facet_count == 0
    assert not topology.is_watertight


def test_a_repair_never_creates_a_non_manifold_edge():
    """Only edges used exactly once are candidates."""
    # A cube plus a stray triangle hanging off one of its edges.
    vertices = np.vstack([CUBE_VERTICES * 10.0, np.array([[5.0, 5.0, 25.0]])])
    triangles = np.vstack([CUBE_TRIANGLES, np.array([[0, 1, 8]])])

    topology = analyse_faceted_volume(mesh(vertices, triangles))

    assert topology.restored_facet_count == 0
    assert not topology.is_watertight


def test_non_manifold_geometry_is_detected_and_refused():
    """Three triangles on one edge is corrupt topology, not a hole."""
    vertices = [
        [0.0, 0.0, 0.0], [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0],
    ]
    triangles = [[0, 1, 2], [0, 1, 3], [0, 1, 4]]

    topology = analyse_faceted_volume(mesh(vertices, triangles))

    assert topology.components[0].non_manifold_edge_count > 0
    assert not topology.is_watertight


# --- welding tolerance ------------------------------------------------------


def split_cube(gap: float):
    """A cube built as two halves whose shared corners are `gap` apart."""
    lower = CUBE_VERTICES.copy() * 10.0
    lower[:, 2] = np.where(CUBE_VERTICES[:, 2] > 0, 5.0, 0.0)
    upper = CUBE_VERTICES.copy() * 10.0
    upper[:, 2] = np.where(CUBE_VERTICES[:, 2] > 0, 10.0, 5.0 + gap)
    return np.vstack([lower, upper]), np.vstack([CUBE_TRIANGLES, CUBE_TRIANGLES + 8])


def test_two_halves_written_at_the_same_position_weld_into_one_body():
    vertices, triangles = split_cube(0.0)

    topology = analyse_faceted_volume(mesh(vertices, triangles))

    assert len(topology.components) == 1


def test_halves_separated_by_more_than_the_tolerance_stay_separate():
    """The tolerance is never widened to force a model closed."""
    vertices, triangles = split_cube(1e-4)

    topology = analyse_faceted_volume(mesh(vertices, triangles))

    assert len(topology.components) == 2


def test_the_weld_tolerance_is_a_tiny_fraction_of_the_model():
    """Small enough that it cannot merge geometry that is genuinely apart."""
    topology = analyse_faceted_volume(mesh(CUBE_VERTICES * 100.0, CUBE_TRIANGLES))

    assert topology.weld_tolerance > 0
    assert topology.weld_tolerance < 100.0 * 1e-6


def test_two_bodies_a_micron_apart_are_not_merged():
    vertices = np.vstack([CUBE_VERTICES * 10.0, CUBE_VERTICES * 10.0 + np.array([10.000001, 0, 0])])
    triangles = np.vstack([CUBE_TRIANGLES, CUBE_TRIANGLES + 8])

    topology = analyse_faceted_volume(mesh(vertices, triangles))

    assert len(topology.components) == 2
    assert topology.is_watertight
    assert topology.volume == pytest.approx(2000.0, rel=1e-9)


# --- stability and determinism ----------------------------------------------


def test_a_repaired_volume_is_stable_across_repeated_runs():
    data = cube_missing(5)

    results = [extract_geometry_from_bytes(data, ".step").model_dump() for _ in range(3)]

    assert results[0] == results[1] == results[2]


def test_the_repair_is_independent_of_facet_order():
    """Deterministic on content, not on the order the file happens to list."""
    holed = np.delete(CUBE_TRIANGLES, 5, axis=0)
    shuffled = holed[::-1].copy()

    first = analyse_faceted_volume(mesh(CUBE_VERTICES * 10.0, holed))
    second = analyse_faceted_volume(mesh(CUBE_VERTICES * 10.0, shuffled))

    assert first.restored_facet_count == second.restored_facet_count == 1
    assert first.volume == pytest.approx(second.volume, rel=1e-12)


def test_a_flipped_triangle_is_still_caught_after_repair():
    flipped = CUBE_TRIANGLES.copy()
    flipped[0] = flipped[0][::-1]

    topology = analyse_faceted_volume(mesh(CUBE_VERTICES * 10.0, flipped))

    assert topology.components[0].winding_consistent is False
    assert not topology.is_watertight


# --- a repaired model is costable -------------------------------------------


def test_a_repaired_faceted_model_can_be_costed():
    response = client.post(
        "/cost/cad-part",
        files={"file": ("holed.step", cube_missing(5), "application/octet-stream")},
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "Repaired",
            "quantity": "10",
            "rate_set_id": "poc-machine-shop",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["geometry"]["repair_applied"] is True
    assert body["geometry"]["volume_confidence"] == "repaired"
    assert body["part_cost"]["total_part_cost"] > 0


def test_a_model_that_could_not_be_repaired_says_the_repair_was_tried():
    """The refusal names what was attempted, so the gap is visible."""
    # One cube missing a single facet, which is restored; another missing an
    # adjacent pair, which is not. The model stays open, and says both.
    geometry = extract_geometry_from_bytes(two_cubes(missing_a=(5,), missing_b=(4, 5)), ".step")

    assert geometry.geometry_status == "incomplete_requires_input"
    assert geometry.repaired_facet_count == 1
    assert "three-edge hole" in geometry.incomplete.explanation
    assert geometry.incomplete.restored_facet_count == 1


# --- nothing else changed ---------------------------------------------------


def test_analytic_step_is_untouched_by_any_of_this():
    geometry = extract_geometry_from_bytes(ALL_FIXTURES["block_with_through_hole"](), ".step")

    expected = 60.0 * 40.0 * 20.0 - np.pi * 5.0**2 * 20.0
    assert geometry.volume == pytest.approx(expected, rel=1e-9)
    assert geometry.geometry_source == "brep"
    assert geometry.repair_applied is False


def test_stl_is_untouched_by_any_of_this():
    box = trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")

    geometry = extract_geometry_from_bytes(box, ".stl")

    assert geometry.volume == pytest.approx(6000.0, rel=1e-9)
    assert geometry.repair_applied is False


def test_analytic_costing_is_unchanged():
    response = client.post(
        "/cost/cad-part",
        files={
            "file": (
                "block.step",
                ALL_FIXTURES["block_with_through_hole"](),
                "application/octet-stream",
            )
        },
        data={
            "material": "Steel",
            "source_unit": "mm",
            "part_id": "P-1",
            "part_name": "Block",
            "quantity": "10",
            "rate_set_id": "poc-machine-shop",
        },
    )

    assert response.status_code == 200
    assert response.json()["geometry"]["repair_applied"] is False


def test_a_lone_triangle_is_not_mistaken_for_a_hole():
    """Caught in testing: a stray triangle also has three free edges.

    Filling it folds a second copy back onto the first, producing a shell of
    zero volume that would then report as watertight — a false positive of the
    worst kind, since it turns junk geometry into a confident number. The
    three edges of a genuine hole come from three different triangles; a lone
    triangle's come from one.
    """
    lone = mesh(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0]],
        [[0, 1, 2]],
    )

    topology = analyse_faceted_volume(lone)

    assert topology.restored_facet_count == 0
    assert not topology.is_watertight


def test_a_stray_triangle_beside_a_solid_does_not_close_the_model():
    vertices = np.vstack([CUBE_VERTICES * 10.0, np.array([[0.0, 0.0, 50.0], [1.0, 0.0, 50.0], [1.0, 1.0, 50.0]])])
    triangles = np.vstack([CUBE_TRIANGLES, np.array([[8, 9, 10]])])

    topology = analyse_faceted_volume(mesh(vertices, triangles))

    assert topology.restored_facet_count == 0
    assert len(topology.open) == 1
    assert not topology.is_watertight
