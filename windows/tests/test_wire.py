"""The Python half of the protocol conformance suite.

These mirror `mac/Sources/BridgeCheck/main.swift` case for case. Two
independent implementations of a wire format drift silently unless something
pins them together; this and that file are that pin.

    python -m pytest tests -q          (or: python -m unittest discover tests)
"""

import unittest

from samcast import wire


class Framing(unittest.TestCase):
    def test_header_layout(self):
        framed = wire.encode(wire.TYPE_CONTROL, b"hi")
        self.assertEqual(len(framed), 7, "5-byte header plus payload")
        self.assertEqual(framed[:4], b"\x00\x00\x00\x02", "length is big-endian")
        self.assertEqual(framed[4], wire.TYPE_CONTROL, "type byte follows the length")

    def test_round_trip(self):
        reader = wire.Reader()
        reader.append(wire.encode(wire.TYPE_CONTROL, b"hi"))
        self.assertEqual(reader.next(), (wire.TYPE_CONTROL, b"hi"))
        self.assertIsNone(reader.next(), "buffer is then empty")

    def test_split_across_reads(self):
        # TCP gives no message boundaries: a header can arrive in pieces.
        framed = wire.encode(wire.TYPE_CONTROL, b"one")
        reader = wire.Reader()
        reader.append(framed[:3])
        self.assertIsNone(reader.next(), "a half-read header yields nothing")
        reader.append(framed[3:])
        self.assertEqual(reader.next(), (wire.TYPE_CONTROL, b"one"))

    def test_batched_in_one_read(self):
        a = wire.encode(wire.TYPE_CONTROL, b"one")
        b = wire.encode(wire.TYPE_FRAME, b"\xff\xd8\xff")
        reader = wire.Reader()
        reader.append(a + b)
        self.assertEqual(reader.next(), (wire.TYPE_CONTROL, b"one"))
        self.assertEqual(reader.next(), (wire.TYPE_FRAME, b"\xff\xd8\xff"))
        self.assertIsNone(reader.next(), "nothing left over")

    def test_empty_payload(self):
        reader = wire.Reader()
        reader.append(wire.encode(wire.TYPE_FRAME, b""))
        self.assertEqual(reader.next(), (wire.TYPE_FRAME, b""))


class MalformedStreams(unittest.TestCase):
    def test_oversized_length_is_refused(self):
        # ~1 GiB claimed. Must be refused rather than allocated.
        reader = wire.Reader()
        reader.append(b"\x40\x00\x00\x00\x01")
        with self.assertRaises(wire.ProtocolError):
            reader.next()

    def test_unknown_type_is_refused(self):
        reader = wire.Reader()
        reader.append(b"\x00\x00\x00\x01\x99\x00")
        with self.assertRaises(wire.ProtocolError):
            reader.next()


class ControlEnvelope(unittest.TestCase):
    def test_round_trip(self):
        reader = wire.Reader()
        reader.append(wire.encode_control("handoff", "https://example.com"))
        _, payload = reader.next()
        envelope = wire.decode_control(payload)
        self.assertEqual(envelope.control, "handoff")
        self.assertEqual(envelope.payload, "https://example.com")

    def test_same_keys_as_the_apple_build(self):
        _, payload = _one(wire.encode_control("endCast"))
        self.assertIn(b'"control"', payload)
        self.assertIn(b'"payload"', payload)

    def test_missing_payload_is_none(self):
        envelope = wire.decode_control(b'{"control":"endCast"}')
        self.assertEqual(envelope.control, "endCast")
        self.assertIsNone(envelope.payload)

    def test_junk_is_rejected(self):
        self.assertIsNone(wire.decode_control(b"not json"))
        self.assertIsNone(wire.decode_control(b"[1,2,3]"))
        self.assertIsNone(wire.decode_control(b'{"payload":"x"}'))


class DiscoveryBeacon(unittest.TestCase):
    def test_round_trip(self):
        beacon = wire.Beacon(id="ID-1", name="swift-heron-3172", kind="mac", port=51234)
        self.assertEqual(wire.decode_beacon(wire.encode_beacon(beacon)), beacon)

    def test_future_version_is_ignored(self):
        self.assertIsNone(wire.decode_beacon(
            b'{"qc":99,"id":"x","name":"n","kind":"mac","port":1}'))

    def test_port_must_be_usable(self):
        self.assertIsNone(wire.decode_beacon(
            b'{"qc":1,"id":"x","name":"n","kind":"mac","port":0}'))
        self.assertIsNone(wire.decode_beacon(
            b'{"qc":1,"id":"x","name":"n","kind":"mac","port":99999}'))

    def test_junk_is_ignored(self):
        self.assertIsNone(wire.decode_beacon(b"not json"))
        self.assertIsNone(wire.decode_beacon(b'{"qc":1,"id":"","port":1}'))


class HandoffURLSafety(unittest.TestCase):
    """The receiver's own check. This is the only value that crosses the
    network and is then handed to the operating system."""

    def test_web_urls_are_allowed(self):
        self.assertIsNotNone(wire.safe_handoff_url("https://example.com/a?b=c"))
        self.assertIsNotNone(wire.safe_handoff_url("http://example.com"))
        self.assertIsNotNone(wire.safe_handoff_url("  https://example.com \n"))

    def test_anything_that_could_reach_the_machine_is_refused(self):
        for hostile in [
            "file:///C:/Windows/System32/config/SAM",
            "file:///etc/passwd",
            "smb://server/share",
            "javascript:alert(1)",
            "vbscript:msgbox",
            "ms-msdt:/id",                 # the Follina vector, as an example
            "someapp://run",
            "C:\\Windows\\System32\\cmd.exe",
            "/Applications/Calculator.app",
            "example.com",                 # no scheme
            "https://",                    # no host
            "",
            "   ",
        ]:
            with self.subTest(hostile=hostile):
                self.assertIsNone(wire.safe_handoff_url(hostile))

    def test_absurdly_long_is_refused(self):
        self.assertIsNone(wire.safe_handoff_url("https://example.com/" + "a" * 5000))

    def test_non_strings_are_refused(self):
        self.assertIsNone(wire.safe_handoff_url(None))
        self.assertIsNone(wire.safe_handoff_url(12345))


def _one(framed: bytes):
    reader = wire.Reader()
    reader.append(framed)
    return reader.next()


if __name__ == "__main__":
    unittest.main()
