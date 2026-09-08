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
swift build -c release 2>&1 | grep -v '^\[' | grep -v '^$' || true
BIN=".build/release/Scribe"
[ -x "$BIN" ] || { echo "build failed: $BIN not produced" >&2; exit 1; }

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
