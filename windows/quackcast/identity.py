"""Persistent device identity and trust — the Windows counterpart to
QuackCastCore's `DeviceIdentity` and `TrustStore`.

Both are stored as JSON next to each other in the user's own config
directory. Nothing is written outside it: no registry keys, no system
directories, no startup entries, nothing that would need admin rights or
survive uninstalling the folder.
"""

from __future__ import annotations

import json
import os
import random
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict


def config_dir() -> Path:
    """`%APPDATA%\\QuackCast` on Windows, `~/.config/quackcast` elsewhere.

    The fallback matters: it is what lets the Windows code be developed and
    tested on a Mac.
    """
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) / "QuackCast" if appdata else Path.home() / ".config" / "quackcast"
    base.mkdir(parents=True, exist_ok=True)
    return base


_ADJECTIVES = [
    "amber", "brisk", "calm", "clever", "dusky", "eager", "fleet", "gentle",
    "hazel", "jolly", "keen", "lucky", "mellow", "noble", "quiet", "rapid",
    "silver", "swift", "teal", "vivid", "witty", "zesty",
]
_ANIMALS = [
    "otter", "falcon", "heron", "lynx", "marten", "osprey", "panda", "quail",
    "raven", "seal", "tapir", "vole", "walrus", "yak", "zebu", "badger",
    "cobra", "dingo", "egret", "ferret",
]


def random_name() -> str:
    """e.g. "swift-heron-3172" — easy to say aloud, unlikely to collide.

    Same word lists as the Swift side, so names from either platform look
    like they belong to the same product.
    """
    return f"{random.choice(_ADJECTIVES)}-{random.choice(_ANIMALS)}-{random.randint(1000, 9999)}"


@dataclass(frozen=True)
class DeviceIdentity:
    id: str
    name: str

    @staticmethod
    def load_or_create(path: Path | None = None) -> "DeviceIdentity":
        """Windows device names are not dependable — they change, they
        collide, and a domain-joined PC may have a name the user never chose.
        A random name generated once and kept forever is what lets peers
        recognise each other across restarts."""
        path = path or (config_dir() / "identity.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data.get("id"), str) and isinstance(data.get("name"), str):
                return DeviceIdentity(data["id"], data["name"])
        except (OSError, ValueError):
            pass
        identity = DeviceIdentity(str(uuid.uuid4()), random_name())
        try:
            path.write_text(
                json.dumps({"id": identity.id, "name": identity.name}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass          # a read-only profile still works, just not persistently
        return identity


class TrustStore:
    """Remembers which devices you have already accepted things from.

    Trust answers exactly one question — *may this device hand me things at
    all?* It never decides where something goes; that is always the open hand
    in front of the camera. Keyed on the peer's stable id, never its name or
    address, both of which can be changed by anyone.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (config_dir() / "trusted.json")
        self._trusted: Dict[str, str] = {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._trusted = {k: str(v) for k, v in data.items() if isinstance(k, str)}
        except (OSError, ValueError):
            pass

    @property
    def trusted(self) -> Dict[str, str]:
        return dict(self._trusted)

    def is_trusted(self, peer_id: str) -> bool:
        return peer_id in self._trusted

    def trust(self, peer_id: str, name: str) -> None:
        self._trusted[peer_id] = name
        self._save()

    def untrust(self, peer_id: str) -> None:
        self._trusted.pop(peer_id, None)
        self._save()

    def untrust_all(self) -> None:
        self._trusted.clear()
        self._save()

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps(self._trusted, indent=2), encoding="utf-8")
        except OSError:
            pass
