# poly-btc

Hyperliquid copy-trade discovery, canonical copy replay, shared-account Core selection, paper Observer,
Dashboard, and launcher.

The product is designed for a small funded account: the objective is not to follow the largest possible number
of wallets, but to follow a compact set of active, copyable, positive-edge wallets whose combined replay still
uses capital efficiently.

## What the system does

The current runtime turns the public Hyperliquid leaderboard into a live, paper-traded Core through this flow:

```text
Leaderboard
    ↓ staged and validated generation
Candidate coarse filter
    ↓ cached 37-day profile (30-day evidence + 7-day warm-up)
Quality gates + canonical copy replay
    ↓ qualified active pool / final copy-follow score
Shared-account smart Core search
    ↓ explicit follow_selection publication
跟单中 (Core) · 候选 (Challenger) · exit-only for held positions
    ↓ forward-only Observer
Paper copy positions, PnL, and execution audit
```

The Dashboard and API expose the same published generation. `watchlist` is a derived ranked view; it is not the
final new-entry membership once an explicit selection generation exists.

## Selection model

Wallet quality and funded-account membership are separate decisions.

- Profile hard gates remove structurally uncopyable wallets, HFT/grid/heavy-DCA/spot-hedge patterns, invalid
  data, insufficient evidence, stale activity, thin copy edge, and copy replays that lose after costs.
- The final copy-follow score ranks wallets that passed those gates. It combines raw profile quality and copy
  evidence; it is displayed on a 0–100 scale while stored natively in `[0, 1]`.
- The Core selector evaluates one shared simulated account using empty-forward, all-backward, and current-Core
  starts, then repeatedly checks add/remove/swap/pair-add moves under strict K-line replay. Score orders the
  candidate pool; it does not force a score prefix or fixed base count.
- Final moves must improve portfolio economics and pass non-overlapping ten-day folds, a recent holdout, and
  1.5x transaction-cost stress without adding a stress liquidation. This can admit several complementary
  wallets in one run, or replace a weak incumbent. There is no fixed minimum Core count or wall-clock cutoff.
- When tuning changes execution parameters, Observer reload waits for one membership consistency pass on the
  same complete generation. The sealed strategy revision activates new parameters and new Core together. Core
  search and portfolio tuning have no wall-clock cutoff; their finite candidate axes and move limits terminate
  the work without publishing a timed-out partial result.
- `follow_selection` is atomically published with the scan generation. Observer opens new positions only for
  enabled Core rows. Removed wallets with open positions remain exit-only until flat.

## Daily and weekly collection

Profiles are not re-downloaded from zero on every daily run.

- New candidates get a full configured profile window.
- Existing candidates use `candidate_fills` cursors and fetch only new fills, merging them into the 37-day
  cache.
- Full refetch self-healing runs on a seven-day cadence, divided into seven rolling shards. Page-cap, cursor,
  or cache-gap anomalies can force an individual full refetch.
- Core and held wallets receive priority refresh. The normal daily budget is 60 minutes with a 15-minute Core
  deadline and a 15-minute finalization reserve; low-priority candidates can be deferred.
- A valid generation is published atomically. A truncated/invalid leaderboard retains the old generation and
  cannot prune, publish, or tune.

Automatic cadence is one daily incremental run and one weekly full leaderboard/profile refresh. The Dashboard
rescan button queues a manual run; changing scanner settings only persists params and does not start a scan.

## Copy replay and automatic tuning

The replay uses the same copyable-fill normalization and shared execution state used by the Observer. It models
shared available balance, isolated margin, volatility-tier sizing, leverage caps, deployment and per-coin caps,
fees/slippage, skipped opens, add pressure, and liquidation/price-path outcomes.

After Core publication, a generation-bound tuner searches:

- stable/mid/high volatility margin ceilings;
- leverage caps and full-power deployment line;
- smart-add gap, shrink, and hard-count parameters.

The search evaluates independent grid axes, finalist combinations, walk-forward folds, holdout, and stress
scenarios. It never changes the Core membership using stale profiles. The effective 30/14/7 portfolio replay is
stored per generation and shown above the “跟单中” list as the 30-day estimated shared-account result.

The current Paper defaults allow automatic application after the validation gates:

```text
FOLLOW_SELECTION_MODE=auto
AUTO_TUNE_MODE=apply
```

