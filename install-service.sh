#!/usr/bin/env bash
# Install + start the always-on background service (systemd --user).
# After this, the API is up at http://localhost:8081 and auto-restarts /
# auto-starts with your graphical session.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

mkdir -p ~/.config/systemd/user
cp browser-llm-api.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now browser-llm-api.service

echo "--- status ---"
systemctl --user --no-pager status browser-llm-api.service | head -8 || true
echo
echo "Logs:    journalctl --user -u browser-llm-api -f"
echo "Stop:    systemctl --user stop browser-llm-api"
echo "Restart: systemctl --user restart browser-llm-api"
