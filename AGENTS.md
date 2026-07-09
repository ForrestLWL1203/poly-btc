# AGENTS.md

## Current Project

This repo is a Hyperliquid copy-trade product. Old non-Hyperliquid research tooling has been removed
from the active tree.

Active Hyperliquid product:

- Scanner/discovery: `hl_discover.py`, `hl/scanner.py`, `hl/metrics.py`, `hl/fills.py`
- Observer/paper copy engine: `hl_observe.py`, `hl/observer.py`
- Dashboard API: `hl_dashboard.py`, `hl/api.py`
- Dashboard frontend: `web/app.jsx`, compiled to `web/app.js`
- Launcher/ops tool: `launcher/launcher.py`, `launcher/server.py`, `launcher/core/*`, `launcher/web/*`

Read `CLAUDE.md` first for private current-memory notes, then verify against code.

## Live VPS Access

Live VPS host/user/key details are private and belong in local `CLAUDE.md`, not in committed docs.
Use the launcher-managed SSH key referenced there, and do not rely on the default SSH identity.
Do not print key contents.

## Codex Ops Pitfalls

- For VPS SSH, copy the known-good command/options from private local notes; do not hand-type or improvise
  `KexAlgorithms`. A single invalid token makes SSH fail locally before any command reaches the VPS.
- For complex remote SQL/Python, do not nest heredocs inside a quoted SSH command from local zsh. Pipe a
  local heredoc to remote stdin instead, e.g. `cat <<'PY' | <known-good ssh> 'cd /root/poly-btc && .venv/bin/python -'`.
- If an SSH/local shell command fails before reaching the VPS, state that clearly and rerun with the safer
  stdin-pipe pattern before drawing conclusions from VPS state.
- To verify whether a dashboard-triggered scan is truly a forced full re-fetch, do not infer from
  `manual=1` or the UI label. Check `commands.payload_json` contains `{"full": true}`, or the scanner
  process/CLI used `--full`, or the completed `scan_runs.full=1`. Otherwise it may be only a manual full
  workset scan while profile fill fetching still uses incremental `candidate_fills` cache unless
  `_due_for_full_resync()` is true.
- Do not restart `hl-scan.service` during deploy just to pick up code. It is a one-shot scan unit
  activated by `hl-scan.timer`, not a long-running daemon; `systemctl restart hl-scan.service` starts
  a real scan immediately. Use `systemctl reset-failed hl-scan.service` to clear failed state, and use
  `python3 hl_discover.py --db data/hl.db regate` when you only need no-network watchlist/copy-BT
  recomputation from cached fills.
- When deploying or restarting live services, never include `hl-scan.service` in a broad restart command
  unless the user explicitly asked to start a scan. `hl-scan.service` is a one-shot scanner, so
  `systemctl restart hl-scan.service` immediately launches a new discovery scan even if the 24h cadence
  has not elapsed. For ordinary code deploys, restart only the affected long-running service, usually
  `hl-observe.service` and/or `hl-dashboard.service`; leave `hl-scan.timer` alone.

## Secrets And Data

Never print, summarize, commit, or copy secret values.

Sensitive paths:

- `secret/`
- `launcher/data/keys/`
- `launcher/data/targets.json`
- `data/`

Filenames and schemas are okay to inspect when needed. Secret values are not.

## Runtime Architecture

All active state is in one SQLite DB, normally `data/hl.db`, using WAL.

The control-plane invariant:

- Dashboard may read business tables.
- Dashboard writes only `commands` and `params`.
- Observer/Scanner write trading/discovery state.
- `process_status` and `scan_progress` drive UI liveness/progress.

Scanner:

- Maintains `leaderboard -> profile -> watchlist`.
- `harvest()` uses Hyperliquid leaderboard data for candidates.
- `_profile_one()` pulls fills, rebuilds episodes, reads portfolio/open state, and computes metrics.
- `metrics.gates_structural()` and `metrics.gates_state()` are binary copyability gates.
- `metrics.score()` returns the raw profile score in native `[0, 1]`; API displays `0-100`.
- Applies DB scanner params through `params.apply_scanner_params()`.
- `hl/follow_score.py` computes the final copy-follow score from raw profile score plus copy backtest
  evidence (`copy_bt_*` 30d/14d/7d, sample confidence, fill rate, liquidations, fee drag).
- `watchlist.score` is the final copy-follow score. `profile.score` remains the raw profile score.
- `scanner.refresh_watchlist()` ranks active wallets by copy-follow score, then auto-updates
  `MIN_FOLLOW_SCORE`: quality cliff first, otherwise capacity target; below the minimum score floor is never followed.
