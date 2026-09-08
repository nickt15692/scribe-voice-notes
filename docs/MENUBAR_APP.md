# The menu bar app

`Scribe.app` is a ~900-line native Swift app that records audio and drives the
Python engine. This is how to build it, change it, and debug it.

For the Python side see [ARCHITECTURE.md](ARCHITECTURE.md). For the traps that
apply across the whole project, [DEVELOPMENT.md](DEVELOPMENT.md).

## What it is, and what it deliberately isn't

It is a **client**, not a rewrite. It captures audio natively and then POSTs to
the same `/api/upload` the browser uses. The transcription pipeline — silence
planning, cleaning, checkpoints, exports — has no idea which client it's
serving, so a server feature reaches both for free.

Two things drove that shape:

- **The microphone.** Wrapping the web UI in a `WKWebView` would have been less
  code, but `getUserMedia` inside a webview needs the bundle signed and
  entitled before macOS grants mic access. Native `AVAudioRecorder` capture
  avoids the entitlement problem entirely, and the Python side never needed to
  change.
- **Not re-implementing the pipeline.** A full native rewrite (WhisperKit)
  would have meant rebuilding the chunk planner, cleaner, exports and
  persistence, and abandoning the tests that cover them.

## Build

```bash
cd ScribeApp && ./build.sh          # → ../Scribe.app
```

No Xcode. `swift build` from the Command Line Tools produces the binary and
`build.sh` lays out the bundle by hand: binary, `Info.plist`, `Scribe.icns`,
then an ad-hoc `codesign` and a `touch` to make LaunchServices re-read it.

Requires Swift 5.9+ and macOS 13+ (`SMAppService`, for launch at login).
`Package.swift` pins tools version 5.9 on purpose — it keeps the package in
Swift 5 language mode under a 6.x compiler, which spares a small AppKit app the
strict-concurrency annotations Swift 6 mode would demand of every menu callback.

## The files

| File | Lines | Responsibility |
|---|---:|---|
| `AppDelegate.swift` | 406 | The status item, the menu, and the state machine. Everything else is called from here. |
| `Server.swift` | 123 | Owns the Python engine: locates the project, spawns `start.sh`, health-polls, shuts it down. |
| `Notifier.swift` | 108 | Banners, with an AppleScript fallback for unsigned builds. |
| `API.swift` | 99 | `URLSession` client and the `Decodable` wire types. |
| `Recorder.swift` | ~190 | `AVCaptureSession` → 16 kHz mono WAV, bound to a chosen input device. |
| `Log.swift` | 37 | Append-only log at `~/.scribe/app.log`, plus `clock()`. |
| `main.swift` | 10 | `NSApplication` bootstrap. |

## State machine

`AppDelegate.State` moves forward through one capture and never sideways:

```
starting ──▶ idle ──▶ recording ──▶ uploading ──▶ transcribing ──▶ idle
    │
    └──▶ serverDown          (terminal; the engine never came up)
```

Every mutation goes through `setState`, which hops to the main thread and calls
`render()`. `render()` is the single place that touches menu titles and the
status icon — network and recorder callbacks arrive on arbitrary queues, so
nothing else may touch AppKit directly.

## One capture, end to end

1. **Start** — `Recorder.requestAccess` resolves the mic permission (the macOS
   prompt appears on first use only), then an `AVCaptureSession` bound to the
   selected device writes `~/.scribe/capture/note-<timestamp>.wav`.
2. **Recording** — 16 kHz mono 16-bit, chosen because it is Whisper's native
   input, so the engine's ffmpeg step is nearly a no-op. That is ~32 KB/s, or
   115 MB/hour, streamed to disk. Nothing accumulates in memory.
3. **Stop** — `Recorder.stop()` only *asks* the session to stop. The file is
   not complete until `AVCaptureFileOutputRecordingDelegate` fires, which is
   what calls `onFinish` → `AppDelegate.uploadCapture`. Never read the capture
   file straight after `stop()`. Then `API.upload` assembles the multipart body *in a temp file* and
   sends it with `uploadTask(fromFile:)`, so a two-hour recording is never held
   in RAM. The capture is deleted once the server has it.
4. **Poll** — `AppDelegate.poll` hits `/api/jobs` every 2 s until the job is
   `done` or `failed`, updating the status line from `job.stage`.
5. **Finish** — a notification, and the cleaned `.md` opens.

Measured: 23.2 minutes of audio transcribed in 1.7 minutes across 124 chunks,
with the engine at 37 MB RSS throughout.

## Server lifecycle

The app is the parent process. `Server.startIfNeeded` first checks whether
something already answers on 8765 — if so it adopts it rather than starting a
second one. Otherwise it spawns `/bin/bash start.sh` with `SCRIBE_NO_OPEN=1`
and polls for up to 90 s, which is generous because a first run may be
pip-installing.

The project root is the bundle's parent directory, falling back to `~/scribe`
if the app has been moved to `/Applications`.

`applicationShouldTerminate` returns `.terminateLater`, POSTs `/api/quit` for a
graceful shutdown, and `terminate()`s the child only if it is still alive after
3 s. That ordering matters: a hard kill would abandon an in-flight chunk, and
while the checkpoint would recover it, the graceful path avoids the situation.

