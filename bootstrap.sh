#!/usr/bin/env bash
#
# Gaming Command Center — one-line bootstrap
# Copyright (C) 2026 Thomas — GPL-3.0-or-later (see LICENSE)
#
# Install with a single command:
#
#   curl -fsSL https://raw.githubusercontent.com/LordHayne/GCC/main/bootstrap.sh | bash
#
# It does exactly two things, nothing hidden: clone (or update) the repo into
# ~/.local/share/gaming-command-center, then run ./install.sh — which detects
# your distro, installs the GUI dependencies (asking first), and sets up the
# launcher, icon and permissions. Prefer to read before you pipe? Download it,
# read it, run it — same result.
#
# Override the install location with GCC_DIR=/path curl … | bash

set -euo pipefail

REPO="https://github.com/LordHayne/GCC.git"
DIR="${GCC_DIR:-$HOME/.local/share/gaming-command-center}"

echo "🎮 Gaming Command Center — bootstrap"
echo

# Run as a normal user; install.sh escalates with sudo only where needed.
if [ "${EUID:-$(id -u)}" -eq 0 ]; then
    echo "❌ Don't run this as root. Run as your normal user — it asks for sudo itself."
    exit 1
fi

# The two tools this script itself needs. Everything else (GTK4, libadwaita,
# PyYAML, …) is install.sh's job.
missing=""
command -v git     >/dev/null 2>&1 || missing="$missing git"
command -v python3 >/dev/null 2>&1 || missing="$missing python3"
if [ -n "$missing" ]; then
    echo "❌ Missing required tools:$missing"
    echo "   Install them with your package manager, then re-run this command:"
    echo "     Arch / CachyOS:  sudo pacman -S$missing"
    echo "     Debian / Ubuntu: sudo apt install$missing"
    echo "     Fedora / Nobara: sudo dnf install$missing"
    exit 1
fi

# Fresh clone, or fast-forward an existing checkout so re-running updates.
if [ -d "$DIR/.git" ]; then
    echo "📂 Updating existing install in $DIR"
    git -C "$DIR" pull --ff-only
else
    echo "📥 Cloning into $DIR"
    mkdir -p "$(dirname "$DIR")"
    git clone --depth 1 "$REPO" "$DIR"
fi

echo
echo "🚀 Running the installer..."
echo
cd "$DIR"
# exec so install.sh inherits the controlling terminal — its sudo and
# dependency prompts then work even though this script was piped from curl.
exec ./install.sh
