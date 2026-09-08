"""What every recognizer is, and the state they share.

A recognizer is a small deterministic rule over the scanned topology. It gets
the whole model, looks only at the faces nobody more specific has already
explained, and returns features together with the evidence that produced them.

Face attribution
----------------
A face belongs to at most one feature. Recognizers run most-specific first — a
bore is claimed as a *hole*, not reported again as a bare cylinder, and a
pocket's floor and walls are not also reported as loose planes. This is what
makes the counts in the summary add up instead of describing the same geometry
several times over. Faces nobody claims are counted and reported, so the share
of the model that was not understood stays visible rather than being quietly
dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from cad_costing_agent.features.model import Feature
from cad_costing_agent.features.topology import FaceDescriptor, TopologyModel


@dataclass
class RecognitionContext:
    """The scanned model plus which faces have already been explained."""

    topology: TopologyModel
    claimed_faces: set[int] = field(default_factory=set)

    def unclaimed_faces(self) -> list[FaceDescriptor]:
        return [
            face
            for index, face in sorted(self.topology.faces.items())
            if index not in self.claimed_faces
        ]

    def is_claimed(self, index: int) -> bool:
        return index in self.claimed_faces

    def claim(self, indices) -> None:
        self.claimed_faces.update(indices)


class Recognizer(Protocol):
    """One deterministic rule. Independently testable against a topology."""

    name: str

    def recognize(self, context: RecognitionContext) -> list[Feature]:
        """Return the features this rule can justify, claiming their faces."""
        ...
