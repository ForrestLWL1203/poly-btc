# AGENTS.md

## Scope

This repository is the active Hyperliquid copy-trade product. It contains:

- leaderboard discovery and wallet profiling;
- copyability scoring and canonical copy replay;
- bounded shared-account Core selection;
- a forward-only paper Observer;
- a read-oriented Dashboard API and React dashboard;
- local/VPS process and deployment tooling.

Old non-Hyperliquid research scripts are not part of the active runtime. Read `CLAUDE.md` first for private
local notes, then verify any assumption against the current code and database schema.

## Non-negotiable invariants

- `data/hl.db` is the normal SQLite state store and runs in WAL mode.
- Dashboard code reads business state and writes only `commands` and `params`.
- Scanner and Observer are the writers of discovery/trading state. Do not make Dashboard routes mutate
  `profile`, `watchlist`, `follow_selection`, `copy_position`, or other business tables directly.
- A published, complete, current `scan_generation` plus its `follow_selection` rows is the source of truth
  for new copy opens. Do not infer production membership from `MIN_FOLLOW_SCORE`, row order, or the raw
  `watchlist` table.
- A published generation may intentionally have zero Core wallets. Do not fall back to an old score line in
  that case. Before the first successful selection generation, Observer may run idle; existing open copies
  are still managed exit-only.
- Settings saves must not start a scan. A scan starts from the explicit Dashboard action or the configured
  automatic cadence.
- Never expose, print, commit, or copy secrets, private keys, target files, live databases, or private VPS
  values. Keep those in local `CLAUDE.md` or ignored paths.

## Runtime map

| Concern | Primary files |
|---|---|
| CLI discovery | `hl_discover.py`, `hl/scanner.py` |
| Generation staging/publication | `hl/generation.py`, `hl/selection.py` |
| Profile metrics/gates | `hl/metrics.py`, `hl/scanner_copy_bt.py`, `hl/follow_score.py` |
| Cached fills/replay inputs | `hl/fills.py`, `hl/copy_data.py`, `hl/copy_evidence.py` |
| Canonical copy replay | `hl/copy_backtest.py`, `hl/copy_engine.py`, `hl/fill_transition.py` |
| Portfolio selection/tuning | `hl/selection.py`, `hl/auto_tune.py`, `hl/sizing.py` |
| Observer/paper execution | `hl_observe.py`, `hl/observer.py`, `hl/rest.py`, `hl/ws.py` |
| Dashboard API | `hl_dashboard.py`, `hl/api.py`, `hl/api_*.py` |
| Dashboard frontend | `web/app.jsx`, `web/components/*`, `web/app.css`, compiled `web/app.js` |
| Launcher/process control | `launcher/launcher.py`, `launcher/server.py`, `launcher/core/*`, `launcher/web/*` |
| Shared schema/migrations | `hl/storage.py` |
| Tunable values | `hl/config.py`, `hl/params.py`, SQLite `params` table |

## Discovery and selection pipeline

The production flow is:

`Leaderboard → staged generation → candidate workset → profile/fill cache → quality gates → derived watchlist →
explicit Core/Challenger selection → atomic publish → Observer reload → async portfolio tuning`

### 1. Generation safety

Each scan gets a generation id. Leaderboard rows are written to staging and validated before profiles are
accepted. The default validation requires:

- at least 85% of the previous valid leaderboard row count (except the first non-empty generation);
- unique wallet addresses;
- at least 99% complete leaderboard windows;
- no malformed/empty snapshot.

An invalid or incomplete generation must retain the last published generation and must not publish a new
selection, prune discovery state, or launch tuning. `scan_generation`, `pipeline_audit`, `scan_progress`, and
`scan_runs` are the operational record.

### 2. Candidate workset and profiles

- Leaderboard harvesting is a zero-per-wallet API coarse filter. The current default weekly turnover box is
  controlled by scanner params, not hardcoded in the UI.
- A fresh candidate profile fetch covers `PROFILE_FETCH_DAYS` (currently 37 days: 30-day scoring window plus
  seven warm-up days). Reported copy evidence remains 30/14/7 days.
- `candidate_fills` is the cache. Daily work normally fetches only the delta after each wallet cursor and
  merges it into the 37-day window. A page-cap/cache-gap/revision problem or the seven-day self-heal schedule
  triggers a full refetch for the affected shard/candidate.
