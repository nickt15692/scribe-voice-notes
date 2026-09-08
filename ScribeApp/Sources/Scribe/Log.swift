import Foundation

/// Append-only log at ~/.scribe/app.log.
///
/// A menu bar app has no terminal, so this is the only place a silent failure
/// can be seen. The engine's own output goes to scribe.log alongside it.
enum Log {
    static let dir: URL = {
        let d = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".scribe")
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }()

    static let file = dir.appendingPathComponent("app.log")
    private static let queue = DispatchQueue(label: "scribe.log")
    private static let stamp = ISO8601DateFormatter()

    static func write(_ message: String) {
        queue.async {
            let line = "\(stamp.string(from: Date())) \(message)\n"
            guard let data = line.data(using: .utf8) else { return }
            if let handle = try? FileHandle(forWritingTo: file) {
                handle.seekToEndOfFile()
                handle.write(data)
                try? handle.close()
            } else {
                try? data.write(to: file)
            }
        }
    }
}

/// "01:02:03" from seconds. Shared by the menu and the notification text.
func clock(_ seconds: Double) -> String {
    let s = max(0, Int(seconds.rounded()))
    return String(format: "%02d:%02d:%02d", s / 3600, (s / 60) % 60, s % 60)
}
