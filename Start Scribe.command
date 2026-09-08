#!/bin/sh
#
# Double-click this to start scribe.
#
# macOS opens .command files in Terminal, which is the point: Terminal already
# has permission to read this folder, whereas an unsigned .app launched from
# Finder does not — ~/Downloads is one of the protected locations, and the app
# gets "Operation not permitted" before it can run anything.
#
# The window that opens is where the server lives. Closing it, or ctrl-c,
# stops the server — same as pressing Quit in the browser.

DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
exec "$DIR/start.sh"
