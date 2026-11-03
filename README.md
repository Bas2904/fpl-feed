# fpl-feed

A free GitHub Action that keeps a complete, verifiable copy of the public Fantasy Premier League API data for one
mini-league (Skjold Ubois, league 600517; manager entry 4358815). It feeds the FPL project's scripts
(`fpl_data.py feed / sell / league`), so no FPL page has to pass through a summarising fetch tool.

No login, cookie or secret is used or stored: every endpoint below is public.

## What it saves

| When | What | Where |
|---|---|---|
| Every 2 h (`feed`, :17) | the json file (`bootstrap-static`), all 380 fixtures, `event-status`, league standings (every page), each member's `entry`, `history` (chips) and picks for every GW whose deadline has passed, my `transfers`, `event/N/live` while a GW is live (each finished GW is stored once more and frozen after FPL's `data_checked`) | branch `data` |
| Once a day (first run 20 h after the last) | `element-summary` for my squad, every member's current picks, the `watchlist` in `config.json`, the top 120 by ownership and the top 40 by `ep_next`; `team/set-piece-notes` | branch `data` |
| 2 h before each deadline (`deadline-archive`, :47, sleeps until T-2h) | dated, xz-compressed json file + `index.json` (sha256, time before the deadline) | `archive/2026-27/` on `main` |

Not archived: Solio and FFIQ projections. Both sites' terms forbid scripted collection and redistribution
(checked 3 Nov 2026), and this repository is public.

## How it stays small (< 1 GB)

- `data` is always ONE commit, force-pushed, so its history does not grow (about 35 MB at the end of the season).
- A run publishes only when a file's content changed. For the json file, the ownership and transfer counters
  (they move every minute) do not count as a change; news, status, prices, roles, ep_next and deadlines do.
- `main` only gains about 38 small archives (~250 KB each).
- Every run records the repository size in `status.json`; it warns above 700 MB and fails above 900 MB.

## Files on `data`

`manifest.json` (every file: sha256, bytes, when its content last changed), `status.json` (last run: requests,
errors, warnings, changed files, current GW, next deadline, repo size), `bootstrap-static.json`, `fixtures.json`,
`event-status.json`, `league/standings.json`, `entries/<id>/{entry,history}.json`, `entries/4358815/transfers.json`,
`picks/gw<N>/<id>.json`, `live/gw<N>.json`, `element-summary/<id>.json`, `set-piece-notes.json`, `probe.json`.
All JSON exactly as FPL returned it (compact).

## Test that FPL answers GitHub

Actions → feed → Run workflow → task `probe`. The run summary shows one row per endpoint (HTTP code, bytes, check);
the same table is saved as `probe.json` on `data`. If the json file row is not 200, FPL is blocking GitHub's
servers; see "If FPL blocks GitHub" below.

## Starting a run without the Actions page

Pushing a change to `trigger/task.txt` on `main` (content `probe`, `core` or `daily`) starts the feed workflow
with that task. This is how Claude starts runs: the proxy it works through cannot call the Actions API.
First live runs (3 Nov 2026): probe 11/11 endpoints HTTP 200 from GitHub's runners; core run 157 requests, 0 errors.

A second public source of the json file: randdalf/fplcache (4 snapshots a day since 2021, GitHub Actions), read by
`fpl_data.py fplcache` in the FPL project as a fallback.

## Opsætning (dansk)

1. github.com → **+** → **New repository**. Navn: `fpl-feed`. **Public** (Actions er gratis og ubegrænsede for
   offentlige repos). Sæt flueben i "Add a README file". **Create repository**.
2. Upload filerne: **Add file → Upload files**, træk hele indholdet af zip-filen ind, også mappen `.github`
   (på Mac: vis skjulte mapper med Cmd+Shift+.). **Commit changes**. På iPhone: upload `feed.py`, `config.json` og
   `README.md` via **Upload files**, og opret de to workflows med **Add file → Create new file**, filnavn
   `.github/workflows/feed.yml` og `.github/workflows/archive.yml`, og indsæt indholdet.
3. **Settings → Actions → General → Workflow permissions**: vælg **Read and write permissions** → **Save**.
4. **Actions** (klik "I understand my workflows, go ahead and enable them" hvis den spørger) → **feed** →
   **Run workflow** → task **probe** → **Run workflow**. Vent ca. 1 minut, åbn kørslen og se tabellen.
5. Alle rækker "yes": kør **feed** én gang mere med task **core**. Herefter kører den selv hver 2. time.
6. Notifikationer: GitHub mailer dig, når en planlagt kørsel fejler (Settings → Notifications → Actions).

## If FPL blocks GitHub

The probe's first row is 403/429/5xx or times out. Options, best first:
1. A self-hosted runner on a machine at home (e.g. a Raspberry Pi): change `runs-on: ubuntu-latest` to
   `runs-on: self-hosted` in both workflows. Residential IPs are not blocked. These workflows have no
   `pull_request` trigger, so code from forks cannot run on the runner.
2. Keep the old routine (the fetch tool and the manager's json file download); the project falls back to it.

## Stop or remove

Actions → feed / deadline-archive → "..." → Disable workflow. Or delete the repository.
