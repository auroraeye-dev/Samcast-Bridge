import Foundation
import AppKit
import QuackBridge
import QuackCastCore
import QuackCastPlatform

// A headless QuackCast peer for the Mac, speaking the cross-platform bridge
// protocol. It exists so a Mac ↔ Windows handoff can be developed and tested
// without building, launching or re-permissioning the GUI app.
//
//   swift run BridgeCLI                 typed commands only
//   swift run BridgeCLI --gestures      also watch the camera for ✊ / 🖐️
//   swift run BridgeCLI --auto-accept   take whatever is offered, no gesture
//   swift run BridgeCLI --as NAME       run under a throwaway identity, so two
//                                       peers can be tested on one machine
//
// Commands once running:  grab · take · list · drop · quit

let arguments = Set(CommandLine.arguments.dropFirst())
let useGestures = arguments.contains("--gestures")
let autoAccept = arguments.contains("--auto-accept")
let verbose = arguments.contains("--verbose")
/// Log what would happen instead of actually opening a browser. Used by the
/// interop tests, which must not take over the screen to prove a point.
let dryRun = arguments.contains("--dry-run")

/// `--as NAME` gives this process a fresh identity instead of the machine's
/// saved one. Two peers on one Mac would otherwise share an id and ignore each
/// other's beacons as their own.
let temporaryName: String? = {
    let all = Array(CommandLine.arguments)
    guard let i = all.firstIndex(of: "--as"), i + 1 < all.count else { return nil }
    return all[i + 1]
}()

func say(_ text: String) {
    print(text)
    fflush(stdout)
}

// MARK: - The peer

final class BridgePeer: PeerTransportDelegate {
    private let transport: LANTransport
    private let trust = TrustStore()
    private var peers: [Peer] = []

    /// A page grabbed here and waiting for someone to take it.
    ///
    /// Its own type rather than `BrowserLink.Page`, which has no public
    /// initialiser — and because a staged URL has no browser behind it.
    struct HeldPage {
        let url: URL
        let title: String
    }
    private var holding: HeldPage?
    private var holdExpiry: Timer?

    /// Peers currently offering us something, most recent last.
    private var offers: [Peer] = []

    init() {
        let identity = temporaryName.map { DeviceIdentity(id: UUID().uuidString, name: $0) }
        transport = LANTransport(identity: identity, kind: .mac,
                                 log: { if verbose { say("    \($0)") } })
        transport.delegate = self
    }

    var name: String { transport.localName }

    func start() {
        transport.start()
    }

    // MARK: Sending

    /// ✊ — grab the page in the frontmost browser and offer it to everyone.
    ///
    /// Nothing is closed yet: the tab is only closed once a peer actually
    /// asks for it, so a gesture nobody catches leaves the page where it was.
    /// `staged` lets a URL be supplied directly (`grab <url>`) instead of
    /// read from the frontmost browser — the same affordance the Python peer
    /// has, so both directions can be exercised without a browser or the
    /// Automation permission prompt.
    func grab(staged: String? = nil) {
        guard holding == nil else { say("already holding \(holding!.title)"); return }
        guard !peers.isEmpty else { say("no peers connected — nothing to hand to"); return }
        do {
            let page: HeldPage
            if let staged {
                guard let url = Wire.safeHandoffURL(staged) else {
                    say("“\(staged)” is not an http(s) address")
                    return
                }
                page = HeldPage(url: url, title: url.absoluteString)
            } else {
                let live = try BrowserLink.frontmostPage()
                page = HeldPage(url: live.url, title: live.title)
            }
            holding = page
            say("✊ holding “\(page.title)” — open your hand at another device")
            for peer in peers { transport.send(.sourceAvailable, to: peer) }
            armExpiry()
        } catch {
            say("couldn't grab a page: \(error.localizedDescription)")
        }
    }

