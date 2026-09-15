import Foundation
import SamcastCore
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// A `PeerTransport` that reaches **any** machine on the local network,
/// including Windows, by speaking plain UDP + TCP instead of
/// MultipeerConnectivity.
///
/// It is a drop-in for `MultipeerTransport`: same port, same control messages,
/// same delegate callbacks. The app above it cannot tell which one it is
/// talking to, which is the whole point — Windows support should not mean a
/// second copy of the product logic.
///
/// See `docs/PROTOCOL.md` for the wire format. Everything here is confined to
/// the local network: it opens no outbound internet connection, uses no
/// broker, and touches nothing outside this process.
public final class LANTransport: PeerTransport {

    public weak var delegate: PeerTransportDelegate?

    private let identity: DeviceIdentity
    private let localKind: Peer.Kind
    private let log: (String) -> Void

    /// Guards every piece of mutable state below. All socket callbacks hop
    /// onto it, so the transport has exactly one place where state changes.
    private let state = DispatchQueue(label: "com.quackcast.bridge.state")

    private var listenFD: Int32 = -1
    private var udpFD: Int32 = -1
    private var listenPort: UInt16 = 0
    private var running = false

    /// A peer we have heard a beacon from, whether or not it is connected.
    private struct Discovered {
        var beacon: Wire.Beacon
        var address: in_addr_t
        var lastSeen: Date
        /// Backoff bookkeeping for redialling, per docs/PROTOCOL.md §2.
        var nextDial: Date = .distantPast
        var failures: Int = 0
    }

    private var discovered: [String: Discovered] = [:]
    private var connections: [String: Connection] = [:]

    /// Connections that exist but have not yet said `hello`, so cannot be
    /// keyed by peer id. They are held here because nothing else refers to
    /// them: without a strong reference a brand-new connection is deallocated
    /// the instant it is created, and the socket dies with it.
    private var pending: [ObjectIdentifier: Connection] = [:]

    /// Peers we have a dial in flight to. A TCP connect takes time, during
    /// which the peer is still "not connected" — without this the once-a-
    /// second tick opens a second, third and fourth connection to the same
    /// peer, and the duplicate-resolution rule then tears them down in turn.
    /// The visible symptom is a link that connects and drops every second.
    private var dialing: Set<String> = []
    private var housekeeping: DispatchSourceTimer?

    public init(identity: DeviceIdentity? = nil,
                kind: Peer.Kind = .mac,
                log: @escaping (String) -> Void = { _ in }) {
        self.identity = identity ?? DeviceIdentity.loadOrCreate(kind: kind)
        self.localKind = kind
        self.log = log
    }

    public var localName: String { identity.name }
    public var localID: String { identity.id }

    public var connectedPeers: [Peer] {
        state.sync { connections.values.compactMap(\.peer) }
    }

    // MARK: - Lifecycle

    public func start() {
        state.async { [self] in
            guard !running else { return }
            guard startListener() else { return }
            guard startDiscovery() else { return }
            running = true
            startHousekeeping()
            log("bridge: \(identity.name) listening on tcp/\(listenPort), discovery on udp/\(Wire.discoveryPort)")
        }
    }

    public func stop() {
        state.async { [self] in
            guard running else { return }
            running = false
            housekeeping?.cancel(); housekeeping = nil
            connections.values.forEach { $0.close() }
            connections.removeAll()
            pending.values.forEach { $0.close() }
            pending.removeAll()
            dialing.removeAll()
            discovered.removeAll()
            if listenFD >= 0 { close(listenFD); listenFD = -1 }
            if udpFD >= 0 { close(udpFD); udpFD = -1 }
            log("bridge: stopped")
        }
    }

    // MARK: - TCP listener

    private func startListener() -> Bool {
        let fd = Sock.makeTCP()
        guard fd >= 0 else { log("bridge: could not create listen socket"); return false }

        // Port 0 asks the kernel for any free port; we then read back which
        // one it gave us and put that in the beacon.
        var addr = Sock.address(host: INADDR_ANY, port: 0)
        let bound = Sock.withSockAddr(&addr) { bind(fd, $0, $1) }
        guard bound == 0, listen(fd, 8) == 0 else {
            log("bridge: could not listen (errno \(errno))")
            close(fd)
            return false
        }

        var actual = sockaddr_in()
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        withUnsafeMutablePointer(to: &actual) { raw in
            raw.withMemoryRebound(to: sockaddr.self, capacity: 1) { _ = getsockname(fd, $0, &len) }
        }
        listenFD = fd
        listenPort = UInt16(bigEndian: actual.sin_port)

        DispatchQueue.global(qos: .utility).async { [weak self] in
            self?.acceptLoop(fd)
        }
        return true
    }

