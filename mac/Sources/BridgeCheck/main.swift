import Foundation
import QuackBridge
import QuackCastCore

// Dependency-free checks on the wire format, runnable with Command Line Tools
// alone (`swift run BridgeCheck`). The Python side runs the equivalent suite
// against the same vectors, which is how the two implementations are kept
// honest about the byte layout described in docs/PROTOCOL.md.

var failures = 0
var checks = 0

/// A throwing condition counts as a failure — an unexpected throw is exactly
/// the kind of thing these checks exist to catch.
func check(_ label: String, _ condition: @autoclosure () throws -> Bool) {
    checks += 1
    let passed = (try? condition()) ?? false
    print(passed ? "  ok   \(label)" : "  FAIL \(label)")
    if !passed { failures += 1 }
}

func section(_ name: String, _ body: () throws -> Void) {
    print(name)
    do { try body() } catch {
        failures += 1
        print("  FAIL unexpected error: \(error)")
    }
}

section("Framing") {
    let framed = Wire.encode(.control, Data("hi".utf8))
    check("header is 5 bytes + payload", framed.count == 7)
    check("length is big-endian", [UInt8](framed.prefix(4)) == [0, 0, 0, 2])
    check("type byte follows the length", framed[4] == 0x01)

    var reader = Wire.Reader()
    reader.append(framed)
    check("a whole message decodes", try reader.next()?.payload == Data("hi".utf8))
    check("the buffer is then empty", try reader.next() == nil)
}

section("Partial and batched reads") {
    // TCP splits and coalesces freely; the reader must cope with both.
    let a = Wire.encode(.control, Data("one".utf8))
    let b = Wire.encode(.frame, Data([0xFF, 0xD8, 0xFF]))
    var reader = Wire.Reader()

    reader.append(a.prefix(3))
    check("a header split mid-way yields nothing yet", try reader.next() == nil)
    reader.append(a.dropFirst(3))
    check("the rest completes it", try reader.next()?.payload == Data("one".utf8))

    reader.append(a + b)                      // two messages arriving in one read
    check("first of two batched messages", try reader.next()?.payload == Data("one".utf8))
    let second = try reader.next()
    check("second of two batched messages", second?.payload == Data([0xFF, 0xD8, 0xFF]))
    check("its type is preserved", second?.type == .frame)
    check("nothing is left over", try reader.next() == nil)
}

section("Refusing malformed streams") {
    var reader = Wire.Reader()
    // A length prefix claiming ~1 GiB: the allocation guard must catch it.
    reader.append(Data([0x40, 0x00, 0x00, 0x00, 0x01]))
    var threw = false
    do { _ = try reader.next() } catch { threw = true }
    check("an oversized length is rejected, not allocated", threw)

    var other = Wire.Reader()
    other.append(Data([0, 0, 0, 1, 0x99, 0x00]))
    var threwType = false
    do { _ = try other.next() } catch { threwType = true }
    check("an unknown message type is rejected", threwType)
}

section("Control envelope") {
    let data = Wire.encodeControl("handoff", payload: "https://example.com")
    var reader = Wire.Reader()
    reader.append(data)
    guard let message = try reader.next() else {
        check("control message framed", false); return
    }
    let envelope = Wire.decodeControl(message.payload)
    check("control round-trips", envelope?.control == "handoff")
    check("payload round-trips", envelope?.payload == "https://example.com")

    let json = String(data: message.payload, encoding: .utf8) ?? ""
    check("uses the same JSON keys as the Multipeer build",
          json.contains("\"control\"") && json.contains("\"payload\""))

    let empty = Wire.decodeControl(Data(#"{"control":"endCast"}"#.utf8))
    check("a missing payload decodes as nil", empty?.control == "endCast" && empty?.payload == nil)
}

section("Discovery beacon") {
    let beacon = Wire.Beacon(id: "ID-1", name: "swift-heron-3172", kind: "mac", port: 51234)
    let decoded = Wire.decodeBeacon(Wire.encodeBeacon(beacon))
    check("beacon round-trips", decoded == beacon)
    check("version is stamped", decoded?.qc == 1)

    check("a future protocol version is ignored",
          Wire.decodeBeacon(Data(#"{"qc":99,"id":"x","name":"n","kind":"mac","port":1}"#.utf8)) == nil)
    check("a beacon without a port is ignored",
          Wire.decodeBeacon(Data(#"{"qc":1,"id":"x","name":"n","kind":"mac","port":0}"#.utf8)) == nil)
    check("junk is ignored", Wire.decodeBeacon(Data("not json".utf8)) == nil)
}

section("Handoff URL safety") {
    check("https is allowed", Wire.safeHandoffURL("https://example.com/a?b=c") != nil)
    check("http is allowed", Wire.safeHandoffURL("http://example.com") != nil)
    check("surrounding whitespace is tolerated", Wire.safeHandoffURL("  https://example.com \n") != nil)

    // Each of these could reach a local resource or start a program if it
    // were handed to the OS opener, so the receiver must refuse them.
    check("file: is refused", Wire.safeHandoffURL("file:///etc/passwd") == nil)
    check("smb: is refused", Wire.safeHandoffURL("smb://server/share") == nil)
    check("javascript: is refused", Wire.safeHandoffURL("javascript:alert(1)") == nil)
    check("a custom app scheme is refused", Wire.safeHandoffURL("someapp://run") == nil)
    check("a bare path is refused", Wire.safeHandoffURL("/Applications/Calculator.app") == nil)
    check("a schemeless host is refused", Wire.safeHandoffURL("example.com") == nil)
    check("https with no host is refused", Wire.safeHandoffURL("https://") == nil)
    check("an absurdly long URL is refused",
          Wire.safeHandoffURL("https://example.com/" + String(repeating: "a", count: 5000)) == nil)
}

// The same fixtures the Python suite runs, so the two gesture classifiers
// cannot quietly disagree about what a fist is. A gesture that means
// different things on a Mac and a PC is worse than one that works on neither.
section("Shared gesture vectors (docs/gesture-vectors.json)") {
    let url = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()      // BridgeCheck
        .deletingLastPathComponent()      // Sources
        .deletingLastPathComponent()      // mac
        .deletingLastPathComponent()      // repo root
        .appendingPathComponent("docs/gesture-vectors.json")

    struct Fixture: Decodable {
        struct Hand: Decodable { let points: [String: [Double]]; let confidence: Double }
        struct Case: Decodable { let name: String; let hand: Hand; let expect: String }
        let cases: [Case]
    }

    let fixtures = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: url))
    check("fixtures are present", !fixtures.cases.isEmpty)

    let classifier = GestureClassifier()
    for fixture in fixtures.cases {
        var points: [HandJoint: Point2D] = [:]
        for (key, value) in fixture.hand.points {
            guard let raw = Int(key), let joint = HandJoint(rawValue: raw), value.count == 2 else { continue }
            points[joint] = Point2D(x: value[0], y: value[1])
        }
        let hand = HandLandmarks(points: points, confidence: fixture.hand.confidence)
        let got: String
        switch classifier.classify(hand) {
        case .openHand: got = "openHand"
        case .closedHand: got = "closedHand"
        case .peace: got = "peace"
        case .none: got = "none"
        }
        check("\(fixture.name) → \(fixture.expect)", got == fixture.expect)
    }
}

print("")
print("\(checks - failures)/\(checks) checks passed")
exit(failures == 0 ? 0 : 1)
