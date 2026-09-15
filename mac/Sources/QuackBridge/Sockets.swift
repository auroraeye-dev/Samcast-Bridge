import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// Thin, readable helpers over the BSD socket API.
///
/// The bridge uses raw sockets rather than Network.framework precisely because
/// the point is to reach a machine that has no Apple frameworks; keeping this
/// layer close to what the Python side does makes the two implementations
/// checkable against each other line by line.
enum Sock {

    /// Build a `sockaddr_in`, handling the byte-order conversions in one place.
    static func address(host: in_addr_t, port: UInt16) -> sockaddr_in {
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr = in_addr(s_addr: host)
        return addr
    }

    /// `withUnsafePointer` on a `sockaddr_in` reinterpreted as `sockaddr`.
    /// Every bind/connect/sendto call needs this dance; doing it once keeps
    /// the pointer juggling out of the transport logic.
    static func withSockAddr<T>(_ addr: inout sockaddr_in, _ body: (UnsafePointer<sockaddr>, socklen_t) -> T) -> T {
        withUnsafePointer(to: &addr) { raw in
            raw.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                body($0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
    }

    static func setOption(_ fd: Int32, _ level: Int32, _ option: Int32, _ value: Int32 = 1) {
        var v = value
        setsockopt(fd, level, option, &v, socklen_t(MemoryLayout<Int32>.size))
    }

    /// A TCP socket that will not kill the process when the far end vanishes.
    ///
    /// On Darwin, writing to a closed socket raises SIGPIPE, whose default
    /// action is to terminate. `SO_NOSIGPIPE` turns that into an `EPIPE`
    /// return instead — without it, a peer closing its laptop lid would take
    /// this app down with it.
    static func makeTCP() -> Int32 {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return fd }
        setOption(fd, SOL_SOCKET, SO_REUSEADDR)
        #if canImport(Darwin)
        setOption(fd, SOL_SOCKET, SO_NOSIGPIPE)
        #endif
        return fd
    }

    /// Write every byte, looping over short writes. `send` may legitimately
    /// accept less than it was given; treating a short write as success is a
    /// classic way to corrupt a length-prefixed stream.
    @discardableResult
    static func writeAll(_ fd: Int32, _ data: Data) -> Bool {
        var sent = 0
        return data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) -> Bool in
            guard let base = raw.baseAddress else { return true }
            while sent < data.count {
                #if canImport(Darwin)
                let n = send(fd, base.advanced(by: sent), data.count - sent, 0)
                #else
                let n = send(fd, base.advanced(by: sent), data.count - sent, Int32(MSG_NOSIGNAL))
                #endif
                if n > 0 { sent += n; continue }
                if n < 0 && errno == EINTR { continue }
                return false
            }
            return true
        }
    }

    /// IPv4 broadcast addresses of every active, non-loopback interface.
    ///
    /// Sending only to 255.255.255.255 is unreliable: some stacks refuse it,
    /// and with several interfaces up (Wi-Fi plus a USB-C dock, say) it may go
    /// out of the wrong one. Enumerating gives us the address that actually
    /// reaches the subnet the other machine is on.
    static func broadcastAddresses() -> [in_addr_t] {
        var found: [in_addr_t] = []
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0, let start = head else { return found }
        defer { freeifaddrs(head) }

        var cursor: UnsafeMutablePointer<ifaddrs>? = start
        while let entry = cursor {
            defer { cursor = entry.pointee.ifa_next }
            let flags = Int32(entry.pointee.ifa_flags)
            guard flags & IFF_UP != 0,
                  flags & IFF_LOOPBACK == 0,
                  flags & IFF_BROADCAST != 0,
                  let sa = entry.pointee.ifa_dstaddr,           // broadcast addr
                  sa.pointee.sa_family == sa_family_t(AF_INET)
            else { continue }
            let value = sa.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { $0.pointee.sin_addr.s_addr }
            if value != 0, !found.contains(value) { found.append(value) }
        }
        return found
    }
}
