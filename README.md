<div align="center">

# Samcast Bridge

### Mac ⇄ Windows. The same gestures, across the fence.

![Protocol](https://img.shields.io/badge/protocol-v1-1f6feb?style=flat-square)
![macOS](https://img.shields.io/badge/macOS-13%2B-1f6feb?style=flat-square&logo=apple&logoColor=white)
![Windows](https://img.shields.io/badge/Windows-10%2B-0078d4?style=flat-square&logo=windows&logoColor=white)
![Checks](https://img.shields.io/badge/checks-43%20Swift%20%C2%B7%2048%20Python-2da44e?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-8250df?style=flat-square)

</div>

---

## Why this is a separate project

The main [Samcast](../Samcast) app talks **MultipeerConnectivity**. That is
a closed Apple framework running over Apple Wireless Direct Link, and a Windows
PC cannot speak a word of it — not with a library, not with a shim, not at all.

So Windows support is not a port of the app. It is a **replacement for the one
layer that cannot cross**: the transport. Everything above it is unchanged.

```
     Apple-only build              this bridge
   ┌────────────────────┐      ┌────────────────────┐
   │   product logic    │      │   product logic    │   ← identical
   ├────────────────────┤      ├────────────────────┤
   │ MultipeerConnect.  │      │   UDP + TCP        │   ← the only difference
   └────────────────────┘      └────────────────────┘
     Mac · iPad only            Mac · Windows · anything
```

The Mac side is ~600 lines of Swift implementing the same `PeerTransport`
protocol the app already uses, so it is a drop-in: the app above it cannot tell
which transport it is talking to. The Windows side is a Python app that
implements the same wire format from the other end.

## The gestures are the same

| | Gesture | What happens |
|:--:|---|---|
| ✊ | **Close your hand** | Grabs the page you're in |
| 🖐️ | **Open your hand** *at the other machine* | It lands **there** |

A link genuinely **moves** — the tab closes on the Mac and the real page opens
on the PC, or the other way round. An app window can only be **mirrored**,
because a running process cannot leave its machine.

**A live meeting is asked about first.** A misread fist on an ordinary page
costs a reopened tab; on a Google Meet, Zoom, Teams or Webex call it drops you
out of the meeting. Those get a prompt, nothing is offered or closed until you
answer, and doing nothing means no. The rule is shared with the Mac app
through `Samcast/docs/meeting-vectors.json`, so both ends agree on what
counts as a call — negative cases included, because a prompt people learn to
dismiss unread protects nobody.

## Try it

Both machines must be on the same network. Nothing else — no pairing, no
account, no internet.

**On the Mac** (needs the main Samcast checked out beside this one):

```bash
cd mac && swift run BridgeCLI --gestures
```

**On the PC:**

```bash
cd windows
pip install -r requirements.txt
python -m samcast
```

Or headless on either side, which is how the two are usually tested:

```bash
python peer_cli.py --verbose
```

Type `grab <url>`, `take`, `list`, `drop` at either peer. `--as NAME` gives a
throwaway identity so **two peers can run on one machine** — worth knowing,
because it means you can watch the whole exchange without a second computer.

## Layout

```
docs/PROTOCOL.md          the wire format — the only shared contract
docs/gesture-vectors.json shared test fixtures, read by BOTH test suites

mac/Sources/QuackBridge/  WireFormat · Sockets · LANTransport
mac/Sources/BridgeCLI/    headless Mac peer
mac/Sources/BridgeCheck/  43 protocol conformance checks

windows/samcast/        wire · transport · session · gestures · camera
                          browser · capture · glow · app
windows/peer_cli.py       headless Windows peer
windows/tests/            38 tests
```

Two things are deliberately *not* duplicated. The Mac side reuses
`SamcastCore` from the main project by path dependency rather than copying
it, so the gesture maths and session rules cannot drift. And where a port was
unavoidable — Python has no access to Swift — both classifiers are checked
against **the same fixture file**, so a disagreement about what a fist is
fails a test instead of confusing a user.

## Checks

```bash
cd mac && swift run BridgeCheck                       # 43 checks
cd windows && python -m unittest discover -s tests -t .   # 48 tests
```

Both suites read `docs/gesture-vectors.json`, and the meeting-detection tests
read `Samcast/docs/meeting-vectors.json` from the sibling checkout.

<details>
<summary><b>What is verified, and what is not</b></summary>

<br>

Verified by running it, Mac ↔ Python, on one machine:

- discovery, connection, and a stable link that does not flap
- a link handed **Mac → PC** and **PC → Mac**, end to end
- the 5-second expiry: an offer nobody catches is withdrawn and the page stays
- framing under split and batched reads, and refusal of malformed streams
- both gesture classifiers agreeing on every shared fixture
- both meeting detectors agreeing on all 31 shared fixtures, and a live
  meeting being withheld until confirmed — including the timeout cancelling
  rather than proceeding

**Not yet run against a real Windows PC.** The Windows-only paths — MediaPipe
hand tracking, reading Chrome's URL through UI Automation, `mss` window
capture, and the Tk window itself — are written but have never executed on
Windows, because there isn't one here. Treat the first run on a PC as the real
test. The protocol layer beneath them *is* tested, which is the part that would
have been hardest to debug remotely.

</details>

<details>
<summary><b>⚠️ Security: read this before using it on a network you don't control</b></summary>

<br>

**Traffic is not encrypted.** MultipeerConnectivity encrypted everything for
free; raw sockets do not. On this bridge, anyone who can watch your LAN can
read a URL in transit or forge a discovery beacon and impersonate a trusted
device. **Use it on your own network, not in a café or an airport.** Adding
TLS with a trust-on-first-use pinned certificate is the top open item, and it
fixes both problems at once.

What *is* defended:

- **Only `http` and `https` URLs are ever opened**, checked independently on
  both sides — so a handoff cannot start a program, read a file, or reach a
  network share. `file:`, `javascript:`, `smb:` and custom app schemes are all
  refused, and there are tests for each.
- **Messages are capped at 8 MiB.** Without a cap, one bad length prefix makes
  the receiver try to allocate 4 GiB.
- **A connection that never identifies itself is closed after 10 seconds**, so
  anything on the network that opens a socket cannot accumulate them.
- **Nothing is installed or elevated.** No service, no driver, no registry
  keys, no startup entries, no admin rights. State is two JSON files in your
  own profile directory; deleting the folder removes every trace.
- **The camera feed never leaves the machine.** Frames are classified in
  memory and discarded — not recorded, not written to disk, not transmitted.

</details>

---

<div align="center">
<sub>MIT licensed · part of <a href="../Samcast">Samcast</a></sub>
</div>
