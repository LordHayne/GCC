#!/usr/bin/env python3
#
# Gaming Command Center — Linux gaming system optimisation
# Copyright (C) 2026 Thomas — GPL-3.0-or-later (see LICENSE)
#
"""App-code update check.

Separate from the fix-database update (game_db): this tells the user when a new
*app* release exists and how to get it on their channel. It never updates the
app behind their back — for git checkouts it offers a git pull; for AppImages it
points at the download; for package-manager installs it just informs.
"""
import os
import time
import json
import shutil
import subprocess
import urllib.request

REPO = "LordHayne/GCC"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
DOWNLOAD_URL = f"https://github.com/{REPO}/releases/latest/download/Gaming_Command_Center-x86_64.AppImage"

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "gaming-command-center")
_STAMP = os.path.join(CACHE_DIR, "update-last-check")
_LATEST = os.path.join(CACHE_DIR, "update-latest")
CHECK_INTERVAL = 24 * 3600   # at most one release check per day


def channel(base_dir):
    """How this copy of the app was installed — decides the update action.
    'appimage' (self-contained file), 'source' (git checkout), or 'managed'
    (AUR / distro package / plain copy — the user's package manager owns it)."""
    if os.environ.get("APPIMAGE"):
        return "appimage"
    if os.path.isdir(os.path.join(base_dir, ".git")):
        return "source"
    return "managed"


def _vtuple(s):
    """Loosely parse 'v0.1.0' / '0.1.0' into a comparable tuple of ints."""
    out = []
    for part in (s or "").lstrip("vV").strip().split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def is_newer(latest, current):
    return _vtuple(latest) > _vtuple(current)


def _throttled():
    try:
        return (time.time() - os.path.getmtime(_STAMP)) < CHECK_INTERVAL
    except OSError:
        return False


def _read_cached_latest():
    try:
        with open(_LATEST, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _fetch_latest_tag(timeout):
    try:
        req = urllib.request.Request(RELEASES_API, headers={
            "User-Agent": "gaming-command-center",
            "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        tag = data.get("tag_name")
        return tag.strip() if isinstance(tag, str) and tag.strip() else None
    except Exception:
        return None


def check_update(current, base_dir, timeout=8, force=False):
    """Best-effort release check, throttled to once a day. Returns a dict
    {current, latest, channel, available} or None when there's no usable answer
    (never checked + offline). Falls back to the last-known latest tag when
    throttled or offline, so the UI can show status without hitting the network
    on every launch. Never raises."""
    latest = None
    if force or not _throttled():
        latest = _fetch_latest_tag(timeout)
        if latest:
            try:
                os.makedirs(CACHE_DIR, exist_ok=True)
                with open(_LATEST, "w", encoding="utf-8") as f:
                    f.write(latest)
                open(_STAMP, "w").close()
            except OSError:
                pass
    if not latest:
        latest = _read_cached_latest()
    if not latest:
        return None
    return {
        "current": current,
        "latest": latest,
        "channel": channel(base_dir),
        "available": is_newer(latest, current),
    }


def appimage_update_tool():
    """Path to an installed AppImage self-updater, or None. The AppImage embeds
    zsync update-information, so AppImageUpdate can delta-update it in place."""
    for name in ("appimageupdatetool", "AppImageUpdate"):
        p = shutil.which(name)
        if p:
            return p
    return None


def run_appimage_update(appimage_path, timeout=300):
    """Delta-update the AppImage in place via AppImageUpdate. Returns (ok, msg)."""
    tool = appimage_update_tool()
    if not tool:
        return False, "AppImageUpdate is not installed"
    if not appimage_path or not os.path.exists(appimage_path):
        return False, "AppImage path unknown"
    try:
        r = subprocess.run([tool, appimage_path], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "update timed out"
    except OSError as e:
        return False, f"could not run updater: {e}"
    if r.returncode == 0:
        return True, "updated"
    lines = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()
    return False, lines[-1] if lines else f"update failed (exit {r.returncode})"


def git_pull(base_dir, timeout=60):
    """Fast-forward the source checkout. Returns (ok, message). --ff-only so a
    diverged/dirty tree fails loudly instead of creating a merge."""
    if not os.path.isdir(os.path.join(base_dir, ".git")):
        return False, "not a git checkout"
    try:
        r = subprocess.run(["git", "-C", base_dir, "pull", "--ff-only"],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "git pull timed out"
    except OSError as e:
        return False, f"could not run git: {e}"
    if r.returncode == 0:
        lines = (r.stdout or "").strip().splitlines()
        return True, lines[-1] if lines else "updated"
    lines = (r.stderr or "").strip().splitlines()
    return False, lines[-1] if lines else f"git pull failed (exit {r.returncode})"
