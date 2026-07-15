#!/usr/bin/env python3
#
# Gaming Command Center — Linux gaming system optimisation
# Copyright (C) 2026 Thomas
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. See the LICENSE file, or <https://www.gnu.org/licenses/>.
#
"""Finds Steam games and reads/writes their launch options.

Steam stores everything in text VDF (a nested "key" "value" format), so this
module carries a small VDF parser/serialiser. Launch options live in
  userdata/<id>/config/localconfig.vdf
which Steam rewrites when it exits — so we only ever edit it while Steam is
closed, and always back it up first.
"""
import glob
import json
import os
import shutil
import subprocess
import urllib.request

STEAM_ROOTS = [
    "~/.steam/steam",
    "~/.local/share/Steam",
    "~/.steam/root",
    "~/.var/app/com.valvesoftware.Steam/data/Steam",  # Flatpak
]


# ============================================================
# Text VDF parser / serialiser
# ============================================================
def parse_vdf(text):
    """Parse Steam text VDF into nested dicts. Tolerant: unknown constructs are
    skipped rather than raising, since we only ever navigate to known keys."""
    i, n = 0, len(text)

    def skip_ws():
        nonlocal i
        while i < n:
            c = text[i]
            if c in " \t\r\n":
                i += 1
            elif c == "/" and i + 1 < n and text[i + 1] == "/":
                while i < n and text[i] != "\n":
                    i += 1
            else:
                break

    def read_string():
        nonlocal i
        # quoted
        if text[i] == '"':
            i += 1
            buf = []
            while i < n:
                c = text[i]
                if c == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    buf.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
                    i += 2
                elif c == '"':
                    i += 1
                    break
                else:
                    buf.append(c)
                    i += 1
            return "".join(buf)
        # unquoted token
        start = i
        while i < n and text[i] not in ' \t\r\n"{}':
            i += 1
        return text[start:i]

    def parse_obj():
        nonlocal i
        obj = {}
        while i < n:
            skip_ws()
            if i >= n or text[i] == "}":
                i += 1
                break
            key = read_string()
            skip_ws()
            if i < n and text[i] == "{":
                i += 1
                obj[key] = parse_obj()
            else:
                obj[key] = read_string()
        return obj

    skip_ws()
    root = {}
    while i < n:
        skip_ws()
        if i >= n:
            break
        key = read_string()
        skip_ws()
        if i < n and text[i] == "{":
            i += 1
            root[key] = parse_obj()
        elif key:
            root[key] = read_string()
        else:
            break
    return root


def _esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def dump_vdf(obj, indent=0):
    """Serialise back to Steam-compatible text VDF (tab-indented)."""
    tab = "\t" * indent
    out = []
    for key, val in obj.items():
        if isinstance(val, dict):
            out.append(f'{tab}"{_esc(key)}"')
            out.append(f"{tab}{{")
            out.append(dump_vdf(val, indent + 1))
            out.append(f"{tab}}}")
        else:
            out.append(f'{tab}"{_esc(key)}"\t\t"{_esc(str(val))}"')
    return "\n".join(out)


def _get_ci(d, key):
    """Case-insensitive dict lookup — VDF keys ('apps' vs 'Apps') vary."""
    if key in d:
        return d[key]
    low = key.lower()
    for k, v in d.items():
        if k.lower() == low:
            return v
    return None


# ============================================================
# Steam discovery
# ============================================================
def find_steam_root():
    for root in STEAM_ROOTS:
        p = os.path.expanduser(root)
        if os.path.isdir(os.path.join(p, "steamapps")):
            return os.path.realpath(p)
    return None


def library_art(root, appid):
    """Local Steam cover/banner art for a game, or {} if nothing is cached.

    Reads Steam's own appcache — fully offline, no network. Steam has used a
    few layouts over time, so we cover all of them for each asset name:
      * new:    librarycache/<appid>/<hash>/library_header.jpg
      * older:  librarycache/<appid>/library_header.jpg
      * legacy: librarycache/<appid>_header.jpg
    Asset names are tried in preference order (plain before localized), so we
    pick the English `library_header.jpg` over `library_header_german.jpg`.
    Keys returned when present: 'header' (460x215 banner), 'portrait' (~600x900).
    """
    if not root:
        return {}
    cache = os.path.join(root, "appcache", "librarycache")
    appid = str(appid)

    def _find(names):
        for n in names:
            for pat in (os.path.join(cache, appid, n),        # older per-appid
                        os.path.join(cache, appid, "*", n),   # new hash subdir
                        os.path.join(cache, f"{appid}_{n}")):  # legacy flat
                hits = sorted(glob.glob(pat))
                if hits:
                    return hits[0]
        return None

    out = {}
    header = _find(["library_header.jpg", "header.jpg", "library_header_german.jpg"])
    if header:
        out["header"] = header
    portrait = _find(["library_600x900.jpg", "library_capsule.jpg"])
    if portrait:
        out["portrait"] = portrait
    return out


def is_steam_running():
    try:
        r = subprocess.run(["pgrep", "-x", "steam"], capture_output=True, timeout=3)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _library_dirs(root):
    """Every steamapps dir, including extra library drives."""
    dirs = [os.path.join(root, "steamapps")]
    vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
    if os.path.isfile(vdf):
        try:
            with open(vdf) as f:
                data = parse_vdf(f.read())
            folders = _get_ci(data, "libraryfolders") or {}
            for entry in folders.values():
                if isinstance(entry, dict):
                    path = entry.get("path")
                    if path:
                        d = os.path.join(path, "steamapps")
                        if os.path.isdir(d):
                            dirs.append(d)
        except OSError:
            pass
    return list(dict.fromkeys(dirs))  # de-dupe, keep order


