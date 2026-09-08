import AppKit

// Menu bar app: no Dock icon, no app menu. LSUIElement in Info.plist does the
// same thing for a bundled launch; setting the policy here covers `swift run`.
let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let delegate = AppDelegate()
app.delegate = delegate
app.run()
