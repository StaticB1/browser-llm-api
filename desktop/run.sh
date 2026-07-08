#!/usr/bin/env bash
# Run the native desktop app on the SYSTEM python3 (which has PyGObject/GTK).
# The venv is only for the server; this client needs nothing but the stdlib + system GTK.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "python3 not found" >&2; exit 1
fi
if ! "$PY" -c 'import gi' 2>/dev/null; then
  echo "System python3 has no PyGObject (gi). Install it, e.g.:" >&2
  echo "  sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 libnotify-bin" >&2
  exit 1
fi
exec "$PY" "$DIR/browser_llm_desktop.py" "$@"
