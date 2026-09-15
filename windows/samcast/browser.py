"""Reading (and closing) the current tab of the focused browser on Windows.

This is the counterpart to `BrowserLink.swift`, and it is what makes "the link
closed here and opened there" possible: only a URL travels, so it arrives
instantly, at full fidelity, and opens natively without disturbing any of the
other machine's tabs.

macOS has AppleScript for this. Windows has no equivalent, so we read the
browser's own accessibility tree via UI Automation — the same mechanism a
screen reader uses. That has two consequences worth knowing:

* It is read-only and requires no special privilege. We are not injecting
  anything into the browser, not attaching a debugger, and not installing an
  extension.
* Firefox exposes its tree only once accessibility is switched on, so it may
  report nothing until then. Chrome, Edge, Brave, Vivaldi and Opera work as-is.

Every import here is lazy, so this module can be imported (and the rest of the
app tested) on a machine that is not Windows.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional

#: Process names we know how to read, mapped to a friendly name.
KNOWN_BROWSERS = {
    "chrome.exe": "Google Chrome",
    "msedge.exe": "Microsoft Edge",
    "brave.exe": "Brave",
    "vivaldi.exe": "Vivaldi",
    "opera.exe": "Opera",
    "firefox.exe": "Firefox",
}


@dataclass
class Page:
    url: str
    title: str = ""
    source: str = ""


class BrowserUnavailable(Exception):
    """Raised with a message meant to be shown to the user verbatim."""


def _require_windows() -> None:
    if not sys.platform.startswith("win"):
        raise BrowserUnavailable("reading the focused browser is only implemented on Windows")


def foreground_process_name() -> Optional[str]:
    """Executable name of the focused window's process, lowercased."""
    _require_windows()
    try:
        import win32gui
        import win32process
        import psutil
    except ImportError as exc:
        raise BrowserUnavailable(f"missing dependency: {exc.name}. Run: pip install -r requirements.txt")

    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name().lower()
    except Exception:
        return None


def frontmost_page() -> Optional[Page]:
    """The page open in the focused browser, or None if the focused window is
    not a browser we can read."""
    try:
        _require_windows()
    except BrowserUnavailable:
        return None

    process = foreground_process_name()
    if not process or process not in KNOWN_BROWSERS:
        return None

    try:
        import uiautomation as auto
    except ImportError:
        return None

    try:
        window = auto.GetForegroundControl()
        if window is None:
            return None
        # The Document control is the page itself; its ValuePattern carries
        # the full URL including the scheme. The address bar is a poorer
        # source — it hides the scheme and shows whatever the user is midway
        # through typing.
        document = window.DocumentControl(searchDepth=24)
        if not document.Exists(maxSearchSeconds=0.6):
            return None
        url = (document.GetValuePattern().Value or "").strip()
        if not url:
            return None
        if "://" not in url:
            url = "https://" + url
        from . import wire
        safe = wire.safe_handoff_url(url)
        if not safe:
            return None
        return Page(url=safe, title=(document.Name or url), source=KNOWN_BROWSERS[process])
    except Exception:
        # UI Automation raises a wide variety of COM errors when a window
        # closes mid-query. None of them should take the app down.
        return None


def close_frontmost_tab() -> bool:
    """The "it left my screen" half of a handoff.

    Ctrl+W to the focused browser. Deliberately only sent when the focused
    window really is a known browser — sending it blindly would close a
    document, a chat, or a tab in some unrelated program.
    """
    try:
        _require_windows()
    except BrowserUnavailable:
        return False
    if (foreground_process_name() or "") not in KNOWN_BROWSERS:
        return False
    try:
        import uiautomation as auto
        auto.SendKeys("{Ctrl}w", waitTime=0)
        return True
    except Exception:
        return False


def open_url(url: str) -> None:
    """Open a handed-over URL in the default browser.

    Validated one final time here. `webbrowser` ultimately hands the string to
    the shell, so a non-web scheme reaching this point could start a program.
    """
    from . import wire
    safe = wire.safe_handoff_url(url)
    if not safe:
        raise ValueError("refusing to open something that is not an http(s) address")
    import webbrowser
    webbrowser.open(safe)
