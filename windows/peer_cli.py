#!/usr/bin/env python3
"""Headless QuackCast peer — the Windows counterpart to `swift run BridgeCLI`.

Useful in its own right (a PC with no camera can still receive), and it is how
the Mac ↔ Windows path is tested without a GUI on either end.

    python peer_cli.py                 typed commands
    python peer_cli.py --auto-accept   take whatever is offered
    python peer_cli.py --dry-run       log incoming links, don't open them
    python peer_cli.py --as NAME       throwaway identity, for testing two
                                       peers on one machine

Commands: grab · take · list · drop · quit
"""

from __future__ import annotations

import argparse
import sys
import threading
import uuid
import webbrowser

from quackcast.identity import DeviceIdentity, TrustStore
from quackcast.session import BridgeSession, Page, PlatformHooks
from quackcast.transport import LANTransport


class CLIHooks(PlatformHooks):
    """Real link opening; page grabbing comes from whatever the platform
    module can manage, falling back to a URL typed after `grab`."""

    def __init__(self, dry_run: bool = False) -> None:
        self.staged: Page | None = None
        self.dry_run = dry_run
        try:
            from quackcast import browser
            self._browser = browser
        except Exception:
            self._browser = None

    def frontmost_page(self) -> Page | None:
        if self.staged:
            page, self.staged = self.staged, None
            return page
        if self._browser:
            return self._browser.frontmost_page()
        return None

    def close_frontmost_tab(self) -> bool:
        return self._browser.close_frontmost_tab() if self._browser else False

    def open_url(self, url: str) -> None:
        # The interop tests must not take over the screen to prove a point.
        if self.dry_run:
            print(f"(dry run — would open {url})", flush=True)
            return
        webbrowser.open(url)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--auto-accept", action="store_true")
    parser.add_argument("--as", dest="name", default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="log incoming links instead of opening them")
    parser.add_argument("--yes", action="store_true",
                        help="hand over live meetings without asking")
    parser.add_argument("--kind", default="windowsPC")
    args = parser.parse_args()

    identity = (
        DeviceIdentity(str(uuid.uuid4()), args.name) if args.name
        else DeviceIdentity.load_or_create()
    )

    def out(text: str) -> None:
        print(text, flush=True)

    transport = LANTransport(
        identity=identity,
        kind=args.kind,
        log=(lambda m: out("    " + m)) if args.verbose else (lambda m: None),
    )
    hooks = CLIHooks(dry_run=args.dry_run)
    session = BridgeSession(transport, hooks, TrustStore(),
                            on_event=out, auto_accept=args.auto_accept,
                            auto_confirm=args.yes)
    transport.start()

    out(f"QuackCast bridge — this PC is “{identity.name}”")
    out("commands: grab [url] · take · yes · no · list · drop · quit")

    for line in sys.stdin:
        parts = line.strip().split(maxsplit=1)
        if not parts:
            continue
        command, rest = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        if command in ("grab", "g"):
            if rest:
                hooks.staged = Page(url=rest, title=rest, source="typed")
            session.grab()
        elif command in ("take", "t"):
            session.take()
        elif command in ("list", "l"):
            peers = session.peers
            if not peers:
                out("no peers — is the other machine running QuackCast on this network?")
            for peer in peers:
                mark = "trusted" if session.trust.is_trusted(peer.id) else "new"
                offering = ", offering something" if any(o.id == peer.id for o in session.offers) else ""
                out(f"  • {peer.name}  [{peer.kind}, {mark}{offering}]")
        elif command in ("yes", "y"):
            session.confirm_grab()
        elif command in ("no", "n"):
            session.decline_grab()
        elif command in ("drop", "d"):
            session.release()
        elif command in ("quit", "q", "exit"):
            break
        else:
            out("commands: grab [url] · take · list · drop · quit")

    # stdin closed: keep serving, so the peer can be left running detached.
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
