"""The rules a Samcast peer follows, tested without a camera or a network.

These are the behaviours that matter most, because getting them wrong loses
somebody's work rather than merely failing:

* a grabbed page is not closed until somebody actually takes it
* an offer nobody catches expires and the page stays put
* a device gets exactly one trust prompt, and never decides *where* anything
  goes
* a payload that is not a web address is never opened
"""

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from samcast import session as session_module
from samcast.identity import TrustStore
from samcast.session import BridgeSession, Page, PlatformHooks
from samcast.transport import Peer

MAC = Peer(id="mac-id", name="swift-heron-1111", kind="mac")
PC = Peer(id="pc-id", name="teal-lynx-2222", kind="windowsPC")


class FakeTransport:
    """Records what would have gone out, and lets tests inject what comes in."""

    def __init__(self):
        self.sent = []
        self.frames = []
        self.on_peers = lambda peers: None
        self.on_control = lambda control, payload, peer: None
        self.on_frame = lambda frame, peer: None

    def send(self, control, peer, payload=None):
        self.sent.append((control, peer.id, payload))

    def send_frame(self, frame, peer):
        self.frames.append((frame, peer.id))

    # helpers for the tests
    def controls(self):
        return [c for c, _, _ in self.sent]

    def arrive(self, control, peer, payload=None):
        self.on_control(control, payload, peer)

    def connect(self, *peers):
        self.on_peers(list(peers))


class FakeHooks(PlatformHooks):
    def __init__(self, page=None):
        self.page = page
        self.closed = 0
        self.opened = []

    def frontmost_page(self):
        return self.page

    def close_frontmost_tab(self):
        self.closed += 1
        return True

    def open_url(self, url):
        self.opened.append(url)


def make(auto_trust=True, page=None, trust=None):
    transport = FakeTransport()
    hooks = FakeHooks(page)
    session = BridgeSession(transport, hooks, trust or _scratch_trust(),
                            auto_trust=auto_trust)
    return session, transport, hooks


_temp_dirs = []


def _scratch_trust():
    """A TrustStore in a throwaway directory — the tests must never touch the
    real one in the user's profile."""
    directory = TemporaryDirectory()
    _temp_dirs.append(directory)
    return TrustStore(Path(directory.name) / "trusted.json")


class Grabbing(unittest.TestCase):
    def test_nothing_happens_without_peers(self):
        session, transport, hooks = make(page=Page("https://example.com", "Example"))
        session.grab()
        self.assertIsNone(session.holding)
        self.assertEqual(transport.sent, [])

    def test_nothing_happens_when_the_front_window_is_not_a_browser(self):
        session, transport, _ = make(page=None)
        transport.connect(MAC)
        session.grab()
        self.assertIsNone(session.holding)
        self.assertNotIn("sourceAvailable", transport.controls())

    def test_grabbing_offers_but_does_not_close_anything(self):
        session, transport, hooks = make(page=Page("https://example.com", "Example"))
        transport.connect(MAC)
        session.grab()
        self.assertIsNotNone(session.holding)
        self.assertIn("sourceAvailable", transport.controls())
        self.assertEqual(hooks.closed, 0,
                         "the tab must survive until somebody actually takes it")

    def test_the_tab_closes_only_once_the_page_has_been_sent(self):
        session, transport, hooks = make(page=Page("https://example.com", "Example"))
        transport.connect(MAC)
        session.grab()
        transport.arrive("requestCast", MAC)

        handoffs = [(c, p) for c, _, p in transport.sent if c == "handoff"]
        self.assertEqual(handoffs, [("handoff", "https://example.com")])
        self.assertEqual(hooks.closed, 1)
        self.assertIsNone(session.holding, "the hold is released after handing over")

    def test_a_request_with_nothing_held_sends_nothing(self):
        session, transport, hooks = make()
        transport.connect(MAC)
        transport.arrive("requestCast", MAC)
        self.assertNotIn("handoff", transport.controls())
        self.assertEqual(hooks.closed, 0)


class Expiry(unittest.TestCase):
    def test_an_offer_nobody_takes_is_withdrawn_and_the_page_stays(self):
        original = session_module.HOLD_TIMEOUT
        session_module.HOLD_TIMEOUT = 0.15
        try:
            session, transport, hooks = make(page=Page("https://example.com", "Example"))
            transport.connect(MAC)
            session.grab()
            time.sleep(0.4)
            self.assertIsNone(session.holding)
            self.assertIn("sourceWithdrawn", transport.controls())
            self.assertEqual(hooks.closed, 0, "a misread gesture must not lose the page")
        finally:
            session_module.HOLD_TIMEOUT = original


class Trust(unittest.TestCase):
    def test_an_unknown_device_is_asked_about_once(self):
        session, transport, _ = make(auto_trust=False)
        transport.connect(MAC)
        transport.arrive("sourceAvailable", MAC)

        session.take()
        self.assertEqual(session.pending_trust, MAC)
        self.assertNotIn("requestCast", transport.controls(),
                         "nothing is requested until the device is allowed")

        session.approve_pending()
        self.assertIn("requestCast", transport.controls())
        self.assertTrue(session.trust.is_trusted(MAC.id))

        # Second time round there is no prompt.
        session.pending_trust = None
        transport.sent.clear()
        transport.arrive("sourceAvailable", MAC)
        session.take()
        self.assertIsNone(session.pending_trust)
        self.assertIn("requestCast", transport.controls())

    def test_rejecting_clears_the_offer(self):
        session, transport, _ = make(auto_trust=False)
        transport.connect(MAC)
        transport.arrive("sourceAvailable", MAC)
        session.take()
        session.reject_pending()
        self.assertIsNone(session.pending_trust)
        self.assertFalse(session.has_offer)
        self.assertFalse(session.trust.is_trusted(MAC.id))

    def test_trust_does_not_decide_where_things_go(self):
        """A trusted device that offers something still gets nothing until
        the user's hand asks for it."""
        trust = _scratch_trust()
        trust.trust(MAC.id, MAC.name)
        session, transport, _ = make(auto_trust=False, trust=trust)
        transport.connect(MAC)
        transport.arrive("sourceAvailable", MAC)
        self.assertNotIn("requestCast", transport.controls())


