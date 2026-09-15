"""Gesture recognition — a faithful port of QuackCastCore's classifier.

The Mac and Windows apps must agree on what a fist *is*, or the product
behaves differently depending on which machine you wave at. This module is
therefore a deliberate line-by-line port of

    Sources/QuackCastCore/Gesture/GestureClassifier.swift
    Sources/QuackCastCore/Gesture/GestureDebouncer.swift

rather than an independent implementation, and `tests/test_gestures.py`
checks it against the same cases the Swift suite uses.

Landmark indices are the same 21 points in the same order in both Vision
(macOS) and MediaPipe Hands (Windows), so the numbering below is shared.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Sequence, Tuple

# The 21 joints, in the order both Vision and MediaPipe report them.
WRIST = 0
THUMB_CMC, THUMB_MP, THUMB_IP, THUMB_TIP = 1, 2, 3, 4
INDEX_MCP, INDEX_PIP, INDEX_DIP, INDEX_TIP = 5, 6, 7, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_DIP, MIDDLE_TIP = 9, 10, 11, 12
RING_MCP, RING_PIP, RING_DIP, RING_TIP = 13, 14, 15, 16
LITTLE_MCP, LITTLE_PIP, LITTLE_DIP, LITTLE_TIP = 17, 18, 19, 20


class Gesture(Enum):
    NONE = "none"
    OPEN_HAND = "openHand"
    CLOSED_HAND = "closedHand"
    PEACE = "peace"


@dataclass
class HandLandmarks:
    """One frame's observation of a hand: joint index → (x, y), normalised to
    0…1, plus a tracking confidence. Missing joints are simply absent."""

    points: Dict[int, Tuple[float, float]]
    confidence: float = 1.0

    def get(self, joint: int) -> Optional[Tuple[float, float]]:
        return self.points.get(joint)

    @classmethod
    def from_mediapipe(cls, landmarks: Sequence, confidence: float = 1.0) -> "HandLandmarks":
        """Build from a MediaPipe `hand_landmarks.landmark` list."""
        return cls(
            points={i: (lm.x, lm.y) for i, lm in enumerate(landmarks)},
            confidence=confidence,
        )


def _distance(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass
class ExtendedFingers:
    index: bool = False
    middle: bool = False
    ring: bool = False
    little: bool = False

    @property
    def count(self) -> int:
        return sum((self.index, self.middle, self.ring, self.little))


# The four non-thumb fingers as (tip, pip) pairs. The thumb is excluded
# because its tip can sit closer to or further from the wrist depending only
# on hand rotation, which makes it useless for this test.
_FINGERS = (
    ("index", INDEX_TIP, INDEX_PIP),
    ("middle", MIDDLE_TIP, MIDDLE_PIP),
    ("ring", RING_TIP, RING_PIP),
    ("little", LITTLE_TIP, LITTLE_PIP),
)


class GestureClassifier:
    """Stateless, geometry-only classification of a single frame."""

    def __init__(
        self,
        min_confidence: float = 0.5,
        open_hand_min_extended: int = 4,
        closed_hand_max_extended: int = 0,
    ) -> None:
        self.min_confidence = min_confidence
        self.open_hand_min_extended = open_hand_min_extended
        self.closed_hand_max_extended = closed_hand_max_extended

    def extended_fingers(self, hand: HandLandmarks) -> ExtendedFingers:
        """A finger counts as extended when its tip is further from the wrist
        than its middle (PIP) joint — true for a straight finger, false for a
        curled one, and independent of how the hand is rotated or how close it
        is to the camera."""
        result = ExtendedFingers()
        wrist = hand.get(WRIST)
        if wrist is None:
            return result
        for name, tip_joint, pip_joint in _FINGERS:
            tip = hand.get(tip_joint)
            pip = hand.get(pip_joint)
            if tip is None or pip is None:
                continue
            setattr(result, name, _distance(tip, wrist) > _distance(pip, wrist))
        return result

    def extended_finger_count(self, hand: HandLandmarks) -> int:
        return self.extended_fingers(hand).count

    def classify(self, hand: HandLandmarks) -> Gesture:
        if hand.confidence < self.min_confidence:
            return Gesture.NONE

        fingers = self.extended_fingers(hand)

        # Checked before the counts, because a V sign has two fingers out and
        # would otherwise fall through to NONE.
        if fingers.index and fingers.middle and not fingers.ring and not fingers.little:
            return Gesture.PEACE
        if fingers.count >= self.open_hand_min_extended:
            return Gesture.OPEN_HAND
        if fingers.count <= self.closed_hand_max_extended:
            return Gesture.CLOSED_HAND
        return Gesture.NONE


class GestureDebouncer:
    """Temporal smoothing: a gesture must be held steadily before it counts.

    Hand tracking flickers between frames — a fist is briefly read as an open
    hand as the fingers close. Without this, a single grab gesture fires
    several conflicting events. Returns the new stable gesture only on the
    frame where it changes, and None otherwise.
    """

    def __init__(self, hold_duration: float = 0.3) -> None:
        self.hold_duration = hold_duration
        self._stable = Gesture.NONE
        self._candidate = Gesture.NONE
        self._candidate_since = 0.0

    @property
    def current(self) -> Gesture:
        return self._stable

    def update(self, raw: Gesture, at: float) -> Optional[Gesture]:
        if raw != self._candidate:
            self._candidate = raw
            self._candidate_since = at
            return None
        if self._candidate != self._stable and (at - self._candidate_since) >= self.hold_duration:
            self._stable = self._candidate
            return self._stable
        return None

    def reset(self) -> None:
        self._stable = Gesture.NONE
        self._candidate = Gesture.NONE
        self._candidate_since = 0.0
