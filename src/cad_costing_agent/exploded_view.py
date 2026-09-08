"""A deterministic exploded arrangement, for looking at an assembly.

What this is, and what it is emphatically not
--------------------------------------------
This computes where to *draw* each component so an assembly can be pulled apart
on screen and inspected. It is a **visual exploded arrangement** derived from
the placements and hierarchy the CAD file already states.

It is **not** an assembly or disassembly sequence, and must never be described
as one. Nothing here knows which part goes in first, what has to come off before
what, or whether a component can be withdrawn along the direction it is drawn
moving. That is a manufacturing and business decision made by people, and no
amount of geometry implies it.

Display only, by construction
-----------------------------
The CAD placement is never modified. Each component gets an **offset**: the
displacement it would take at full explosion. The viewer draws

    displayed = original placement + offset x factor

so a factor of zero is exactly the original assembly, and no arithmetic here can
reach a volume, a mass, a feature, a time or a cost. This module imports nothing
from the rest of the application — a structural test parses its imports — so it
could not reach the costing engine even by accident, and nothing downstream of
it consumes what it produces.

How components are grouped
--------------------------
Exploding every occurrence independently would scatter a sub-assembly's parts
among its neighbours' and destroy the very relationships that make an assembly
readable. So components move in **rigid groups**:

1. Find the shallowest depth at which the tree actually branches — a chain of
   single children is skipped, because separating one component from itself
   moves nothing. On the real 198 MB export the top two levels are single
   children and the branching happens at depth 3, among 83 siblings.
2. Every occurrence belongs to the group headed by its ancestor at that depth.
3. A group moves as one. Its members keep their positions relative to each other
   **exactly**, and only the groups separate from one another.

Directions come from the placements themselves: a group moves away from the
assembly's centre along the line from that centre to the group's own centre,
which is the arrangement an engineer expects. A group sitting on the centre has
no such line, and rather than leaving it buried inside the others it is given a
direction from a deterministic spiral over the sphere — the same input always
produces the same arrangement.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

METHOD = "radial_from_assembly_centre_by_rigid_group"

DISPLACEMENT_FRACTION = 0.6
"""How far a group travels at full explosion, as a fraction of how far apart the
groups already are. Chosen so components clearly separate while the assembly
still reads as one object rather than a scattering of unrelated parts."""

MINIMUM_SPREAD = 1.0
"""Fallback scale, in millimetres, when every group sits at the same point and
there is no spread to take a distance from."""

DEGENERATE_DIRECTION = 1e-9
"""Below this, a group's offset from the centre is not a direction."""

# The golden angle, which distributes points over a sphere without clustering.
_GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))

IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)

VISUAL_ONLY_NOTE = (
    "A visual exploded arrangement, derived from the assembly's own placements and hierarchy. "
    "It is not an assembly or disassembly sequence: nothing here knows what has to come off "
    "before what, or whether a component can actually be withdrawn along the direction it is "
    "shown moving. Sequencing is a manufacturing decision, not a property of the geometry."
)


class ExplodedPlacement(BaseModel):
    """Where one occurrence is drawn, beside where the CAD actually puts it."""

    occurrence_id: str
    definition_id: str
    group_occurrence_id: str | None = Field(
        default=None,
        description=(
            "The ancestor whose group this occurrence moves with. Members of a group keep "
            "their positions relative to each other exactly."
        ),
    )
    original_translation: list[float] = Field(
        min_length=3, max_length=3, description="The CAD placement, unchanged."
    )
    offset: list[float] = Field(
        min_length=3,
        max_length=3,
        description="Displacement at a factor of 1.0. Display only; added, never written back.",
    )
    direction_source: str = Field(
        default="radial",
        description="'radial' from the assembly centre, 'spiral_fallback', or 'none'.",
    )

    def translation_at(self, factor: float) -> tuple[float, float, float]:
        """Where this occurrence is drawn at a given explosion factor."""
        return tuple(
            original + offset * factor
            for original, offset in zip(self.original_translation, self.offset)
        )


class ExplodedViewState(BaseModel):
    """The whole arrangement: display-only, and labelled as such."""

    enabled: bool = False
    factor: float = Field(
        default=0.0, ge=0.0, description="0 is the assembly as built; 1 is fully separated."
    )
    method: str = METHOD
    explode_depth: int = Field(
        default=0, description="The tree depth whose siblings separate from one another."
    )
    group_count: int = 0
    distance: float = Field(default=0.0, description="How far a group travels at factor 1.0, in mm.")
    centre: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    component_transforms: list[ExplodedPlacement] = Field(default_factory=list)
    note: str = VISUAL_ONLY_NOTE
    notes: list[str] = Field(default_factory=list)

    def placement(self, occurrence_id: str) -> ExplodedPlacement:
        for item in self.component_transforms:
            if item.occurrence_id == occurrence_id:
                return item
        raise KeyError(f"No exploded placement for occurrence '{occurrence_id}'.")


def _translation(occurrence) -> tuple[float, float, float]:
    """The occurrence's world translation, whichever carrier it arrives in."""
    transform = getattr(occurrence, "transform", None)
    world = getattr(transform, "world", None) if transform is not None else None
    if world is None:
        placement = getattr(occurrence, "placement", None)
        world = getattr(placement, "world", IDENTITY) if placement is not None else IDENTITY
    return (float(world[9]), float(world[10]), float(world[11]))


