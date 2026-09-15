"""What a QuackCast peer *does* — independent of platform and of UI.

The transport moves bytes; this decides what a fist and an open hand mean.
Keeping it here means the Tk app and the headless CLI behave identically, and
that the rules can be tested without a camera, a network or a screen.

The rules, in one place:

* A fist **offers** the current page. Nothing is closed yet.
* The offer **expires after 5 seconds** if nobody takes it, and the page stays
  exactly where it was. A misread gesture must never lose your work.
* An open hand at another device **asks** for what is being offered.
* Only when that request arrives does the holder send the URL and close its
  own tab — in that order, so a failure cannot lose the page.
* Trust decides *whether* a device may hand you things. The hand decides
  *where* they go. The two are never conflated.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from . import pagerisk
from .identity import TrustStore
from .transport import LANTransport, Peer

#: How long a grabbed page waits for someone to take it, in seconds.
HOLD_TIMEOUT = 5.0

#: How long a "are you sure?" prompt waits before cancelling itself.
#: Doing nothing must mean no: someone who did not mean to make that gesture
#: is unlikely to reach for a button.
CONFIRM_TIMEOUT = 12.0


@dataclass
class Page:
    url: str
    title: str = ""
    source: str = ""


class PlatformHooks:
    """What the session needs from the operating system.

    Injected rather than imported so the session can be tested with fakes, and
    so the Windows-only bits stay out of the logic.
    """

    def frontmost_page(self) -> Optional[Page]:
        """The page in the focused browser, or None if it isn't a browser."""
        return None

    def close_frontmost_tab(self) -> bool:
        return False

    def open_url(self, url: str) -> None:
        raise NotImplementedError