Paper uses zero-day/zero-forward-count exploration thresholds so the complete loop can be tested from a cold
database. For real-money deployment, use conservative shadow and forward-evidence thresholds and review the
persisted `params` values before enabling any live execution.

## Runtime components

| Area | Entry points |
|---|---|
| Scanner/discovery | `hl_discover.py`, `hl/scanner.py`, `hl/metrics.py`, `hl/fills.py` |
| Generation/selection | `hl/generation.py`, `hl/selection.py`, `hl/follow_score.py` |
| Replay/tuning | `hl/copy_backtest.py`, `hl/copy_engine.py`, `hl/auto_tune.py` |
| Observer/paper copy | `hl_observe.py`, `hl/observer.py` |
| Dashboard API | `hl_dashboard.py`, `hl/api.py`, `hl/api_*.py` |
| Dashboard frontend | `web/app.jsx`, `web/components/*`, compiled `web/app.js` |
| Launcher/ops | `launcher/launcher.py`, `launcher/server.py`, `launcher/core/*`, `launcher/web/*` |
| Schema/migrations | `hl/storage.py` |

Important durable tables include `scan_generation`, `leaderboard_staging`, `profile`, `candidate_fills`,
`episode`, `wallet_registry`, `watchlist`, `follow_selection`, `pipeline_audit`, `copy_position`,
`copy_action`, `auto_tune_runs`, and `auto_tune_state`.

## Dashboard

The dashboard focuses on operator decisions rather than internal model terminology:

- wallet tabs: “跟单中”, “候选”, “降级”;
- list columns include final score, target-wallet activity, current-parameter replay, actual followed count,
  actual PnL, win rate, and main coin;
- wallet details are lazy-loaded after clicking a row;
- actual PnL includes realized closed PnL plus unrealized PnL for open copy positions;
- pipeline audit explains profile, selection, follow, and tuner decisions;
- portfolio replay summary is displayed only when it belongs to the current published generation.

## Run locally

From the repository root:

```bash
# Dashboard and static frontend
python3 hl_dashboard.py --db data/hl.db --static web --host 127.0.0.1 --port 8810

# Scanner daemon / manual commands
python3 hl_discover.py --db data/hl.db serve-rescan
python3 hl_discover.py --db data/hl.db scan --days 14 --scan-interval 8
python3 hl_discover.py --db data/hl.db scan --full --days 14 --scan-interval 8
python3 hl_discover.py --db data/hl.db regate
python3 hl_discover.py --db data/hl.db repair-watchlist
python3 hl_discover.py --db data/hl.db watchlist --top 40

# Forward-only Observer / report
python3 hl_observe.py --db data/hl.db observe
python3 hl_observe.py --db data/hl.db report

# Local launcher
python3 launcher/launcher.py --port 8799 --no-browser
```

The launcher starts the local operations UI. It does not automatically start a scan or Observer. The Dashboard
or systemd/process supervisor controls those workers.

## Mock dashboard

```bash
python3 web/dev/seed_mock.py data/hl_mock.db
python3 web/dev/mock_consumer.py data/hl_mock.db
DASH_PASSWORD=mock123 python3 hl_dashboard.py --db data/hl_mock.db --static web --host 127.0.0.1 --port 8810
```

## Build and verify

The React frontends are precompiled and do not bundle React themselves:

```bash
web/build.sh
launcher/web/build.sh
python3 -m py_compile hl/*.py hl_*.py launcher/*.py launcher/core/*.py
python3 -m unittest discover -s tests
```

Edit JSX/CSS sources and rebuild; do not hand-edit `web/app.js` or `launcher/web/app.js`. For UI changes, smoke
the local mock dashboard and inspect the rendered result.

## Operations and safety

- Dashboard writes only commands/params; workers own business-state writes.
- Observer is forward-only and has priority for Hyperliquid REST weight.
- Do not restart `hl-scan.service` to deploy code: it starts a real scan when activated. Restart only the
  affected long-running service, normally `hl-dashboard.service` and/or `hl-observe.service`.
- Before diagnosing a manual “full” scan, verify the command payload has `full=true`, the CLI used `--full`, or
  the completed run records `full=1`; a manual workset scan can still use incremental cached fills.
- Never commit `data/`, `secret/`, `launcher/data/keys/`, `launcher/data/targets.json`, or live database
  snapshots. Keep private deployment details in local ignored notes.
