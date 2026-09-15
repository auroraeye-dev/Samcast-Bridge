"""Recognising pages that would do real damage if handed over by accident.

A direct port of `Sources/SamcastCore/Session/PageRisk.swift`, and checked
against the same fixture file — `Samcast/docs/meeting-vectors.json` — so the
Mac, iOS and Windows builds cannot disagree about what counts as a call.

The reasoning, from the Swift original: the gesture is never perfectly
reliable, and a hand closing to pick up a mug reads much like a deliberate
fist. For an ordinary page a misread costs a reopened tab. For a live meeting
it drops you out of the call in front of everyone. Those are not the same
mistake, so the app asks first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


@dataclass(frozen=True)
class MeetingMatch:
    service: str
    code: Optional[str] = None


@dataclass(frozen=True)
class PageRisk:
    meeting: Optional[MeetingMatch] = None

    @property
    def needs_confirmation(self) -> bool:
        return self.meeting is not None


ORDINARY = PageRisk()

#: Pages on a meeting domain that are plainly not a call. Prompting on these
#: would train people to dismiss prompts unread, which protects nobody.
_MARKETING = {
    "pricing", "download", "about", "support", "signin", "login",
    "features", "blog", "contact", "terms", "privacy",
}

#: Services where any room path is a call.
_ROOM_SERVICES = [
    ("meet.jit.si", "Jitsi Meet"),
    ("whereby.com", "Whereby"),
    ("chime.aws", "Amazon Chime"),
    ("gather.town", "Gather"),
    ("around.co", "Around"),
    ("bluejeans.com", "BlueJeans"),
    ("gotomeeting.com", "GoToMeeting"),
]


def assess(url_string: str) -> PageRisk:
    """Anything unparseable is treated as ordinary. This is a safety prompt,
    not a filter, and it must not block a handoff it merely failed to read."""
    if not isinstance(url_string, str):
        return ORDINARY
    try:
        parsed = urlparse(url_string.strip())
    except ValueError:
        return ORDINARY
    if not parsed.hostname:
        return ORDINARY
    return assess_parts(parsed.hostname, parsed.path or "")


def assess_parts(raw_host: str, raw_path: str) -> PageRisk:
    host = _normalise(raw_host)
    path = raw_path.lower()

    # --- Google Meet -------------------------------------------------------
    # Only a real room counts; `meet.google.com` alone is the landing page.
    if _matches(host, "meet.google.com"):
        code = _meet_code(path)
        if code:
            return PageRisk(MeetingMatch("Google Meet", code))
        if path.startswith("/lookup/") and len(path) > len("/lookup/"):
            return PageRisk(MeetingMatch("Google Meet", None))
        return ORDINARY

    # --- Zoom ---------------------------------------------------------------
    if _matches(host, "zoom.us") or _matches(host, "zoomgov.com"):
        for marker in ("/j/", "/s/", "/wc/", "/my/"):
            if marker in path:
                return PageRisk(MeetingMatch("Zoom", _segment_after(marker, path)))
        return ORDINARY

    # --- Microsoft Teams ------------------------------------------------------
    if _matches(host, "teams.microsoft.com") or _matches(host, "teams.live.com"):
        for marker in ("meetup-join", "/l/meeting", "/meet/", "/v2/?meetingjoin"):
            if marker in path:
                return PageRisk(MeetingMatch("Microsoft Teams", None))
        return ORDINARY

    # --- Webex ----------------------------------------------------------------
    if _matches(host, "webex.com"):
        for marker in ("/meet/", "/join/", "/wbxmjs/", "/j.php"):
            if marker in path:
                return PageRisk(MeetingMatch("Webex", None))
        return ORDINARY

    # --- Any-room services ------------------------------------------------------
    for domain, name in _ROOM_SERVICES:
        if _matches(host, domain):
            trimmed = path.strip("/")
            if trimmed and not _is_marketing(trimmed):
                return PageRisk(MeetingMatch(name, None))
            return ORDINARY

    return ORDINARY


def _normalise(host: str) -> str:
    """`www.` is noise, and a match should cover subdomains — a Zoom link is
    usually `acme.zoom.us`, never the bare domain."""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def _matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _is_marketing(path: str) -> bool:
    return path.split("/")[0] in _MARKETING


def _meet_code(path: str) -> Optional[str]:
    """Google Meet room codes look like `abc-defg-hij` — three groups of
    ASCII letters, 3-4-3. Matched by shape rather than by regex so the rule
    stays obvious."""
    candidate = path.strip("/")
    if not candidate:
        return None
    groups = candidate.split("-")
    if len(groups) != 3:
        return None
    if [len(g) for g in groups] != [3, 4, 3]:
        return None
    if not all(g.isascii() and g.isalpha() for g in groups):
        return None
    return candidate


def _segment_after(marker: str, path: str) -> Optional[str]:
    """The path segment immediately following a marker, e.g. the id in
    `/j/1234567890`."""
    index = path.find(marker)
    if index < 0:
        return None
    rest = path[index + len(marker):]
    value = rest.split("/")[0].split("?")[0]
    return value or None
