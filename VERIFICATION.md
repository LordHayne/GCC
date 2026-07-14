# Fix Database — Verification & Maintenance Plan

Design notes for how `games.yaml` stays trustworthy and current as it grows.
Nothing here is built yet except the `verified` flag; this is the agreed
direction so we can pick it up later without re-deriving it.

## Two separate questions

Keep these apart — they're often conflated:

- **Verification:** how does a fix become trustworthy? (Does it actually work?)
  Answered by the trust levels + reporting below.
- **Distribution:** how and how fast does `games.yaml` reach the user?
  Answered by the distribution section further down.

Both matter, and they're independent. The `game_db` trust boundary (only four
fix types load, never a shell command) is what makes *both* safe — no matter who
writes to the database or how it's delivered, the app can't run arbitrary code.

## The problem

One person cannot verify the database. Nobody can buy and test every game, and
a per-game fix that nobody confirmed is a guess. But the whole value of the tool
is "one click and it works" — the first user who applies a fix that does nothing
(or breaks their game) is gone, and posts a bad comment under the launch thread.

So verification has to scale past the maintainer, the same way ProtonDB scaled
past any single tester: the community reports what worked, and confidence builds
from many independent confirmations.

## Three trust levels

A fix is never just "in the database" — it carries a trust level the UI shows:

| Level | Meaning | Badge |
|-------|---------|-------|
| **untested** | Fresh from a PR, nobody has confirmed it | amber "untested" |
| **community-confirmed** | N users reported "it worked" | blue, with the count |
| **verified** | A human (maintainer) gave it the final sign-off | green "Verified" |

Only `untested` and `verified` exist today (the `verified: true` flag in
`games.yaml`). `community-confirmed` needs the reporting pipeline below.

## Roadmap — start lean, scale on demand

**Do not build a server first.** A server means hosting cost, uptime, GDPR,
abuse handling — infrastructure to maintain before we know anyone uses the app.
GitHub is already the backend; use it until demand justifies more.

### Phase 1 — GitHub as the backend (no infrastructure)

- Each fix gets a "Worked / Didn't work" control in the app.
- It opens a **pre-filled GitHub issue** in the browser: game, fix id, the
  result, and basic system info (GPU vendor, session, Proton version).
- The maintainer reads the reports and sets `verified: true` by commit when
  enough credible confirmations exist.
- Cost: zero. Also validates whether people report at all before we invest more.

### Phase 2 — lightweight aggregation (only once Phase 1 shows traction)

- **Cloudflare Workers + D1 (SQLite)** — serverless, near-free at small scale,
  no VPS to maintain.
- App POSTs an anonymous report: `{appid, fix_id, result, client_hash}`.
- App GETs aggregates and shows "👍 42 / 👎 3".
- Past a threshold a fix becomes **community-confirmed** automatically.

## Security — votes are attackable

Crowd counts can be gamed: 10 fake 👍 could "confirm" a bad fix, and confirmed
fixes are one-click applied. Two defenses:

1. **The `game_db` whitelist still holds.** No matter how many votes a fix has,
   it can only ever be an `info`, a `launch_option`, a home-only `file`, or a
   whitelisted `tool_action`. A vote can never turn a fix into a shell command.
   This is the hard guarantee and it already exists.
2. **Keep a human in the loop for the green badge.** Votes produce
   *community-confirmed*; a maintainer review produces *verified*. The level
   that says "apply this without thinking" is never purely vote-driven.

Additional Phase-2 hardening if abuse shows up: rate-limit by client hash,
weight reports that include real system info, flag statistical anomalies.

## Distribution — how the database reaches users

Separate from verification: once a fix is in `games.yaml`, how do users get it?
Three shapes, on a speed-vs-control axis:

1. **Bundled only (PR-based, like AUR).** `games.yaml` ships with the app; new
   entries arrive by reviewed PR. Safe and version-controlled, but a fix can be
   days from PR to a user's machine — they only get it on the next release.
2. **Fetched (like ProtonDB).** The app pulls the latest `games.yaml` on start.
   Users get merged fixes immediately. **No server needed — GitHub Raw is free
   hosting**; the app just reads the raw URL. (Server cost is only a concern if
   we move to the Phase-2 aggregation API, and even that is near-free.)
3. **Hybrid (recommended).** Bundle `games.yaml` as an offline baseline, and
   pull updates optionally — on start, or via a `--update-db` action. Works
   fully offline, updates when online.

**Recommendation: hybrid.** Ship a baseline, fetch the newest from GitHub Raw,
cache it. Keep it simple:

- A single `db_version:` integer at the top of `games.yaml`. The app compares
  versions and replaces its cache when the remote is higher. No per-version
  files (`games-v1.yaml`, …) — Git already versions the file, and it's a few KB,
  so fetch the whole thing rather than diffing.
- "Auto-merge after review" is a contradiction; the real flow is: PR → human
  review → merge → the merged file is what everyone fetches.
- The `game_db` whitelist is what makes fetch-on-start safe to even consider.

## Current state

- `verified` flag in `games.yaml` and `game_db.py` — done.
- Green "Verified" / amber "untested" badges in the Games page — done.
- Reporting pipeline (Phase 1, verification) — not started.
- Aggregation server (Phase 2, verification) — not started.
- Database fetch/update (distribution) — not started; bundled-only today.

## Open questions for later

- Report identity: fully anonymous, or tie to a Steam ID / hashed machine id to
  curb ballot-stuffing? (Privacy vs. abuse-resistance.)
- Thresholds: how many confirmations, and what positive ratio, for
  community-confirmed?
- Does the app auto-pull the database (from GitHub raw or the Phase-2 API), or
  ship it with releases only? Auto-pull spreads fixes faster but pushes each
  merged entry to everyone immediately.
