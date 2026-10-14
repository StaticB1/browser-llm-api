#!/usr/bin/env bash
# Guarantee ./venv exists and can import everything server.py needs.
#
# Why this exists (2026-09-07): serve.sh used to fall back to the system
# python3 when venv/bin/python was missing. System python is PEP-668 and can
# never hold nodriver, so a deleted venv became a crash loop reporting
# "ModuleNotFoundError: No module named 'nodriver'" — 241 restarts, nothing
# listening on 8081, and no alarm anywhere. Worse, the venv had been gone for
# ten days: the running process already held its imports, so the service kept
# answering normally until the next restart exposed it.
#
# So a missing venv is treated as a repairable state, not a fatal one, and
# `check` exposes it while it is still latent.
#
#   scripts/ensure-venv.sh check    exit 0 if the venv imports, 1 if not. Changes nothing.
#   scripts/ensure-venv.sh          check, and rebuild the venv if the check fails.
#
# Every message goes to stderr: mcp.sh calls this and its stdout is the MCP
# protocol channel, where one stray line corrupts the JSON frames.
set -uo pipefail
# This script lives in scripts/; everything below runs from the checkout root.
cd "$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"

PY=venv/bin/python
# Import names, not package names: python-multipart -> multipart, Pillow -> PIL.
DEPS='import nodriver, fastapi, uvicorn, pydantic, httpx, multipart, PIL'

check() { [ -x "$PY" ] && "$PY" -c "$DEPS" >/dev/null 2>&1; }

if check; then exit 0; fi
if [ "${1:-}" = check ]; then exit 1; fi

echo "[ensure-venv] venv missing or incomplete — rebuilding" >&2
if [ -d venv ] && [ ! -x "$PY" ]; then
  # A half-built or interpreter-less venv: python3 -m venv will not repair it.
  rm -rf venv
fi
python3 -m venv venv >&2 || {
  echo "[ensure-venv] python3 -m venv failed — is the python3-venv package installed?" >&2; exit 1; }
"$PY" -m pip install --upgrade pip >&2 || true
"$PY" -m pip install -r requirements.txt >&2 || {
  echo "[ensure-venv] pip install failed — no network, or a package is unavailable" >&2; exit 1; }
# Editable install: provides the browser-llm command (and browser-llm-mcp, browser-llm-design).
"$PY" -m pip install -e . >&2 || true
check || { echo "[ensure-venv] rebuilt the venv but the imports still fail" >&2; exit 1; }
echo "[ensure-venv] venv rebuilt" >&2
