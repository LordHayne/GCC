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
# Gaming Command Center — Installer
# One script does everything: install files, set permissions, configure polkit
# Usage: ./install.sh

set -e

echo "🎮 Gaming Command Center — Installer"
echo "====================================="
echo

# Check we're not root (need sudo for some parts)
if [ "$EUID" -eq 0 ]; then
    echo "❌ Don't run as root. Run as your normal user."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "📦 Installing system files (requires sudo)..."
echo

# 1. Helper scripts → /usr/local/bin
# Two of them on purpose: runtime tweaks (core parking, governor) run without a
# password, persistent /etc changes ask for authentication. See the polkit file.
sudo install -m 755 -o root -g root "$SCRIPT_DIR/gaming-ccd-helper" /usr/local/bin/gaming-ccd-helper
sudo install -m 755 -o root -g root "$SCRIPT_DIR/gaming-cc-etc-helper" /usr/local/bin/gaming-cc-etc-helper
echo "  ✅ Helpers installed → /usr/local/bin/gaming-{ccd,cc-etc}-helper"

# 2. Polkit policy → /usr/share/polkit-1/actions/
# Must be world-readable (0644) — plain `cp` would carry over the repo file's
# permissions and polkit may then ignore the action.
sudo install -m 644 -o root -g root "$SCRIPT_DIR/com.gaming.commandcenter.policy" \
    /usr/share/polkit-1/actions/com.gaming.commandcenter.policy
echo "  ✅ Polkit policy installed (no password for Game Mode)"

# 3. App icon → system icons
for size in 48 64 128 256 512; do
    sudo mkdir -p "/usr/share/icons/hicolor/${size}x${size}/apps"
    sudo cp "$SCRIPT_DIR/GCC_logo.png" "/usr/share/icons/hicolor/${size}x${size}/apps/gaming-command-center.png"
done
sudo mkdir -p /usr/share/icons/hicolor/scalable/apps
sudo cp "$SCRIPT_DIR/GCC_logo.png" /usr/share/icons/hicolor/scalable/apps/gaming-command-center.png
sudo gtk-update-icon-cache /usr/share/icons/hicolor/ 2>/dev/null || true
echo "  ✅ App icon installed"

# 4. Desktop file → applications
sudo mkdir -p /usr/share/applications
sudo tee /usr/share/applications/gaming-command-center.desktop > /dev/null << 'DESKTOP'
[Desktop Entry]
Name=Gaming Command Center
Comment=Linux gaming optimization — CPU CCD parking, GPU overclocking, system setup wizard
Exec=python3 INSTALLDIR/command-center.py
Icon=gaming-command-center
Terminal=false
Type=Application
Categories=Game;System;Utility;
Keywords=gaming;ryzen;cpu;gpu;nvidia;overclock;ccd;gamemode;linux;
DESKTOP

# Fix the Exec path in the desktop file
sudo sed -i "s|INSTALLDIR|$SCRIPT_DIR|" /usr/share/applications/gaming-command-center.desktop
echo "  ✅ Desktop launcher installed"

# 5. Update caches
sudo update-desktop-database /usr/share/applications/ 2>/dev/null || true
sudo gtk-update-icon-cache /usr/share/icons/hicolor/ 2>/dev/null || true

echo
echo "✅ Installation complete!"
echo
echo "You can now:"
echo "  • Launch from your app menu → 'Gaming Command Center'"
echo "  • Or run: python3 $SCRIPT_DIR/command-center.py"
echo
echo "🎮 Game Mode will work without password (polkit configured)"
echo "⚡ Apply Fix buttons in Setup Wizard will work"
echo
echo "Enjoy! 🐧🎮"