#!/bin/bash
#
# Gaming Command Center — Linux gaming system optimisation
# Copyright (C) 2026 Thomas
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. See the LICENSE file, or <https://www.gnu.org/licenses/>.
#
# Gaming Command Center — Uninstaller
# Removes everything install.sh puts on the system. It deliberately does NOT
# remove the GUI dependencies (PyGObject/GTK4/libadwaita/PyYAML) — those are
# shared system packages other apps may rely on — nor any /etc tweaks you
# applied from the Setup Wizard (those are your system's config, not ours).
# Usage: ./uninstall.sh

set -e

echo "🎮 Gaming Command Center — Uninstaller"
echo "======================================"
echo

if [ "$EUID" -eq 0 ]; then
    echo "❌ Don't run as root. Run as your normal user (it will ask for sudo)."
    exit 1
fi

echo "🗑️  Removing system files (requires sudo)..."
echo

# 1. Helper scripts
sudo rm -f /usr/local/bin/gaming-ccd-helper /usr/local/bin/gaming-cc-etc-helper
echo "  ✅ Helpers removed"

# 2. Polkit policy
sudo rm -f /usr/share/polkit-1/actions/com.gaming.commandcenter.policy
echo "  ✅ Polkit policy removed"

# 3. App icons (every size install.sh wrote, plus scalable)
for size in 48 64 128 256 512; do
    sudo rm -f "/usr/share/icons/hicolor/${size}x${size}/apps/gaming-command-center.png"
done
sudo rm -f /usr/share/icons/hicolor/scalable/apps/gaming-command-center.png
sudo gtk-update-icon-cache /usr/share/icons/hicolor/ 2>/dev/null || true
echo "  ✅ App icon removed"

# 4. Desktop launchers — the current app-id name, the old mismatched name, and
#    any user-local copy (that one needs no sudo).
sudo rm -f /usr/share/applications/com.gaming.commandcenter.desktop \
           /usr/share/applications/gaming-command-center.desktop
rm -f "$HOME/.local/share/applications/com.gaming.commandcenter.desktop"
sudo update-desktop-database /usr/share/applications/ 2>/dev/null || true
update-desktop-database "$HOME/.local/share/applications/" 2>/dev/null || true
echo "  ✅ Desktop launcher removed"

echo
echo "✅ Uninstalled."
echo
echo "Left untouched on purpose:"
echo "  • GUI dependencies (GTK4, libadwaita, PyYAML, …) — shared system packages"
echo "  • Any /etc fixes you applied (NVIDIA modprobe, Coolbits, …) — your config"
echo "  • This source folder — delete it yourself if you're done: $(cd "$(dirname "$0")" && pwd)"
echo
echo "Bye! 🐧"