    private func acceptLoop(_ fd: Int32) {
        while true {
            let client = accept(fd, nil, nil)
            if client < 0 {
                if errno == EINTR { continue }
                return                                  // listener closed
            }
            #if canImport(Darwin)
            Sock.setOption(client, SOL_SOCKET, SO_NOSIGPIPE)
            #endif
            Sock.setOption(client, Int32(IPPROTO_TCP), TCP_NODELAY)
            state.async { [weak self] in self?.adopt(Connection(fd: client)) }
        }
    }

    // MARK: - UDP discovery

    private func startDiscovery() -> Bool {
        let fd = socket(AF_INET, SOCK_DGRAM, 0)
        guard fd >= 0 else { return false }
        Sock.setOption(fd, SOL_SOCKET, SO_REUSEADDR)
        #if canImport(Darwin)
        // Lets a second Samcast on the same machine also bind 50505, which
        // is what makes it possible to test two peers on one Mac.
        Sock.setOption(fd, SOL_SOCKET, SO_REUSEPORT)
        #endif
        Sock.setOption(fd, SOL_SOCKET, SO_BROADCAST)

        var addr = Sock.address(host: INADDR_ANY, port: Wire.discoveryPort)
        guard Sock.withSockAddr(&addr, { bind(fd, $0, $1) }) == 0 else {
            log("bridge: could not bind udp/\(Wire.discoveryPort) (errno \(errno))")
            close(fd)
            return false
        }
        udpFD = fd

        DispatchQueue.global(qos: .utility).async { [weak self] in
            self?.beaconReceiveLoop(fd)
        }
        return true
    }

