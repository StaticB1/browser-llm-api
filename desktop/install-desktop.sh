#!/usr/bin/env bash
# Install the native desktop app's launcher + icon into the user's XDG dirs so it
# shows up in the GNOME app grid (search "Browser LLM"). No root, no build step.
#
#   ./desktop/install-desktop.sh            # install
#   ./desktop/install-desktop.sh --uninstall
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_PY="$DIR/browser_llm_desktop.py"

APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor"
DESKTOP_FILE="$APPS/browser-llm-desktop.desktop"
ICON_SCALABLE="$ICONS/scalable/apps/browser-llm-desktop.svg"

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$DESKTOP_FILE" "$ICON_SCALABLE" \
        "$ICONS/48x48/apps/browser-llm-desktop.png" \
        "$ICONS/256x256/apps/browser-llm-desktop.png"
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" || true
  command -v gtk-update-icon-cache  >/dev/null 2>&1 && gtk-update-icon-cache -f -t "$ICONS" 2>/dev/null || true
  echo "Uninstalled Browser LLM desktop launcher."
  exit 0
fi

# --- icon: scalable SVG + a couple of rasterised PNG sizes ---
mkdir -p "$ICONS/scalable/apps" "$ICONS/48x48/apps" "$ICONS/256x256/apps"
cp "$DIR/icon.svg" "$ICON_SCALABLE"
for sz in 48 256; do
  python3 - "$DIR/icon.svg" "$ICONS/${sz}x${sz}/apps/browser-llm-desktop.png" "$sz" <<'PY' 2>/dev/null || true
import sys, gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf
src, dst, sz = sys.argv[1], sys.argv[2], int(sys.argv[3])
GdkPixbuf.Pixbuf.new_from_file_at_size(src, sz, sz).savev(dst, "png", [], [])
PY
done

# --- .desktop launcher (substitute the absolute path to the app) ---
mkdir -p "$APPS"
sed "s|__APP__|$APP_PY|g" "$DIR/browser-llm-desktop.desktop.in" > "$DESKTOP_FILE"
chmod +x "$DESKTOP_FILE"

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" || true
command -v gtk-update-icon-cache  >/dev/null 2>&1 && gtk-update-icon-cache -f -t "$ICONS" 2>/dev/null || true

echo "Installed. Search 'Browser LLM' in Activities, or launch now with:"
echo "  gtk-launch browser-llm-desktop"
