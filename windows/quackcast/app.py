"""The QuackCast window for Windows.

Deliberately Tkinter: it ships with the standard Python installer, so the app
runs after `pip install -r requirements.txt` without a GUI toolkit download,
and there is no installer to trust. Everything heavy — camera, sockets,
capture — already runs on its own threads; this file only draws.
"""

from __future__ import annotations

import io
import queue
import sys
import webbrowser
from typing import Optional

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:                                     # a Python built without Tk
    tk = None
    ttk = None

from . import browser as browser_link
from . import capture as capture_module
from .camera import HandTracker
from .gestures import Gesture
from .glow import GlowBurst
from .identity import DeviceIdentity, TrustStore
from .session import BridgeSession, Page, PlatformHooks
from .transport import LANTransport, Peer

BG = "#101418"
PANEL = "#171c22"
TEXT = "#e8edf2"
MUTED = "#8b98a5"
AMBER = "#f5a623"
CYAN = "#39c8d8"


class WindowsHooks(PlatformHooks):
    """Wires the session to the real machine."""

    def frontmost_page(self) -> Optional[Page]:
        page = browser_link.frontmost_page()
        if not page:
            return None
        return Page(url=page.url, title=page.title, source=page.source)

    def close_frontmost_tab(self) -> bool:
        return browser_link.close_frontmost_tab()

    def open_url(self, url: str) -> None:
        browser_link.open_url(url)


