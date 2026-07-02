#!/usr/bin/env bash
# Run the Browser LLM API server. Portable: run from anywhere, any user.
#
# Auto-detects the display:
#   * a usable X display  -> run directly on it (ChatGPT image generation works)
#   * no display          -> fall back to headless `xvfb-run` (Gemini text+images
#                            work; ChatGPT image gen needs a real display)
#
#   ./serve.sh                 # foreground (Ctrl-C to stop)
#
# Env overrides (all optional):
#   DISPLAY, XAUTHORITY, GEMINI_IMAGE_DIR, GEMINI_PUBLIC_URL, DEFAULT_PROVIDER
set -euo pipefail
cd "$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

export GEMINI_IMAGE_DIR="${GEMINI_IMAGE_DIR:-$HOME/Pictures/gemini}"
export GEMINI_PUBLIC_URL="${GEMINI_PUBLIC_URL:-http://localhost:8081}"
export DEFAULT_PROVIDER="${DEFAULT_PROVIDER:-gemini-browser}"

# Prefer the project venv, else system python3.
if [ -x venv/bin/python ]; then PY=venv/bin/python; else PY=python3; fi

# Clear stale singleton locks left by a previous hard crash (ignore if absent).
rm -f gemini_profile/Singleton* chatgpt_profile/Singleton* 2>/dev/null || true

# Is there a usable X display?
display_ok() {
  [ -n "${DISPLAY:-}" ] || return 1
  if command -v xdpyinfo >/dev/null 2>&1; then xdpyinfo >/dev/null 2>&1; return; fi
  if command -v xset     >/dev/null 2>&1; then xset q     >/dev/null 2>&1; return; fi
  return 0   # DISPLAY is set but we can't probe it — assume usable
}

if display_ok; then
  # Locate an Xauthority if one wasn't provided (gdm / lightdm / default).
  if [ -z "${XAUTHORITY:-}" ]; then
    for c in "/run/user/$(id -u)/gdm/Xauthority" "$HOME/.Xauthority"; do
      [ -f "$c" ] && export XAUTHORITY="$c" && break
    done
  fi
  echo "[serve] display $DISPLAY (ChatGPT images enabled) · default=$DEFAULT_PROVIDER · $GEMINI_PUBLIC_URL" >&2
  exec "$PY" server.py
else
  echo "[serve] no display — headless Xvfb (Gemini images ok; ChatGPT images need a display) · default=$DEFAULT_PROVIDER" >&2
  exec xvfb-run -a "$PY" server.py
fi
