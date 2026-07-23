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
    ↓ account ≥ $20k, 7d notional volume ≥ $250k, positive 7d/30d PnL
Official Portfolio stability + 30d Perp prefilter
    ↓ four adjacent 7d Perp-return folds ≥ 5%, positive 30d Perp PnL, Perp-profit share ≥ 60%
Sector-isolated structure filter
    ↓ cached 37-day profile (30-day evidence + 7-day warm-up)
Canonical Copy replay: Challenger evidence → personal Core
    ↓ 10 Campaigns, non-overlapping stability, path/cost/outlier/capacity stress
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

- Leaderboard ROI windows are audit/ranking references rather than admission gates. Raw recall requires useful
  account size/activity and positive 7/30-day PnL. Before any fill download, the already-required official
  Portfolio response must show four adjacent 7-day Perp-return folds of at least 5%; incomplete history is
  deferred rather than rejected. Profile hard gates then remove invalid data and systematic uncopyable
  structures; HFT needs at least ten complete rounds, Grid needs at least five complete rounds with a strict
  majority of repeated adds, and a one-off Heavy-DCA round is pressure-replayed.
- Any positive 30-day canonical-Copy result remains in the research Profile pool. Missing samples, stale
  activity, score, or outlier evidence block Core but do not erase that research evidence. Refined
  candle paths are fetched only for current Core and wallets that already clear every non-path business gate,
  bounded at 40 for resource safety. The provisional pre-path score is not a gate because path-risk evidence
  is itself 15% of the final score; the 75-point line is applied after that path is available. New Core requires
  eight independent Campaigns, at least 10% strict-Copy return over 30 days, and at least 3% over the latest
  rolling 7 days. All four non-overlapping 7-day folds must contain Campaign evidence, at least three must be
  profitable, and the one permitted losing fold cannot exceed 25% of total 30-day profit. Per-wallet cost
  stress, body-after-top-three, open-fill rate and capacity remain score diagnostics; the funded final
  portfolio must still remain profitable after taker fees are stressed to 1.5x and pass aggregate execution
  capacity. Average net per close remains a ranking diagnostic rather than a second hard gate.
- The final copy-follow score is calibrated as funded economics 30%, repeatability 25%, edge confidence 15%,
  operability 15%, and path risk 15%. Economics combines 30-day and latest-7-day follower return magnitude,
  fold timing and median per-close density; overlapping 14-day return and the legacy raw profile score
  contribute zero. Incomplete
  Campaign/fold evidence shrinks the total, so a tiny perfect streak cannot outrank mature proof. New Core
  requires at least 75/100 and at least 45% Campaign win rate. Body win rate and net after removing the three
  largest winning trades remain diagnostic score inputs. The score is displayed on a 0–100 scale while stored
  natively in `[0, 1]`.
- The bounded Core pool receives one per-wallet K-line certification for liquidation and path-risk evidence.
  Count, add/remove/swap and parameter search then use normalized fills and the shared-account execution model;
  they do not repeatedly scan candles. The winning membership receives exactly one conservative, path-complete
  30-day strict-Copy certification before publication. Score orders the candidate pool; it does not force a
  score prefix or fixed base count.
- Final moves must improve portfolio economics and pass the same four-fold evidence/profitability/loss-bound
  contract plus aggregate cost stress. Normal replacement, addition, and reordering is weekly. Production evidence refresh runs Monday
  and Thursday (alternating three/four-day gaps); each refresh still removes a wallet immediately for a hard
  failure, but never adds a wallet to approach a count.
  New promotions require two complete qualifying generations at least 24 hours apart; ordinary soft churn is
  suppressed for a Core wallet's first 14 days.
- When tuning changes execution parameters, Observer reload waits for one membership consistency pass on the
  same complete generation. The sealed strategy revision activates new parameters and new Core together. Core
  search and portfolio tuning have no wall-clock cutoff; their finite candidate axes and move limits terminate
  the work without publishing a timed-out partial result.
- `follow_selection` is atomically published with the scan generation. Observer opens new positions only for
  enabled Core rows. Removed wallets with open positions remain exit-only until flat.
- Core has no minimum wallet quota and a maximum of sixteen. A complete scan may publish any count from zero to sixteen;
  quality and funded shared-account contribution alone decide membership.

## Scheduled complete candidate reevaluation

Profiles are not re-downloaded from zero on every scheduled run.

- New candidates get a full configured profile window.
- Existing candidates use `candidate_fills` cursors and fetch only new fills, merging them into the 37-day
  cache.
- Only a newly discovered wallet or a missing/incomplete coverage marker bootstraps the full 37-day source
  window. Page-capped bootstraps persist a continuation cursor and resume from it on the next run.