- Updating the follow line inserts a `reload_params` command so the observer refreshes the target set.
- Scanner/regate/watchlist/auto-tune decisions are snapshotted to `pipeline_audit`.
  Use `/api/pipeline-audit?limit=100&stamp=&stage=&addr=` or SQL against `pipeline_audit` to answer
  why a wallet was active/rejected/followed/below-line and what copy-backtest/auto-tune evidence was used.

Post-scan auto tuning:

- `hl/auto_tune.py` uses the current followed wallet set (`watchlist.score >= MIN_FOLLOW_SCORE` and enabled controls).
- Tuning is portfolio-level, not per-wallet: it merges all selected wallets' `candidate_fills` by time and calls
  `run_backtest("portfolio", fills, ...)` with one shared simulated account.
- The portfolio replay sees funding contention: shared balance/available, open positions, deploy cap, per-coin cap,
  no-cash skips, deploy-cap skips, coin-full skips, add budget pressure, and peak concurrency.
- The sizing grid tunes margin upper bounds, leverage caps, and the full-power deployment line.
- The add grid tunes smart-add core params (`ADD_GAP_K`, `ADD_GAP_SHRINK_G`, `ADD_MAX_HARD`) after the sizing candidate.
- Automatic wallet-count selection first tries an N-prefix portfolio replay: candidate top-N sets are replayed
  through one shared simulated account with current follow params, and the line moves to the best
  profitable/capacity-safe prefix only when it materially beats the capacity target. If cached fills are
  insufficient/too large, it falls back to `choose_follow_line` (score cliff or capacity target). This selector
  uses current params only; after it sets `MIN_FOLLOW_SCORE`, sizing/add grids run on the selected final set.

Observer:

- Forward-only: starts cursors at current time and does not copy historical fills.
- Signal is REST `userFillsByTime`.
- Pricing is WS BBO for standard perps and REST `l2Book` for builder/stock perps.
- Persists copy state in `copy_position`/`copy_action`.
- Runs taker and maker-shadow paper books when configured.
- Consumes pause/resume/close/wallet_toggle/reload_params commands.
- Reloads follow/sizing params through `Observer._reload_params()`.

## Commands

Dashboard:

```bash
python3 hl_dashboard.py --db data/hl.db --static web --host 127.0.0.1 --port 8810
```

Scanner:

```bash
python3 hl_discover.py --db data/hl.db scan --days 14 --scan-interval 8
python3 hl_discover.py --db data/hl.db regate
python3 hl_discover.py --db data/hl.db watchlist --top 40
```

Observer:

```bash
python3 hl_observe.py --db data/hl.db observe
python3 hl_observe.py --db data/hl.db report
```

Launcher:

```bash
python3 launcher/launcher.py --port 8799 --no-browser
```

Mock dashboard preview:

```bash
python3 web/dev/seed_mock.py data/hl_mock.db
python3 web/dev/mock_consumer.py data/hl_mock.db
DASH_PASSWORD=mock123 python3 hl_dashboard.py --db data/hl_mock.db --static web --host 127.0.0.1 --port 8810
```

## Frontend Build

Both frontends are precompiled React without bundling React itself. Edit JSX, then compile JS.

Dashboard:

```bash
web/build.sh
```

Launcher:

```bash
launcher/web/build.sh
```

Do not hand-edit minified `web/app.js` or `launcher/web/app.js`.

## Deployment And Ops

VPS mode:

- Clones/resets code from GitHub with `git reset --hard origin/<branch>`.
- Installs `hl-dashboard.service`, `hl-observe.service`, `hl-scan.service`, `hl-scan.timer`.
- Enables dashboard and scan timer.
- Enables observer but does not start it automatically; dashboard controls it.
- Optional Caddy reverse proxy handles HTTPS when a domain is configured.

Local mode:

- Launcher starts only the dashboard directly.
- Dashboard `procman` starts/stops observer and one-shot scans through detached child processes.

Before changing deployment logic, read:

- `hl/procman.py`
- `launcher/core/services.py`
- `launcher/core/templates.py`
- `launcher/core/ops.py`

## Known Caveats

- `hl/api.py` and legacy supervisor code can resolve process command failures with status `"error"`,
  while the normal command contract uses `"failed"`.
- Keep public docs free of private hosts, credentials, keys, and operational secrets.

## Verification

For Python-only changes:

```bash
python3 -m py_compile hl/*.py hl_*.py launcher/*.py launcher/core/*.py
```

For dashboard/launcher UI changes, run the relevant build script and then smoke the local server or mock preview.
