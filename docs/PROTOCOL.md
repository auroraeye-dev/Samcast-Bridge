# Samcast Bridge wire protocol — v1

The Apple-only build talks MultipeerConnectivity, which Windows cannot speak at
all: it is a closed Apple framework over Apple Wireless Direct Link. Reaching
Windows therefore means replacing the whole transport with something both sides
can implement from scratch. This document is that contract — it is the only
thing the macOS and Windows apps share, and either side must be reimplementable
from this page alone.

Everything below is plain UDP and TCP over the local network. No broker, no
cloud, no pairing server, no internet access.

---

## 1. Discovery — UDP broadcast

Every peer sends a **beacon** every `2.0 s` to UDP port **50505**, addressed to
the IPv4 broadcast address of each active interface (and `255.255.255.255` as a
fallback). Every peer also *listens* on 50505.

Beacon payload is a single UTF-8 JSON object, no framing:

```json
{
  "qc": 1,
  "id": "9C8E1B2A-...",
  "name": "swift-heron-3172",
  "kind": "mac",
  "port": 51234
}
```

| Field  | Meaning |
|---|---|
| `qc`   | Protocol version. A peer **must ignore** a beacon whose `qc` it does not implement. |
| `id`   | Stable per-install UUID. This — never the name, never the IP — is what trust is keyed on. |
| `name` | Human-readable, e.g. `swift-heron-3172`. Display only. |
| `kind` | `mac` · `windowsPC` · `iPad` · `iPhone` · `unknown`. Display only. |
| `port` | The TCP port this peer is accepting control connections on. |

A peer is considered **lost** if no beacon arrives from its `id` for `8.0 s`
(four missed beacons). Beacons from our own `id` are ignored — broadcast comes
back to the sender on most stacks.

> **Why broadcast and not mDNS/Bonjour?** Bonjour is not present on a stock
> Windows install (it ships with iTunes, which not everyone has), and depending
> on a service the user may not have installed is a worse failure than four
> lines of UDP. The cost is that discovery does not cross subnets — acceptable,
> since the product premise is devices in the same room.

## 2. Connection — TCP, one per pair

Each peer listens on an ephemeral TCP port and advertises it in its beacon.

When peer **A** discovers peer **B**, exactly one of them dials, chosen by
comparing ids as strings:

```
A dials B  ⟺  A.id < B.id
```

Both sides apply the same rule, so a pair never ends up with two overlapping
connections. (In the Multipeer build, *not* having this rule was the cause of
connections that survived only a few seconds — both ends invited, and each new
session tore down the last.)

On connect, the dialer immediately sends a `hello` control message. The
listener replies with its own `hello`. Until a `hello` is received, the
connection has no identity attached and **no other message on it may be
acted upon**.

A dropped connection is retried with backoff `1, 2, 4, 8 s`, capped at `8 s`,
for as long as the peer's beacons are still arriving.

## 3. Framing

Every TCP message is length-prefixed so the stream can carry both small JSON
and multi-hundred-kilobyte images:

```
┌────────────┬────────┬───────────────────┐
│  length    │  type  │      payload      │
│  4 bytes   │ 1 byte │   length bytes    │
│ big-endian │        │                   │
└────────────┴────────┴───────────────────┘
```

`length` counts the payload only, not itself and not `type`.

| `type` | Meaning | Payload |
|---|---|---|
| `0x01` | Control | UTF-8 JSON, see §4 |
| `0x02` | Screen frame | JPEG bytes |

A payload longer than **8 MiB** is rejected and the connection closed. This is
the single most important line in any socket program: without a cap, one
malformed or hostile length prefix makes the receiver try to allocate up to 4
GiB. A JPEG of a downscaled window is ~100–300 KiB, so 8 MiB is generous.

## 4. Control messages

JSON, matching the envelope the Apple build already uses so both transports
carry an identical control plane:

```json
{ "control": "handoff", "payload": "https://example.com/article" }
```

`payload` is optional and is `null` for messages that carry nothing.

| `control` | Direction | Meaning |
|---|---|---|
| `hello` | both, on connect | `payload` is `"<id>\|<name>\|<kind>"`. Identifies the connection. |
| `sourceAvailable` | sender → peers | "I have grabbed something and am holding it for you." |
| `sourceWithdrawn` | sender → peers | The hold expired or was cancelled; nothing is waiting any more. |
| `requestCast` | receiver → sender | "Open hand seen here — give it to me." |
| `handoff` | sender → receiver | `payload` is the URL. The receiver opens it natively. |
| `endCast` | either | Stop the live stream. |

### The handoff exchange

```
   holder (✊)                              receiver (🖐️)
       │                                          │
       │────────── sourceAvailable ──────────────▶│   "something is waiting"
       │                                          │
       │◀───────── requestCast ───────────────────│   open hand seen
       │                                          │
       │────────── handoff "https://…" ──────────▶│   the URL itself
       │                                          │
       │   close the tab here      the page opens here
```

The holder closes its own tab **only after** `requestCast` arrives — so a
handoff that nobody catches leaves the page exactly where it was.

If no `requestCast` arrives within **5.0 s**, the holder sends
`sourceWithdrawn` and restores its own state. This is the same nullification
window the Apple build uses, and it is what stops a page from vanishing into
nothing when a gesture is misread.

## 5. Screen frames

`type 0x02` carries a JPEG of the sender's focused window, sent only while a
cast is active. Frames are **advisory**: a receiver that is behind may drop
them freely, and a sender must not wait for acknowledgement.

Target ≤ 1100 px wide, ≤ 12 fps, JPEG quality ~70 — the same budget as the
Apple build, chosen for a wireless link rather than for fidelity.

Note that a window can only ever be *mirrored*. A running process cannot leave
its machine, so the app keeps running on the sender and only its picture
travels. Links are the opposite: they genuinely move, and are always preferred
when the focused window is a browser.

## 6. Trust

Trust is keyed on the beacon `id` and answers exactly one question: **may this
device hand me things at all?** It never decides *where* something goes — that
is always, only, the open hand in front of a camera.

- First time an unknown `id` offers you something, the user is asked once.
- Accepted ids are persisted and never asked about again.
- Trust is per-direction and per-device; it is not transitive.

## 7. Security properties, stated plainly

What this protocol does **not** do, so nobody assumes otherwise:

- **Traffic is not encrypted.** MultipeerConnectivity gave us encryption for
  free; raw sockets do not. Anyone able to sniff your LAN can read a URL in
  transit, or forge a beacon. Do not use the bridge on a network you do not
  control — a café or an airport — until this is fixed. Adding TLS with a
  trust-on-first-use pinned certificate is the top open item.
- **`id` is self-asserted.** A peer that claims another peer's `id` inherits
  its trust. TLS pinning fixes this too; they are the same fix.
- **Nothing is executed.** The only payload that crosses is a URL string and
  JPEG bytes. The receiver opens URLs with the OS default handler and refuses
  any scheme other than `http` and `https`, so a handoff cannot launch a
  program, write a file, or run a command on the receiving machine.

The third point is the one that matters most and it is enforced on both sides
independently, not just by the sender.
