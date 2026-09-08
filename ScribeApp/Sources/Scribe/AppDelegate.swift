import AppKit
import ServiceManagement
import UniformTypeIdentifiers

/// The status item, its menu, and the state machine behind them.
///
/// States move strictly forward through one capture:
///   starting → idle → recording → uploading → transcribing → idle
/// with `serverDown` as the one terminal failure. Everything that touches the
/// menu goes through `setState`/`render` on the main thread; the network and
/// recorder callbacks arrive on arbitrary queues.
final class AppDelegate: NSObject, NSApplicationDelegate {
    private enum State {
        case starting, idle, recording, uploading, transcribing(String), serverDown
    }

    private let server = Server()
    private let recorder = Recorder()
    private var statusItem: NSStatusItem!
    private var state: State = .starting
    private var presets: [String: Preset] = [:]
    private var recent: [JobInfo] = []
    private var stoppingServer = false
    private var tick: Timer?

    private var selectedPreset: String {
        get { UserDefaults.standard.string(forKey: "preset") ?? "accurate" }
        set { UserDefaults.standard.set(newValue, forKey: "preset") }
    }

    /// Which app opens a transcript. Empty/absent means "whatever macOS uses
    /// for .md". Scoped to this app on purpose: changing the system-wide
    /// handler would also redirect every .md in every code repo, which is
    /// rarely what you want when the global default is an editor.
    private static let transcriptAppKey = "transcriptAppPath"

    /// The one place a transcript gets opened. `Notifier` calls this too, so
    /// the click path and the auto-open path can't drift apart.
    static func openTranscript(_ path: String) {
        let file = URL(fileURLWithPath: path)
        let chosen = UserDefaults.standard.string(forKey: transcriptAppKey) ?? ""

        guard !chosen.isEmpty, FileManager.default.fileExists(atPath: chosen) else {
            NSWorkspace.shared.open(file)          // system default
            return
        }
        NSWorkspace.shared.open([file],
                                withApplicationAt: URL(fileURLWithPath: chosen),
                                configuration: NSWorkspace.OpenConfiguration()) { _, err in
            if let err = err {
                Log.write("open in chosen app failed, falling back: \(err.localizedDescription)")
                NSWorkspace.shared.open(file)
            }
        }
    }

