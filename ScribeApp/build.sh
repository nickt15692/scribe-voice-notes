#!/usr/bin/env bash
#
# Build the menu bar app and assemble ../Scribe.app.
#
# No Xcode involved: `swift build` from the Command Line Tools produces the
# binary, and the bundle is laid out by hand. The existing Scribe.icns in
# Resources/ is kept.
#
# The bundle is ad-hoc signed (`-s -`). That is enough for macOS to attach the
# microphone permission to it, but the signature changes every build, so a
# rebuild can re-prompt for the mic. Normal use — binary unchanged — prompts once.

set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP="$HERE/../Scribe.app"

cd "$HERE"
BIN=".build/release/Scribe"

# The binary is removed first and the compiler's exit status is honoured. An
# earlier version piped through grep and swallowed the status, so a failed
# compile left the previous binary in place and the script cheerfully reported
# success — shipping an app that silently hadn't changed.
rm -f "$BIN"
if ! swift build -c release; then
    echo "build failed" >&2
    exit 1
fi
[ -x "$BIN" ] || { echo "build produced no binary at $BIN" >&2; exit 1; }

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/Scribe"
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"

# The icon is copied from the source tree every build. It used to live only
# inside the bundle, which meant anything that cleaned Resources/ silently left
# the app iconless until someone noticed in the Dock.
if [ -f "$HERE/Scribe.icns" ]; then
    cp "$HERE/Scribe.icns" "$APP/Contents/Resources/Scribe.icns"
else
    echo "warning: ScribeApp/Scribe.icns missing — app will have no icon" >&2
fi

codesign --force --sign - "$APP" 2>&1 | grep -v 'replacing existing signature' || true
touch "$APP"                     # nudge LaunchServices to re-read the bundle

echo "built: $APP"
codesign -dv "$APP" 2>&1 | grep -E '^(Identifier|Signature)' | sed 's/^/  /'
