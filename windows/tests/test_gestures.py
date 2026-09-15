"""Gesture parity with the Swift core.

The cases come from `docs/gesture-vectors.json`, which the Swift check suite
reads too. If the two classifiers ever disagree about what a fist is, one of
these suites fails — which is the point, because a gesture that means
different things on a Mac and a PC is worse than one that works on neither.
"""

import json
import unittest
from pathlib import Path

from quackcast.gestures import Gesture, GestureClassifier, GestureDebouncer, HandLandmarks

VECTORS = Path(__file__).resolve().parents[2] / "docs" / "gesture-vectors.json"


def _load(case) -> HandLandmarks:
    return HandLandmarks(
        points={int(k): tuple(v) for k, v in case["hand"]["points"].items()},
        confidence=case["hand"]["confidence"],
    )


class SharedVectors(unittest.TestCase):
    def test_every_shared_case(self):
        data = json.loads(VECTORS.read_text())
        self.assertTrue(data["cases"], "fixtures must not be empty")
        classifier = GestureClassifier()
        for case in data["cases"]:
            with self.subTest(case=case["name"]):
                result = classifier.classify(_load(case))
                self.assertEqual(result.value, case["expect"])


class Debouncing(unittest.TestCase):
    """Hand tracking flickers; a gesture must be held before it counts."""

    def test_a_flicker_never_fires(self):
        d = GestureDebouncer(hold_duration=0.3)
        self.assertIsNone(d.update(Gesture.CLOSED_HAND, 0.00))
        self.assertIsNone(d.update(Gesture.OPEN_HAND, 0.05))   # flicker
        self.assertIsNone(d.update(Gesture.CLOSED_HAND, 0.10))
        self.assertIsNone(d.update(Gesture.CLOSED_HAND, 0.20))
        self.assertEqual(d.current, Gesture.NONE, "nothing held long enough")

    def test_a_held_gesture_fires_once(self):
        d = GestureDebouncer(hold_duration=0.3)
        self.assertIsNone(d.update(Gesture.CLOSED_HAND, 0.0))
        self.assertIsNone(d.update(Gesture.CLOSED_HAND, 0.2))
        self.assertEqual(d.update(Gesture.CLOSED_HAND, 0.35), Gesture.CLOSED_HAND)
        self.assertIsNone(d.update(Gesture.CLOSED_HAND, 0.5),
                          "must not fire again while still held")

    def test_changing_gesture_fires_again(self):
        d = GestureDebouncer(hold_duration=0.3)
        d.update(Gesture.CLOSED_HAND, 0.0)
        d.update(Gesture.CLOSED_HAND, 0.4)
        self.assertEqual(d.current, Gesture.CLOSED_HAND)
        d.update(Gesture.OPEN_HAND, 0.5)
        self.assertEqual(d.update(Gesture.OPEN_HAND, 0.9), Gesture.OPEN_HAND)

    def test_reset_clears_state(self):
        d = GestureDebouncer(hold_duration=0.1)
        d.update(Gesture.OPEN_HAND, 0.0)
        d.update(Gesture.OPEN_HAND, 0.5)
        d.reset()
        self.assertEqual(d.current, Gesture.NONE)


class Geometry(unittest.TestCase):
    def test_extension_is_scale_invariant(self):
        """The same hand held closer to the camera must classify the same —
        the test is a comparison, never an absolute distance."""
        classifier = GestureClassifier()
        data = json.loads(VECTORS.read_text())
        case = next(c for c in data["cases"] if c["expect"] == "openHand")
        near = _load(case)
        far = HandLandmarks(
            points={j: (0.5 + (x - 0.5) * 0.3, 0.5 + (y - 0.5) * 0.3)
                    for j, (x, y) in near.points.items()},
            confidence=near.confidence,
        )
        self.assertEqual(classifier.classify(far), Gesture.OPEN_HAND)


if __name__ == "__main__":
    unittest.main()