- Current scan budgets are 60 minutes total, 15 minutes for Core/held priority refresh, a 15-minute finalization
  reserve, and a normal profile upper bound of 300. Core and wallets with open copies are outside the ordinary
  discovery cap. Workset and fill modes are recorded separately (`priority`, `rotation`, `all`; `delta`,
  `full_refetch`, or `mixed`).
- The seven-shard rolling refresh avoids a single weekly API spike. `FULL_REFRESH_SHARDS=7` and
  `CANDIDATE_MAX_RECHECK_DAYS=7` are the relevant defaults.

### 3. Quality gates and scores

`active`/`qualified` means the wallet has passed the quality and copyability requirements. It is not a promise
that every active wallet must fit into the funded Core account.

Hard qualification checks include structural copyability, valid portfolio/open-state data, copyable perp
markets, HFT/grid/heavy-DCA/spot-hedge structures, extreme concurrency, thin copy edge, minimum evidence,
recent activity, and the configured copy replay gate. Current important defaults include:

- at least five independent evidence days and seven closed copy episodes;
- copy replay net PnL after costs must be positive;
- expected margin return below 2% is treated as thin edge and is excluded;
- 30/14/7 copy windows use 7/5/5 closed-sample floors;
- actionable open rate must be at least 70% and capacity fit at least 85% for portfolio admission.

`profile.score` is the raw profile quality score. `watchlist.score` is the final copy-follow score, combining
raw quality with canonical copy evidence. The score orders qualified candidates; it is not the final Core
membership rule. `MIN_FOLLOW_SCORE` remains only as migration/display compatibility and must not be used as
the Observer's membership truth after a published selection exists.

### 4. Core/Challenger lifecycle

The persistent `wallet_registry` retains identity, roles, good/bad confirmations, data errors, and reasons.
The user-facing roles are:

- **Core** (`role=core`): Observer may open new copy positions.
- **Challenger** (`role=challenger`): qualified candidate, no new copy opens.
- **Exit-only** (`role=exit_only`): no new opens, but existing copies are managed to exit.
- **Cooldown/rejected/quarantine**: internal lifecycle/data states, not new-entry targets.

Core selection first ranks quality-qualified candidates by final copy-follow score, then evaluates them in one
shared simulated account. The selector uses a compact beam search (default seed target 10, beam width 3),
one-for-one polishing, and bounded one-for-two replacement checks. The seed target is a search depth, never a
minimum Core count. After the seed, each additional wallet must improve portfolio net PnL by at least 1% of
the current portfolio; otherwise expansion stops. The search is bounded by `MAX_TARGETS=40` and a 600-second
search budget. If replay cannot be completed safely, retain the previous published Core instead of fabricating
membership.

Selection evaluates shared balance, funding contention, open capture, capacity fit, deployment, drawdown, fee/
slippage drag, and concentration. A wallet can have a high personal score and still remain Challenger when
adding it does not improve the funded shared account. Conversely, replacement checks can remove a weak Core
wallet when a better qualified candidate improves the portfolio. There is no fixed “at least seven wallets”
requirement.

`FOLLOW_SELECTION_MODE=auto` lets the scanner publish this selection. `manual` carries the current selection
rows into the next generation and leaves membership operator-owned; it does not silently rewrite the Core.

### 5. Atomic publication and tuning

After a complete scan, the scanner:

1. rebuilds the derived watchlist;
2. computes the explicit selection;
3. writes generation-scoped selection/audit rows;
4. atomically publishes the generation;
5. launches generation-bound tuning separately.

The tuner is portfolio-level. It replays all Core fills through one shared account and searches independent
volatility-tier margin bounds, leverage caps, deployment line, and smart-add parameters. It validates finalist
combinations with non-overlapping folds, holdout and stress checks, and writes the effective portfolio replay
summary used by the Dashboard.

Current Paper defaults deliberately allow the full closed loop:

- `AUTO_TUNE_MODE=apply`;
- minimum shadow days, forward closed episodes, leverage coverage, and price-path coverage are zero for Paper;
- candidate still must pass the OOS/holdout/stress/risk gates;
- live-money deployments should use conservative shadow/coverage/forward thresholds instead.

Tuning must never mutate Core membership by re-running stale profiles. A tuner failure, timeout, memory cap, or
missing fill cache must leave the published selection intact. A later complete generation is the source of new
profile evidence. `auto_tune_state.effective_portfolio_replay` is valid only when its generation matches the
current published generation.

## Observer and execution model

- Observer is forward-only. It starts each target cursor at the current time and never backfills historical
  fills into a new copy book.
- Signal source is REST `userFillsByTime`; standard-perp pricing uses WS BBO and builder/stock pricing uses REST
  `l2Book`.