    /// Open the cleaned .md as soon as a job finishes. Defaults to on, which is
    /// what makes "notify and open" true on unsigned builds: the AppleScript
    /// fallback banner can't carry a click action back to us, so the open has
    /// to happen here instead of on a tap.
    private var openWhenReady: Bool {
        get { UserDefaults.standard.object(forKey: "openWhenReady") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "openWhenReady") }
    }

    // MARK: menu items

    private let toggleItem = NSMenuItem(title: "Start Recording",
                                        action: #selector(toggleRecording), keyEquivalent: "")
    private let statusLine = NSMenuItem(title: "Starting engine…", action: nil, keyEquivalent: "")
    private let modelItem = NSMenuItem(title: "Model", action: nil, keyEquivalent: "")
    private let recentItem = NSMenuItem(title: "Recent", action: nil, keyEquivalent: "")
    private let loginItem = NSMenuItem(title: "Launch at Login",
                                       action: #selector(toggleLogin), keyEquivalent: "")
    private let openItem = NSMenuItem(title: "Open Transcript When Ready",
                                      action: #selector(toggleOpenWhenReady), keyEquivalent: "")
    private let openWithItem = NSMenuItem(title: "Open Transcripts In", action: nil, keyEquivalent: "")

    // MARK: lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.menu = buildMenu()
        Log.write("status item installed")

        Notifier.shared.setup()
        render()

        server.startIfNeeded { ok in
            self.setState(ok ? .idle : .serverDown)
            if ok {
                self.refreshPresets()
                self.refreshRecent()
            }
        }

        tick = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            self?.tickElapsed()
        }
    }

    // Cmd-Q, logout and the menu's Quit all land here. The engine is a child of
    // this process, so it is stopped gracefully before the app goes away.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if stoppingServer { return .terminateNow }
        stoppingServer = true
        if recorder.isRecording { _ = recorder.stop() }
        server.stop {
            DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }

    // MARK: menu

    private func buildMenu() -> NSMenu {
        let menu = NSMenu()
        menu.autoenablesItems = false

        toggleItem.target = self
        loginItem.target = self
        openItem.target = self
        statusLine.isEnabled = false

        modelItem.submenu = NSMenu()
        recentItem.submenu = NSMenu()
        openWithItem.submenu = NSMenu()
        refreshOpenWith()

        let library = NSMenuItem(title: "Open Library", action: #selector(openLibrary), keyEquivalent: "")
        library.target = self
        let quit = NSMenuItem(title: "Quit Scribe", action: #selector(quit), keyEquivalent: "q")
        quit.target = self

        menu.addItem(toggleItem)
        menu.addItem(statusLine)
        menu.addItem(.separator())
        menu.addItem(modelItem)
        menu.addItem(recentItem)
        menu.addItem(library)
        menu.addItem(.separator())
        menu.addItem(openItem)
        menu.addItem(openWithItem)
        menu.addItem(loginItem)
        menu.addItem(quit)
        return menu
    }

    private func setState(_ new: State) {
        DispatchQueue.main.async {
            self.state = new
            self.render()
        }
    }

    private func render() {
        switch state {
        case .starting:
            icon("waveform")
            toggleItem.title = "Start Recording"
            toggleItem.isEnabled = false
            statusLine.title = "Starting engine…"
        case .serverDown:
            icon("exclamationmark.triangle")
            toggleItem.title = "Start Recording"
            toggleItem.isEnabled = false
            statusLine.title = "Engine failed to start — see ~/.scribe/scribe.log"
        case .idle:
            icon("waveform")
            toggleItem.title = "Start Recording"
            toggleItem.isEnabled = true
            statusLine.title = "Ready · \(presets[selectedPreset]?.label ?? selectedPreset)"
        case .recording:
            icon("record.circle.fill", tint: .systemRed)
            toggleItem.title = "Stop Recording"
            toggleItem.isEnabled = true
            statusLine.title = "Recording · 00:00:00"
        case .uploading:
            icon("hourglass")
            toggleItem.title = "Stop Recording"
            toggleItem.isEnabled = false
            statusLine.title = "Handing off…"
        case .transcribing:
            icon("hourglass")
            toggleItem.title = "Start Recording"
            toggleItem.isEnabled = false
            statusLine.title = "Transcribing…"
        }
        loginItem.state = SMAppService.mainApp.status == .enabled ? .on : .off
        openItem.state = openWhenReady ? .on : .off
        modelItem.isEnabled = !presets.isEmpty
        recentItem.isEnabled = !recent.isEmpty
    }

    private func icon(_ symbol: String, tint: NSColor? = nil) {
        guard let base = NSImage(systemSymbolName: symbol, accessibilityDescription: "Scribe") else { return }
        if let tint = tint {
            let config = NSImage.SymbolConfiguration(paletteColors: [tint])
            let tinted = base.withSymbolConfiguration(config) ?? base
            tinted.isTemplate = false
            statusItem.button?.image = tinted
        } else {
            base.isTemplate = true            // follows the menu bar's light/dark
            statusItem.button?.image = base
        }
    }

    private func tickElapsed() {
        guard case .recording = state else { return }
        statusLine.title = "Recording · \(clock(recorder.elapsed))"
    }

    // MARK: recording

    @objc private func toggleRecording() {
        switch state {
        case .idle: startRecording()
        case .recording: stopRecording()
        default: break
        }
    }

    private func startRecording() {
        Recorder.requestAccess { granted in
            guard granted else {
                Log.write("microphone permission denied")
                self.alert("Microphone access is off",
                           "Allow Scribe under System Settings → Privacy & Security → Microphone, then try again.")
                return
            }
            do {
                try self.recorder.start()
                self.setState(.recording)
            } catch {
                Log.write("could not start recording: \(error)")
                self.alert("Couldn't start recording", "\(error)")
            }
        }
    }

    private func stopRecording() {
        guard let file = recorder.stop() else { return }
        setState(.uploading)
        API.upload(file: file, preset: selectedPreset) { result in
            switch result {
            case .success(let job):
                Log.write("uploaded as job \(job.id)")
                try? FileManager.default.removeItem(at: file)    // the engine has its own copy
                self.setState(.transcribing(job.id))
                self.poll(job.id)
            case .failure(let err):
                Log.write("upload failed: \(err) — capture kept at \(file.path)")
                self.setState(.idle)
                self.alert("Upload failed", "The recording is saved at \(file.path).\n\n\(err)")
            }
        }
    }

    // Two-second poll of /api/jobs until the job settles. Cheap on localhost,
    // and no socket to keep alive across a 20-minute transcription.
    private func poll(_ jobID: String) {
        DispatchQueue.global().asyncAfter(deadline: .now() + 2) {
            API.jobs { jobs in
                guard let job = jobs.first(where: { $0.id == jobID }) else {
                    Log.write("job \(jobID) vanished while polling")
                    self.setState(.idle)
                    return
                }
                switch job.status {
                case "done":
                    Log.write("job \(jobID) done: \(job.words) words, \(job.paragraphs) paragraphs")
                    Notifier.shared.transcriptReady(job)
                    // On the AppleScript fallback the banner isn't clickable,
                    // so open the transcript here instead. Skipped when the
                    // notification itself can carry the click.
                    if self.openWhenReady, !Notifier.shared.canUseNotificationCenter,
                       let path = job.written.first {
                        DispatchQueue.main.async {
                            AppDelegate.openTranscript(path)
                        }
                    }
                    self.setState(.idle)
                    self.refreshRecent()
                case "failed":
                    Log.write("job \(jobID) failed: \(job.error)")
                    Notifier.shared.failed(job)
                    self.setState(.idle)
                    self.refreshRecent()
                default:
                    DispatchQueue.main.async {
                        self.statusLine.title = "Transcribing · \(job.stage)"
                    }
                    self.poll(jobID)
                }
            }
        }
    }

    // MARK: submenus

    private func refreshPresets() {
        API.presets { presets in
            DispatchQueue.main.async {
                self.presets = presets
                if presets[self.selectedPreset] == nil, let first = presets.keys.sorted().first {
                    self.selectedPreset = first
                }
                let menu = self.modelItem.submenu!
                menu.removeAllItems()
                for key in presets.keys.sorted() {
                    let item = NSMenuItem(title: presets[key]!.label,
                                          action: #selector(self.pickPreset(_:)), keyEquivalent: "")
                    item.target = self
                    item.representedObject = key
                    item.state = key == self.selectedPreset ? .on : .off
                    menu.addItem(item)
                }
                self.render()
            }
        }
    }

    @objc private func pickPreset(_ sender: NSMenuItem) {
        guard let key = sender.representedObject as? String else { return }
        selectedPreset = key
        for item in modelItem.submenu?.items ?? [] {
            item.state = (item.representedObject as? String) == key ? .on : .off
        }
        render()
    }

    private func refreshRecent() {
        API.jobs { jobs in
            DispatchQueue.main.async {
                self.recent = Array(jobs.filter { $0.status == "done" && !$0.written.isEmpty }.prefix(5))
                let menu = self.recentItem.submenu!
                menu.removeAllItems()
                for job in self.recent {
                    let item = NSMenuItem(title: "\(job.name) · \(job.words) words",
                                          action: #selector(self.openRecent(_:)), keyEquivalent: "")
                    item.target = self
                    item.representedObject = job.written[0]
                    menu.addItem(item)
                }
                self.render()
            }
        }
    }

    @objc private func openRecent(_ sender: NSMenuItem) {
        guard let path = sender.representedObject as? String else { return }
        AppDelegate.openTranscript(path)
    }

    // MARK: other actions

    /// Browsers that support Chromium's `--app=` flag, which is what gives a
    /// chromeless window with its own Dock entry. Safari and Firefox have no
    /// equivalent, so with either of those the library opens as a normal tab.
    private static let chromiumBrowsers = [
        "Google Chrome", "Brave Browser", "Microsoft Edge", "Vivaldi", "Chromium",
    ]

    /// The Chromium browser to use, preferring whichever one you've set as your
    /// default — opening the library in a browser you don't use is worse than
    /// losing the chromeless window.
    private func chromiumExecutable() -> URL? {
        var candidates: [String] = []
        if let def = NSWorkspace.shared.urlForApplication(toOpen: URL(string: "https://example.com")!) {
            candidates.append(def.deletingPathExtension().lastPathComponent)
        }
        candidates += AppDelegate.chromiumBrowsers

        for name in candidates where AppDelegate.chromiumBrowsers.contains(name) {
            let bin = "/Applications/\(name).app/Contents/MacOS/\(name)"
            if FileManager.default.isExecutableFile(atPath: bin) {
                return URL(fileURLWithPath: bin)
            }
        }
        return nil          // Safari/Firefox default, or nothing Chromium installed
    }

    @objc private func openLibrary() {
        let url = Server.baseURL
        if let bin = chromiumExecutable() {
            let p = Process()
            p.executableURL = bin
            p.arguments = ["--app=\(url.absoluteString)"]
            if (try? p.run()) != nil {
                Log.write("library opened in \(bin.lastPathComponent) (app window)")
                return
            }
        }
        Log.write("library opened in the default browser (tab)")
        NSWorkspace.shared.open(url)      // Safari, Firefox, or launch failure
    }

    /// Apps macOS says can handle markdown, with TextEdit guaranteed present.
    private func refreshOpenWith() {
        guard let menu = openWithItem.submenu else { return }
        menu.removeAllItems()
        let chosen = UserDefaults.standard.string(forKey: AppDelegate.transcriptAppKey) ?? ""

        let systemDefault = NSMenuItem(title: "Default App",
                                       action: #selector(pickTranscriptApp(_:)), keyEquivalent: "")
        systemDefault.target = self
        systemDefault.representedObject = ""
        systemDefault.state = chosen.isEmpty ? .on : .off
        menu.addItem(systemDefault)
        menu.addItem(.separator())

        let type = UTType(filenameExtension: "md") ?? .plainText
        var seen = Set<String>()
        var apps = NSWorkspace.shared.urlsForApplications(toOpen: type)

        let textEdit = URL(fileURLWithPath: "/System/Applications/TextEdit.app")
        if !apps.contains(textEdit) { apps.insert(textEdit, at: 0) }

        for app in apps {
            // Skip browsers and the per-user updater copies of Edge, which are
            // technically registered for .md but never what you want.
            let name = app.deletingPathExtension().lastPathComponent
            if app.path.contains("/Library/Application Support/") { continue }
            if AppDelegate.chromiumBrowsers.contains(name) || name == "Safari" { continue }
            if !seen.insert(name).inserted { continue }

            let item = NSMenuItem(title: name,
                                  action: #selector(pickTranscriptApp(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = app.path
            item.state = (chosen == app.path) ? .on : .off
            menu.addItem(item)
            if menu.numberOfItems > 10 { break }
        }
    }

    @objc private func pickTranscriptApp(_ sender: NSMenuItem) {
        let path = (sender.representedObject as? String) ?? ""
        UserDefaults.standard.set(path, forKey: AppDelegate.transcriptAppKey)
        Log.write("transcripts will open in: \(path.isEmpty ? "system default" : path)")
        refreshOpenWith()
    }

    @objc private func toggleOpenWhenReady() {
        openWhenReady.toggle()
        render()
    }

    @objc private func toggleLogin() {
        let service = SMAppService.mainApp
        do {
            if service.status == .enabled { try service.unregister() } else { try service.register() }
            Log.write("launch at login: \(service.status == .enabled ? "on" : "off")")
        } catch {
            Log.write("launch at login failed: \(error)")
            alert("Couldn't change login item", "\(error)")
        }
        render()
    }

    @objc private func quit() {
        if recorder.isRecording {
            NSApp.activate(ignoringOtherApps: true)
            let a = NSAlert()
            a.messageText = "Still recording"
            a.informativeText = "Stop recording first to keep this session, or quit and discard it."
            a.addButton(withTitle: "Cancel")
            a.addButton(withTitle: "Quit and Discard")
            if a.runModal() == .alertFirstButtonReturn { return }
        }
        NSApp.terminate(nil)
    }

    private func alert(_ title: String, _ text: String) {
        DispatchQueue.main.async {
            NSApp.activate(ignoringOtherApps: true)
            let a = NSAlert()
            a.messageText = title
            a.informativeText = text
            a.runModal()
        }
    }
}
