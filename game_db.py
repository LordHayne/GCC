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
"""Loads and validates the per-game fix database (games.yaml).

games.yaml is edited by strangers via pull request, so this module is the trust
boundary: it enforces a strict schema and a fix-type whitelist while loading.
Anything it does not recognise is dropped, not executed — the database can never
carry an arbitrary shell command into the app.
"""
import os
import time
import tempfile
import urllib.request

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

# The database bundled with the app (source checkout or AppImage).
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "games.yaml")

# Auto-update: fetch the community-maintained database straight from main, so a
# fix merged today reaches users tomorrow — independent of app releases. The
# loader below is still the trust boundary (whitelisted fix types only), so a
# downloaded database can never carry an executable payload.
DB_URL = "https://raw.githubusercontent.com/LordHayne/GCC/main/games.yaml"
CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "gaming-command-center")
CACHE_DB = os.path.join(CACHE_DIR, "games.yaml")
_STAMP = os.path.join(CACHE_DIR, "db-last-check")
UPDATE_INTERVAL = 24 * 3600   # at most one network check per day

# The only fix types the app will ever act on. A fix with any other type is
# discarded on load. `tool_action` is further constrained below.
ALLOWED_FIX_TYPES = {"info", "launch_option", "file", "tool_action"}

# tool_action fixes may only name one of these — each maps to something the app
# already knows how to do safely. No free-form action string is honoured.
ALLOWED_TOOL_ACTIONS = {"game_mode"}

ALLOWED_GPU = {"nvidia", "amd", "intel"}
ALLOWED_CPU = {"amd", "intel"}
ALLOWED_SESSION = {"wayland", "x11"}

# Curated "can I even play this?" verdict, surfaced as the library traffic light.
# `broken` is the expectation-manager: a game that will NOT run on Linux at all
# (almost always anti-cheat disabled server-side) — no launch option or file can
# fix it, so we say so up front instead of after two hours of fiddling. Any other
# value is dropped, exactly like the fix-type whitelist.
ALLOWED_PLAYABILITY = {"playable", "fixable", "broken"}

# Storefronts a game can come from. "steam" is the default and the only one we
# scan/auto-detect; the rest are non-Steam titles that can't carry a steam_id
# (Valorant, League, Fortnite, …). We still list them so the traffic light can
# warn "this famous game won't run on Linux" even though it's not on Steam.
ALLOWED_PLATFORMS = {"steam", "riot", "epic", "battlenet", "ea", "rockstar", "other"}


class Fix:
    def __init__(self, ftype, value="", path="", content="", action="", verified=False,
                 source=""):
        self.type = ftype
        self.value = value      # info text / launch option string
        self.path = path        # file fix: target path
        self.content = content  # file fix: file contents
        self.action = action    # tool_action: which action
        # True only when a human has tested that this fix solves the issue.
        # Everything from a fresh PR starts unverified so the UI can flag it.
        self.verified = verified
        # Provenance for researched (unverified) fixes: where the fix came from,
        # e.g. a PCGamingWiki/ProtonDB URL. Empty for hand-authored entries.
        self.source = source

    @property
    def is_applicable(self):
        """Can the app apply this itself, or is it advisory (info)?"""
        return self.type in ("launch_option", "file", "tool_action")


class Issue:
    def __init__(self, symptom, cause, fix, when=None):
        self.symptom = symptom
        self.cause = cause
        self.fix = fix
        self.when = when or {}   # {"gpu": "nvidia", "cpu": "amd", "session": "wayland"}

    def matches_system(self, gpu=None, session=None, cpu=None):
        """True if this issue is relevant to the current setup. Each `when` key
        that is set must match; an absent key matches everyone. So a fix only
        for NVIDIA on Wayland stays hidden on an AMD-GPU or X11 box."""
        if "gpu" in self.when and gpu and self.when["gpu"] != gpu:
            return False
        if "cpu" in self.when and cpu and self.when["cpu"] != cpu:
            return False
        if "session" in self.when and session and self.when["session"] != session:
            return False
        return True


