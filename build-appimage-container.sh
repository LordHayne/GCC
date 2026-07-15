#!/usr/bin/env bash
#
# Gaming Command Center — AppImage build orchestrator (container = Phase 2).
# Runs the build inside an old, stable Ubuntu base so the resulting AppImage is
# both portable (old glibc) and free of the bleeding-edge glycin image loader
# that breaks a native CachyOS build. Needs podman (rootless is fine).
#
# Usage:  ./build-appimage-container.sh
# Output: build/Gaming_Command_Center-x86_64.AppImage
# Override the base image with APPIMAGE_BASE=docker.io/library/ubuntu:24.04

set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${APPIMAGE_BASE:-docker.io/library/ubuntu:22.04}"

command -v podman >/dev/null 2>&1 || {
    echo "❌ podman is not installed. Install it first:  sudo pacman -S --needed podman"
    exit 1
}

mkdir -p "$SRC/build"

# On btrfs (common on CachyOS) rootless podman's default overlay driver fails
# ("overlay is not supported over btrfs"); podman's native btrfs driver works.
# Override with PODMAN_STORAGE_DRIVER= if your $HOME is on ext4/xfs (use overlay).
STORAGE_DRIVER="${PODMAN_STORAGE_DRIVER:-btrfs}"

echo "🎮 Building AppImage in $IMAGE (storage driver: $STORAGE_DRIVER)…"
echo "   (pulls the image on first run)"
echo

# Repo mounted read-only; the finished AppImage lands in ./build via /out.
# :Z relabels for SELinux hosts; harmless elsewhere.
# --network host: a build only needs outbound apt/wget, and host networking
# avoids rootless pasta/slirp needing /dev/net/tun (absent on some kernels).
# --root keeps container storage inside build/ (gitignored), sidestepping the
# overlay-vs-btrfs driver record in the default ~/.local graphroot.
podman --storage-driver "$STORAGE_DRIVER" --root "$SRC/build/.containers" run --rm \
    --network host \
    -v "$SRC:/src:ro,Z" \
    -v "$SRC/build:/out:Z" \
    "$IMAGE" \
    bash /src/appimage/build-in-container.sh

echo
echo "Done. Output in $SRC/build/"
ls -lh "$SRC/build/"*.AppImage 2>/dev/null || echo "(no AppImage produced — see the log above)"
