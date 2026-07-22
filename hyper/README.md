# Hyperliquid copy-trade module

Hyperliquid copy-trade discovery, canonical copy replay, shared-account Core selection, paper Observer,
Dashboard, and launcher.

The product is designed for a small funded account: the objective is not to follow the largest possible number
of wallets, but to follow a compact set of active, copyable, positive-edge wallets whose combined replay still
uses capital efficiently.

## Package layout

```text
hyper/
├── discovery/  candidate harvesting, profiling, generation publication, audit
├── copy/       fill normalization, canonical replay, copy policy, position transitions
├── selection/  wallet scoring, Core formation, optimization, strategy revisions
├── market/     Hyperliquid REST/WS, market universe, price paths, volatility
├── execution/  forward-only Observer and risk assessment
├── ops/        process control, credentials, Paper reset
├── cli/        stable command-line entry points
├── launcher/   local/VPS deployment tooling
└── config.py, params.py, storage.py, util.py  shared foundations
```

New business code belongs in one of these responsibility packages rather than directly under `hyper/`.

## What the system does

The current runtime turns the public Hyperliquid leaderboard into a live, paper-traded Core through this flow:

```text
Leaderboard
    ↓ staged and validated generation
Candidate coarse filter
    ↓ account ≥ $5k, 7d notional volume ≥ $250k, positive 7d or 30d PnL
30d Perp prefilter + sector-isolated structure filter
    ↓ cached 37-day profile (30-day evidence + 7-day warm-up)
Canonical Copy replay: Research → Challenger → personal Core
    ↓ campaign economics, path risk, cost/tail/capacity stress
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

- Leaderboard ROI is a ranking/audit signal, not a hard admission threshold. Profile hard gates remove invalid
  data and systematic uncopyable structures; HFT needs at least ten complete rounds, Grid needs at least five
  complete rounds with a strict majority of repeated adds, and a one-off Heavy-DCA round is pressure-replayed.
- Strict Copy evidence has three roles: positive 30-day economics are Research-only; at least 5% with seven
  closes, five independent campaigns/days, positive 1.5x-cost stress and positive profit after removing the two
  largest campaigns is Challenger; new Core requires at least 10% plus the campaign/sample/risk gates.
- The final copy-follow score ranks wallets that passed those gates. It combines raw profile quality and copy
  evidence; it is displayed on a 0–100 scale while stored natively in `[0, 1]`.
- The Core selector evaluates one shared simulated account using empty-forward, all-backward, and current-Core
  starts, then repeatedly checks add/remove/swap/pair-add moves under strict K-line replay. Score orders the
  candidate pool; it does not force a score prefix or fixed base count.
- Final moves must improve portfolio economics and pass non-overlapping ten-day folds, a recent holdout, and
  1.5x transaction-cost stress without adding a stress liquidation. Normal replacement/reordering is weekly;
  daily evidence refresh still removes a wallet immediately for a hard failure. While Core has fewer than ten
  wallets, a daily run may add independently qualified, portfolio-safe wallets without evicting incumbents.
- When tuning changes execution parameters, Observer reload waits for one membership consistency pass on the
  same complete generation. The sealed strategy revision activates new parameters and new Core together. Core
  search and portfolio tuning have no wall-clock cutoff; their finite candidate axes and move limits terminate
  the work without publishing a timed-out partial result.
- `follow_selection` is atomically published with the scan generation. Observer opens new positions only for
  enabled Core rows. Removed wallets with open positions remain exit-only until flat.
- A complete scan may legally publish zero Core. This activates an empty strategy revision, converts old Core
  rows to exit-only, and never rolls back to an economically disqualified prior wallet.

## Daily complete candidate reevaluation

Profiles are not re-downloaded from zero on every daily run.

- New candidates get a full configured profile window.
- Existing candidates use `candidate_fills` cursors and fetch only new fills, merging them into the 37-day
  cache.
- Only a newly discovered wallet or a missing/incomplete coverage marker bootstraps the full 37-day source
  window. Page-capped bootstraps persist a continuation cursor and resume from it on the next run.
- Leaderboard candidates require at least `$5,000` account value, `$250,000` leveraged 7-day notional volume,
  and positive PnL in either the 7-day or 30-day official window. There is no ROI magnitude gate or volume cap.
  The Portfolio precheck uses only the primary 30-day window: Perp PnL must be positive and at least 60% of
  total PnL. The 7-day and lifetime windows remain visible in audit but do not form an `AND` rejection.
- Every survivor plus current Core/Challenger/open-position owners is evaluated in the same generation. There
  is no Top-N cap, rotation shard, recovery/exploration quota or deferred candidate tail.
- A valid generation is published atomically. A truncated/invalid leaderboard retains the old generation and
  cannot prune, publish, or tune.

Automatic cadence is one complete candidate-universe reevaluation every day. Previously known wallets remain
history-incremental; only genuinely new/incomplete wallets bootstrap 37 days. The Dashboard rescan button queues
the same complete reevaluation; changing scanner settings only persists params and does not start a scan.

Before production rollout, operators can run the same pipeline against an online SQLite backup:

```bash
python3 -m hyper.cli.discover --db /path/to/production.db shadow-scan --report /private/report.json
```

One-off acceptance scans can override only the ROI/PnL harvest surface without changing production params:

```bash
python3 -m hyper.cli.discover --db /path/to/production.db shadow-scan \
  --report /private/report.json \
  --week-roi-min-pct 15 --month-roi-min-pct 45 --all-roi-min-pct 50 \
  --week-pnl-min 2000 --month-pnl-min 8000 --all-pnl-min 0
