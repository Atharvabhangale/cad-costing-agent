"""Classify what a STEP file actually contains, without parsing it.

Why this exists
---------------
A STEP file can describe a part in fundamentally different ways, and the
difference decides what can be recognized:

- an **analytic B-Rep** carries exact surfaces — `CYLINDRICAL_SURFACE`,
  `PLANE`, `ADVANCED_FACE` — so a hole is a cylinder and its diameter is a
  number in the file;
- a **faceted** export carries only flat polygons (`FACETED_BREP`,
  `POLY_LOOP`), so a hole is a ring of triangles and its diameter has to be
  reconstructed;
- a **wireframe** export carries neither, and nothing can be recognized.

Deciding this by loading the file is expensive and, for a broken file,
impossible: a 198 MB export that OpenCASCADE rejects still takes twenty
seconds to reject. Counting entity keywords in the raw bytes takes about a
second on the same file and answers the question before anything heavy is
attempted.

This is a byte scan, not a STEP parser. It counts keyword occurrences, which is
enough to classify a file and to size the work ahead, and is deliberately
tolerant of anything it does not understand.

The OPEN_SHELL defect
---------------------
ISO 10303-42 defines `FACETED_BREP` as a `MANIFOLD_SOLID_BREP`, whose outer
boundary must be a `CLOSED_SHELL`. Some Creo exports write `FACETED_BREP`
referencing an `OPEN_SHELL` instead, which is schema-invalid. OpenCASCADE
validates it and transfers **nothing at all** — the file reads without error
and yields zero faces. That is detected here so the failure can be explained
rather than reported as "this part has no features".
"""

from __future__ import annotations

from dataclasses import dataclass, field

BREP = "brep"
FACETED = "faceted"
WIREFRAME = "wireframe"
EMPTY = "empty"

# Surfaces that carry exact geometry. Their presence is what makes a file
# analytic; `PLANE` is excluded because a faceted file is nothing but planes.
_ANALYTIC_SURFACES = (
    b"CYLINDRICAL_SURFACE",
    b"CONICAL_SURFACE",
    b"SPHERICAL_SURFACE",
    b"TOROIDAL_SURFACE",
    b"B_SPLINE_SURFACE",
    b"SURFACE_OF_REVOLUTION",
    b"SURFACE_OF_LINEAR_EXTRUSION",
)

_COUNTED = {
    "advanced_face": b"ADVANCED_FACE",
    "face_surface": b"FACE_SURFACE",
    "faceted_brep": b"FACETED_BREP(",
    "poly_loop": b"POLY_LOOP",
    "closed_shell": b"CLOSED_SHELL",
    "open_shell": b"OPEN_SHELL",
    "manifold_solid_brep": b"MANIFOLD_SOLID_BREP",
    "cartesian_point": b"CARTESIAN_POINT",
    "plane": b"PLANE(",
}


@dataclass
class StepRepresentation:
    """What a STEP file contains, and therefore what can be done with it."""

    representation: str
    schema: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    analytic_surface_count: int = 0
    faceted_brep_on_open_shell: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def is_faceted(self) -> bool:
        return self.representation == FACETED

    @property
    def is_brep(self) -> bool:
        return self.representation == BREP

    @property
    def facet_count(self) -> int:
        """Polygonal facets, which is what a faceted file has instead of faces."""
        return self.counts.get("poly_loop", 0)

    @property
    def shell_count(self) -> int:
        return self.counts.get("closed_shell", 0) + self.counts.get("open_shell", 0)


def _schema_of(head: bytes) -> str | None:
    marker = head.find(b"FILE_SCHEMA")
    if marker < 0:
        return None
    opening = head.find(b"'", marker)
    closing = head.find(b"'", opening + 1)
    if opening < 0 or closing < 0:
        return None
    return head[opening + 1 : closing].decode("utf-8", "replace").strip() or None


def classify_step_bytes(data: bytes) -> StepRepresentation:
    """Classify STEP content by counting entity keywords in the raw bytes.

    `bytes.count` scans in C, so counting a dozen keywords over a 200 MB export
    costs a couple of seconds — against roughly twenty for OpenCASCADE merely to
    read the same file and a hundred to transfer it.
    """
    counts = {name: data.count(keyword) for name, keyword in _COUNTED.items()}
    analytic = sum(data.count(keyword) for keyword in _ANALYTIC_SURFACES)

    notes: list[str] = []
    faceted_brep = counts["faceted_brep"]
    poly_loops = counts["poly_loop"]
    advanced_faces = counts["advanced_face"]

    on_open_shell = bool(
        faceted_brep and counts["open_shell"] and not counts["closed_shell"]
    )
    if on_open_shell:
        notes.append(
            f"{faceted_brep} FACETED_BREP entities reference an OPEN_SHELL. ISO 10303-42 "
            "requires a CLOSED_SHELL, so OpenCASCADE rejects them and transfers no geometry "
            "at all. This is a defect in the exporting CAD system's output."
        )

    if analytic > 0 or advanced_faces > 0:
        representation = BREP
        if poly_loops:
            notes.append(
                f"The file mixes {advanced_faces} analytic faces with {poly_loops} facets; "
                "it is treated as analytic."
            )
    elif faceted_brep or poly_loops:
        representation = FACETED
        notes.append(
            f"{poly_loops} polygonal facets in {counts['closed_shell'] + counts['open_shell']} "
            "shell(s), with no analytic surfaces. Exact diameters and axes are not stored in "
            "this kind of file and can only be reconstructed from the polygons."
        )
    elif counts["cartesian_point"]:
        representation = WIREFRAME
        notes.append(
            "The file contains points but no faces or facets, so it is a wireframe or "
            "point-only export with no surface geometry to analyse."
        )
    else:
        representation = EMPTY
        notes.append("No geometric entities were found in this file.")

    return StepRepresentation(
        representation=representation,
        schema=_schema_of(data[:4096]),
        counts=counts,
        analytic_surface_count=analytic,
        faceted_brep_on_open_shell=on_open_shell,
        notes=notes,
    )
