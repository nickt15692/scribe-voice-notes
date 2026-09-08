// swift-tools-version:5.9
//
// Tools version 5.9 on purpose: it keeps the package in Swift 5 language mode
// under the 6.x compiler, which spares a small AppKit app the strict-concurrency
// annotations that Swift 6 mode would demand for every NSMenu callback.
import PackageDescription

let package = Package(
    name: "Scribe",
    platforms: [.macOS(.v13)],          // SMAppService (launch at login) needs 13+
    targets: [
        .executableTarget(
            name: "Scribe",
            path: "Sources/Scribe"
        )
    ]
)