```

The source database is opened read-only, all mutations stay in a mode-0600 temporary database, and the temporary
database is removed after a redacted JSON report is written.

## Copy replay and automatic tuning

The replay uses the same copyable-fill normalization and shared execution state used by the Observer. It models
shared available balance, isolated margin, volatility-tier sizing, leverage caps, deployment and per-coin caps,
fees/slippage, skipped opens, add pressure, and liquidation/price-path outcomes.

Core win rate is computed from independent campaigns: overlapping positions from the same source wallet, market
board, and direction are collapsed into one directional bet before win-rate/Wilson tests. New Core requires
`12/5/3` closes, ten 30-day campaigns, 60% campaign win rate, an 80% one-sided Wilson lower bound of 50%,
PF ≥ 1.25, at least 3% return after removing the two largest campaigns, and positive 1.5x-cost stress. Retention
uses 7% return, 55% win rate and a 45% Wilson lower bound; soft failures need two distinct complete scans, while
hard risk exits immediately.

The same 15-minute price path now records wallet and campaign intratrade drawdown, underwater duration,
time below -5%, deep-loss events and recovery. New Core is capped at 12% intratrade drawdown; 12–15% is
Challenger-only and above 15% is rejected. Current -8%, or -5% lasting 24 hours, becomes exit-only.

Source-wallet high-water state is persisted per contiguous Core member cycle. A 3% giveback freezes opens/adds,
6% halves every source position without same-cycle refill, and 10% exits all positions with a seven-day cooldown.
One source may use 25% total effective margin. Same-direction baskets use the most conservative included tier:
20% stable Crypto, 15% mid Crypto, 10% high-volatility Crypto, and 10% `xyz`/stock; there are at most three
simultaneous symbols and two same-direction stock symbols. The account-wide 85% hard-margin ceiling remains.

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
| Scanner/discovery | `hyper/cli/discover.py`, `hyper/discovery/scanner.py`, `hyper/discovery/metrics.py` |
| Generation/selection | `hyper/discovery/generation.py`, `hyper/selection/state.py`, `hyper/selection/follow_score.py` |
| Replay/tuning | `hyper/copy/copy_backtest.py`, `hyper/copy/copy_engine.py`, `hyper/selection/auto_tune.py` |
| Market data | `hyper/market/rest.py`, `hyper/market/ws.py`, `hyper/market/price_path.py` |
| Observer/paper copy | `hyper/cli/observe.py`, `hyper/execution/observer.py` |
| Runtime operations | `hyper/ops/procman.py`, `hyper/ops/credentials.py`, `hyper/ops/paper_reset.py` |
| Dashboard API | `dashboard/server.py`, `dashboard/api/*` |
| Dashboard frontend | `dashboard/web/app.jsx`, `dashboard/web/components/*`, compiled `dashboard/web/app.js` |
| Launcher/ops | `hyper/launcher/launcher.py`, `hyper/launcher/server.py`, `hyper/launcher/core/*`, `hyper/launcher/web/*` |
| Schema/migrations | `hyper/storage.py` |

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
python3 -m dashboard.server --db data/hl.db --static dashboard/web --host 127.0.0.1 --port 8810

# Scanner daemon / manual commands
python3 -m hyper.cli.discover --db data/hl.db serve-rescan
python3 -m hyper.cli.discover --db data/hl.db scan --days 14 --scan-interval 8
python3 -m hyper.cli.discover --db data/hl.db scan --full --days 14 --scan-interval 8
python3 -m hyper.cli.discover --db data/hl.db regate
python3 -m hyper.cli.discover --db data/hl.db repair-watchlist
python3 -m hyper.cli.discover --db data/hl.db watchlist --top 40

# Forward-only Observer / report
python3 -m hyper.cli.observe --db data/hl.db observe
python3 -m hyper.cli.observe --db data/hl.db report

# Local launcher
python3 -m hyper.launcher.launcher --port 8799 --no-browser
```

The launcher starts the local operations UI. It does not automatically start a scan or Observer. The Dashboard
or systemd/process supervisor controls those workers.

## Mock dashboard

```bash
python3 dashboard/web/dev/seed_mock.py data/hl_mock.db
python3 dashboard/web/dev/mock_consumer.py data/hl_mock.db
DASH_PASSWORD=mock123 python3 -m dashboard.server --db data/hl_mock.db --static dashboard/web --host 127.0.0.1 --port 8810
```

## Build and verify

The React frontends are precompiled and do not bundle React themselves:

```bash
dashboard/web/build.sh
hyper/launcher/web/build.sh
python3 -m compileall -q hyper dashboard
python3 -m unittest discover -s hyper/tests
```

Edit JSX/CSS sources and rebuild; do not hand-edit `dashboard/web/app.js` or `hyper/launcher/web/app.js`. For UI changes, smoke
the local mock dashboard and inspect the rendered result.

## Operations and safety

- Dashboard writes only commands/params; workers own business-state writes.
- Observer is forward-only and has priority for Hyperliquid REST weight.
- Do not restart `hl-scan.service` to deploy code: it starts a real scan when activated. Restart only the
  affected long-running service, normally `hl-dashboard.service` and/or `hl-observe.service`.
- Before diagnosing a manual “full” scan, verify the command payload has `full=true`, the CLI used `--full`, or
  the completed run records `full=1`; a manual workset scan can still use incremental cached fills.
- Never commit `data/`, `secret/`, `hyper/launcher/data/keys/`, `hyper/launcher/data/targets.json`, or live database
  snapshots. Keep private deployment details in local ignored notes.
