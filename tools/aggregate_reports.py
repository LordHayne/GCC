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
"""Aggregate community fix reports into reports.json — the engine of the
self-sustaining verification loop.

Run nightly by .github/workflows/aggregate-reports.yml. It reads reports from
two public streams and tallies them per fix, so the app's "community-confirmed"
counts grow with no maintainer action:

  1. GitHub issues labelled `fix-report` (the account channel) — via the REST
     API. Issues additionally labelled invalid/spam/duplicate/wontfix are
     excluded, which is the maintainer's veto against a bad report.
  2. A Google Form response sheet published as CSV (the no-account channel) —
     via the FORM_CSV_URL env var (optional; skipped if unset).

Both channels carry the same fields (game, appid, fix, result, GPU, …) because
the app fills them from one place. The output keys match report_stats.fix_key,
so the app maps counts straight onto its fix cards.

This script only ever READS public data and WRITES reports.json. It has no
access to anything the app can act on — reports.json is pure counts.
"""
import os
import re
import sys
import csv
import json
import io
import urllib.request

# Import the shared key function from the app so keys never drift.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from report_stats import fix_key   # noqa: E402

REPO = os.environ.get("GITHUB_REPOSITORY", "LordHayne/GCC")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
FORM_CSV_URL = os.environ.get("FORM_CSV_URL", "").strip()
OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "reports.json")

# A fix-report issue also carrying one of these labels is the maintainer saying
# "don't count this" — spam, a mistake, a dupe. Excluded from the tally.
EXCLUDE_LABELS = {"invalid", "spam", "duplicate", "wontfix"}

GPU_VENDORS = (("nvidia", "nvidia"), ("geforce", "nvidia"), ("rtx", "nvidia"),
               ("gtx", "nvidia"), ("radeon", "amd"), ("amd", "amd"),
               ("intel", "intel"), ("arc", "intel"))


def _vendor(gpu_text):
    """Map a GPU string to a coarse vendor bucket, so counts can be shown
    per-vendor (a fix may work on NVIDIA but not AMD)."""
    t = (gpu_text or "").lower()
    for needle, vendor in GPU_VENDORS:
        if needle in t:
            return vendor
    return "other"


def _http_json(url):
    headers = {"User-Agent": "gcc-aggregator", "Accept": "application/vnd.github+json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace")), r.headers


def _next_link(headers):
    """Follow GitHub's Link header for pagination."""
    link = headers.get("Link", "")
    for part in link.split(","):
        m = re.search(r'<([^>]+)>;\s*rel="next"', part)
        if m:
            return m.group(1)
    return None


# ---------- report extraction ----------

def _result_worked(text):
    """True = worked, False = didn't, None = can't tell (skip)."""
    t = (text or "").lower()
    if "not work" in t or "didn't" in t or "did not" in t or "❌" in t:
        return False
    if "worked" in t or "✅" in t:
        return True
    return None


def _from_issue_body(body):
    """Pull (appid, fix_summary, worked, gpu) out of an issue body in the exact
    format the app writes. Returns None if it doesn't look like a report."""
    if not body:
        return None
    appid = fix = gpu = None
    worked = None
    for line in body.splitlines():
        s = line.strip()
        m = re.search(r"\(appid\s+(\d+)\)", s)
        if m:
            appid = int(m.group(1))
        if s.lower().startswith("fix:"):
            fix = s.split(":", 1)[1].strip()
        if s.lower().startswith("result:"):
            worked = _result_worked(s)
        if s.lower().startswith("system:"):
            g = re.search(r"gpu:\s*([^·]+)", s, re.I)
            if g:
                gpu = g.group(1).strip()
    if appid is None or not fix or worked is None:
        return None
    return appid, fix, worked, gpu


def _from_csv_row(row):
    """Pull one report out of a Google Form CSV row (columns named after the
    form fields: Game, AppID, Fix, Result, GPU, …). Case-insensitive headers."""
    low = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
    try:
        appid = int(re.sub(r"\D", "", low.get("appid", "")) or "0")
    except ValueError:
        appid = 0
    fix = low.get("fix", "")
    worked = _result_worked(low.get("result", ""))
    gpu = low.get("gpu", "")
    if not appid or not fix or worked is None:
        return None
    return appid, fix, worked, gpu


# ---------- sources ----------

def collect_issues():
    reports = []
    if not TOKEN:
        # Unauthenticated works too (public repo, 60 req/hr) but Actions always
        # provides a token; warn if somehow missing.
        print("note: no GITHUB_TOKEN — using unauthenticated API (low rate limit)")
    url = (f"https://api.github.com/repos/{REPO}/issues"
           f"?labels=fix-report&state=all&per_page=100")
    pages = 0
    while url and pages < 50:
        items, headers = _http_json(url)
        pages += 1
        for it in items:
            if "pull_request" in it:            # /issues also returns PRs
                continue
            labels = {l["name"].lower() for l in it.get("labels", [])}
            if labels & EXCLUDE_LABELS:
                continue
            rep = _from_issue_body(it.get("body", ""))
            if rep:
                reports.append(rep)
        url = _next_link(headers)
    print(f"issues: {len(reports)} usable reports")
    return reports


def collect_form():
    if not FORM_CSV_URL:
        print("form: FORM_CSV_URL not set — skipping no-account channel")
        return []
    req = urllib.request.Request(FORM_CSV_URL, headers={"User-Agent": "gcc-aggregator"})
    with urllib.request.urlopen(req, timeout=30) as r:
        text = r.read().decode("utf-8", "replace")
    reports = []
    for row in csv.DictReader(io.StringIO(text)):
        rep = _from_csv_row(row)
        if rep:
            reports.append(rep)
    print(f"form: {len(reports)} usable reports")
    return reports


# ---------- tally ----------

def tally(reports):
    fixes = {}
    for appid, fix, worked, gpu in reports:
        key = fix_key(appid, fix)
        e = fixes.setdefault(key, {"worked": 0, "failed": 0, "gpu": {}})
        bucket = "worked" if worked else "failed"
        e[bucket] += 1
        v = _vendor(gpu)
        wf = e["gpu"].setdefault(v, [0, 0])
        wf[0 if worked else 1] += 1
    return fixes


def main():
    reports = collect_issues() + collect_form()
    fixes = tally(reports)
    # Deterministic output so an unchanged tally produces an identical file (no
    # empty commits). The timestamp is intentionally omitted from the committed
    # file for the same reason; freshness comes from the commit itself.
    payload = {"fixes": {k: fixes[k] for k in sorted(fixes)}}
    new = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    old = ""
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            old = f.read()
    except OSError:
        pass
    if new == old:
        print(f"reports.json unchanged ({len(fixes)} fixes) — nothing to commit")
        return
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(new)
    print(f"reports.json written: {len(fixes)} fixes, {len(reports)} reports")


if __name__ == "__main__":
    main()
