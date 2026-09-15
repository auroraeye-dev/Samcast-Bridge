"""The cross-platform transport: UDP discovery plus one TCP connection per peer.

Sockets only, so that any language can implement the other end.
Anything Apple-specific would defeat the entire purpose of this module.
Two peers find each other by broadcast, then exactly one of them dials.
Very little state is kept: who was heard from, and who is connected.
In-flight dials are tracked, or the tick opens a second connection.
Keeping the wire format in wire.py leaves this file about sockets alone.


Counterpart to `mac/Sources/QuackBridge/LANTransport.swift`. Both implement
docs/PROTOCOL.md; neither imports anything the other platform could not.

Everything is confined to the local network. No broker, no cloud service, no
outbound internet connection, and nothing is written to disk from here.
"""

from __future__ import annotations

import errno
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import wire
from .identity import DeviceIdentity


@dataclass(frozen=True)
class Peer:
    id: str
    name: str
    kind: str = "unknown"


@dataclass
class _Discovered:
    beacon: wire.Beacon
    address: str
    last_seen: float
    next_dial: float = 0.0
    failures: int = 0


class _Connection:
    """One TCP connection: a read thread, and writes serialised by a lock."""

    def __init__(self, sock: socket.socket, transport: "LANTransport") -> None:
        self.sock = sock
        self.peer: Optional[Peer] = None
        self.reader = wire.Reader()
        self.opened_at = time.monotonic()
        self._transport = transport
        self._write_lock = threading.Lock()
        self._closed = False

    def start(self) -> None:
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self) -> None:
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    break                       # clean close by the far end
                self._transport._ingest(data, self)
        except OSError:
            pass
        finally:
            self.close()

    def write(self, data: bytes) -> None:
        if self._closed:
            return
        try:
            with self._write_lock:
                self.sock.sendall(data)
        except OSError:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self._transport._dropped(self)


