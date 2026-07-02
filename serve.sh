#!/usr/bin/env bash
# One command to run the Browser LLM API server.
#
# Runs Chrome on the real X display (:1) so BOTH text and ChatGPT image
# generation work (GPT-image renders on a GPU-backed <canvas> that stalls under
# headless Xvfb). Handles stale profile-lock cleanup and sane env defaults.
#
#   ./serve.sh                 # start in the foreground (Ctrl-C to stop)
#
# Env overrides (all optional):
#   DISPLAY, XAUTHORITY, GEMINI_IMAGE_DIR, GEMINI_PUBLIC_URL, DEFAULT_PROVIDER
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

export DISPLAY="${DISPLAY:-:1}"
export XAUTHORITY="${XAUTHORITY:-/run/user/$(id -u)/gdm/Xauthority}"
export GEMINI_IMAGE_DIR="${GEMINI_IMAGE_DIR:-$HOME/Pictures/gemini}"
export GEMINI_PUBLIC_URL="${GEMINI_PUBLIC_URL:-http://localhost:8081}"
export DEFAULT_PROVIDER="${DEFAULT_PROVIDER:-chatgpt-browser}"

# Clear stale singleton locks left by a previous hard crash (ignore if absent).
rm -f gemini_profile/Singleton* chatgpt_profile/Singleton* 2>/dev/null || true

echo "[serve] Browser LLM API on ${GEMINI_PUBLIC_URL} (DISPLAY=$DISPLAY, default=$DEFAULT_PROVIDER)" >&2
exec ./venv/bin/python server.py
