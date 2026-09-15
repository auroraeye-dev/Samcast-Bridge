"""Framing and message encoding for the QuackCast bridge protocol.

This is a direct counterpart to `mac/Sources/QuackBridge/WireFormat.swift`.
The two files are the whole compatibility surface between the Mac and Windows
apps, so they are deliberately written to read the same way — if you change
one, change the other, and run both check suites.

See docs/PROTOCOL.md for the format itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlparse

VERSION = 1

DISCOVERY_PORT = 50505
BEACON_INTERVAL = 2.0
PEER_TIMEOUT = 8.0

#: Hard ceiling on one message. Without this a bad or hostile length prefix
#: would have the receiver try to allocate up to 4 GiB; a real frame is a few
#: hundred KiB.
MAX_PAYLOAD = 8 * 1024 * 1024

TYPE_CONTROL = 0x01
TYPE_FRAME = 0x02
_KNOWN_TYPES = (TYPE_CONTROL, TYPE_FRAME)


class ProtocolError(Exception):
    """The stream is unusable and the connection must be closed.

    A length-prefixed stream cannot be resynchronised once it is out of step:
    there is no marker to search for, so every subsequent read would be
    garbage. Hanging up is the only correct response.
    """


# --------------------------------------------------------------------------
# Framing:  length(4, big-endian) | type(1) | payload
# --------------------------------------------------------------------------

def encode(msg_type: int, payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + bytes([msg_type]) + payload


class Reader:
    """Incremental reader: feed it whatever the socket returned, take out
    whole messages.

    TCP provides no message boundaries. A reader that assumed one `recv` is
    one message would appear to work on a quiet LAN and corrupt silently the
    moment frames start flowing.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def append(self, data: bytes) -> None:
        self._buffer.extend(data)

    def next(self) -> Optional[Tuple[int, bytes]]:
        """Return `(type, payload)`, or None if more bytes are needed."""
        if len(self._buffer) < 5:
            return None
        length = int.from_bytes(self._buffer[:4], "big")
        if length > MAX_PAYLOAD:
            raise ProtocolError(
                f"message of {length} bytes exceeds the {MAX_PAYLOAD} byte limit"
            )
        msg_type = self._buffer[4]
        if msg_type not in _KNOWN_TYPES:
            raise ProtocolError(f"unknown message type 0x{msg_type:02x}")
        if len(self._buffer) < 5 + length:
            return None
        payload = bytes(self._buffer[5:5 + length])
        del self._buffer[:5 + length]
        return msg_type, payload


# --------------------------------------------------------------------------
# Control envelope
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Envelope:
    control: str
    payload: Optional[str] = None


def encode_control(control: str, payload: Optional[str] = None) -> bytes:
    """Identical JSON to the envelope the Apple build sends, so both
    transports carry the same control plane."""
    body = json.dumps({"control": control, "payload": payload}).encode("utf-8")
    return encode(TYPE_CONTROL, body)


def decode_control(payload: bytes) -> Optional[Envelope]:
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    control = obj.get("control")
    if not isinstance(control, str):
        return None
    value = obj.get("payload")
    return Envelope(control, value if isinstance(value, str) else None)


# --------------------------------------------------------------------------
# Discovery beacon
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Beacon:
    id: str
    name: str
    kind: str
    port: int
    qc: int = VERSION


def encode_beacon(beacon: Beacon) -> bytes:
    return json.dumps({
        "qc": beacon.qc,
        "id": beacon.id,
        "name": beacon.name,
        "kind": beacon.kind,
        "port": beacon.port,
    }).encode("utf-8")


def decode_beacon(data: bytes) -> Optional[Beacon]:
    """Returns None for anything we do not understand — including a future
    protocol version, which must be ignored rather than guessed at."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict) or obj.get("qc") != VERSION:
        return None
    peer_id = obj.get("id")
    port = obj.get("port")
    if not isinstance(peer_id, str) or not peer_id:
        return None
    if not isinstance(port, int) or not (0 < port < 65536):
        return None
    return Beacon(
        id=peer_id,
        name=str(obj.get("name") or peer_id),
        kind=str(obj.get("kind") or "unknown"),
        port=port,
    )


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------

def safe_handoff_url(raw: str) -> Optional[str]:
    """The receiving side's own check on a handed-over URL.

    Enforced here, independently of whatever the sender claims to have sent,
    because this is the only value that crosses the network and is then handed
    to the operating system. Anything but http/https — `file:`, `smb:`, a
    custom scheme registered by some installed program — could reach a local
    resource or launch something, so only the two web schemes are allowed.
    """
    if not isinstance(raw, str):
        return None
    trimmed = raw.strip()
    if not trimmed or len(trimmed) > 4096:
        return None
    try:
        parsed = urlparse(trimmed)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    if not parsed.netloc:
        return None
    return trimmed