class Game:
    def __init__(self, name, steam_id, issues, aliases=None,
                 playability=None, playability_reason="", playability_source="",
                 platform="steam"):
        self.name = name
        self.steam_id = steam_id            # int for Steam games, None otherwise
        self.platform = platform            # "steam" (scanned) or a non-Steam store
        self.issues = issues
        self.aliases = aliases or []
        # None | "playable" | "fixable" | "broken" — the library traffic light.
        # None means "no curated verdict", so the UI falls back to ProtonDB.
        self.playability = playability
        self.playability_reason = playability_reason    # why, in one line (esp. broken)
        self.playability_source = playability_source    # where the verdict came from

    @property
    def is_steam(self):
        return self.platform == "steam"


def _clean_str(v):
    """YAML block scalars fold in newlines and indentation — collapse runs of
    whitespace so multi-line symptom/cause text renders as one tidy paragraph."""
    return " ".join(str(v).split())


def _parse_fix(raw):
    """Validate one fix dict. Returns a Fix or None (drop it)."""
    if not isinstance(raw, dict):
        return None
    ftype = raw.get("type")
    if ftype not in ALLOWED_FIX_TYPES:
        return None
    verified = raw.get("verified") is True
    source = _clean_str(raw.get("source", ""))

    if ftype == "info":
        text = _clean_str(raw.get("value", ""))
        return Fix("info", value=text, verified=verified, source=source) if text else None

    if ftype == "launch_option":
        val = _clean_str(raw.get("value", ""))
        return Fix("launch_option", value=val, verified=verified, source=source) if val else None

    if ftype == "file":
        path = str(raw.get("path", "")).strip()
        content = raw.get("content", "")
        if not path or not isinstance(content, str):
            return None
        # Home-only: reject absolute system paths and traversal. A community DB
        # must not be able to write outside the user's own config.
        expanded = os.path.expanduser(path)
        home = os.path.expanduser("~")
        real = os.path.normpath(expanded)
        if not real.startswith(home + os.sep):
            return None
        return Fix("file", path=path, content=content, verified=verified, source=source)

    if ftype == "tool_action":
        action = str(raw.get("action", "")).strip()
        if action not in ALLOWED_TOOL_ACTIONS:
            return None
        return Fix("tool_action", action=action, verified=verified, source=source)

    return None


def _parse_when(raw):
    if not isinstance(raw, dict):
        return {}
    when = {}
    gpu = str(raw.get("gpu", "")).lower().strip()
    cpu = str(raw.get("cpu", "")).lower().strip()
    ses = str(raw.get("session", "")).lower().strip()
    if gpu in ALLOWED_GPU:
        when["gpu"] = gpu
    if cpu in ALLOWED_CPU:
        when["cpu"] = cpu
    if ses in ALLOWED_SESSION:
        when["session"] = ses
    return when


def _parse_issue(raw):
    if not isinstance(raw, dict):
        return None
    fix = _parse_fix(raw.get("fix"))
    if fix is None:
        return None
    symptom = _clean_str(raw.get("symptom", "")) or "Unknown issue"
    cause = _clean_str(raw.get("cause", ""))
    return Issue(symptom, cause, fix, when=_parse_when(raw.get("when")))


def _valid_db_text(text):
    """True if `text` parses as a games.yaml with at least a `games:` list.
    Used to reject a corrupt or truncated download before it touches the cache."""
    if not HAVE_YAML:
        return False
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    return isinstance(data, dict) and isinstance(data.get("games"), list)


def resolve_db_path():
    """The freshest usable database: the downloaded cache if present and valid,
    else the copy bundled with the app. main's games.yaml is always a superset of
    any release's bundled copy, so preferring the cache is always correct."""
    try:
        if os.path.isfile(CACHE_DB):
            with open(CACHE_DB, encoding="utf-8") as f:
                if _valid_db_text(f.read()):
                    return CACHE_DB
    except OSError:
        pass
    return DB_PATH


