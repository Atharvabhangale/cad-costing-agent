"""The recognizer pipeline, in the order it runs.

Order is the design. Each recognizer claims the faces it explains, so the more
specific rule has to run before the more general one that would otherwise
swallow the same geometry:

1. **holes** — a bore is a hole, not a bare cylinder.
2. **pockets** — a recess claims its floor and walls, including any rounded
   corners, before those corners can be mistaken for standalone fillets.
3. **fillets** then **chamfers** — edge treatments, before the leftover
   cylinders and planes.
4. **cylinders** — complete cylindrical surfaces nothing more specific
   explained: shafts, bosses, and bores whose ends were unreadable.
5. **planes** — every remaining flat face, completing the inventory.
"""

from cad_costing_agent.features.recognizers.base import Recognizer, RecognitionContext
from cad_costing_agent.features.recognizers.blends import ChamferRecognizer, FilletRecognizer
from cad_costing_agent.features.recognizers.cylindrical import CylinderRecognizer
from cad_costing_agent.features.recognizers.holes import HoleRecognizer
from cad_costing_agent.features.recognizers.planar import PlanarFaceRecognizer
from cad_costing_agent.features.recognizers.pockets import PocketRecognizer

DEFAULT_RECOGNIZERS: tuple[Recognizer, ...] = (
    HoleRecognizer(),
    PocketRecognizer(),
    FilletRecognizer(),
    ChamferRecognizer(),
    CylinderRecognizer(),
    PlanarFaceRecognizer(),
)

__all__ = [
    "DEFAULT_RECOGNIZERS",
    "ChamferRecognizer",
    "CylinderRecognizer",
    "FilletRecognizer",
    "HoleRecognizer",
    "PlanarFaceRecognizer",
    "PocketRecognizer",
    "RecognitionContext",
    "Recognizer",
]
