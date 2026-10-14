#!/usr/bin/env bash
# Portable installer: set up a venv, install deps, and register an always-on
# systemd --user service that runs THIS clone (no hardcoded paths — the unit is
# generated from the template with the real install directory).
#
# After this the API is at http://localhost:8081 and auto-starts / auto-restarts
# (survives logout via linger).
set -euo pipefail
# This script lives in scripts/; DIR is the checkout root the units run from.
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$DIR"

# 1) venv + dependencies. ensure-venv.sh owns this so the installer, serve.sh,
# mcp.sh and the watchdog all build the venv the same way.
echo "[install] checking the venv…"
./scripts/ensure-venv.sh

# 2) generate the systemd --user unit for THIS machine/clone
mkdir -p ~/.config/systemd/user
UNIT="$HOME/.config/systemd/user/browser-llm-api.service"
sed "s#__INSTALL_DIR__#$DIR#g" deploy/systemd/browser-llm-api.service.template > "$UNIT"
echo "[install] wrote $UNIT (WorkingDirectory=$DIR)"

# 3) the watchdog. Without it a broken venv is invisible until the next restart
# — which once meant ten days of a doomed service and then a silent crash loop.
sed "s#__INSTALL_DIR__#$DIR#g" deploy/systemd/browser-llm-health.service.template \
  > "$HOME/.config/systemd/user/browser-llm-health.service"
cp deploy/systemd/browser-llm-health.timer "$HOME/.config/systemd/user/browser-llm-health.timer"

# 4) enable + start, and keep it running after logout
systemctl --user daemon-reload
systemctl --user enable --now browser-llm-api.service
systemctl --user enable --now browser-llm-health.timer
loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true

echo
echo "[install] up at http://localhost:8081   (model: chatgpt-browser, the default | gemini-browser, text only)"
systemctl --user --no-pager status browser-llm-api.service | head -6 || true
echo
echo "  logs:    journalctl --user -u browser-llm-api -f"
echo "  restart: systemctl --user restart browser-llm-api"
echo "  re-auth: DISPLAY=:1 ./venv/bin/browser-llm login chatgpt   # or: gemini"
echo "  health:  scripts/healthcheck.sh --deep   (timer runs it every 10 min)"