    private func beaconReceiveLoop(_ fd: Int32) {
        var buffer = [UInt8](repeating: 0, count: 2048)
        while true {
            var from = sockaddr_in()
            var fromLen = socklen_t(MemoryLayout<sockaddr_in>.size)
            let n: Int = withUnsafeMutablePointer(to: &from) { raw in
                raw.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                    recvfrom(fd, &buffer, buffer.count, 0, sa, &fromLen)
                }
            }
            if n < 0 {
                if errno == EINTR { continue }
                return
            }
            guard n > 0, let beacon = Wire.decodeBeacon(Data(buffer[0 ..< n])) else { continue }
            let source = from.sin_addr.s_addr
            state.async { [weak self] in self?.heard(beacon, from: source) }
        }
    }

    private func sendBeacon() {
        guard udpFD >= 0 else { return }
        let beacon = Wire.Beacon(id: identity.id,
                                 name: identity.name,
                                 kind: localKind.rawValue,
                                 port: listenPort)
        let data = Wire.encodeBeacon(beacon)
        var targets = Sock.broadcastAddresses()
        if targets.isEmpty { targets = [INADDR_BROADCAST] }
        for target in targets {
            var addr = Sock.address(host: target, port: Wire.discoveryPort)
            _ = data.withUnsafeBytes { raw in
                Sock.withSockAddr(&addr) { sa, len in
                    sendto(udpFD, raw.baseAddress, data.count, 0, sa, len)
                }
            }
        }
    }

    /// A beacon arrived. Records or refreshes the peer; the dial decision is
    /// left to the housekeeping tick so it happens in one place.
    private func heard(_ beacon: Wire.Beacon, from address: in_addr_t) {
        guard beacon.id != identity.id else { return }      // our own broadcast
        if var existing = discovered[beacon.id] {
            existing.beacon = beacon
            existing.address = address
            existing.lastSeen = Date()
            discovered[beacon.id] = existing
        } else {
            discovered[beacon.id] = Discovered(beacon: beacon, address: address, lastSeen: Date())
            log("bridge: discovered \(beacon.name) (\(beacon.kind))")
        }
    }

    // MARK: - Housekeeping: beacons, expiry, redial

    private func startHousekeeping() {
        let timer = DispatchSource.makeTimerSource(queue: state)
        timer.schedule(deadline: .now(), repeating: 1.0)
        timer.setEventHandler { [weak self] in self?.tick() }
        housekeeping = timer
        timer.resume()
    }

    private var lastBeacon = Date.distantPast

    private func tick() {
        guard running else { return }
        let now = Date()

        if now.timeIntervalSince(lastBeacon) >= Wire.beaconInterval {
            lastBeacon = now
            sendBeacon()
        }

        // Forget peers whose beacons stopped, and drop their connections.
        for (id, peer) in discovered where now.timeIntervalSince(peer.lastSeen) > Wire.peerTimeout {
            discovered.removeValue(forKey: id)
            if let conn = connections.removeValue(forKey: id) {
                conn.close()
                log("bridge: lost \(peer.beacon.name)")
                publishPeers()
            }
        }

        // A connection that never says hello holds a socket open for nothing.
        // Anything on the network can open a TCP connection to us, so this is
        // also what stops an unresponsive dialler from accumulating sockets.
        for (key, conn) in pending where now.timeIntervalSince(conn.openedAt) > 10 {
            pending.removeValue(forKey: key)
            log("bridge: closing a connection that never identified itself")
            conn.close()
        }

        // Dial peers we are responsible for dialling.
        for (id, peer) in discovered {
            guard connections[id] == nil,
                  !dialing.contains(id),
                  identity.id < id,                 // exactly one side dials
                  now >= peer.nextDial
            else { continue }
            dialing.insert(id)
            dial(peer)
        }
    }

    private func dial(_ peer: Discovered) {
        let id = peer.beacon.id
        let fd = Sock.makeTCP()
        guard fd >= 0 else { return }
        var addr = Sock.address(host: peer.address, port: peer.beacon.port)

        // Blocking connect on a background queue: the state queue must never
        // stall on the network, or discovery and beacons stop with it.
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let ok = Sock.withSockAddr(&addr) { connect(fd, $0, $1) } == 0
            guard let self else { close(fd); return }
            self.state.async {
                self.dialing.remove(id)
                guard self.running else { close(fd); return }
                guard ok else {
                    close(fd)
                    self.backOff(id)
                    return
                }
                guard self.connections[id] == nil else { close(fd); return }
                Sock.setOption(fd, Int32(IPPROTO_TCP), TCP_NODELAY)
                self.adopt(Connection(fd: fd))
            }
        }
    }

    /// 1, 2, 4, 8 s, then every 8 s, for as long as beacons keep arriving.
    private func backOff(_ id: String) {
        guard var peer = discovered[id] else { return }
        peer.failures = min(peer.failures + 1, 4)
        let delay = pow(2.0, Double(peer.failures - 1))
        peer.nextDial = Date().addingTimeInterval(min(delay, 8.0))
        discovered[id] = peer
    }

    // MARK: - Connections

    private func adopt(_ conn: Connection) {
        pending[ObjectIdentifier(conn)] = conn
        conn.onData = { [weak self, weak conn] data in
            guard let self, let conn else { return }
            self.state.async { self.ingest(data, on: conn) }
        }
        conn.onClose = { [weak self, weak conn] in
            guard let self, let conn else { return }
            self.state.async { self.dropped(conn) }
        }
        // Say who we are straight away; §2 of the protocol requires a hello
        // before anything on the connection may be acted upon.
        conn.write(Wire.encodeControl("hello", payload: helloPayload()))
        conn.startReading()
    }

    private func helloPayload() -> String {
        "\(identity.id)|\(identity.name)|\(localKind.rawValue)"
    }

    private func dropped(_ conn: Connection) {
        pending.removeValue(forKey: ObjectIdentifier(conn))
        guard let id = conn.peer?.id else { return }    // never identified itself
        guard connections[id] === conn else { return }
        connections.removeValue(forKey: id)
        log("bridge: disconnected \(conn.peer?.displayName ?? id)")
        publishPeers()
    }

    private func ingest(_ data: Data, on conn: Connection) {
        conn.reader.append(data)
        while true {
            let message: (type: Wire.MessageType, payload: Data)?
            do {
                message = try conn.reader.next()
            } catch {
                // A length-prefixed stream cannot be resynchronised once it is
                // out of step, so the only safe response is to hang up.
                log("bridge: dropping connection — \(error)")
                conn.close()
                return
            }
            guard let message else { return }

            switch message.type {
            case .control:
                guard let envelope = Wire.decodeControl(message.payload) else { continue }
                handle(envelope, on: conn)
            case .frame:
                guard let peer = conn.peer else { continue }   // no hello yet
                let frame = message.payload
                DispatchQueue.main.async { [weak self] in
                    guard let self else { return }
                    self.delegate?.transport(self, didReceiveFrame: frame, from: peer)
                }
            }
        }
    }

    private func handle(_ envelope: Wire.Envelope, on conn: Connection) {
        if envelope.control == "hello" {
            let parts = (envelope.payload ?? "").components(separatedBy: "|")
            guard parts.count >= 2, !parts[0].isEmpty else { conn.close(); return }
            let kind = parts.count >= 3 ? (Peer.Kind(rawValue: parts[2]) ?? .unknown) : .unknown
            let peer = Peer(id: parts[0], displayName: parts[1], kind: kind)

            // Two peers can dial each other in the instant before either
            // beacon is processed. Keep one deterministically.
            if let existing = connections[peer.id], existing !== conn {
                if identity.id < peer.id { conn.close(); return }
                existing.close()
            }
            conn.peer = peer
            pending.removeValue(forKey: ObjectIdentifier(conn))
            connections[peer.id] = conn
            if var known = discovered[peer.id] { known.failures = 0; discovered[peer.id] = known }
            log("bridge: connected \(peer.displayName) (\(peer.kind.rawValue))")
            publishPeers()
            return
        }

        // Everything else requires an identified connection.
        guard let peer = conn.peer else { return }
        guard let control = ControlMessage(rawValue: envelope.control) else { return }

        var payload = envelope.payload
        if control == .handoff {
            // Validate here, on the receiving side, rather than trusting the
            // sender: this is the only value that crosses the wire and is then
            // handed to the OS.
            guard let raw = payload, let url = Wire.safeHandoffURL(raw) else {
                log("bridge: refused a handoff payload that was not a web URL")
                return
            }
            payload = url.absoluteString
        }

        let finalPayload = payload
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.delegate?.transport(self, didReceive: control, payload: finalPayload, from: peer)
        }
    }

    private func publishPeers() {
        let peers = connections.values.compactMap(\.peer)
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.delegate?.transport(self, didUpdate: peers)
        }
    }

    // MARK: - PeerTransport

    public func send(_ message: ControlMessage, payload: String?, to peer: Peer) {
        state.async { [self] in
            guard let conn = connections[peer.id] else {
                log("bridge: send \(message.rawValue) failed — \(peer.displayName) not connected")
                return
            }
            conn.write(Wire.encodeControl(message.rawValue, payload: payload))
            log("bridge: sent \(message.rawValue) to \(peer.displayName)")
        }
    }

    /// Send an encoded screen frame (JPEG). Frames are advisory: if the socket
    /// is backed up the frame is dropped rather than queued, because a stale
    /// frame is worth less than the next fresh one.
    public func sendFrameData(_ frame: Data, to peer: Peer) {
        state.async { [self] in
            guard let conn = connections[peer.id], !conn.isBacklogged else { return }
            conn.write(Wire.encode(.frame, frame))
        }
    }

    public func startStreaming(to peer: Peer) { /* push-driven via sendFrameData */ }
    public func stopStreaming(to peer: Peer) { /* sender simply stops sending */ }
}

