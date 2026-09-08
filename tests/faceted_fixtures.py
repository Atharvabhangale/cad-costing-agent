"""Synthetic faceted STEP fixtures, written locally.

The real files that motivated the faceted path are 200 MB Creo assembly
exports — far too large and too complex to unit-test against. So the same kind
of file is produced here at a size a test can assert on: a known shape is
tessellated with OpenCASCADE and written out as `FACETED_BREP` polygons, with
every analytic surface discarded exactly as a real faceted export discards
them.

That makes the expected features knowable. A 10 mm hole tessellated into
polygons is still a 10 mm hole, so a test can require the reconstruction to
recover 10 mm rather than merely "a hole".

`open_shell=True` reproduces the schema-invalid form the real exports use —
`FACETED_BREP` over an `OPEN_SHELL`, which ISO 10303-42 forbids — so the
detection of that defect can be tested without a 200 MB file.
"""

from __future__ import annotations

import numpy as np

from cad_costing_agent.cad_mesh import build_renderable_mesh
from tests.step_fixtures import _box, _cut, _cylinder, to_step


def _number(value: float) -> str:
    """STEP real literals: always a decimal point, and deterministic."""
    text = f"{float(value):.9G}"
    if "E" in text:
        mantissa, exponent = text.split("E")
        if "." not in mantissa:
            mantissa += ".0"
        return f"{mantissa}E{exponent}"
    if "." not in text:
        text += "."
    return text


def faceted_step_from_shape(shape, *, name: str = "FACETED", open_shell: bool = False) -> bytes:
    """Tessellate an OCCT shape and write it as a faceted STEP file."""
    mesh = build_renderable_mesh(to_step(shape), "step")
    positions = np.asarray(mesh.positions, dtype=np.float64).reshape(-1, 3)
    triangles = np.asarray(mesh.indices, dtype=np.int64).reshape(-1, 3)
    return faceted_step_from_mesh(positions, triangles, name=name, open_shell=open_shell)


