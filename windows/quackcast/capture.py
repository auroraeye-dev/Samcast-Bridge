"""Capturing the focused window as JPEG frames — the Windows counterpart to
`ScreenCaptureKitSource.swift`.

Used only for things that *cannot* be handed over as a link. A running process
cannot leave its machine, so a non-browser window is mirrored: the app keeps
running here and only its picture travels. Links are always preferred when the
focused window is a browser, because a link genuinely moves.

Frame budget matches the Apple build — ≤1100px wide, ~12 fps, JPEG quality 70
— chosen for a wireless link rather than for fidelity.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

MAX_WIDTH = 1100
FRAMES_PER_SECOND = 12
JPEG_QUALITY = 70

#: Never capture our own window: mirroring a window that is *displaying* the
#: mirror feeds the stream back into itself and produces an infinite tunnel.
_OWN_TITLE_MARKERS = ("quackcast",)


@dataclass
class CaptureTarget:
    title: str
    rect: Tuple[int, int, int, int]        # left, top, right, bottom


class CaptureUnavailable(Exception):
    """Raised with a message meant to be shown to the user verbatim."""


def focused_window() -> Optional[CaptureTarget]:
    """The window the user is working in, or None if there isn't a usable one."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import win32gui
    except ImportError:
        return None

    hwnd = win32gui.GetForegroundWindow()
    if not hwnd or not win32gui.IsWindowVisible(hwnd):
        return None
    title = win32gui.GetWindowText(hwnd) or ""
    if any(marker in title.lower() for marker in _OWN_TITLE_MARKERS):
        return None
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None
    # Tiny windows are almost always tool palettes or a stray tooltip.
    if right - left < 120 or bottom - top < 120:
        return None
    return CaptureTarget(title=title, rect=(left, top, right, bottom))


def grab_jpeg(target: CaptureTarget) -> Optional[bytes]:
    """One frame of the given window region, encoded as JPEG."""
    try:
        import mss
        from PIL import Image
    except ImportError as exc:
        raise CaptureUnavailable(
            f"missing dependency: {exc.name}. Run: pip install -r requirements.txt"
        )
    import io

    left, top, right, bottom = target.rect
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    with mss.mss() as sct:
        raw = sct.grab({"left": left, "top": top, "width": width, "height": height})

    image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    if image.width > MAX_WIDTH:
        scale = MAX_WIDTH / image.width
        image = image.resize((MAX_WIDTH, max(1, int(image.height * scale))), Image.BILINEAR)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


class WindowStreamer:
    """Sends frames of the focused window to one peer until stopped.

    The target window is fixed when streaming starts. Following the focus
    instead would mean the viewer sees whatever the sender clicks on next,
    including their mail — a mirror should show what was offered, and only
    that.
    """

    def __init__(self, send: Callable[[bytes], None],
                 on_error: Optional[Callable[[str], None]] = None) -> None:
        self._send = send
        self._on_error = on_error or (lambda message: None)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.target: Optional[CaptureTarget] = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, target: Optional[CaptureTarget] = None) -> bool:
        if self.is_running:
            return True
        target = target or focused_window()
        if not target:
            self._on_error("Bring the window you want to share to the front first.")
            return False
        self.target = target
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def _loop(self) -> None:
        interval = 1.0 / FRAMES_PER_SECOND
        while not self._stop.is_set() and self.target:
            started = time.monotonic()
            try:
                frame = grab_jpeg(self.target)
                if frame:
                    self._send(frame)
            except CaptureUnavailable as exc:
                self._on_error(str(exc))
                return
            except Exception as exc:
                self._on_error(f"Capture stopped: {exc}")
                return
            time.sleep(max(0.0, interval - (time.monotonic() - started)))
