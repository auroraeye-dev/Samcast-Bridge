<div align="center">

# Samcast Bridge

### Mac ⇄ Windows. The same gestures, across the fence.

![Protocol](https://img.shields.io/badge/protocol-v1-1f6feb?style=flat-square)
![macOS](https://img.shields.io/badge/macOS-13%2B-1f6feb?style=flat-square&logo=apple&logoColor=white)
![Windows](https://img.shields.io/badge/Windows-10%2B-0078d4?style=flat-square&logo=windows&logoColor=white)
![Checks](https://img.shields.io/badge/checks-43%20Swift%20%C2%B7%2048%20Python-2da44e?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-8250df?style=flat-square)

</div>

> ### ⚠️ Read this before anything else
>
> **The Windows code has never been run on Windows.** It compiles and its
> logic is tested, but MediaPipe hand tracking, reading Chrome's URL, window
> capture and the app window itself have only ever been *checked*, never
> *executed* on that operating system. Expect the first run to find problems.
>
> **This does not talk to the [Samcast](https://github.com/auroraeye-dev/Samcast)
> app.** That app speaks MultipeerConnectivity, which is Apple-only. Only the
> headless `BridgeCLI` in this repo speaks the cross-platform protocol — so
> Mac ⇄ Windows today means a terminal on the Mac, not the app with the glow.
>
> **Traffic is not encrypted.** Unlike the Apple transport. Use it on a
> network you control.

---

## Why this exists separately

The main app talks **MultipeerConnectivity** — a closed Apple framework over
Apple Wireless Direct Link. A Windows PC cannot speak a word of it: not with
a library, not with a shim, not at all.

So Windows support is not a port of the app. It is a **replacement for the
one layer that cannot cross**:

```
     Apple-only build              this bridge
   ┌────────────────────┐      ┌────────────────────┐
   │   product logic    │      │   product logic    │   ← identical
   ├────────────────────┤      ├────────────────────┤
   │ MultipeerConnect.  │      │   UDP + TCP        │   ← the only difference
   └────────────────────┘      └────────────────────┘
     Mac · iPad only            Mac · Windows · anything
```

The Mac side implements the same `PeerTransport` protocol the app already
uses, so it is a drop-in. The Windows side implements the same wire format
from the other end.

---

## Install and run

Both machines must be on the **same Wi-Fi network**, on the **same subnet**.

### Step 1 — prove the network works. No dependencies needed.

The headless peer is **pure Python standard library**. Do this before
installing anything, so a dependency problem can't be mistaken for a network
problem.

**On Windows**, install **Python 3.11 or 3.12** from
[python.org](https://www.python.org/downloads/) — tick *"Add python.exe to
PATH"*. Then:

```
git clone https://github.com/auroraeye-dev/Samcast-Bridge.git
cd Samcast-Bridge\windows
python peer_cli.py --as win-test --verbose
```

> **🔥 Windows Firewall will prompt on this first run.** Tick **Private
> networks** and Allow. If you miss it, **discovery fails silently** — no
> error, nothing connects, and nothing below will work. This is by far the
> most likely thing to go wrong.

**On the Mac**, you need the [Samcast](https://github.com/auroraeye-dev/Samcast)
repo checked out **beside this one** (see [Layout](#layout)):

```bash
cd Samcast-Bridge/mac
swift run BridgeCLI --as mac-test --verbose
```

Within a few seconds **both** should print:

```
bridge: discovered mac-test (mac)
bridge: connected mac-test (mac)
```

**That is the checkpoint that matters.** If it connects, the protocol works
on real Windows and everything after is features.

### Step 2 — hand a link across

At either peer, type:

```
grab https://en.wikipedia.org/wiki/Duck
```

At the other, type `take`. It opens in that machine's browser. Then reverse
it.

Commands: `grab [url]` · `take` · `yes` / `no` · `list` · `drop` · `quit`

### Step 3 — the Windows app

```
pip install -r requirements.txt
python -m samcast
```

Now you get a window, a peer list, the glow, and camera gestures.

If `pip` fails on mediapipe, **carry on anyway** — every Windows dependency
is imported lazily, so everything except hand gestures still works and the
app tells you what is missing instead of crashing.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Nothing connects, no errors | **Windows Firewall blocked it** | Allow Python on **Private networks**. Check Settings ▸ Network & Internet ▸ Firewall ▸ Allow an app |
| Still nothing | Different subnets | `ipconfig` on the PC — the IPv4 address must match the Mac's first three octets. Broadcast does not cross subnets |
| `pip install` fails on mediapipe | Python 3.13 or 3.14 | MediaPipe has no wheels for those yet. Use **3.11 or 3.12** |
| `ModuleNotFoundError: _tkinter` | Python without Tk | Reinstall from python.org — Tk ships with it. Or use `peer_cli.py`, which needs no GUI |
| Camera checkbox does nothing | mediapipe / opencv missing | `pip install -r requirements.txt`. The app reports which module it wants |
| Grab does nothing in Firefox | Firefox exposes its accessibility tree only on request | Use Chrome or Edge, or enable accessibility in Firefox |
| Grab does nothing in any browser | `uiautomation` / `pywin32` missing | `pip install -r requirements.txt` |
| The Mac app can't see the PC | The app speaks MultipeerConnectivity | Expected. Use `BridgeCLI`, not the app |
| `error: no such module 'SamcastCore'` | The sibling checkout is missing | Clone [Samcast](https://github.com/auroraeye-dev/Samcast) next to this repo |
| `test_pagerisk` fails | Same reason — it reads shared fixtures from that repo | Clone it, or skip that one test |
| Peers appear then vanish repeatedly | Two peers sharing an identity | Give each one `--as <name>` |

### Testing two peers on one machine

`--as NAME` gives a throwaway identity, so you can watch the whole exchange
without a second computer. `--dry-run` logs incoming links instead of opening
them, so tests don't fill your screen with browser tabs.

```bash
python peer_cli.py --as alpha --verbose
python peer_cli.py --as beta --auto-accept --dry-run
```

---

## Layout

The Mac side reuses `SamcastCore` from the main project **by path
dependency**, so the gesture maths and session rules cannot drift. That means
the two repos must sit **side by side**:

```
somewhere/
├── Samcast/            ← github.com/auroraeye-dev/Samcast
└── Samcast-Bridge/     ← this repo
```

Moving or renaming either folder breaks the Mac build.

```
docs/PROTOCOL.md           the wire format — the only shared contract
docs/gesture-vectors.json  shared fixtures, read by BOTH test suites

mac/Sources/QuackBridge/   WireFormat · Sockets · LANTransport
mac/Sources/BridgeCLI/     headless Mac peer
mac/Sources/BridgeCheck/   43 protocol conformance checks

windows/samcast/           wire · transport · session · gestures · camera
                           browser · capture · glow · app
windows/peer_cli.py        headless Windows peer
windows/tests/             48 tests
```

---

## Checks

```bash
cd mac     && swift run BridgeCheck                      # 43 checks
cd windows && python -m unittest discover -s tests -t .  # 48 tests
```

Where a port was unavoidable — Python cannot call Swift — both
implementations are checked against **the same fixture files**, so a
disagreement about what a fist is, or what counts as a live meeting, fails a
test instead of confusing a user.

<details>
<summary><b>What is verified, and what is not</b></summary>

<br>

**Verified by running it**, Mac ↔ Python on one machine:

- discovery, connection, and a stable link that does not flap
- a link handed **Mac → PC** and **PC → Mac**, end to end
- the 5-second expiry: an offer nobody catches is withdrawn, page stays put
- framing under split and batched reads, and refusal of malformed streams
- both gesture classifiers agreeing on every shared fixture
- both meeting detectors agreeing on all 31 shared fixtures

**Never run on Windows**: MediaPipe hand tracking, reading Chrome's URL via
UI Automation, `mss` window capture, and the Tk window. The protocol layer
beneath them is tested, which is the part that would have been hardest to
debug remotely.

</details>

---

## Security

**Traffic is not encrypted.** MultipeerConnectivity encrypted everything for
free; raw sockets do not. On this bridge, anyone who can watch your LAN can
read a URL in transit or forge a discovery beacon and impersonate a trusted
device. **Use it on your own network.** TLS with a trust-on-first-use pinned
certificate is the top open item and fixes both problems at once.

What *is* defended:

- **Only `http` and `https` URLs are ever opened**, checked independently on
  both sides — so a handoff cannot start a program, read a file, or reach a
  network share. `file:`, `javascript:`, `smb:`, UNC paths and custom app
  schemes are all refused, with tests for each.
- **Messages are capped at 8 MiB.** Without a cap, one bad length prefix
  makes the receiver try to allocate 4 GiB.
- **A connection that never identifies itself is closed after 10 seconds**,
  so anything on the network that opens a socket cannot accumulate them.
- **Nothing is installed or elevated.** No service, driver, registry key or
  startup entry, and no admin rights. State is two JSON files in your own
  profile directory; deleting the folder removes every trace.
- **The camera feed never leaves the machine.** Frames are classified in
  memory and discarded — not recorded, not written to disk, not transmitted.

---

## License

MIT. See [LICENSE](LICENSE). Part of
**[Samcast](https://github.com/auroraeye-dev/Samcast)**.
