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
"""Community report statistics — the read side of the self-sustaining loop.

Users vote "worked / didn't work" on a fix; those reports land in a public
stream (GitHub issues + a Google Form sheet). A scheduled GitHub Action tallies
them nightly into reports.json at the repo root. This module fetches that file
(exactly like game_db fetches games.yaml) and answers "how many people confirmed
this fix, on what hardware?" — so the app can show a community-confirmed count
that grows on its own, with no maintainer action.

reports.json shape (produced by tools/aggregate_reports.py):
    {
      "generated": "2026-07-15T00:00:00Z",
      "fixes": {
        "<appid>|<fix summary>": {
          "worked": 12, "failed": 1,
          "gpu": {"nvidia": [10, 1], "amd": [2, 0]}   # [worked, failed]
        }
      }
    }
"""
import os
import json
import time
import tempfile
import urllib.request

REPORTS_URL = "https://raw.githubusercontent.com/LordHayne/GCC/main/reports.json"
CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "gaming-command-center")
CACHE_FILE = os.path.join(CACHE_DIR, "reports.json")
_STAMP = os.path.join(CACHE_DIR, "reports-last-check")
UPDATE_INTERVAL = 24 * 3600   # at most one network check per day

# How many positive reports before the app is willing to show a fix as
# "community-confirmed", and the minimum share of positive reports. Kept modest
# so the tier means something without needing a maintainer, but high enough that
# a single vote (or a lone troll) can't flip a fix. verified: true (the green
# star) is always separate and always a human decision.
CONFIRM_MIN_WORKED = 3
CONFIRM_MIN_RATIO = 0.6


def fix_key(appid, fix_summary):
    """Stable identifier for one fix, shared by the app and the aggregator.
    Whitespace is collapsed so the app's rendered summary and the aggregator's
    parsed text key the same way."""
    return f"{appid}|{' '.join(str(fix_summary).split())}"


def _valid(data):
    return isinstance(data, dict) and isinstance(data.get("fixes"), dict)


def load():
    """The cached tally, or {} if nothing has been fetched yet. Never raises."""
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if _valid(data) else {}
    except (OSError, ValueError):
        return {}


def _throttled():
    try:
        return (time.time() - os.path.getmtime(_STAMP)) < UPDATE_INTERVAL
    except OSError:
        return False


def maybe_update(force=False, timeout=10):
    """Refresh the cached tally from GitHub, at most once a day. Returns
    'updated' / 'current' / 'skipped' / 'offline: <reason>'. Never raises — the
    app keeps working (just without counts) whatever happens. The repo may not
    even have a reports.json yet; a 404 is a normal, quiet 'offline'."""
    if not force and _throttled():
        return "skipped"
    try:
        req = urllib.request.Request(
            REPORTS_URL, headers={"User-Agent": "gaming-command-center"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception as e:                      # network / 404 / timeout
        return f"offline: {e}"
    try:
        data = json.loads(text)
    except ValueError:
        return "offline: remote reports.json is not valid JSON"
    if not _valid(data):
        return "offline: remote reports.json has no 'fixes' map"
    old = ""
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            old = f.read()
    except OSError:
        pass
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, prefix=".reports-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, CACHE_FILE)             # atomic swap
        open(_STAMP, "w").close()               # record the successful check
    except OSError as e:
        return f"offline: {e}"
    return "updated" if text != old else "current"


def counts_for(data, appid, fix_summary):
    """Return {'worked', 'failed', 'gpu'} for one fix, or None if unreported.
    `data` is a loaded reports.json (pass load() once, reuse for the whole page)."""
    if not _valid(data):
        return None
    entry = data["fixes"].get(fix_key(appid, fix_summary))
    if not isinstance(entry, dict):
        return None
    worked = int(entry.get("worked", 0) or 0)
    failed = int(entry.get("failed", 0) or 0)
    if worked == 0 and failed == 0:
        return None
    return {"worked": worked, "failed": failed, "gpu": entry.get("gpu", {})}


def is_community_confirmed(counts):
    """True if a fix has crossed the community-confirmed threshold — enough
    positive reports, mostly positive. Independent of verified (the green star)."""
    if not counts:
        return False
    worked, failed = counts["worked"], counts["failed"]
    total = worked + failed
    return worked >= CONFIRM_MIN_WORKED and total and (worked / total) >= CONFIRM_MIN_RATIO