class BridgeSession:
    """One peer's behaviour. Thread-safe; callbacks may fire from any thread."""

    def __init__(
        self,
        transport: LANTransport,
        hooks: PlatformHooks,
        trust: Optional[TrustStore] = None,
        on_event: Optional[Callable[[str], None]] = None,
        on_state: Optional[Callable[[], None]] = None,
        auto_accept: bool = False,
        auto_trust: bool = True,
        auto_confirm: bool = False,
    ) -> None:
        self.transport = transport
        self.hooks = hooks
        self.trust = trust or TrustStore()
        self.on_event = on_event or (lambda text: None)
        self.on_state = on_state or (lambda: None)
        self.auto_accept = auto_accept
        #: When False, the first thing an unknown device offers you needs one
        #: explicit approval — the same "allow this device once" step the Mac
        #: app shows. The headless CLI leaves it True; the app turns it off.
        self.auto_trust = auto_trust
        #: A device waiting for that first approval, if any.
        self.pending_trust: Optional[Peer] = None

        #: When True, a live meeting is handed over without asking. Only the
        #: headless peer sets this, and only when told to with --yes.
        self.auto_confirm = auto_confirm
        #: A page the user is being asked about: (page, MeetingMatch).
        self.pending_confirmation: Optional[tuple] = None
        self._confirm_timer: Optional[threading.Timer] = None

        self._lock = threading.RLock()
        self.peers: List[Peer] = []
        self.holding: Optional[Page] = None
        self.offers: List[Peer] = []
        self._hold_timer: Optional[threading.Timer] = None

        transport.on_peers = self._peers_changed
        transport.on_control = self._control
        transport.on_frame = self._frame

        #: Set by the app to display an incoming window stream.
        self.on_remote_frame: Optional[Callable[[bytes, Peer], None]] = None

    # -- sending -----------------------------------------------------------

    def grab(self) -> None:
        """✊ — offer the focused page to every connected peer."""
        with self._lock:
            if self.holding:
                return
            if not self.peers:
                self.on_event("No peers nearby — nothing to hand to.")
                return

        page = self.hooks.frontmost_page()
        if not page:
            self.on_event("Bring a browser window to the front, then make a fist.")
            return

        # Some pages cost far more than a reopened tab if the gesture was
        # misread. Nothing is offered, closed or timed until this is answered.
        risk = pagerisk.assess(page.url)
        if risk.needs_confirmation and not self.auto_confirm:
            self._ask_before_handing_over(page, risk.meeting)
            return

        self._begin_handoff(page)

    def _begin_handoff(self, page: Page) -> None:
        """Commit: offer the page and start the clock that withdraws it."""
        with self._lock:
            self.holding = page
            peers = list(self.peers)
            self._arm_expiry()

        for peer in peers:
            self.transport.send("sourceAvailable", peer)
        self.on_event(f"Holding “{page.title or page.url}” — open your hand at another device")
        self.on_state()

    def _ask_before_handing_over(self, page: Page, meeting) -> None:
        """A live meeting is on screen. Ask first.

        Handing a meeting over is a reasonable thing to want — it moves the
        call to another device — so this must not block it. It only insists
        the user meant it, because the cost of being wrong is being dropped
        from a call in front of other people.
        """
        with self._lock:
            self.pending_confirmation = (page, meeting)
            if self._confirm_timer:
                self._confirm_timer.cancel()
            self._confirm_timer = threading.Timer(CONFIRM_TIMEOUT, self.decline_grab)
            self._confirm_timer.daemon = True
            self._confirm_timer.start()
        named = f"{meeting.service} · {meeting.code}" if meeting.code else meeting.service
        self.on_event(f"Move this {named} to another device? It will close here.")
        self.on_state()

    def confirm_grab(self) -> None:
        with self._lock:
            waiting = self.pending_confirmation
            self.pending_confirmation = None
            if self._confirm_timer:
                self._confirm_timer.cancel()
                self._confirm_timer = None
        if not waiting:
            return
        self._begin_handoff(waiting[0])

    def decline_grab(self) -> None:
        """Nothing was closed, so there is nothing to undo."""
        with self._lock:
            waiting = self.pending_confirmation
            self.pending_confirmation = None
            if self._confirm_timer:
                self._confirm_timer.cancel()
                self._confirm_timer = None
        if waiting:
            self.on_event(f"Left your {waiting[1].service} alone.")
        self.on_state()

    @property
    def is_asking(self) -> bool:
        with self._lock:
            return self.pending_confirmation is not None

    def _arm_expiry(self) -> None:
        if self._hold_timer:
            self._hold_timer.cancel()
        self._hold_timer = threading.Timer(HOLD_TIMEOUT, self._expire_hold)
        self._hold_timer.daemon = True
        self._hold_timer.start()

    def _expire_hold(self) -> None:
        with self._lock:
            if not self.holding:
                return
            self.holding = None
            peers = list(self.peers)
        for peer in peers:
            self.transport.send("sourceWithdrawn", peer)
        self.on_event("Nobody took it — the page stays here.")
        self.on_state()

    def release(self) -> None:
        with self._lock:
            if not self.holding:
                return
            self.holding = None
            if self._hold_timer:
                self._hold_timer.cancel()
            peers = list(self.peers)
        for peer in peers:
            self.transport.send("sourceWithdrawn", peer)
        self.on_state()

    # -- receiving ---------------------------------------------------------

    def take(self) -> None:
        """🖐️ — ask whoever is offering to hand it over."""
        with self._lock:
            source = self.offers[-1] if self.offers else None
        if not source:
            return

        # Trust governs *whether* a device may hand you things. It is asked
        # once, on the first thing an unknown device offers, and never again.
        # It does not decide where anything goes — the open hand does that.
        if not self.auto_trust and not self.trust.is_trusted(source.id):
            with self._lock:
                self.pending_trust = source
            self.on_event(f"{source.name} has not handed you anything before — allow it?")
            self.on_state()
            return

        self.on_event(f"Asking {source.name} for it…")
        self.transport.send("requestCast", source)

    def approve_pending(self) -> None:
        """The user allowed the device waiting for first approval."""
        with self._lock:
            peer = self.pending_trust
            self.pending_trust = None
        if not peer:
            return
        self.trust.trust(peer.id, peer.name)
        self.on_event(f"{peer.name} is trusted from now on.")
        self.take()

    def reject_pending(self) -> None:
        with self._lock:
            peer = self.pending_trust
            self.pending_trust = None
            if peer:
                self.offers = [p for p in self.offers if p.id != peer.id]
        self.on_state()

    @property
    def has_offer(self) -> bool:
        with self._lock:
            return bool(self.offers)

    # -- transport callbacks ----------------------------------------------

    def _peers_changed(self, peers: List[Peer]) -> None:
        with self._lock:
            known = {p.id for p in peers}
            self.peers = peers
            self.offers = [p for p in self.offers if p.id in known]
            if self.pending_trust and self.pending_trust.id not in known:
                self.pending_trust = None
        self.on_state()

    def _control(self, control: str, payload: Optional[str], peer: Peer) -> None:
        if control == "sourceAvailable":
            with self._lock:
                self.offers = [p for p in self.offers if p.id != peer.id] + [peer]
            self.on_event(f"{peer.name} has something for you — open your hand")
            self.on_state()
            if self.auto_accept:
                self.take()

        elif control == "sourceWithdrawn":
            with self._lock:
                self.offers = [p for p in self.offers if p.id != peer.id]
            self.on_state()

        elif control == "requestCast":
            with self._lock:
                page = self.holding
                self.holding = None
                if self._hold_timer:
                    self._hold_timer.cancel()
            if not page:
                return
            # Send first, close second: if the send fails the page is still
            # on screen, which is the failure everyone would rather have.
            self.transport.send("handoff", peer, page.url)
            self.hooks.close_frontmost_tab()
            self.on_event(f"Handed “{page.title or page.url}” to {peer.name}")
            self.on_state()

        elif control == "handoff":
            # The transport already rejected anything that was not an http(s)
            # URL; this is the second, independent check.
            from . import wire
            safe = wire.safe_handoff_url(payload or "")
            if not safe:
                self.on_event(f"Refused something from {peer.name} that was not a web address.")
                return
            if self.auto_trust and not self.trust.is_trusted(peer.id):
                self.trust.trust(peer.id, peer.name)
                self.on_event(f"Trusting {peer.name} from now on.")
            with self._lock:
                self.offers = [p for p in self.offers if p.id != peer.id]
            self.on_event(f"Received from {peer.name}")
            self.hooks.open_url(safe)
            self.on_state()

    def _frame(self, frame: bytes, peer: Peer) -> None:
        if self.on_remote_frame:
            self.on_remote_frame(frame, peer)

    # -- gestures ----------------------------------------------------------

    def handle_gesture(self, gesture: str) -> None:
        """Called with a *stable* gesture name from the debouncer."""
        # A question is already on screen. Answer it with the buttons —
        # waving again must not stack a second prompt behind the first.
        if self.is_asking:
            return
        if gesture == "closedHand":
            self.grab()
        elif gesture == "openHand" and self.has_offer:
            self.take()
