#!/usr/bin/env bash
# Portable installer: set up a venv, install deps, and register an always-on
# systemd --user service that runs THIS clone (no hardcoded paths — the unit is
# generated from the template with the real install directory).
#
# After this the API is at http://localhost:8081 and auto-starts / auto-restarts
# (survives logout via linger).
set -euo pipefail
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$DIR"

# 1) venv + dependencies
if [ ! -x venv/bin/python ]; then
  echo "[install] creating venv…"
  python3 -m venv venv
fi
echo "[install] installing dependencies…"
# `python -m pip` (not venv/bin/pip): the pip script's shebang hardcodes the venv
# path at creation time and breaks if the clone is later moved/renamed.
./venv/bin/python -m pip install -q --upgrade pip
./venv/bin/python -m pip install -q -r requirements.txt

# 2) generate the systemd --user unit for THIS machine/clone
mkdir -p ~/.config/systemd/user
UNIT="$HOME/.config/systemd/user/browser-llm-api.service"
sed "s#__INSTALL_DIR__#$DIR#g" browser-llm-api.service.template > "$UNIT"
echo "[install] wrote $UNIT (WorkingDirectory=$DIR)"

# 3) enable + start, and keep it running after logout
systemctl --user daemon-reload
systemctl --user enable --now browser-llm-api.service
loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true

echo
echo "[install] up at http://localhost:8081   (model: gemini-browser | chatgpt-browser)"
systemctl --user --no-pager status browser-llm-api.service | head -6 || true
echo
echo "  logs:    journalctl --user -u browser-llm-api -f"
echo "  restart: systemctl --user restart browser-llm-api"
echo "  re-auth: DISPLAY=:1 ./venv/bin/python login.py gemini   # or: chatgpt"
