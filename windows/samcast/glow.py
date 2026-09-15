"""The glow burst — the Windows counterpart to `GlowBurst.swift`.

Same visual language as the Mac app, deliberately: amber rings expanding
outward when something leaves this machine, cyan rings contracting inward when
something arrives. Users learn one animation, not two.

It also does real work. The rings run for 2.2 seconds, which is longer than
the handoff usually takes — that is the point. It covers the network latency
so the transfer feels deliberate rather than laggy, and it gives unambiguous
feedback about *which direction* things went.
"""

from __future__ import annotations

from typing import Optional

try:
    import tkinter as tk
except ImportError:                 # a Python built without Tk
    tk = None                       # the app reports this rather than crashing

DURATION_MS = 2200
RING_COUNT = 5
AMBER = "#f5a623"      # leaving
CYAN = "#39c8d8"       # arriving


class GlowBurst:
    """Draws staggered rings on a Tk canvas. Safe to trigger repeatedly."""

    def __init__(self, canvas: "tk.Canvas") -> None:
        self.canvas = canvas
        self._after: Optional[str] = None
        self._items: list[int] = []

    def burst(self, outward: bool = True) -> None:
        self.cancel()
        colour = AMBER if outward else CYAN
        self._animate(0, colour, outward)

    def cancel(self) -> None:
        if self._after:
            try:
                self.canvas.after_cancel(self._after)
            except Exception:
                pass
            self._after = None
        for item in self._items:
            self.canvas.delete(item)
        self._items = []

    def _animate(self, elapsed: int, colour: str, outward: bool) -> None:
        for item in self._items:
            self.canvas.delete(item)
        self._items = []

        width = self.canvas.winfo_width() or 320
        height = self.canvas.winfo_height() or 120
        cx, cy = width / 2, height / 2
        max_radius = min(width, height) * 0.48
        progress = elapsed / DURATION_MS

        for index in range(RING_COUNT):
            # Stagger the rings so they read as a pulse rather than one circle.
            phase = progress - index * 0.12
            if phase <= 0 or phase >= 1:
                continue
            radius = max_radius * (phase if outward else (1 - phase))
            if radius <= 1:
                continue
            # Tk has no alpha on canvas items, so the fade is done by blending
            # the ring colour toward the background as it travels.
            self._items.append(self.canvas.create_oval(
                cx - radius, cy - radius, cx + radius, cy + radius,
                outline=_fade(colour, 1 - phase), width=max(1, int(3 * (1 - phase))),
            ))

        if elapsed < DURATION_MS:
            self._after = self.canvas.after(16, self._animate, elapsed + 16, colour, outward)
        else:
            self.cancel()


def _fade(hex_colour: str, amount: float, background: str = "#101418") -> str:
    """Blend toward the panel background — Tk canvas items have no alpha."""
    amount = max(0.0, min(1.0, amount))
    fg = tuple(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    bg = tuple(int(background[i:i + 2], 16) for i in (1, 3, 5))
    blended = tuple(int(b + (f - b) * amount) for f, b in zip(fg, bg))
    return "#%02x%02x%02x" % blended