class LANTransport:
    """Discovers peers and exchanges control messages and screen frames.

    Callbacks all fire on background threads; a UI must marshal them onto its
    own thread (the Tk app uses `after(0, …)`).
    """

    def __init__(
        self,
        identity: Optional[DeviceIdentity] = None,
        kind: str = "windowsPC",
        on_peers: Optional[Callable[[List[Peer]], None]] = None,
        on_control: Optional[Callable[[str, Optional[str], Peer], None]] = None,
        on_frame: Optional[Callable[[bytes, Peer], None]] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.identity = identity or DeviceIdentity.load_or_create()
        self.kind = kind
        self.on_peers = on_peers or (lambda peers: None)
        self.on_control = on_control or (lambda control, payload, peer: None)
        self.on_frame = on_frame or (lambda frame, peer: None)
        self.log = log or (lambda message: None)

        self._lock = threading.RLock()
        self._discovered: Dict[str, _Discovered] = {}
        self._connections: Dict[str, _Connection] = {}
        # Connections that exist but have not said `hello` yet, so cannot be
        # keyed by peer id.
        self._pending: List[_Connection] = []
        # Peers we have a dial in flight to. A TCP connect takes time, during
        # which the peer is still "not connected" — without this the
        # once-a-second tick opens a second and third connection to the same
        # peer, and the duplicate-resolution rule then tears them down in
        # turn. The visible symptom is a link that connects and drops every
        # second.
        self._dialing: set[str] = set()

        self._listener: Optional[socket.socket] = None
        self._udp: Optional[socket.socket] = None
        self._port = 0
        self._running = False
        self._last_beacon = 0.0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._start_listener()
        self._start_discovery()
        self._running = True
        threading.Thread(target=self._housekeeping_loop, daemon=True).start()
        self.log(
            f"bridge: {self.identity.name} listening on tcp/{self._port}, "
            f"discovery on udp/{wire.DISCOVERY_PORT}"
        )

    def stop(self) -> None:
        self._running = False
        with self._lock:
            for conn in list(self._connections.values()) + list(self._pending):
                conn.close()
            self._connections.clear()
            self._pending.clear()
            self._discovered.clear()
            self._dialing.clear()
        for sock in (self._listener, self._udp):
            try:
                if sock:
                    sock.close()
            except OSError:
                pass
        self._listener = self._udp = None

    @property
    def connected_peers(self) -> List[Peer]:
        with self._lock:
            return [c.peer for c in self._connections.values() if c.peer]

    # -- TCP ---------------------------------------------------------------

    def _start_listener(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 0))       # 0 = any free port; read it back below
        listener.listen(8)
        self._listener = listener
        self._port = listener.getsockname()[1]
        threading.Thread(target=self._accept_loop, args=(listener,), daemon=True).start()

    def _accept_loop(self, listener: socket.socket) -> None:
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return                      # listener closed
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._adopt(_Connection(client, self))

    def _dial(self, entry: _Discovered) -> None:
        peer_id = entry.beacon.id
        try:
            sock = socket.create_connection((entry.address, entry.beacon.port), timeout=4)
        except OSError:
            with self._lock:
                self._dialing.discard(peer_id)
            self._back_off(peer_id)
            return
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self._lock:
            self._dialing.discard(peer_id)
            if peer_id in self._connections or not self._running:
                sock.close()
                return
        self._adopt(_Connection(sock, self))

    def _back_off(self, peer_id: str) -> None:
        """1, 2, 4, 8 s, then every 8 s, for as long as beacons keep arriving."""
        with self._lock:
            entry = self._discovered.get(peer_id)
            if not entry:
                return
            entry.failures = min(entry.failures + 1, 4)
            entry.next_dial = time.monotonic() + min(2 ** (entry.failures - 1), 8.0)

    def _adopt(self, conn: _Connection) -> None:
        with self._lock:
            self._pending.append(conn)
        # Identify ourselves at once: §2 of the protocol forbids acting on
        # anything else until a hello has been exchanged.
        conn.write(wire.encode_control("hello", self._hello_payload()))
        conn.start()

    def _hello_payload(self) -> str:
        return f"{self.identity.id}|{self.identity.name}|{self.kind}"

    def _dropped(self, conn: _Connection) -> None:
        with self._lock:
            if conn in self._pending:
                self._pending.remove(conn)
            peer = conn.peer
            if not peer or self._connections.get(peer.id) is not conn:
                return
            del self._connections[peer.id]
        self.log(f"bridge: disconnected {peer.name}")
        self._publish_peers()

    # -- UDP discovery -----------------------------------------------------

    def _start_discovery(self) -> None:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            # Lets two peers run on one machine, which is how this gets tested.
            try:
                udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        udp.bind(("0.0.0.0", wire.DISCOVERY_PORT))
        self._udp = udp
        threading.Thread(target=self._beacon_receive_loop, args=(udp,), daemon=True).start()

    def _beacon_receive_loop(self, udp: socket.socket) -> None:
        while True:
            try:
                data, (address, _) = udp.recvfrom(2048)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                return
            beacon = wire.decode_beacon(data)
            if beacon:
                self._heard(beacon, address)

    def _broadcast_addresses(self) -> List[str]:
        """Where to send beacons.

        Windows has no `getifaddrs`, and depending on a third-party package
        for this would be a poor trade. Instead we derive the /24 broadcast
        address of each local IPv4 and add the global broadcast address as a
        fallback. This covers ordinary home and office networks; it would miss
        an unusual netmask, which is a limitation worth knowing about but not
        worth a dependency.
        """
        targets = {"255.255.255.255"}
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if ip.startswith("127."):
                    continue
                parts = ip.split(".")
                if len(parts) == 4:
                    targets.add(".".join(parts[:3] + ["255"]))
        except OSError:
            pass
        return sorted(targets)

    def _send_beacon(self) -> None:
        if not self._udp:
            return
        data = wire.encode_beacon(wire.Beacon(
            id=self.identity.id, name=self.identity.name,
            kind=self.kind, port=self._port,
        ))
        for target in self._broadcast_addresses():
            try:
                self._udp.sendto(data, (target, wire.DISCOVERY_PORT))
            except OSError:
                pass

    def _heard(self, beacon: wire.Beacon, address: str) -> None:
        if beacon.id == self.identity.id:
            return                          # our own broadcast, echoed back
        with self._lock:
            entry = self._discovered.get(beacon.id)
            if entry:
                entry.beacon = beacon
                entry.address = address
                entry.last_seen = time.monotonic()
            else:
                self._discovered[beacon.id] = _Discovered(beacon, address, time.monotonic())
                self.log(f"bridge: discovered {beacon.name} ({beacon.kind})")

    # -- housekeeping ------------------------------------------------------

    def _housekeeping_loop(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception as exc:                        # never let the loop die
                self.log(f"bridge: housekeeping error {exc!r}")
            time.sleep(1.0)

    def _tick(self) -> None:
        now = time.monotonic()

        if now - self._last_beacon >= wire.BEACON_INTERVAL:
            self._last_beacon = now
            self._send_beacon()

        with self._lock:
            # Forget peers whose beacons stopped.
            for peer_id, entry in list(self._discovered.items()):
                if now - entry.last_seen > wire.PEER_TIMEOUT:
                    del self._discovered[peer_id]
                    conn = self._connections.get(peer_id)
                    if conn:
                        self.log(f"bridge: lost {entry.beacon.name}")
                        conn.close()

            # A connection that never says hello holds a socket open for
            # nothing. Anything on the network can open one, so this is also
            # what stops them accumulating.
            for conn in [c for c in self._pending if now - c.opened_at > 10]:
                self.log("bridge: closing a connection that never identified itself")
                conn.close()

            to_dial = [
                entry for peer_id, entry in self._discovered.items()
                if peer_id not in self._connections
                and peer_id not in self._dialing
                and self.identity.id < peer_id          # exactly one side dials
                and now >= entry.next_dial
            ]
            self._dialing.update(entry.beacon.id for entry in to_dial)

        for entry in to_dial:
            threading.Thread(target=self._dial, args=(entry,), daemon=True).start()

    # -- receiving ---------------------------------------------------------

    def _ingest(self, data: bytes, conn: _Connection) -> None:
        conn.reader.append(data)
        while True:
            try:
                message = conn.reader.next()
            except wire.ProtocolError as exc:
                self.log(f"bridge: dropping connection — {exc}")
                conn.close()
                return
            if message is None:
                return
            msg_type, payload = message

            if msg_type == wire.TYPE_FRAME:
                if conn.peer:
                    self.on_frame(payload, conn.peer)
                continue

            envelope = wire.decode_control(payload)
            if envelope:
                self._handle(envelope, conn)

    def _handle(self, envelope: wire.Envelope, conn: _Connection) -> None:
        if envelope.control == "hello":
            parts = (envelope.payload or "").split("|")
            if len(parts) < 2 or not parts[0]:
                conn.close()
                return
            peer = Peer(parts[0], parts[1], parts[2] if len(parts) > 2 else "unknown")

            with self._lock:
                # Two peers can dial each other in the instant before either
                # beacon is processed; keep one deterministically.
                existing = self._connections.get(peer.id)
                if existing is not None and existing is not conn:
                    if self.identity.id < peer.id:
                        conn.close()
                        return
                    existing.close()
                conn.peer = peer
                if conn in self._pending:
                    self._pending.remove(conn)
                self._connections[peer.id] = conn
                entry = self._discovered.get(peer.id)
                if entry:
                    entry.failures = 0
            self.log(f"bridge: connected {peer.name} ({peer.kind})")
            self._publish_peers()
            return

        peer = conn.peer
        if not peer:
            return                          # nothing may be acted on before hello

        payload = envelope.payload
        if envelope.control == "handoff":
            # Validated here, on the receiving side, rather than trusting
            # whatever the sender claims to have sent.
            safe = wire.safe_handoff_url(payload or "")
            if not safe:
                self.log("bridge: refused a handoff payload that was not a web URL")
                return
            payload = safe

        self.on_control(envelope.control, payload, peer)

    def _publish_peers(self) -> None:
        self.on_peers(self.connected_peers)

    # -- sending -----------------------------------------------------------

    def send(self, control: str, peer: Peer, payload: Optional[str] = None) -> None:
        with self._lock:
            conn = self._connections.get(peer.id)
        if not conn:
            self.log(f"bridge: send {control} failed — {peer.name} not connected")
            return
        conn.write(wire.encode_control(control, payload))
        self.log(f"bridge: sent {control} to {peer.name}")

    def send_frame(self, frame: bytes, peer: Peer) -> None:
        with self._lock:
            conn = self._connections.get(peer.id)
        if conn:
            conn.write(wire.encode(wire.TYPE_FRAME, frame))
