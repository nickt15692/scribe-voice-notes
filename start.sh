#!/usr/bin/env bash
#
# Start scribe. Run it from anywhere, run it as often as you like — it sets up
# whatever is missing and then starts the server.
#
#   ./start.sh            start on http://localhost:8765
#   SCRIBE_PORT=9000 ./start.sh
#
# `run.sh` is the bare uvicorn line underneath this, for when you already have
# the venv active and don't want the checks.

set -euo pipefail

# Resolve this script's own directory rather than trusting the working one, so
# the launcher survives being run from anywhere — and so the folder can be
# renamed, moved, or contain spaces without any of this breaking.
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# Finder hands a double-clicked app a bare PATH — /usr/bin:/bin and little else
# — so Homebrew is invisible and ffmpeg isn't found. Python inherits this too,
# so it matters for every ffmpeg call during transcription, not just the check.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

PORT="${SCRIBE_PORT:-8765}"
VENV="$HERE/.venv"
URL="http://localhost:$PORT"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

# --- ffmpeg ------------------------------------------------------------------
command -v ffmpeg >/dev/null 2>&1 || die \
"ffmpeg is required and isn't installed.

    brew install ffmpeg"

# --- already running? --------------------------------------------------------
# Otherwise uvicorn dies on a bound port with a traceback that looks like a bug.
if lsof -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
    say "scribe is already running on $URL"
    open "$URL" 2>/dev/null || true
    exit 0
fi

# --- interpreter -------------------------------------------------------------
# Homebrew first and by explicit version: MLX wants 3.11+, and a bare `python3`
# on this machine is Anaconda's, which is a rockier path for MLX wheels.
pick_python() {
    for candidate in \
        /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.12 \
        /opt/homebrew/bin/python3.13 /usr/local/bin/python3.11 \
        python3.11 python3.12 python3.13 python3
    do
        path="$(command -v "$candidate" 2>/dev/null)" || continue
        if "$path" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)' 2>/dev/null; then
            printf '%s' "$path"
            return 0
        fi
    done
    return 1
}

# --- venv --------------------------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
    PYTHON="$(pick_python)" || die \
"No Python 3.11 or newer found, which MLX needs.

    brew install python@3.11"
    say "Creating virtualenv with $PYTHON"
    "$PYTHON" -m venv "$VENV"
    "$VENV/bin/python" -m pip install --upgrade pip --quiet
fi

# --- dependencies ------------------------------------------------------------
# Keyed on a hash of requirements.txt, so editing that file reinstalls on the
# next start and an unchanged one costs nothing.
STAMP="$VENV/.requirements-sha"
WANT="$(shasum "$HERE/requirements.txt" | awk '{print $1}')"
HAVE="$(cat "$STAMP" 2>/dev/null || true)"

if [ "$WANT" != "$HAVE" ]; then
    say "Installing dependencies (first run pulls MLX and torch — a few minutes)"
    "$VENV/bin/python" -m pip install -r "$HERE/requirements.txt"
    printf '%s' "$WANT" > "$STAMP"
fi

# --- go ----------------------------------------------------------------------
# The lsof check above only catches a server that is already *listening*. Two
# starts a few seconds apart both sail past it, race for the port, and open a
# tab each — which is where the mystery second tab came from. This covers that
# window; once the server answers, lsof takes over again.
LOCK="$VENV/.starting"
if [ -d "$LOCK" ]; then
    if [ -z "$(find "$LOCK" -maxdepth 0 -mmin +2 2>/dev/null)" ]; then
        say "Another start is already in progress — give it a few seconds."
        exit 0
    fi
    rmdir "$LOCK" 2>/dev/null || true      # older than two minutes: it died
fi
mkdir "$LOCK" 2>/dev/null || true

# Open in an app window — no tabs, no URL bar, its own Dock entry — rather than
# as a tab in whatever you were already doing. Chromium's `--app=` is what makes
# this stop looking like a website.
#
# Deliberately still a real browser rather than an embedded WKWebView: the
# microphone is the entire point of this app, and getUserMedia inside a webview
# needs the bundle signed and entitled before macOS will grant it. Here the
# permission you already granted localhost just keeps working.
open_ui() {
    [ -z "${SCRIBE_NO_OPEN:-}" ] || return 0
    if [ "${SCRIBE_BROWSER_WINDOW:-1}" = "1" ]; then
        for app in "Google Chrome" "Brave Browser" "Microsoft Edge"; do
            bin="/Applications/$app.app/Contents/MacOS/${app##*/}"
            if [ -x "$bin" ]; then
                "$bin" --app="$URL" >/dev/null 2>&1 &
                return 0
            fi
        done
    fi
    open "$URL" 2>/dev/null || true          # Safari/Firefox: ordinary tab
}

# Open once the server actually answers, not before — on a cold start the port
# isn't listening for a second or two.
(
    for _ in $(seq 1 60); do
        if curl -fsS -o /dev/null "$URL/" 2>/dev/null; then
            rmdir "$LOCK" 2>/dev/null || true
            open_ui
            exit 0
        fi
        sleep 0.5
    done
    rmdir "$LOCK" 2>/dev/null || true
) &

say ""
say "scribe → $URL          (ctrl-c to stop)"
say ""

exec "$VENV/bin/python" -m uvicorn scribe.app:app \
    --host 127.0.0.1 --port "$PORT" "$@"