- Observer loads enabled Core addresses from the current published selection. Existing positions for removed,
  disabled, or no-longer-Core wallets stay exit-only.
- Copy state is persisted in `copy_position` and `copy_action`; taker and optional maker-shadow books are
  separate. Live `copy_position` PnL includes realized closed PnL plus unrealized PnL for open rows.
- Sizing is equity/available-balance based and volatility-tiered. Profits compound; drawdown contracts sizing
  through the configured equity curve. Isolated margin, per-coin/deploy caps, liquidity filters, and add caps
  remain hard execution boundaries.
- `COPY_STOP_ENABLE` is currently false by default. Do not assume stop-loss behavior exists unless the persisted
  follow params enable it.
- Core/strategy reloads are command-driven (`reload_params`) and do not copy historical fills.

## Dashboard contract

The dashboard reads the API and controls workers through the command/params plane. Important endpoints include:

- `/api/overview`, `/api/positions`, `/api/history`, `/api/shadow`;
- `/api/wallets?tab=followed|challenger|dropped`;
- `/api/wallets/{address}` for lazy wallet details;
- `/api/positions/{id}` for lazy position detail;
- `/api/pipeline-audit` for generation, profile, selection, watchlist, and tuner reasons;
- `/api/params`, `/api/commands`, and process/scan status endpoints.

The wallet list is intentionally light. Detail and position-detail requests are lazy. The UI labels the current
roles as “跟单中”, “候选”, and “降级”; do not reintroduce internal role/model/data columns into the operator
table without a concrete decision use.

## Commands and local verification

Run from the repository root:

```bash
# Dashboard
python3 hl_dashboard.py --db data/hl.db --static web --host 127.0.0.1 --port 8810

# Scanner / maintenance
python3 hl_discover.py --db data/hl.db serve-rescan
python3 hl_discover.py --db data/hl.db scan --days 14 --scan-interval 8
python3 hl_discover.py --db data/hl.db scan --full --days 14 --scan-interval 8
python3 hl_discover.py --db data/hl.db regate
python3 hl_discover.py --db data/hl.db repair-watchlist
python3 hl_discover.py --db data/hl.db watchlist --top 40

# Observer
python3 hl_observe.py --db data/hl.db observe
python3 hl_observe.py --db data/hl.db report

# Launcher
python3 launcher/launcher.py --port 8799 --no-browser

# Mock dashboard
python3 web/dev/seed_mock.py data/hl_mock.db
python3 web/dev/mock_consumer.py data/hl_mock.db
DASH_PASSWORD=mock123 python3 hl_dashboard.py --db data/hl_mock.db --static web --host 127.0.0.1 --port 8810
```

`scan --full` means a true profile fill refetch. A Dashboard manual rescan is not automatically a full
refetch unless its command payload requests `full=true`, the CLI uses `--full`, or the completed scan records
`full=1`.

Before Python changes:

```bash
python3 -m py_compile hl/*.py hl_*.py launcher/*.py launcher/core/*.py
python3 -m unittest discover -s tests
```

After dashboard edits, edit JSX/CSS sources and rebuild; never hand-edit the compiled bundle:

```bash
web/build.sh
launcher/web/build.sh
```

For UI changes, smoke the local mock dashboard and inspect the rendered page. Keep generated screenshots and
temporary databases out of commits.

## Deployment and process-control pitfalls

- The VPS deployment source of truth is the Git repository. Deploy code, then restart only the affected
  long-running service (`hl-dashboard.service` and/or `hl-observe.service`).
- `hl-scan.service` starts a real scan when activated/restarted. Never include it in a broad restart or restart
  it merely to pick up code. Use `systemctl reset-failed hl-scan.service` only to clear failed state.
- Scanner and Observer share Hyperliquid REST weight. Observer signal polling has priority; scanner pace adapts
  to whether Observer has active work.
- For complex remote SQL/Python, pipe a local script to the known-good SSH command instead of nesting heredocs
  in a quoted remote shell command.
- Never use a destructive Git reset on a user worktree without explicit approval. Preserve unrelated changes.
- If a command fails before reaching the VPS (for example malformed SSH options), say so and retry with the
  known-good command before drawing conclusions from remote state.

## Data and audit retention

Raw fill cache is bounded to the configured profile window plus warm-up. `wallet_registry`, generation history,
selection history, and pipeline audit are durable decision history. Live fill dedup data and account snapshots
are retained with explicit TTLs. Do not prune the old generation manually while a scan or tuner is active.
