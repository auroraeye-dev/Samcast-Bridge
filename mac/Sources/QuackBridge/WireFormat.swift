import Foundation

/// Framing and message encoding for the bridge protocol (docs/PROTOCOL.md).
///
/// Kept deliberately small and free of socket code so it can be reasoned about
/// — and checked — on its own. The Python side implements the identical
/// format; `BridgeCheck` and `windows/tests` verify the two agree.
public enum Wire {

    /// Protocol version carried in every discovery beacon.
    public static let version = 1

    /// UDP port every peer broadcasts and listens on.
    public static let discoveryPort: UInt16 = 50505

    /// How often a beacon goes out, and how long silence is tolerated before
    /// a peer is declared gone (four missed beacons).
    public static let beaconInterval: TimeInterval = 2.0
    public static let peerTimeout: TimeInterval = 8.0

    /// Hard ceiling on a single message. Without this a bad or hostile length
    /// prefix would have the receiver try to allocate up to 4 GiB; a real
    /// frame is a few hundred KiB.
    public static let maxPayload = 8 * 1024 * 1024

    public enum MessageType: UInt8 {
        case control = 0x01
        case frame = 0x02
    }

    // MARK: - Framing

    /// `length(4, big-endian) | type(1) | payload`
    public static func encode(_ type: MessageType, _ payload: Data) -> Data {
        var out = Data(capacity: payload.count + 5)
        let n = UInt32(payload.count)
        out.append(UInt8((n >> 24) & 0xFF))
        out.append(UInt8((n >> 16) & 0xFF))
        out.append(UInt8((n >> 8) & 0xFF))
        out.append(UInt8(n & 0xFF))
        out.append(type.rawValue)
        out.append(payload)
        return out
    }

    /// Incremental reader: feed it whatever a socket returned, take out whole
    /// messages. TCP gives no message boundaries, so a reader that assumed one
    /// read == one message would work on a fast LAN and corrupt under load.
    public struct Reader {
        private var buffer = Data()

        public init() {}

        public enum ReadError: Error, CustomStringConvertible {
            case oversized(Int)
            case unknownType(UInt8)

            public var description: String {
                switch self {
                case .oversized(let n): return "message of \(n) bytes exceeds the \(Wire.maxPayload) byte limit"
                case .unknownType(let t): return "unknown message type 0x\(String(t, radix: 16))"
                }
            }
        }

        public mutating func append(_ data: Data) { buffer.append(data) }

        /// Returns the next complete message, or nil if more bytes are needed.
        /// Throws when the stream is unusable — the caller must then close the
        /// connection rather than try to resynchronise, because there is no
        /// way to find the next boundary in a corrupted length-prefixed stream.
        public mutating func next() throws -> (type: MessageType, payload: Data)? {
            guard buffer.count >= 5 else { return nil }
            let header = [UInt8](buffer.prefix(5))
            let length = (Int(header[0]) << 24) | (Int(header[1]) << 16)
                       | (Int(header[2]) << 8) | Int(header[3])
            guard length <= Wire.maxPayload else { throw ReadError.oversized(length) }
            guard let type = MessageType(rawValue: header[4]) else {
                throw ReadError.unknownType(header[4])
            }
            guard buffer.count >= 5 + length else { return nil }
            let payload = buffer.subdata(in: 5 ..< (5 + length))
            buffer.removeSubrange(0 ..< (5 + length))
            return (type, payload)
        }
    }

    // MARK: - Control envelope

    /// Identical JSON to the envelope the MultipeerConnectivity build sends,
    /// so both transports carry the same control plane.
    public struct Envelope: Codable, Equatable {
        public let control: String
        public let payload: String?

        public init(control: String, payload: String? = nil) {
            self.control = control
            self.payload = payload
        }
    }

    public static func encodeControl(_ control: String, payload: String? = nil) -> Data {
        let json = (try? JSONEncoder().encode(Envelope(control: control, payload: payload))) ?? Data()
        return encode(.control, json)
    }

    public static func decodeControl(_ payload: Data) -> Envelope? {
        try? JSONDecoder().decode(Envelope.self, from: payload)
    }

    // MARK: - Discovery beacon

    public struct Beacon: Codable, Equatable {
        public let qc: Int
        public let id: String
        public let name: String
        public let kind: String
        public let port: UInt16

        public init(id: String, name: String, kind: String, port: UInt16) {
            self.qc = Wire.version
            self.id = id
            self.name = name
            self.kind = kind
            self.port = port
        }
    }

    public static func encodeBeacon(_ beacon: Beacon) -> Data {
        (try? JSONEncoder().encode(beacon)) ?? Data()
    }

    /// Returns nil for anything that isn't a beacon we understand — including
    /// a future protocol version, which must be ignored rather than guessed at.
    public static func decodeBeacon(_ data: Data) -> Beacon? {
        guard let beacon = try? JSONDecoder().decode(Beacon.self, from: data) else { return nil }
        guard beacon.qc == Wire.version, !beacon.id.isEmpty, beacon.port != 0 else { return nil }
        return beacon
    }

    // MARK: - Safety

    /// The receiving side's own check on a handed-over URL.
    ///
    /// Enforced here, independently of whatever the sender claims to have
    /// sent, because this is the only value that crosses the network and is
    /// then acted on by the OS. Anything but http/https — `file:`, `smb:`,
    /// a custom scheme registered by some installed app — could reach a local
    /// resource or launch a program, so only the two web schemes are allowed.
    public static func safeHandoffURL(_ raw: String) -> URL? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count <= 4096, let url = URL(string: trimmed) else { return nil }
        guard let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https" else { return nil }
        guard let host = url.host, !host.isEmpty else { return nil }
        return url
    }
}