    /// The 5-second nullification window: if nobody takes it, put it back.
    private func armExpiry() {
        holdExpiry?.invalidate()
        holdExpiry = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: false) { [weak self] _ in
            guard let self, self.holding != nil else { return }
            self.holding = nil
            for peer in self.peers { self.transport.send(.sourceWithdrawn, to: peer) }
            say("nobody took it — the page stays here")
        }
    }

    func drop() {
        guard holding != nil else { say("not holding anything"); return }
        holding = nil
        holdExpiry?.invalidate()
        for peer in peers { transport.send(.sourceWithdrawn, to: peer) }
        say("released")
    }

    // MARK: Receiving

    /// 🖐️ — ask whoever is offering to hand it over.
    func take() {
        guard let source = offers.last else { say("nothing is being offered"); return }
        say("🖐️ asking \(source.displayName) for it…")
        transport.send(.requestCast, to: source)
    }

    func list() {
        if peers.isEmpty {
            say("no peers — is the other machine running QuackCast on this network?")
        }
        for peer in peers {
            let mark = trust.isTrusted(peer.id) ? "trusted" : "new"
            let offering = offers.contains(peer) ? ", offering something" : ""
            say("  • \(peer.displayName)  [\(peer.kind.rawValue), \(mark)\(offering)]")
        }
    }

    // MARK: PeerTransportDelegate

    func transport(_ transport: PeerTransport, didUpdate peers: [Peer]) {
        let previous = Set(self.peers.map(\.id))
        self.peers = peers
        for peer in peers where !previous.contains(peer.id) {
            say("→ \(peer.displayName) (\(peer.kind.rawValue)) connected")
        }
        offers.removeAll { peer in !peers.contains(where: { $0.id == peer.id }) }
    }

    func transport(_ transport: PeerTransport, didReceive message: ControlMessage, payload: String?, from peer: Peer) {
        switch message {
        case .sourceAvailable:
            offers.removeAll { $0.id == peer.id }
            offers.append(peer)
            say("\(peer.displayName) has something for you — open your hand (or type `take`)")
            if autoAccept { take() }

        case .sourceWithdrawn:
            offers.removeAll { $0.id == peer.id }

        case .requestCast:
            // Someone opened their hand at their device. Hand it over, then
            // close it here — in that order, so nothing is lost if the send
            // fails.
            guard let page = holding else {
                say("\(peer.displayName) asked, but there is nothing held here")
                return
            }
            holdExpiry?.invalidate()
            holding = nil
            transport.send(.handoff, payload: page.url.absoluteString, to: peer)
            if !dryRun { BrowserLink.closeFrontmostTab() }
            say("→ handed “\(page.title)” to \(peer.displayName)")

        case .handoff:
            // The transport has already rejected anything that is not an
            // http(s) URL; this is the second, independent check.
            guard let raw = payload, let url = Wire.safeHandoffURL(raw) else {
                say("refused a handoff from \(peer.displayName): not a web address")
                return
            }
            if !trust.isTrusted(peer.id) {
                trust.trust(peer.id, name: peer.displayName)
                say("trusting \(peer.displayName) from now on")
            }
            offers.removeAll { $0.id == peer.id }
            say("← received \(url.absoluteString) from \(peer.displayName)")
            if dryRun {
                say("(dry run — not opening)")
            } else {
                NSWorkspace.shared.open(url)
            }

        case .endCast:
            break
        }
    }

    func transport(_ transport: PeerTransport, didReceiveFrame frame: Any, from peer: Peer) {
        // Window streaming is not wired into the CLI; the GUI app displays it.
    }

    // MARK: Gestures

    func startGestures() {
        let tracker = VisionHandTracker()
        let classifier = GestureClassifier()
        var debouncer = GestureDebouncer(holdDuration: 0.3)

        tracker.onHands = { [weak self] hands, time in
            guard let self else { return }
            let raw = hands.first.map { classifier.classify($0) } ?? HandGesture.none
            guard let stable = debouncer.update(raw, at: time) else { return }
            DispatchQueue.main.async {
                switch stable {
                case .closedHand: self.grab()
                case .openHand: if !self.offers.isEmpty { self.take() }
                default: break
                }
            }
        }
        do {
            try tracker.start()
            say("camera on — ✊ to grab, 🖐️ to take")
        } catch {
            say("camera unavailable (\(error)) — typed commands still work")
        }
        // Held for the lifetime of the process.
        gestureTracker = tracker
    }

    private var gestureTracker: VisionHandTracker?
}

// MARK: - Run loop

let peer = BridgePeer()
peer.start()
say("QuackCast bridge — this Mac is “\(peer.name)”")
say("commands: grab · take · list · drop · quit")
if useGestures { peer.startGestures() }

// stdin on a background thread so the main run loop stays free for the
// sockets, the camera and the 5-second hold timer.
DispatchQueue.global(qos: .userInitiated).async {
    while let line = readLine(strippingNewline: true) {
        let command = line.trimmingCharacters(in: .whitespaces).lowercased()
        DispatchQueue.main.async {
            switch command {
            case _ where command == "grab" || command == "g" || command == "✊":
                peer.grab()
            case _ where command.hasPrefix("grab ") || command.hasPrefix("g "):
                peer.grab(staged: String(command.drop(while: { $0 != " " }).dropFirst()))
            case "take", "t", "🖐", "🖐️": peer.take()
            case "list", "l": peer.list()
            case "drop", "d": peer.drop()
            case "quit", "q", "exit": exit(0)
            case "": break
            default: say("commands: grab · take · list · drop · quit")
            }
        }
    }
    // stdin closed (piped input, or Ctrl-D): keep serving rather than exiting,
    // so the peer can be left running in the background.
}

RunLoop.main.run()
