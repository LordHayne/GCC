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

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "games.yaml")

# The only fix types the app will ever act on. A fix with any other type is
# discarded on load. `tool_action` is further constrained below.
ALLOWED_FIX_TYPES = {"info", "launch_option", "file", "tool_action"}

# tool_action fixes may only name one of these — each maps to something the app
# already knows how to do safely. No free-form action string is honoured.
ALLOWED_TOOL_ACTIONS = {"game_mode"}

ALLOWED_GPU = {"nvidia", "amd", "intel"}
ALLOWED_SESSION = {"wayland", "x11"}


class Fix:
    def __init__(self, ftype, value="", path="", content="", action=""):
        self.type = ftype
        self.value = value      # info text / launch option string
        self.path = path        # file fix: target path
        self.content = content  # file fix: file contents
        self.action = action    # tool_action: which action

    @property
    def is_applicable(self):
        """Can the app apply this itself, or is it advisory (info)?"""
        return self.type in ("launch_option", "file", "tool_action")


class Issue:
    def __init__(self, symptom, cause, fix, when=None):
        self.symptom = symptom
        self.cause = cause
        self.fix = fix
        self.when = when or {}   # {"gpu": "nvidia", "session": "wayland"}

    def matches_system(self, gpu=None, session=None):
        """True if this issue is relevant to the current setup. Issues without
        a `when` block apply to everyone."""
        if "gpu" in self.when and gpu and self.when["gpu"] != gpu:
            return False
        if "session" in self.when and session and self.when["session"] != session:
            return False
        return True


class Game:
    def __init__(self, name, steam_id, issues, aliases=None):
        self.name = name
        self.steam_id = steam_id
        self.issues = issues
        self.aliases = aliases or []


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

    if ftype == "info":
        text = _clean_str(raw.get("value", ""))
        return Fix("info", value=text) if text else None

    if ftype == "launch_option":
        val = _clean_str(raw.get("value", ""))
        return Fix("launch_option", value=val) if val else None

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
        return Fix("file", path=path, content=content)

    if ftype == "tool_action":
        action = str(raw.get("action", "")).strip()
        if action not in ALLOWED_TOOL_ACTIONS:
            return None
        return Fix("tool_action", action=action)

    return None


def _parse_when(raw):
    if not isinstance(raw, dict):
        return {}
    when = {}
    gpu = str(raw.get("gpu", "")).lower().strip()
    ses = str(raw.get("session", "")).lower().strip()
    if gpu in ALLOWED_GPU:
        when["gpu"] = gpu
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


def load_games(path=DB_PATH):
    """Parse games.yaml into {steam_id: Game}. Returns (games, error).

    Never raises for bad data — malformed entries are skipped so one bad
    community PR cannot break the whole database for everyone.
    """
    if not HAVE_YAML:
        return {}, "PyYAML not installed (pacman -S python-yaml)"
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
        try:
            steam_id = int(raw.get("steam_id"))
        except (TypeError, ValueError):
            continue
        name = str(raw.get("name", "")).strip() or f"App {steam_id}"
        issues = [i for i in (_parse_issue(r) for r in raw.get("issues", []) or []) if i]
        aliases = [str(a).lower() for a in raw.get("aliases", []) or []]
        games[steam_id] = Game(name, steam_id, issues, aliases)

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
