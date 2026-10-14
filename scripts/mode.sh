#!/usr/bin/env bash
# Toggle how the background service (browser-llm-api) runs Chrome, via a systemd
# drop-in override — no need to edit the unit.
#
#   ./mode.sh headless   # invisible (Xvfb): Gemini text+images + ChatGPT text; NO ChatGPT image gen
#   ./mode.sh visible    # real display: everything incl. ChatGPT image gen (a Chrome window shows)
#   ./mode.sh            # print the current mode
#
# The choice persists across restarts/reboots. `visible` uses $DISPLAY (default :1).
set -euo pipefail
UNIT=browser-llm-api.service
DROP="$HOME/.config/systemd/user/$UNIT.d"
CONF="$DROP/10-display.conf"
DISP="${DISPLAY:-:1}"

show() {
  if [ -f "$CONF" ] && grep -qE 'Environment=DISPLAY=$' "$CONF"; then
    echo "mode: headless (Xvfb) — no ChatGPT image gen"
  elif [ -f "$CONF" ]; then
    echo "mode: visible ($(grep -oE 'DISPLAY=[^ ]+' "$CONF" | head -1)) — ChatGPT image gen enabled"
  else
    echo "mode: auto-detect (no override) — uses \$DISPLAY if present, else Xvfb"
  fi
}

case "${1:-show}" in
  headless) mkdir -p "$DROP"; printf '[Service]\nEnvironment=DISPLAY=\n' > "$CONF" ;;
  visible)  mkdir -p "$DROP"; printf '[Service]\nEnvironment=DISPLAY=%s\n' "$DISP" > "$CONF" ;;
  show|"")  show; exit 0 ;;
  *) echo "usage: $0 [headless|visible]"; exit 1 ;;
esac

systemctl --user daemon-reload
if systemctl --user is-active "$UNIT" >/dev/null 2>&1 || \
   systemctl --user is-enabled "$UNIT" >/dev/null 2>&1; then
  systemctl --user restart "$UNIT"
  echo "restarted $UNIT"
fi
show