class IncomingPayloads(unittest.TestCase):
    def test_a_web_url_is_opened(self):
        session, transport, hooks = make()
        transport.connect(MAC)
        transport.arrive("handoff", MAC, "https://example.com/article")
        self.assertEqual(hooks.opened, ["https://example.com/article"])

    def test_anything_that_is_not_a_web_url_is_refused(self):
        for hostile in ["file:///C:/Windows/System32/config/SAM",
                        "javascript:alert(1)",
                        "someapp://run",
                        "C:\\Windows\\System32\\cmd.exe",
                        ""]:
            with self.subTest(hostile=hostile):
                session, transport, hooks = make()
                transport.connect(MAC)
                transport.arrive("handoff", MAC, hostile)
                self.assertEqual(hooks.opened, [],
                                 "the receiver must not hand this to the OS")


class MeetingConfirmation(unittest.TestCase):
    """A misread fist on an ordinary page costs a reopened tab. On a live
    call it drops you out of the meeting, so that one gets asked about."""

    MEET = Page("https://meet.google.com/abc-defg-hij", "Standup")
    ORDINARY = Page("https://example.com/article", "Article")

    def test_an_ordinary_page_is_never_questioned(self):
        session, transport, _ = make(page=self.ORDINARY)
        transport.connect(MAC)
        session.grab()
        self.assertFalse(session.is_asking)
        self.assertIsNotNone(session.holding)
        self.assertIn("sourceAvailable", transport.controls())

    def test_a_meeting_is_not_offered_until_confirmed(self):
        session, transport, hooks = make(page=self.MEET)
        transport.connect(MAC)
        session.grab()

        self.assertTrue(session.is_asking)
        self.assertIsNone(session.holding, "nothing is held yet")
        self.assertEqual(transport.sent, [], "and nothing is announced")
        self.assertEqual(hooks.closed, 0, "and above all, nothing is closed")

    def test_confirming_proceeds_normally(self):
        session, transport, _ = make(page=self.MEET)
        transport.connect(MAC)
        session.grab()
        session.confirm_grab()

        self.assertFalse(session.is_asking)
        self.assertIsNotNone(session.holding)
        self.assertIn("sourceAvailable", transport.controls())

    def test_declining_leaves_everything_alone(self):
        session, transport, hooks = make(page=self.MEET)
        transport.connect(MAC)
        session.grab()
        session.decline_grab()

        self.assertFalse(session.is_asking)
        self.assertIsNone(session.holding)
        self.assertEqual(transport.sent, [])
        self.assertEqual(hooks.closed, 0)

    def test_silence_means_no(self):
        """Someone who did not mean to make that gesture will not reach for
        a button, so the timeout must cancel rather than proceed."""
        original = session_module.CONFIRM_TIMEOUT
        session_module.CONFIRM_TIMEOUT = 0.15
        try:
            session, transport, hooks = make(page=self.MEET)
            transport.connect(MAC)
            session.grab()
            time.sleep(0.4)
            self.assertFalse(session.is_asking)
            self.assertIsNone(session.holding)
            self.assertEqual(hooks.closed, 0)
            self.assertEqual(transport.sent, [])
        finally:
            session_module.CONFIRM_TIMEOUT = original

    def test_waving_again_does_not_stack_a_second_prompt(self):
        session, transport, _ = make(page=self.MEET)
        transport.connect(MAC)
        session.handle_gesture("closedHand")
        first = session.pending_confirmation
        session.handle_gesture("closedHand")
        self.assertIs(session.pending_confirmation, first)

    def test_auto_confirm_skips_the_question(self):
        """The headless peer can be told to stop asking, for scripted use."""
        transport = FakeTransport()
        hooks = FakeHooks(self.MEET)
        session = BridgeSession(transport, hooks, _scratch_trust(), auto_confirm=True)
        transport.connect(MAC)
        session.grab()
        self.assertFalse(session.is_asking)
        self.assertIsNotNone(session.holding)


class Gestures(unittest.TestCase):
    def test_a_fist_grabs_and_an_open_hand_takes(self):
        session, transport, hooks = make(page=Page("https://example.com", "Example"))
        transport.connect(MAC)

        session.handle_gesture("closedHand")
        self.assertIsNotNone(session.holding)

        session.release()
        transport.arrive("sourceAvailable", MAC)
        session.handle_gesture("openHand")
        self.assertIn("requestCast", transport.controls())

    def test_an_open_hand_with_nothing_offered_does_nothing(self):
        session, transport, _ = make()
        transport.connect(MAC)
        session.handle_gesture("openHand")
        self.assertEqual(transport.controls(), [])


if __name__ == "__main__":
    unittest.main()
