// swift-tools-version: 5.9
import PackageDescription

// The Mac half of the QuackCast bridge.
//
// It deliberately adds nothing to QuackCastCore: the gesture maths, the
// session state machine, device identity and the trust store are reused
// verbatim from the main project via a path dependency, so the two builds can
// never drift apart in how they decide things. What lives here is exactly one
// thing — a `PeerTransport` that speaks plain sockets instead of
// MultipeerConnectivity, because MultipeerConnectivity cannot reach Windows.
let package = Package(
    name: "QuackBridge",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "QuackBridge", targets: ["QuackBridge"]),
        // Headless peer: run it on the Mac and talk to a Windows PC without
        // building or launching the GUI app.
        .executable(name: "BridgeCLI", targets: ["BridgeCLI"]),
        // Protocol conformance checks, runnable with Command Line Tools alone.
        .executable(name: "BridgeCheck", targets: ["BridgeCheck"])
    ],
    dependencies: [
        // Sibling checkout of the main project. See README for the layout.
        .package(path: "../../QuackCast")
    ],
    targets: [
        .target(
            name: "QuackBridge",
            dependencies: [.product(name: "QuackCastCore", package: "QuackCast")]
        ),
        .executableTarget(
            name: "BridgeCLI",
            dependencies: [
                "QuackBridge",
                .product(name: "QuackCastCore", package: "QuackCast"),
                .product(name: "QuackCastPlatform", package: "QuackCast")
            ]
        ),
        .executableTarget(
            name: "BridgeCheck",
            dependencies: ["QuackBridge", .product(name: "QuackCastCore", package: "QuackCast")]
        )
    ]
)
