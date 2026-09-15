"""Live-meeting detection, against the fixtures the Swift core uses.

The file lives in the main QuackCast checkout because Core is the source of
truth for this rule; the bridge already requires that checkout as a sibling.
If it is missing the test says so rather than passing quietly — a parity test
that silently skips is worse than none.
"""

import json
import unittest
from pathlib import Path

from quackcast import pagerisk

VECTORS = Path(__file__).resolve().parents[3] / "QuackCast" / "docs" / "meeting-vectors.json"


class SharedVectors(unittest.TestCase):
    def test_every_shared_case(self):
        self.assertTrue(
            VECTORS.exists(),
            f"shared fixtures not found at {VECTORS} — the main QuackCast "
            f"checkout must sit beside this one",
        )
        cases = json.loads(VECTORS.read_text())["cases"]
        self.assertTrue(cases)
        for case in cases:
            with self.subTest(url=case["url"]):
                risk = pagerisk.assess(case["url"])
                expected = case["expect_service"]
                if expected is None:
                    self.assertIsNone(
                        risk.meeting,
                        f"{case['url']} must NOT prompt — a prompt people learn "
                        f"to dismiss unread protects nobody",
                    )
                else:
                    self.assertIsNotNone(risk.meeting, f"{case['url']} must prompt")
                    self.assertEqual(risk.meeting.service, expected)
                    if case.get("expect_code"):
                        self.assertEqual(risk.meeting.code, case["expect_code"])


class Robustness(unittest.TestCase):
    def test_unparseable_input_is_ordinary(self):
        """A safety prompt must never block a handoff it merely failed to read."""
        for junk in ["not a url", "", "   ", "://", None, 1234]:
            with self.subTest(junk=junk):
                self.assertFalse(pagerisk.assess(junk).needs_confirmation)

    def test_needs_confirmation_flag(self):
        self.assertTrue(pagerisk.assess("https://meet.google.com/abc-defg-hij").needs_confirmation)
        self.assertFalse(pagerisk.assess("https://example.com").needs_confirmation)


if __name__ == "__main__":
    unittest.main()