def installed_appids(root):
    """AppIDs with a downloaded install (appmanifest present)."""
    ids = {}
    for d in _library_dirs(root):
        for mf in glob.glob(os.path.join(d, "appmanifest_*.acf")):
            try:
                with open(mf) as f:
                    data = parse_vdf(f.read())
                st = _get_ci(data, "AppState") or {}
                appid = int(st.get("appid"))
                ids[appid] = st.get("name", f"App {appid}")
            except (OSError, TypeError, ValueError):
                continue
    return ids


def _localconfig_paths(root):
    return glob.glob(os.path.join(root, "userdata", "*", "config", "localconfig.vdf"))


def _apps_node(localconfig_tree):
    """Navigate to UserLocalConfigStore>Software>Valve>Steam>apps, or None."""
    node = _get_ci(localconfig_tree, "UserLocalConfigStore")
    for key in ("Software", "Valve", "Steam", "apps"):
        if not isinstance(node, dict):
            return None
        node = _get_ci(node, key)
    return node if isinstance(node, dict) else None


def known_appids(root):
    """AppIDs Steam has a record of (played / configured), even if not currently
    installed — read from every user's localconfig apps section."""
    ids = set()
    for path in _localconfig_paths(root):
        try:
            with open(path) as f:
                apps = _apps_node(parse_vdf(f.read()))
        except OSError:
            continue
        if apps:
            for k in apps:
                try:
                    ids.add(int(k))
                except ValueError:
                    pass
    return ids


def detect_appids():
    """Union of installed and known AppIDs -> {appid: name|None}.

    Returns ({}, reason) style is avoided; callers check find_steam_root first.
    """
    root = find_steam_root()
    if not root:
        return {}
    result = dict(installed_appids(root))          # appid -> name
    for appid in known_appids(root):
        result.setdefault(appid, None)
    return result


# ============================================================
# Launch options
# ============================================================
def get_launch_options(appid):
    """Current launch option string for appid, or "" if none/unknown."""
    root = find_steam_root()
    if not root:
        return ""
    for path in _localconfig_paths(root):
        try:
            with open(path) as f:
                apps = _apps_node(parse_vdf(f.read()))
        except OSError:
            continue
        entry = _get_ci(apps or {}, str(appid))
        if isinstance(entry, dict):
            lo = _get_ci(entry, "LaunchOptions")
            if lo is not None:
                return lo
    return ""


def set_launch_options(appid, value):
    """Write a launch option for appid. Returns (ok, message).

    Refuses while Steam is running (it would overwrite the file on exit) and
    backs the file up before touching it.
    """
    root = find_steam_root()
    if not root:
        return False, "Steam installation not found"
    if is_steam_running():
        return False, "Steam is running — close it first, then apply the fix"

    paths = _localconfig_paths(root)
    if not paths:
        return False, "No Steam user profile found (log in to Steam once)"

    # Prefer the profile that already knows this app; else the first profile.
    target = None
    for path in paths:
        try:
            with open(path) as f:
                tree = parse_vdf(f.read())
        except OSError:
            continue
        apps = _apps_node(tree)
        if apps is not None and _get_ci(apps, str(appid)) is not None:
            target = (path, tree)
            break
    if target is None:
        path = paths[0]
        try:
            with open(path) as f:
                tree = parse_vdf(f.read())
        except OSError as e:
            return False, f"Could not read {path}: {e}"
        target = (path, tree)

    path, tree = target
    apps = _apps_node(tree)
    if apps is None:
        return False, "localconfig.vdf has an unexpected structure — not editing it"

    entry = _get_ci(apps, str(appid))
    if not isinstance(entry, dict):
        entry = {}
        apps[str(appid)] = entry
    # Reuse the exact existing key name if present (LaunchOptions casing).
    lo_key = "LaunchOptions"
    for k in entry:
        if k.lower() == "launchoptions":
            lo_key = k
            break
    entry[lo_key] = value

    backup = path + ".gcc-backup"
    try:
        shutil.copy2(path, backup)
        text = '"UserLocalConfigStore"\n{\n' + \
               dump_vdf(_get_ci(tree, "UserLocalConfigStore"), 1) + "\n}\n"
        tmp = path + ".gcc-tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)  # atomic
    except OSError as e:
        return False, f"Could not write launch options: {e}"

    return True, f"Launch option set (backup at {os.path.basename(backup)})"


# ============================================================
# ProtonDB tier (read-only context signal)
# ============================================================
def protondb_tier(appid, timeout=6):
    """(tier, report_count) from ProtonDB, or (None, 0). Never raises."""
    url = f"https://www.protondb.com/api/v1/reports/summaries/{appid}.json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        return data.get("tier"), int(data.get("total", 0))
    except Exception:
        return None, 0


if __name__ == "__main__":
    root = find_steam_root()
    print(f"Steam root: {root}")
    print(f"Steam running: {is_steam_running()}")
    if root:
        inst = installed_appids(root)
        known = known_appids(root)
        print(f"Installed: {len(inst)}  Known: {len(known)}")
        for appid, name in sorted(detect_appids().items()):
            lo = get_launch_options(appid)
            print(f"  {appid}  {name or '(not installed)'}")
            if lo:
                print(f"       launch: {lo}")
