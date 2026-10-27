"""Resource limits, in one place and separately configurable.

These used to be entangled: the upload cap lived in the viewer's module, so how
big a file you could *cost* was decided by what the browser could *draw*. They
are different questions and now have different answers.

    CAD_COSTING_MAX_UPLOAD_MB        how much file the server will accept
    CAD_COSTING_MAX_BREP_FACES       how complex a B-Rep feature recognition will analyse
    CAD_COSTING_MAX_FACETS           how many polygons faceted reconstruction will analyse
    CAD_COSTING_MAX_VIEWER_TRIANGLES how big a mesh the browser is asked to render
    CAD_COSTING_MAX_ASSEMBLY_COMPONENTS how many occurrences decomposition will walk
    CAD_COSTING_MAX_FEEDBACK_ESTIMATES  how many stored estimates the analytics will read
    CAD_COSTING_MIN_FEEDBACK_SAMPLE     observations before a pattern is called actionable
    CAD_COSTING_MAX_BULK_ROWS           parts one bulk preview will price
    CAD_COSTING_MAX_BULK_FILES          CAD files one bulk preview will accept
    CAD_COSTING_MAX_BULK_TOTAL_MB       combined size of one bulk preview's CAD files

The ordering that matters: a model can be **too complex to draw and still
perfectly costable**. Geometry and mass need a volume, which is cheap; the
viewer needs a mesh in the browser, which is not. So exceeding the viewer limit
degrades the preview and nothing else, and exceeding a recognition limit
degrades feature analysis and nothing else. Neither blocks an estimate.

Every value can be overridden by environment variable, so a bigger machine can
be given bigger budgets without editing code.
"""

from __future__ import annotations

import os

_MEGABYTE = 1024 * 1024


def _positive_int(name: str, default: int) -> int:
    """Read an override, falling back to the default for anything unusable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


MAX_UPLOAD_MB = _positive_int("CAD_COSTING_MAX_UPLOAD_MB", 500)
"""Largest upload the server accepts. Real Creo faceted exports run to ~200 MB."""

MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * _MEGABYTE

MAX_BREP_FACES = _positive_int("CAD_COSTING_MAX_BREP_FACES", 20_000)
"""Analytic faces above which feature recognition declines rather than producing
a feature list nobody could read."""

MAX_FACETS = _positive_int("CAD_COSTING_MAX_FACETS", 200_000)
"""Polygons above which faceted reconstruction declines. Fitting circles to a
few hundred thousand facets is minutes of work for an answer that would be an
assembly-wide dump, not a part's feature list."""

MAX_GEOMETRY_FACETS = _positive_int("CAD_COSTING_MAX_GEOMETRY_FACETS", 1_000_000)
"""Polygons above which a faceted model's *volume* is not computed. Deliberately
far higher than `MAX_FACETS`: deriving a volume is one weld and one closure scan,
while reconstructing features fits geometry to every body. A model can be well
past the feature budget and still perfectly costable."""

MAX_VIEWER_TRIANGLES = _positive_int("CAD_COSTING_MAX_VIEWER_TRIANGLES", 80_000)
"""Triangles the browser is asked to render. A denser model is not refused: it
is decimated down to this budget for display only. Purely a preview limit —
exceeding it never affects geometry, mass or cost."""

MAX_ASSEMBLY_COMPONENTS = _positive_int("CAD_COSTING_MAX_ASSEMBLY_COMPONENTS", 5_000)
"""Occurrences the assembly reader will walk before stopping. A separate budget
again, because decomposing a structure is a different question from measuring
geometry: an assembly can be far too large to enumerate component by component
and still perfectly costable as one part. Exceeding it truncates the structure
and says so; it never blocks an estimate."""

MAX_VIEWER_INPUT_TRIANGLES = _positive_int("CAD_COSTING_MAX_VIEWER_INPUT_TRIANGLES", 2_000_000)
"""Triangles the viewer will *read* before deciding it cannot build a preview at
all. Distinct from `MAX_VIEWER_TRIANGLES`, which is what it will *send*: the gap
between them is the decimation's working room, and this value is what keeps its
memory bounded. Exceeding it costs the preview and nothing else."""


MAX_FEEDBACK_ESTIMATES = _positive_int("CAD_COSTING_MAX_FEEDBACK_ESTIMATES", 2_000)
"""Stored estimates the feedback analytics will read in one request.

A separate budget again, and for a different reason from the others: this one
bounds how much *history* is summarised, not how much geometry is processed.
Exceeding it truncates the window and the response says so — an analytics answer
computed over the most recent N estimates is still useful, while one that timed
out is not."""

MIN_FEEDBACK_SAMPLE = _positive_int("CAD_COSTING_MIN_FEEDBACK_SAMPLE", 8)
"""Observations before a directional pattern is reported as actionable.

Not a statistical test — this project makes no distributional claims — but a
floor below which a median is a coincidence. Eight is deliberately modest: it is
enough that one extreme correction cannot define a bias, and low enough to be
reachable in a real shop's first week. Configurable because the right number
depends on how much work a shop actually reviews, and every metric reports the
threshold it was judged against alongside its own sample count."""


MAX_BULK_ROWS = _positive_int("CAD_COSTING_MAX_BULK_ROWS", 100)
"""Parts one bulk costing preview will price. Each row runs the whole pipeline
(import, recognition, suggestion, time estimation, pricing) inside one request,
so this bounds how long a request can run, not how much history is read."""

MAX_BULK_FILES = _positive_int("CAD_COSTING_MAX_BULK_FILES", 50)
"""CAD files one bulk preview accepts. Several rows may name the same file."""

MAX_BULK_TOTAL_MB = _positive_int("CAD_COSTING_MAX_BULK_TOTAL_MB", 300)
"""Combined size of a bulk preview's CAD files. Each file is still held to
`MAX_UPLOAD_BYTES`; this bounds the sum, which all sits in memory at once."""

MAX_BULK_TOTAL_BYTES = MAX_BULK_TOTAL_MB * _MEGABYTE

MAX_BULK_CSV_BYTES = 1 * _MEGABYTE
"""Largest batch CSV. A hundred rows is a few tens of kilobytes; this is a
sanity bound, not a tuning knob."""
