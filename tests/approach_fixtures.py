"""STEP fixtures for tool approach directions and setup proposals.

Built with OpenCASCADE from boxes and cylinders, written through OCCT's own
STEP writer, exactly as `tests/step_fixtures.py` builds its parts, so the
expected approach directions can be stated from the construction:

- the block is 60 × 40 × 20 mm, its top face at z = 20 and bottom at z = 0;
- "+Z" is the tool coming down from above, "−Z" from below, and so on.

Kept in their own module so the recognizer fixtures, and the expectations
written against them, are untouched.
"""

from __future__ import annotations

from tests.step_fixtures import BLOCK, _box, _cut, _cylinder, _fuse, to_step


def _bracket():
    """The C-bracket body, with coplanar faces from the fuses merged into one each."""
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

    shape = _fuse(_box(60.0, 40.0, 10.0), _box(10.0, 40.0, 50.0))
    shape = _fuse(shape, _box(45.0, 40.0, 10.0, at=(0.0, 0.0, 40.0)))
    unify = ShapeUpgrade_UnifySameDomain(shape, True, True, True)
    unify.Build()
    return unify.Shape()


# --- 1. holes on top only ----------------------------------------------------
# Two Ø8 blind holes, 12 deep, opening on the top face. Expected: both holes
# approach along +Z only, and +Z reaches them.
def block_with_top_holes() -> bytes:
    shape = _box(*BLOCK)
    for x in (15.0, 45.0):
        shape = _cut(shape, _cylinder(4.0, 20.0, (x, 20.0, 8.0)))
    return to_step(shape)


# --- 2. a hole on top and a hole in one side ---------------------------------
# A Ø8 blind hole from the top (+Z) and a Ø6 blind hole 10 deep into the +X
# face (x = 60), at mid-height. Expected: two distinct directions, +Z and +X.
def block_with_top_and_side_holes() -> bytes:
    shape = _cut(_box(*BLOCK), _cylinder(4.0, 20.0, (20.0, 20.0, 8.0)))
    shape = _cut(shape, _cylinder(3.0, 20.0, (50.0, 20.0, 10.0), direction=(1.0, 0.0, 0.0)))
    return to_step(shape)


# --- 3. blind holes from the top and from the bottom -------------------------
# A Ø8 blind hole 8 deep from the top and a Ø6 blind hole 8 deep from the
# bottom, offset so they do not meet. Expected: +Z and −Z, opposite.
def block_with_top_and_bottom_holes() -> bytes:
    shape = _cut(_box(*BLOCK), _cylinder(4.0, 20.0, (15.0, 20.0, 12.0)))
    return to_step(_cut(shape, _cylinder(3.0, 9.0, (45.0, 20.0, -1.0))))


# --- 4. one through hole ------------------------------------------------------
# The recognizer fixture's Ø10 through hole: both ends open, so the hole is
# reachable along +Z and along −Z.
def block_with_through_hole() -> bytes:
    return to_step(_cut(_box(*BLOCK), _cylinder(5.0, 40.0, (30.0, 20.0, -10.0))))


# --- 5. a bracket whose top flange hangs over a hole in its base -------------
# A C-shaped bracket: a 60 × 40 × 10 base (z 0..10), an upright 10 mm wall at
# x 0..10 rising to z = 50, and a 10 mm top flange (z 40..50) reaching out to
# x = 45. A Ø8 through hole in the base at x = 30 sits under the top flange:
# along +Z every ray from its top end meets the flange's underside 30 mm up, so
# +Z is blocked; along −Z it leaves the part, so −Z is the approach used.
def bracket_with_overhung_through_hole() -> bytes:
    return to_step(_cut(_bracket(), _cylinder(4.0, 30.0, (30.0, 20.0, -10.0))))


# --- 6. a hole no straight tool path reaches ---------------------------------
# The same bracket with a Ø8 blind hole 6 deep opening upward on the base's top
# face, under the flange. Its only candidate is +Z (out of its open end), and
# that is blocked by the flange, so it is reported unreachable.
def bracket_with_unreachable_blind_hole() -> bytes:
    return to_step(_cut(_bracket(), _cylinder(4.0, 10.0, (30.0, 20.0, 4.0))))


APPROACH_FIXTURES = {
    "block_with_top_holes": block_with_top_holes,
    "block_with_top_and_side_holes": block_with_top_and_side_holes,
    "block_with_top_and_bottom_holes": block_with_top_and_bottom_holes,
    "block_with_through_hole": block_with_through_hole,
    "bracket_with_overhung_through_hole": bracket_with_overhung_through_hole,
    "bracket_with_unreachable_blind_hole": bracket_with_unreachable_blind_hole,
}