## Making changes

**Add a menu item.** Declare an `NSMenuItem` as a property, set `.target = self`
in `buildMenu()`, add it to the menu, and give it an `@objc` handler. If it has
a checkmark, set its `.state` inside `render()` rather than in the handler, so
one function owns the visible state.

**Add a preference.** Follow `selectedPreset` / `openWhenReady`: a computed
property over `UserDefaults`. Use `object(forKey:) as? Bool ?? true` for
booleans that default to on — `bool(forKey:)` returns `false` for an unset key,
which silently inverts your default.

**Change where transcripts open.** Everything goes through
`AppDelegate.openTranscript(_:)` — the auto-open after a job, the Recent menu,
and the notification click all call it, so add new open sites there rather than
calling `NSWorkspace` directly. It reads the `transcriptAppPath` default: empty
means the system handler, otherwise `NSWorkspace.open(_:withApplicationAt:)`
with a fallback to the system handler if that app has since been moved or
uninstalled. The **Open Transcripts In** submenu is rebuilt by `refreshOpenWith`
from `NSWorkspace.urlsForApplications(toOpen:)` for the markdown UTType.

That list needs filtering: macOS reports every browser as markdown-capable, and
on a machine with Edge installed it also returns stale per-user copies under
`Library/Application Support`. Both are excluded, or the submenu fills with
things you would never pick.

**Talk to a new endpoint.** Add a `Decodable` type and a wrapper in `API.swift`.
`JobInfo` decodes `/api/jobs`; adding a field to `Job.as_dict()` in
`scribe/jobs.py` is safe, but *removing* one makes the whole decode return nil
and the app will report the job as vanished.

**Add a device-related feature.** `Recorder.inputDevices()` enumerates inputs,
`selectedDevice()` resolves the saved `inputDeviceID` (empty = system default,
and also the fallback when a pinned device is not connected). The submenu is
rebuilt in `menuWillOpen` rather than at launch, because headsets appear and
disappear while the app runs.

**Change the audio format.** `Recorder.start`'s settings dictionary. Anything
ffmpeg can read works, since the engine transcodes anyway — but 16 kHz mono is
what Whisper wants, so raising it only costs disk.

## Gotchas

**Notifications don't work on unsigned builds.**
`UNUserNotificationCenter.requestAuthorization` fails with `UNErrorDomain
Code=1` for an ad-hoc-signed app, and LaunchServices re-registration does not
help — it wants a real Developer ID ($99/yr). `Notifier` falls back to
`osascript display notification`, which works but cannot carry a click action.
That is precisely why **Open Transcript When Ready** exists and defaults to on:
`AppDelegate` opens the file directly on the fallback path. Check
`Notifier.canUseNotificationCenter` before assuming a click will arrive.

**Every rebuild re-prompts for the microphone.** Ad-hoc signatures change on
each build, so macOS treats the result as a new app. Normal use — binary
unchanged — prompts once.

**The icon must be a build input.** It lives at `ScribeApp/Scribe.icns` and is
copied in by `build.sh`. It previously existed only inside the bundle, so
anything that cleaned `Contents/Resources/` left the app silently iconless.

**The chromeless window is Chromium-only.** `openLibrary` prefers your *default*
browser when it supports `--app=`, and falls back to a hardcoded Chromium list,
then to an ordinary tab. Safari and Firefox have no equivalent flag, so with
either as default the library opens as a normal tab. Don't pass `--app=` to an
arbitrary default browser; Safari chokes on it.

**Scribe's transcript app is deliberately not the system default.**
`transcriptAppPath` only affects this app. Changing the system-wide `.md`
handler instead would redirect every markdown file on the machine, including
every README in every repo — usually not what someone wants when their global
default is an editor.

**`SMAppService` registers a bundle path.** Toggle Launch at Login, then move
`Scribe.app`, and the login item points at nothing. Re-toggle after moving it.

**Only one job runs at a time.** The engine has a single worker thread, so
submitting two recordings back-to-back queues them; the second waits. This is
correct for one person talking, but it means a double-submit doubles the wait
rather than parallelising.

**A disconnected mic ends the recording, but not silently.** `Recorder`
observes `AVCaptureDeviceWasDisconnected`; if the device in use vanishes it
stops the session, alerts, and still hands the partial file over to be
transcribed. Losing a headset mid-sentence keeps what was captured.

**`build.sh` must not swallow the compiler's exit status.** An earlier version
piped `swift build` through `grep`, which discarded the status — so a failed
compile left the previous binary in place and the script reported success,
shipping an app that silently hadn't changed. It now removes the binary first
and honours the exit code.

**Unverified.** What happens if the Mac sleeps mid-recording. Plausible on a
long session and not handled explicitly.

## Debugging

`~/.scribe/app.log` is the app's own log — it is the only place a silent
failure shows up, since a menu bar app has no terminal. `~/.scribe/scribe.log`
is the engine's stdout, rewritten on each launch.

A useful first line of triage:

```bash
tail -5 ~/.scribe/app.log
pgrep -x Scribe && pgrep -f "uvicorn scribe.app:app"
```

If the app is running but the engine isn't, `startIfNeeded` timed out — the
reason will be in `scribe.log`.