class QuackCastApp:
    def __init__(self) -> None:
        if tk is None:
            raise SystemExit(
                "This Python has no Tk support, so the window cannot open.\n"
                "Reinstall Python from python.org (Tk is included), or use the\n"
                "headless peer instead:  python peer_cli.py"
            )

        self.identity = DeviceIdentity.load_or_create()
        self.transport = LANTransport(identity=self.identity, kind="windowsPC",
                                      log=self._log)
        self.session = BridgeSession(
            self.transport, WindowsHooks(), TrustStore(),
            on_event=self._event, on_state=self._state_changed,
            auto_trust=False,                # first contact is approved once
        )
        self.session.on_remote_frame = self._remote_frame
        self.tracker = HandTracker(on_gesture=self._gesture, on_status=self._event)
        self.streamer = capture_module.WindowStreamer(
            send=self._send_frame, on_error=self._event
        )
        self._stream_peer: Optional[Peer] = None

        #: Callbacks arrive on socket and camera threads; Tk must be touched
        #: only from its own. Everything crosses over through this queue.
        self._inbox: "queue.Queue[tuple]" = queue.Queue()
        self._viewer: Optional[tk.Toplevel] = None
        self._viewer_label: Optional[tk.Label] = None
        self._viewer_image = None                       # keep a reference alive

        self._build_ui()
        self.transport.start()
        self.root.after(50, self._drain)

    # -- UI ---------------------------------------------------------------

    def _build_ui(self) -> None:
        self.root = tk.Tk()
        self.root.title("QuackCast")
        self.root.configure(bg=BG)
        self.root.geometry("420x600")
        self.root.minsize(380, 520)

        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=20, pady=(18, 6))
        tk.Label(header, text="🦆 QuackCast", bg=BG, fg=TEXT,
                 font=("Segoe UI", 18, "bold")).pack(anchor="w")
        tk.Label(header, text=f"this PC is “{self.identity.name}”", bg=BG, fg=MUTED,
                 font=("Segoe UI", 10)).pack(anchor="w")

        self.canvas = tk.Canvas(self.root, bg=BG, height=120, highlightthickness=0)
        self.canvas.pack(fill="x", padx=20, pady=6)
        self.glow = GlowBurst(self.canvas)

        self.status = tk.Label(self.root, text="Looking for devices…", bg=BG, fg=TEXT,
                               font=("Segoe UI", 11), wraplength=360, justify="left")
        self.status.pack(fill="x", padx=20, pady=(0, 8))

        buttons = tk.Frame(self.root, bg=BG)
        buttons.pack(fill="x", padx=20)
        self.grab_button = self._button(buttons, "✊  Grab this page", self.session.grab)
        self.grab_button.pack(fill="x", pady=3)
        self.take_button = self._button(buttons, "🖐️  Take what's offered", self.session.take)
        self.take_button.pack(fill="x", pady=3)
        self.share_button = self._button(buttons, "🖥️  Share this window", self._toggle_stream)
        self.share_button.pack(fill="x", pady=3)

        # Shown only when an unknown device offers something for the first time.
        self.trust_frame = tk.Frame(self.root, bg=PANEL)
        self.trust_label = tk.Label(self.trust_frame, text="", bg=PANEL, fg=TEXT,
                                    font=("Segoe UI", 10), wraplength=340, justify="left")
        self.trust_label.pack(fill="x", padx=12, pady=(10, 6))
        trust_buttons = tk.Frame(self.trust_frame, bg=PANEL)
        trust_buttons.pack(fill="x", padx=12, pady=(0, 10))
        self._button(trust_buttons, "Allow", self.session.approve_pending).pack(side="left", expand=True, fill="x", padx=(0, 4))
        self._button(trust_buttons, "Not now", self.session.reject_pending).pack(side="left", expand=True, fill="x", padx=(4, 0))

        # Shown only when a fist lands on something that would hurt to lose
        # by accident — a live meeting, above all. Placed above the peer list
        # so it cannot be missed.
        self.confirm_frame = tk.Frame(self.root, bg=PANEL,
                                      highlightthickness=1, highlightbackground=AMBER)
        self.confirm_label = tk.Label(self.confirm_frame, text="", bg=PANEL, fg=TEXT,
                                      font=("Segoe UI", 10), wraplength=340, justify="left")
        self.confirm_label.pack(fill="x", padx=12, pady=(10, 6))
        confirm_buttons = tk.Frame(self.confirm_frame, bg=PANEL)
        confirm_buttons.pack(fill="x", padx=12, pady=(0, 10))
        self._button(confirm_buttons, "Move the call",
                     self.session.confirm_grab).pack(side="left", expand=True, fill="x", padx=(0, 4))
        self._button(confirm_buttons, "Stay here",
                     self.session.decline_grab).pack(side="left", expand=True, fill="x", padx=(4, 0))

        self.camera_var = tk.BooleanVar(value=False)
        camera_row = tk.Checkbutton(
            self.root, text="Watch for hand gestures (camera)", variable=self.camera_var,
            command=self._toggle_camera, bg=BG, fg=MUTED, selectcolor=PANEL,
            activebackground=BG, activeforeground=TEXT, highlightthickness=0,
            font=("Segoe UI", 10),
        )
        camera_row.pack(anchor="w", padx=20, pady=(10, 2))

        tk.Label(self.root, text="NEARBY", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=20, pady=(10, 2))
        self.peer_list = tk.Listbox(self.root, bg=PANEL, fg=TEXT, height=4,
                                    borderwidth=0, highlightthickness=0,
                                    font=("Segoe UI", 10), activestyle="none")
        self.peer_list.pack(fill="x", padx=20)

        tk.Label(self.root, text="ACTIVITY", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=20, pady=(12, 2))
        self.log = tk.Text(self.root, bg=PANEL, fg=MUTED, height=7, borderwidth=0,
                           highlightthickness=0, font=("Consolas", 9), wrap="word")
        self.log.pack(fill="both", expand=True, padx=20, pady=(0, 18))
        self.log.configure(state="disabled")

        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self._state_changed()

    def _button(self, parent, text: str, command) -> tk.Button:
        return tk.Button(parent, text=text, command=command, bg=PANEL, fg=TEXT,
                         activebackground="#222a33", activeforeground=TEXT,
                         relief="flat", font=("Segoe UI", 11), pady=8, cursor="hand2")

    # -- thread hand-off ---------------------------------------------------

    def _log(self, message: str) -> None:
        self._inbox.put(("log", message))

    def _event(self, message: str) -> None:
        self._inbox.put(("event", message))

    def _state_changed(self) -> None:
        self._inbox.put(("state", None))

    def _gesture(self, gesture: Gesture) -> None:
        self._inbox.put(("gesture", gesture))

    def _remote_frame(self, frame: bytes, peer: Peer) -> None:
        self._inbox.put(("frame", (frame, peer)))

    def _drain(self) -> None:
        """The single point where background work reaches Tk."""
        try:
            while True:
                kind, value = self._inbox.get_nowait()
                if kind == "log":
                    self._append(value)
                elif kind == "event":
                    self.status.configure(text=value)
                    self._append(value)
                    lowered = value.lower()
                    if lowered.startswith("holding") or lowered.startswith("handed"):
                        self.glow.burst(outward=True)
                    elif lowered.startswith("received"):
                        self.glow.burst(outward=False)
                elif kind == "state":
                    self._refresh()
                elif kind == "gesture":
                    self.session.handle_gesture(value.value)
                elif kind == "frame":
                    self._show_frame(value[0], value[1])
        except queue.Empty:
            pass
        self.root.after(50, self._drain)

    def _append(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _refresh(self) -> None:
        self.peer_list.delete(0, "end")
        for peer in self.session.peers:
            mark = "✓" if self.session.trust.is_trusted(peer.id) else "•"
            offering = "  — offering something" if any(
                o.id == peer.id for o in self.session.offers) else ""
            self.peer_list.insert("end", f" {mark} {peer.name}  ({peer.kind}){offering}")
        if not self.session.peers:
            self.peer_list.insert("end", "  nothing found yet")

        waiting = self.session.pending_confirmation
        if waiting:
            page, meeting = waiting
            named = f"{meeting.service} · {meeting.code}" if meeting.code else meeting.service
            self.confirm_label.configure(
                text=f"Move this {named} to another device? It will close here and "
                     f"you'll leave the call on this PC.")
            self.confirm_frame.pack(fill="x", padx=20, pady=(10, 0))
        else:
            self.confirm_frame.pack_forget()

        pending = self.session.pending_trust
        if pending:
            self.trust_label.configure(
                text=f"{pending.name} wants to hand you something and has not before. "
                     f"Allow this device from now on?")
            self.trust_frame.pack(fill="x", padx=20, pady=(10, 0))
        else:
            self.trust_frame.pack_forget()

        self.take_button.configure(state=("normal" if self.session.has_offer else "disabled"))
        self.grab_button.configure(state=("disabled" if self.session.holding else "normal"))
        self.share_button.configure(
            text="⏹  Stop sharing" if self.streamer.is_running else "🖥️  Share this window")

    # -- actions -----------------------------------------------------------

    def _toggle_camera(self) -> None:
        if self.camera_var.get():
            self.tracker.start()
        else:
            self.tracker.stop()

    def _toggle_stream(self) -> None:
        """Mirror the focused window to a peer.

        Used for what cannot be handed over as a link. The process stays here;
        only its picture travels.
        """
        if self.streamer.is_running:
            self.streamer.stop()
            if self._stream_peer:
                self.transport.send("endCast", self._stream_peer)
            self._stream_peer = None
            self._refresh()
            return

        peers = self.session.peers
        if not peers:
            self._event("No peers nearby to share with.")
            return
        self._stream_peer = peers[0]
        if self.streamer.start():
            target = self.streamer.target
            self._event(f"Sharing “{target.title if target else 'window'}” "
                        f"with {self._stream_peer.name}")
            self.glow.burst(outward=True)
        self._refresh()

    def _send_frame(self, frame: bytes) -> None:
        if self._stream_peer:
            self.transport.send_frame(frame, self._stream_peer)

    def _show_frame(self, frame: bytes, peer: Peer) -> None:
        """Display an incoming mirrored window."""
        try:
            from PIL import Image, ImageTk
        except ImportError:
            self._append("Install Pillow to view a shared window.")
            return
        if self._viewer is None or not self._viewer.winfo_exists():
            self._viewer = tk.Toplevel(self.root)
            self._viewer.title(f"QuackCast — {peer.name}")
            self._viewer.configure(bg="black")
            self._viewer_label = tk.Label(self._viewer, bg="black")
            self._viewer_label.pack(fill="both", expand=True)
        image = Image.open(io.BytesIO(frame))
        self._viewer_image = ImageTk.PhotoImage(image)
        self._viewer_label.configure(image=self._viewer_image)

    def _quit(self) -> None:
        self.tracker.stop()
        self.streamer.stop()
        self.transport.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    QuackCastApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