- Leaderboard candidates require at least `$20,000` account value, `$250,000` leveraged 7-day notional volume,
  and positive 7-day and 30-day PnL. Leaderboard ROI values remain audit/ranking references. The cheap
  Portfolio precheck then splits the dense `perpMonth` PnL/account-value series into four non-overlapping
  7-day folds, requires every fold to return at least 5%, and also requires positive 30-day Perp PnL plus at
  least 60% Perp-profit share. Campaign independence and 1.5x Copy execution-cost stress are confirmed later
  from fills under our `$10,000` replay rather than inferred from Portfolio.
- Every survivor plus current Core/Challenger/open-position owners is evaluated in the same generation. There
  is no Top-N cap, rotation shard, recovery/exploration quota or deferred candidate tail.
- A valid generation is published atomically. A truncated/invalid leaderboard retains the old generation and
  cannot prune, publish, or tune.

Production automatically performs one complete candidate-universe evidence refresh every Monday and Thursday;
the local daemon fallback uses a 72-hour interval. Previously known wallets remain history-incremental, and only
genuinely new/incomplete wallets bootstrap 37 days. The Dashboard rescan button queues the same complete
reevaluation; changing scanner settings only persists params and does not start a scan.

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

For a network-free, mutation-free waterfall over one already frozen generation:

```bash
python3 -m hyper.cli.discover --db /path/to/production.db audit-pipeline \
  --generation GENERATION_ID --report /private/funnel.json
```

## Copy replay and automatic tuning

The replay uses the same copyable-fill normalization and shared execution state used by the Observer. It models
shared available balance, isolated margin, volatility-tier sizing, leverage caps, deployment and per-coin caps,
fees/slippage, skipped opens, add pressure, and liquidation/price-path outcomes.

Overlapping positions from the same source wallet, market board, and direction are collapsed into one independent
Campaign. Target-wallet stability is screened first from official Portfolio: all four adjacent 7-day folds need
at least 5% return. Our strict Copy separately requires at least 10% return over 30 days and at least 5% over
the latest rolling 7 days. Its four floating-equity folds are a timing-stability check: every fold needs at
least one independent Campaign, at least three folds must be profitable, and the one permitted losing fold
cannot exceed 25% of total 30-day profit. The aggregate must remain profitable after charging taker fees at
1.5x. Average closed-position net relative to weekly starting equity remains a score/diagnostic signal. The
30-day aggregate still needs ten Campaigns. Thin folds are unknown evidence, never synthetic losses. The
single outlier stress removes the largest winning Campaign and requires the remainder positive.

The same 15-minute price path now records wallet and campaign intratrade drawdown, underwater duration,
time below -5%, deep-loss events and recovery. New Core is capped at 12% intratrade drawdown; 12–15% is
Challenger-only and above 15% is rejected. Current -8%, or -5% lasting 24 hours, becomes exit-only.

Source-wallet profit high-water is not used as an admission or execution gate. One source may use 25% total
effective margin. Same-direction baskets use the most conservative included tier:
20% stable Crypto, 15% mid Crypto, 10% high-volatility Crypto, and 10% `xyz`/stock; there are at most three
simultaneous symbols and two same-direction stock symbols. The account-wide 85% hard-margin ceiling remains.

An explicit optimization run starts from the already qualified wallet pool and searches:

- stable/mid/high volatility margin ceilings;
- leverage caps and full-power deployment line;
- smart-add gap, shrink, and hard-count parameters.

The search evaluates independent grid axes, finalist combinations, walk-forward folds, holdout, and stress
scenarios from fills. It never changes the Core membership using stale profiles and never runs a candle replay
for every parameter or membership proposal. After the winning parameters and membership are fixed, the one final
strict 30-day portfolio certification supplies the estimated shared-account result shown above the “跟单中”
list. Publication reuses that result; it does not synchronously recalculate every Core and Challenger for
Dashboard enrichment.

Leverage candidates preserve approximate tier exposure by pairing lower leverage with reciprocally higher
margin (`margin × leverage` stays near the active notional before caps). Profit remains the primary objective;
inside the near-best profit band the tuner prefers fewer liquidations, less balance congestion, better open
capture, and then stronger measured add fidelity. A profit-retaining proposal that strictly reduces liquidation
evidence can be accepted as a safety repair even when it does not claim the ordinary minimum relative gain.

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
  the completed run records `full=1`; explicit full bypasses the two-hour Portfolio decision cache but still
  uses incremental fills for wallets whose complete 37-day history is already present.
- Never commit `data/`, `secret/`, `hyper/launcher/data/keys/`, `hyper/launcher/data/targets.json`, or live database
  snapshots. Keep private deployment details in local ignored notes.