def faceted_step_from_mesh(
    positions: np.ndarray,
    triangles: np.ndarray,
    *,
    name: str = "FACETED",
    open_shell: bool = False,
    unit: str = ".MILLI.",
    declare_unit: bool = True,
) -> bytes:
    """Write a triangle mesh as FACETED_BREP STEP text.

    `unit` is the SI prefix written into the file's `LENGTH_UNIT`; `$` means
    metres, as the real Creo exports use. `declare_unit=False` omits the
    declaration entirely, which is what a file that cannot be measured safely
    looks like.
    """
    lines: list[str] = []
    next_id = [1]

    def emit(body: str) -> int:
        identifier = next_id[0]
        next_id[0] += 1
        lines.append(f"#{identifier}={body};")
        return identifier

    # Vertices are shared, as a real export shares them.
    point_ids = [
        emit(f"CARTESIAN_POINT('',({_number(x)},{_number(y)},{_number(z)}))")
        for x, y, z in positions
    ]

    face_ids: list[int] = []
    for triangle in triangles:
        corners = [point_ids[int(index)] for index in triangle]
        loop = emit("POLY_LOOP('',(" + ",".join(f"#{identifier}" for identifier in corners) + "))")
        bound = emit(f"FACE_OUTER_BOUND('',#{loop},.T.)")

        # The facet's supporting plane, as a real faceted export carries one.
        corner_points = positions[triangle]
        normal = np.cross(corner_points[1] - corner_points[0], corner_points[2] - corner_points[0])
        magnitude = float(np.linalg.norm(normal))
        if magnitude == 0:
            continue  # degenerate triangle: no plane to write
        normal = normal / magnitude
        reference = np.array([1.0, 0.0, 0.0]) if abs(normal[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        reference = np.cross(normal, reference)
        reference /= np.linalg.norm(reference)

        origin = emit(
            "CARTESIAN_POINT('',("
            + ",".join(_number(value) for value in corner_points[0])
            + "))"
        )
        axis = emit("DIRECTION('',(" + ",".join(_number(value) for value in normal) + "))")
        ref = emit("DIRECTION('',(" + ",".join(_number(value) for value in reference) + "))")
        placement = emit(f"AXIS2_PLACEMENT_3D('',#{origin},#{axis},#{ref})")
        plane = emit(f"PLANE('',#{placement})")
        face_ids.append(emit(f"FACE_SURFACE('',(#{bound}),#{plane},.T.)"))

    shell_kind = "OPEN_SHELL" if open_shell else "CLOSED_SHELL"
    shell = emit(
        f"{shell_kind}('',(" + ",".join(f"#{identifier}" for identifier in face_ids) + "))"
    )
    brep = emit(f"FACETED_BREP('',#{shell})")

    # A minimal representation wrapper, so the file looks like what a CAD
    # system writes rather than a bare entity list.
    origin = emit("CARTESIAN_POINT('',(0.,0.,0.))")
    axis = emit("DIRECTION('',(0.,0.,1.))")
    ref = emit("DIRECTION('',(1.,0.,0.))")
    placement = emit(f"AXIS2_PLACEMENT_3D('',#{origin},#{axis},#{ref})")
    context = "$"
    if declare_unit:
        length = emit(f"(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT({unit},.METRE.))")
        angle = emit("(NAMED_UNIT(*)PLANE_ANGLE_UNIT()SI_UNIT($,.RADIAN.))")
        solid = emit("(NAMED_UNIT(*)SI_UNIT($,.STERADIAN.)SOLID_ANGLE_UNIT())")
        body = (
            "(GEOMETRIC_REPRESENTATION_CONTEXT(3)"
            f"GLOBAL_UNIT_ASSIGNED_CONTEXT((#{length},#{angle},#{solid}))"
            "REPRESENTATION_CONTEXT('','3D'))"
        )
        context = f"#{emit(body)}"
    emit(f"FACETED_BREP_SHAPE_REPRESENTATION('{name}',(#{placement},#{brep}),{context})")

    header = (
        "ISO-10303-21;\n"
        "HEADER;\n"
        "FILE_DESCRIPTION((''),'2;1');\n"
        f"FILE_NAME('{name}','2026-01-01T00:00:00',(''),(''),'','','');\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\n"
        "ENDSEC;\n"
        "DATA;\n"
    )
    return (header + "\n".join(lines) + "\nENDSEC;\nEND-ISO-10303-21;\n").encode("utf-8")


# --- fixtures ---------------------------------------------------------------
# Each is the faceted twin of a shape whose true dimensions are known, so the
# reconstruction can be asserted against real numbers.


def faceted_plate_with_four_holes(open_shell: bool = False) -> bytes:
    """80 x 60 x 10 plate, four Ø10 through holes. The worked example."""
    shape = _box(80.0, 60.0, 10.0)
    for x, y in ((15.0, 15.0), (65.0, 15.0), (15.0, 45.0), (65.0, 45.0)):
        shape = _cut(shape, _cylinder(5.0, 30.0, (x, y, -10.0)))
    return faceted_step_from_shape(shape, name="PLATE_4_HOLES", open_shell=open_shell)


def faceted_plain_block() -> bytes:
    """60 x 40 x 20 block: six flat regions and nothing else to find."""
    return faceted_step_from_shape(_box(60.0, 40.0, 20.0), name="PLAIN_BLOCK")


def faceted_block_with_blind_hole() -> bytes:
    """60 x 40 x 20 block, one Ø16 hole 12 deep from the top."""
    shape = _cut(_box(60.0, 40.0, 20.0), _cylinder(8.0, 20.0, (30.0, 20.0, 8.0)))
    return faceted_step_from_shape(shape, name="BLIND_HOLE")


def faceted_block_with_boss() -> bytes:
    """Block with an external Ø30 boss, which is a shaft rather than a hole."""
    from tests.step_fixtures import _fuse

    shape = _fuse(_box(60.0, 40.0, 20.0), _cylinder(15.0, 12.0, (30.0, 20.0, 20.0)))
    return faceted_step_from_shape(shape, name="BOSS")


def faceted_tube(sides: int, radius: float = 10.0, height: float = 20.0) -> bytes:
    """An open tube of `sides` flat panels, built directly as a mesh.

    The honesty test for the faceted path. A circle cut into many segments and
    a designed polygon are the same kind of object — flat panels around an axis
    — and the only thing separating them is how far the panels stand inside the
    circle through their corners. A tube with few sides must not be reported as
    a round hole; one with many must.
    """
    angles = [2.0 * np.pi * step / sides for step in range(sides)]
    ring = [(radius * np.cos(angle), radius * np.sin(angle)) for angle in angles]
    positions = []
    for x, y in ring:
        positions.append((x, y, 0.0))
        positions.append((x, y, height))
    triangles = []
    for index in range(sides):
        low = 2 * index
        high = 2 * index + 1
        next_low = 2 * ((index + 1) % sides)
        next_high = next_low + 1
        triangles.append((low, next_low, high))
        triangles.append((high, next_low, next_high))
    return faceted_step_from_mesh(
        np.asarray(positions, dtype=np.float64),
        np.asarray(triangles, dtype=np.int64),
        name=f"TUBE_{sides}",
    )


ALL_FACETED_FIXTURES = {
    "faceted_plain_block": faceted_plain_block,
    "faceted_plate_with_four_holes": faceted_plate_with_four_holes,
    "faceted_block_with_blind_hole": faceted_block_with_blind_hole,
    "faceted_block_with_boss": faceted_block_with_boss,
}