def _spiral_direction(index: int, total: int) -> tuple[float, float, float]:
    """A deterministic direction for a group that sits on the assembly centre.

    Points spread over a sphere by the golden angle, so a set of co-located
    groups fans out evenly instead of piling up along one axis. The same index
    always gives the same direction, which is what makes the whole arrangement
    reproducible.
    """
    count = max(total, 1)
    # z walks from near +1 to near -1; the angle advances by the golden angle.
    z = 1.0 - (2.0 * index + 1.0) / count
    radius = math.sqrt(max(0.0, 1.0 - z * z))
    angle = _GOLDEN_ANGLE * index
    return (math.cos(angle) * radius, math.sin(angle) * radius, z)


def _explode_depth(depths: list[int]) -> int:
    """The shallowest depth whose siblings are worth separating.

    A chain of single children is skipped: separating an only child from its
    parent moves the whole assembly and shows nothing. If the tree never
    branches, the shallowest depth is used, and the result is simply a small
    rigid translation.
    """
    if not depths:
        return 0
    counts: dict[int, int] = {}
    for depth in depths:
        counts[depth] = counts.get(depth, 0) + 1
    for depth in sorted(counts):
        if counts[depth] >= 2:
            return depth
    return min(counts)


def build_exploded_view(assembly, *, factor: float = 0.0) -> ExplodedViewState:
    """Compute a deterministic exploded arrangement for one assembly.

    `assembly` is anything carrying `.occurrences` with an `occurrence_id`, a
    `definition_id`, a `parent_occurrence_id`, a `depth` and a world transform.
    Nothing is read from the geometry, so an assembly whose components carry no
    drawable shape still gets a complete arrangement — which is exactly the case
    for the real Creo export, where the structure is known and the geometry is
    not.
    """
    occurrences = list(getattr(assembly, "occurrences", []) or [])
    if not occurrences:
        return ExplodedViewState(
            enabled=False,
            factor=factor,
            notes=["This file has no assembly occurrences, so there is nothing to explode."],
        )

    by_id = {occurrence.occurrence_id: occurrence for occurrence in occurrences}
    depth = _explode_depth([occurrence.depth for occurrence in occurrences])

    def group_of(occurrence) -> str | None:
        """The ancestor at the exploding depth, or None for anything above it."""
        current = occurrence
        seen = 0
        while current is not None and current.depth > depth:
            parent_id = current.parent_occurrence_id
            current = by_id.get(parent_id) if parent_id else None
            seen += 1
            if seen > len(occurrences):  # pragma: no cover - a cycle cannot occur
                return None
        if current is None or current.depth != depth:
            return None
        return current.occurrence_id

    membership: dict[str, str | None] = {
        occurrence.occurrence_id: group_of(occurrence) for occurrence in occurrences
    }

    # A group's position is the mean of its members', so a sub-assembly moves
    # from where its parts actually are rather than from wherever its own
    # coordinate origin happens to sit.
    members: dict[str, list[tuple[float, float, float]]] = {}
    for occurrence in occurrences:
        group = membership[occurrence.occurrence_id]
        if group is not None:
            members.setdefault(group, []).append(_translation(occurrence))

    group_ids = sorted(members)
    positions = {
        group: tuple(sum(axis) / len(values) for axis in zip(*values))
        for group, values in members.items()
    }

    if not group_ids:
        return ExplodedViewState(
            enabled=False,
            factor=factor,
            explode_depth=depth,
            notes=["No component group could be formed, so nothing is displaced."],
        )

    centre = tuple(
        sum(positions[group][axis] for group in group_ids) / len(group_ids) for axis in range(3)
    )

    lows = [min(positions[group][axis] for group in group_ids) for axis in range(3)]
    highs = [max(positions[group][axis] for group in group_ids) for axis in range(3)]
    spread = math.dist(lows, highs)
    distance = max(spread, MINIMUM_SPREAD) * DISPLACEMENT_FRACTION

    offsets: dict[str, tuple[float, float, float]] = {}
    sources: dict[str, str] = {}
    for index, group in enumerate(group_ids):
        vector = tuple(positions[group][axis] - centre[axis] for axis in range(3))
        length = math.sqrt(sum(value * value for value in vector))
        if length <= DEGENERATE_DIRECTION:
            direction = _spiral_direction(index, len(group_ids))
            sources[group] = "spiral_fallback"
        else:
            direction = tuple(value / length for value in vector)
            sources[group] = "radial"
        offsets[group] = tuple(value * distance for value in direction)

    placements = [
        ExplodedPlacement(
            occurrence_id=occurrence.occurrence_id,
            definition_id=occurrence.definition_id,
            group_occurrence_id=membership[occurrence.occurrence_id],
            original_translation=list(_translation(occurrence)),
            offset=list(offsets.get(membership[occurrence.occurrence_id] or "", (0.0, 0.0, 0.0))),
            direction_source=sources.get(membership[occurrence.occurrence_id] or "", "none"),
        )
        for occurrence in occurrences
    ]

    notes = [
        f"{len(group_ids)} rigid group(s) separate at depth {depth}; everything below a group "
        "moves with it, so components inside a sub-assembly keep their positions relative to "
        "each other exactly."
    ]
    fallbacks = sum(1 for group in group_ids if sources[group] == "spiral_fallback")
    if fallbacks:
        notes.append(
            f"{fallbacks} group(s) sit on the assembly centre and have no outward direction, so "
            "they were given deterministic directions spread over a sphere rather than being "
            "left buried inside the rest."
        )

    return ExplodedViewState(
        enabled=True,
        factor=factor,
        explode_depth=depth,
        group_count=len(group_ids),
        distance=distance,
        centre=list(centre),
        component_transforms=placements,
        notes=notes,
    )
