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

# ── Distro detection ─────────────────────────────────────────────────────────
# Mirrors distro.py: map the /etc/os-release ID (and ID_LIKE, for derivatives we
# don't list by name) onto one of three package-manager families.
detect_distro() {
    local id="" id_like=""
    if [ -r /etc/os-release ]; then
        id=$(. /etc/os-release 2>/dev/null; echo "$ID")
        id_like=$(. /etc/os-release 2>/dev/null; echo "$ID_LIKE")
    fi
    case " $id $id_like " in
        *arch*|*cachyos*|*manjaro*|*endeavouros*|*garuda*|*artix*) echo arch;   return ;;
        *debian*|*ubuntu*|*mint*|*pop*|*elementary*|*kali*)        echo debian; return ;;
        *fedora*|*rhel*|*centos*|*rocky*|*alma*|*nobara*)          echo fedora; return ;;
    esac
    # Fallback: whichever package manager is actually installed.
    command -v pacman >/dev/null 2>&1 && { echo arch;   return; }
    command -v apt    >/dev/null 2>&1 && { echo debian; return; }
    command -v dnf    >/dev/null 2>&1 && { echo fedora; return; }
    echo ""   # unknown
}

DISTRO=$(detect_distro)

# ── Dependencies ─────────────────────────────────────────────────────────────
# The app is Python + GTK4 + libadwaita; without those it won't even open a
# window. Verify here so we never print "complete" and then crash on launch.
echo "🔎 Checking dependencies..."

# One preflight that proves the app can import its actual GUI stack + YAML.
# Truer than querying the package DB, and it can't be fooled by odd pkg names.
preflight() {
    python3 - <<'PY' 2>/dev/null
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw   # noqa: F401
import yaml   # noqa: F401
PY
}

if ! command -v python3 >/dev/null 2>&1; then
    echo "  ❌ python3 is not installed. Install it first, then re-run ./install.sh:"
    case "$DISTRO" in
        arch)   echo "     sudo pacman -S python" ;;
        debian) echo "     sudo apt install python3" ;;
        fedora) echo "     sudo dnf install python3" ;;
        *)      echo "     (use your distro's package manager to install python3)" ;;
    esac
    exit 1
fi

if preflight; then
    echo "  ✅ Python + GTK4 + libadwaita + PyYAML present"
else
    echo "  ⚠️  Missing GUI dependencies (PyGObject / GTK4 / libadwaita / PyYAML)"
    case "$DISTRO" in
        arch)   PKGS="python-gobject gtk4 libadwaita python-yaml";          PM="sudo pacman -S --needed --noconfirm" ;;
        debian) PKGS="python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-yaml";  PM="sudo apt install -y" ;;
        fedora) PKGS="python3-gobject gtk4 libadwaita python3-pyyaml";       PM="sudo dnf install -y" ;;
        *)      PKGS=""; PM="" ;;
    esac

    if [ -z "$PKGS" ]; then
        echo "  ❌ Couldn't detect your distro's package manager."
        echo "     Install these manually, then re-run ./install.sh:"
        echo "       PyGObject · GTK4 · libadwaita · PyYAML"
        exit 1
    fi

    echo "     Packages: $PKGS"
    if [ -t 0 ]; then
        read -r -p "     Install them now? [Y/n] " reply
    else
        reply="y"   # piped/non-interactive run → proceed
    fi
    case "$reply" in
        [Nn]*) echo "  ❌ Skipped — the app can't run without these. Aborting."; exit 1 ;;
    esac

    echo "  📥 Installing GUI dependencies..."
    if ! $PM $PKGS; then
        echo "  ❌ Dependency install failed. Fix the errors above and re-run."
        exit 1
    fi

    if preflight; then
        echo "  ✅ Dependencies installed and verified"
    else
        echo "  ❌ Still can't import GTK4/libadwaita after install."
        echo "     Please report this along with your distro ID ($DISTRO)."
        exit 1
    fi
fi

# Optional tools — the app degrades gracefully, so only nudge, never block.
command -v sensors >/dev/null 2>&1 || echo "  ℹ️  optional: 'sensors' (lm_sensors) missing — CPU temperature will be hidden"
command -v pkexec  >/dev/null 2>&1 || echo "  ℹ️  optional: 'pkexec' (polkit) missing — the /etc setup fixes need it"
echo

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
# The basename MUST equal the app-id (Adw.Application application_id), otherwise
# Wayland compositors (COSMIC, GNOME, …) can't map the window to this entry and
# fall back to a generic/blank taskbar icon. StartupWMClass covers X11 as well.
sudo mkdir -p /usr/share/applications
# Drop the old mismatched name from earlier installs so we don't leave a duplicate.
sudo rm -f /usr/share/applications/gaming-command-center.desktop
sudo tee /usr/share/applications/com.gaming.commandcenter.desktop > /dev/null << 'DESKTOP'
[Desktop Entry]
Name=Gaming Command Center
Comment=Linux gaming optimization — CPU CCD parking, GPU overclocking, system setup wizard
Exec=python3 INSTALLDIR/command-center.py
Icon=gaming-command-center
Terminal=false
Type=Application
StartupWMClass=com.gaming.commandcenter
Categories=Game;System;Utility;
Keywords=gaming;ryzen;cpu;gpu;nvidia;overclock;ccd;gamemode;linux;
DESKTOP

# Fix the Exec path in the desktop file
sudo sed -i "s|INSTALLDIR|$SCRIPT_DIR|" /usr/share/applications/com.gaming.commandcenter.desktop
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