// MARK: - One TCP connection

/// Owns a socket, a read thread and a serial write queue.
final class Connection {
    let fd: Int32
    var peer: Peer?
    var reader = Wire.Reader()

    var onData: ((Data) -> Void)?
    var onClose: (() -> Void)?

    /// When the connection was created, so one that never identifies itself
    /// can be reaped rather than holding a socket open indefinitely.
    let openedAt = Date()

    private let writes: DispatchQueue
    private var closed = false

    /// Read on the transport's state queue, written on the write queue, so it
    /// needs its own lock rather than relying on either.
    private let counter = NSLock()
    private var queuedBytes = 0
    private let backlogLimit = 4 * 1024 * 1024

    /// True when writes are piling up faster than the socket drains, used to
    /// skip frames instead of growing an unbounded queue in memory.
    var isBacklogged: Bool {
        counter.lock(); defer { counter.unlock() }
        return queuedBytes > backlogLimit
    }

    private func addQueued(_ delta: Int) {
        counter.lock(); queuedBytes += delta; counter.unlock()
    }

    init(fd: Int32) {
        self.fd = fd
        self.writes = DispatchQueue(label: "com.quackcast.bridge.write.\(fd)")
    }

    func startReading() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            var buffer = [UInt8](repeating: 0, count: 64 * 1024)
            while true {
                let n = recv(self.fd, &buffer, buffer.count, 0)
                if n > 0 {
                    self.onData?(Data(buffer[0 ..< n]))
                    continue
                }
                if n < 0 && errno == EINTR { continue }
                break                                   // 0 = clean close, <0 = error
            }
            self.close()
        }
    }

    func write(_ data: Data) {
        guard !closed else { return }
        addQueued(data.count)
        writes.async { [weak self] in
            guard let self else { return }
            defer { self.addQueued(-data.count) }
            guard !self.closed else { return }
            if !Sock.writeAll(self.fd, data) { self.close() }
        }
    }

    func close() {
        guard !closed else { return }
        closed = true
        shutdown(fd, SHUT_RDWR)
        Darwin.close(fd)
        onClose?()
    }
}
