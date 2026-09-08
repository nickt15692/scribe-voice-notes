import Foundation

/// Owns the Python engine as a child process.
///
/// The app doesn't reimplement any of the pipeline; it starts `start.sh` — the
/// same launcher a terminal would use — and talks to it over localhost. That
/// keeps one code path for venv setup, dependency installs and PATH, and means
/// the web UI is still there as the library view.
final class Server {
    static let port = 8765
    static let baseURL = URL(string: "http://127.0.0.1:\(port)")!

    let projectRoot: URL
    private var process: Process?

    init() {
        // The bundle lives in the project root, so start.sh is one level up
        // from Scribe.app. Fall back to ~/scribe if the bundle was moved.
        let fm = FileManager.default
        let beside = Bundle.main.bundleURL.deletingLastPathComponent()
        let home = fm.homeDirectoryForCurrentUser.appendingPathComponent("scribe")
        if fm.isExecutableFile(atPath: beside.appendingPathComponent("start.sh").path) {
            projectRoot = beside
        } else {
            projectRoot = home
        }
        Log.write("project root: \(projectRoot.path)")
    }

    // MARK: health

    func isUp(_ completion: @escaping (Bool) -> Void) {
        var req = URLRequest(url: Server.baseURL.appendingPathComponent("api/jobs"))
        req.timeoutInterval = 2
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            completion((resp as? HTTPURLResponse)?.statusCode == 200)
        }.resume()
    }

    /// Start the engine unless something is already answering on the port.
    /// `onReady(false)` after 90 s — a first run may be pip-installing.
    func startIfNeeded(_ onReady: @escaping (Bool) -> Void) {
        isUp { up in
            if up {
                Log.write("server already running; adopting it")
                onReady(true)
                return
            }
            self.spawn()
            self.waitUntilUp(deadline: Date().addingTimeInterval(90), onReady)
        }
    }

    private func spawn() {
        let script = projectRoot.appendingPathComponent("start.sh")
        guard FileManager.default.isExecutableFile(atPath: script.path) else {
            Log.write("start.sh not found at \(script.path)")
            return
        }

        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [script.path]
        p.currentDirectoryURL = projectRoot

        var env = ProcessInfo.processInfo.environment
        env["SCRIBE_NO_OPEN"] = "1"                 // the app is the UI now
        env["SCRIBE_PORT"] = String(Server.port)
        p.environment = env

        // Fresh log per launch, same file the old launcher used.
        let logURL = Log.dir.appendingPathComponent("scribe.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        if let handle = try? FileHandle(forWritingTo: logURL) {
            p.standardOutput = handle
            p.standardError = handle
        }

        do {
            try p.run()
            process = p
            Log.write("server spawned, pid \(p.processIdentifier)")
        } catch {
            Log.write("server spawn failed: \(error)")
        }
    }

    private func waitUntilUp(deadline: Date, _ onReady: @escaping (Bool) -> Void) {
        isUp { up in
            if up {
                Log.write("server ready")
                onReady(true)
            } else if Date() > deadline {
                Log.write("server did not come up before deadline")
                onReady(false)
            } else {
                DispatchQueue.global().asyncAfter(deadline: .now() + 1) {
                    self.waitUntilUp(deadline: deadline, onReady)
                }
            }
        }
    }

    // MARK: shutdown

    /// Graceful first — the same /api/quit the web UI uses, which lets an
    /// in-flight job checkpoint — then terminate the child if it lingers.
    func stop(_ completion: @escaping () -> Void) {
        var req = URLRequest(url: Server.baseURL.appendingPathComponent("api/quit"))
        req.httpMethod = "POST"
        req.timeoutInterval = 3
        URLSession.shared.dataTask(with: req) { _, _, _ in
            DispatchQueue.global().asyncAfter(deadline: .now() + 3) {
                if let p = self.process, p.isRunning {
                    Log.write("server still running after quit; terminating")
                    p.terminate()
                }
                Log.write("server stopped")
                completion()
            }
        }.resume()
    }
}