def _throttled():
    try:
        return (time.time() - os.path.getmtime(_STAMP)) < UPDATE_INTERVAL
    except OSError:
        return False


def maybe_update(force=False, timeout=10):
    """Refresh the cached database from GitHub, at most once a day. Returns:
    'updated' (new content cached), 'current' (already latest), 'skipped'
    (throttled / no YAML), or 'offline: <reason>'. Never raises — the app keeps
    working on the cached or bundled copy whatever happens.

    The stamp is only written on a successful fetch, so an offline first run
    retries on the next launch rather than going quiet for a day."""
    if not HAVE_YAML:
        return "skipped"
    if not force and _throttled():
        return "skipped"
    try:
        req = urllib.request.Request(DB_URL, headers={"User-Agent": "gaming-command-center"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception as e:                      # network / URL / timeout
        return f"offline: {e}"
    if not _valid_db_text(text):
        return "offline: remote database is not valid YAML"
    old = ""
    try:
        with open(CACHE_DB, encoding="utf-8") as f:
            old = f.read()
    except OSError:
        pass
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, prefix=".games-", suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, CACHE_DB)               # atomic swap
        open(_STAMP, "w").close()               # record the successful check
    except OSError as e:
        return f"offline: {e}"
    return "updated" if text != old else "current"


def load_games(path=None):
    """Parse games.yaml into {steam_id: Game}. Returns (games, error).

    With no path, loads the freshest available copy (downloaded cache or the
    bundled database). Never raises for bad data — malformed entries are skipped
    so one bad community PR cannot break the whole database for everyone.
    """
    if not HAVE_YAML:
        return {}, "PyYAML not installed (pacman -S python-yaml)"
    if path is None:
        path = resolve_db_path()
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return {}, "games.yaml not found"
    except yaml.YAMLError as e:
        return {}, f"games.yaml is not valid YAML: {e}"

    if not isinstance(data, dict) or not isinstance(data.get("games"), list):
        return {}, "games.yaml has no 'games:' list"

    games = {}
    for raw in data["games"]:
        if not isinstance(raw, dict):
            continue
        platform = str(raw.get("platform", "steam")).lower().strip() or "steam"
        if platform not in ALLOWED_PLATFORMS:
            continue

        if platform == "steam":
            # Steam games are keyed (and matched at runtime) by their appid.
            try:
                steam_id = int(raw.get("steam_id"))
            except (TypeError, ValueError):
                continue
            key = steam_id
        else:
            # Non-Steam titles carry no appid — key them by "platform:name" so
            # they still have a stable identity in the database.
            steam_id = None
            nm = str(raw.get("name", "")).strip()
            if not nm:
                continue
            key = f"{platform}:{nm.lower()}"

        name = str(raw.get("name", "")).strip() or f"App {steam_id}"
        issues = [i for i in (_parse_issue(r) for r in raw.get("issues", []) or []) if i]
        aliases = [str(a).lower() for a in raw.get("aliases", []) or []]
        play = str(raw.get("playability", "")).lower().strip()
        play = play if play in ALLOWED_PLAYABILITY else None
        preason = _clean_str(raw.get("playability_reason", ""))
        psource = _clean_str(raw.get("playability_source", ""))
        games[key] = Game(name, steam_id, issues, aliases,
                          playability=play, playability_reason=preason,
                          playability_source=psource, platform=platform)

    return games, ""


if __name__ == "__main__":
    games, err = load_games()
    if err:
        print("ERROR:", err)
        raise SystemExit(1)
    print(f"Loaded {len(games)} games from games.yaml\n")
    for g in games.values():
        print(f"  {g.name}  (steam_id {g.steam_id})")
        for iss in g.issues:
            tag = f" [{', '.join(f'{k}={v}' for k, v in iss.when.items())}]" if iss.when else ""
            kind = iss.fix.type + (f":{iss.fix.action}" if iss.fix.action else "")
            print(f"    - {iss.symptom}{tag}")
            print(f"        fix: {kind}")
