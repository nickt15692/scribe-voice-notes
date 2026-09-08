import AppKit
import UserNotifications

/// "Transcribed" banners, with a fallback for unsigned builds.
///
/// `UNUserNotificationCenter` refuses an ad-hoc-signed app outright —
/// `UNErrorDomain Code=1, "Notifications are not allowed for this application"`
/// — and no amount of LaunchServices re-registration changes that; it wants a
/// real signing identity. Since this app is built and run locally, the banner
/// falls back to AppleScript's `display notification`, which always works.
///
/// The cost of the fallback is that the banner can't carry a click action back
/// to us, so "clicking opens the transcript" is only true on the UN path. When
/// we're on the fallback, `AppDelegate` opens the file directly instead — same
/// end result, minus the click.
final class Notifier: NSObject, UNUserNotificationCenterDelegate {
    static let shared = Notifier()

    /// True once UN has authorised us. Until then every banner goes through
    /// AppleScript, which is also the permanent state for unsigned builds.
    private(set) var canUseNotificationCenter = false

    /// UN traps outright if the process has no bundle identifier, which is the
    /// case under a bare `swift run`.
    private var isBundled: Bool { Bundle.main.bundleIdentifier != nil }

    func setup() {
        guard isBundled else {
            Log.write("notifications: not bundled, using AppleScript fallback")
            return
        }
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { ok, err in
            self.canUseNotificationCenter = ok
            if ok {
                Log.write("notifications: granted (clickable)")
            } else {
                Log.write("notifications: unavailable, using AppleScript fallback"
                          + (err.map { " — \($0.localizedDescription)" } ?? ""))
            }
        }
    }

    func transcriptReady(_ job: JobInfo) {
        post(id: job.id,
             title: "Transcribed",
             body: "\(job.words) words · \(job.paragraphs) paragraphs · \(clock(job.duration))",
             path: job.written.first)
    }

    func failed(_ job: JobInfo) {
        let reason = job.error.isEmpty ? "See ~/.scribe/scribe.log" : job.error
        post(id: job.id, title: "Transcription failed", body: reason, path: nil)
    }

    private func post(id: String, title: String, body: String, path: String?) {
        guard canUseNotificationCenter else {
            postViaAppleScript(title: title, body: body)
            return
        }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        if let path = path { content.userInfo = ["path": path] }
        let request = UNNotificationRequest(identifier: id, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { err in
            if let err = err {
                Log.write("notification failed, falling back: \(err.localizedDescription)")
                self.postViaAppleScript(title: title, body: body)
            }
        }
    }

    private func postViaAppleScript(title: String, body: String) {
        // Both strings are interpolated into an AppleScript literal, so escape
        // backslashes first, then quotes, or a transcript containing a quote
        // would break the script.
        func escaped(_ s: String) -> String {
            s.replacingOccurrences(of: "\\", with: "\\\\")
             .replacingOccurrences(of: "\"", with: "\\\"")
        }
        let script = "display notification \"\(escaped(body))\" with title \"\(escaped(title))\""
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", script]
        do { try p.run() } catch { Log.write("osascript notification failed: \(error)") }
    }

    // Click → open the transcript. Only reachable on the UN path.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        if let path = response.notification.request.content.userInfo["path"] as? String {
            AppDelegate.openTranscript(path)
        }
        completionHandler()
    }

    // A menu bar app can count as "foreground"; show the banner anyway.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler:
                                    @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }
}
