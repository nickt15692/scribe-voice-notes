#!/usr/bin/env bash
set -euo pipefail
exec uvicorn scribe.app:app --host 127.0.0.1 --port 8765 "$@"
