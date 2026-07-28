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
    ↓ account ≥ $20k, 7d notional volume ≥ $150k, positive 7d/30d PnL
Official Portfolio 30d Perp prefilter
    ↓ Perp ROI ≥ 20%, positive Perp PnL, Perp-profit share ≥ 60%
Deep fills source quality
    ↓ closed 30d/7d profit; open loss ≤50% of closed profit; Episode quality/activity/structure; Top40
Fills-only rough Copy
    ↓ conservative 30d/7d both profitable, Copy wins ≥ 60%, open follow ≥ 70%
Composite score + conservative 70/30 Copy profit priority → unified tuning → strict K-line Copy
    ↓ profit-ordered Top16 only; final individual/shared 10%/3%, open-loss ratio ≤50%; no minimum quota
跟单中 (Core) · 候选 (Challenger) · exit-only for held positions
    ↓ forward-only Observer
Paper copy positions, PnL, and execution audit
```

The Dashboard and API expose the same published generation. `watchlist` is a derived ranked view; it is not the
final new-entry membership once an explicit selection generation exists.

## Selection model

Wallet quality and funded-account membership are separate decisions.

- Raw recall requires useful account size/activity and positive 7/30-day PnL. Before fill download, official
  Portfolio requires positive 30-day Perp PnL, at least 60% Perp-profit share and at least 20% dynamic Perp
  return. Official return compounds each positive-equity operating segment on that segment's own starting
  capital: a full withdrawal and later redeposit preserves both sides of the evidence without dividing by
  zero or treating the transfer as a loss. A real loss to zero remains a -100% segment and cannot be erased
  by redeposit. Incomplete official history is Challenger evidence, never a fabricated return failure.
- Deep fills provide two source-wallet lanes: at least ten complete 30-day Episodes with at least 70% source
  wins, or 7–9 Episodes with at least 85% wins, 30% official Perp return and positive recent-7d source PnL.
  Both require a true new open within 72 hours and structural copyability. A Top3 concentration check triggers only
  at 70% of gross profit; then the remaining body must still win at least 70% and not lose money. At most the
  best 40 source wallets receive rough replay.
- Profitability is proved only by complete closed Episodes. Positive unrealized PnL is displayed as a reference
  and has zero weight. Negative unrealized PnL is charged in full:
  `qualificationPnl = closedPnl - abs(min(unrealizedPnl, 0))`. Closed PnL must be positive in both 30-day and
  7-day windows, conservative PnL must remain positive, and 30-day open loss may not exceed 50% of closed
  profit. This definition is shared by source profiles, individual Copy, shared replay and tuning.
- Rough Copy uses one continuously compounded `$10,000` comparison account and requires both dynamic 30d and
  rolling-7d conservative profitability, positive closed PnL in both windows, an open-loss ratio at or below
  50%, seven closed Copy Episodes over 30 days, 60% closed-Episode Copy wins, 70% open follow and
  complete valuation. The rolling-7d window has no minimum closed-Episode count.
  Historical replay assumes liquidity was executable. A source flat-to-open lifecycle remains pending until its
  cumulative position reaches the tier minimum notional and is excluded only if it never does. Once confirmed,
  our open is sized independently by our margin, leverage and capacity rules instead of being capped by source
  notional. Further fills from the confirming opening OID extend the opening anchor and are never treated as
  smart adds. A later add OID may wait for cumulative slices to become actionable, but its first Copy execution
  seals the OID and later slices cannot submit another add. Structural execution density counts distinct OIDs,
  not exchange fill fragments. Live Observer liquidity skips remain separate audit evidence.
  Return magnitude orders the pool and does not pre-empt the later unified parameter tune.
- The composite score remains the broad quality indicator: conservative profitability 40%,
  source/Copy closed-Episode win stability 30%,
  copyability 20% and activity 10%. There is no score floor. Qualified wallets instead form Core by
  `70% × conservative 30d dynamic Copy return + 30% × conservative 7d dynamic Copy return`, with 30d return,
  7d return, composite score
  and address as fixed tie-breaks. The rough form of that value bounds Top16; the final tuned surface strictly
  replays and reranks those same finalists.
- Final individual strict replay requires conservative dynamic 30d/7d returns of 10%/3%, positive closed PnL,
  a 30-day open-loss ratio at or below 50%, 60% closed-Episode Copy wins, 70% open follow,
  complete data/path evidence and at most three proxy liquidations. A separate severity gate rejects a wallet
  from the rough candidate pool as soon as any liquidated Copy episode loses at least 5% of the dynamic account
  equity recorded when that episode opened, even if its count is only one. Final shared replay requires
  conservative 10%/3%, a 30-day open-loss ratio at or below 50%, and 70% open follow on both standardized and
  actual Paper capital. Campaign, weekly-fold, per-close,
  cost-multiple, maximum-drawdown and 75-point gates do not exist.
- Confirmed source-account zeroing liquidations and those >=5% Copy liquidation events are persisted in
  `wallet_risk_event`. Discovery cache pruning and 30/37-day window expiry cannot make the wallet eligible again.
- Wallet count and parameters are tuned together over profit-ranked prefixes. The winning surface requalifies every
  individual and receives exactly one conservative path-complete shared replay before publication.
- Final moves must pass the dynamic 30d/7d shared-account return and path-completeness contract.
  Complete candidate discovery runs Monday and Thursday; the frozen Challenger cohort is refreshed on the other
  five days. Daily refresh first certifies with the active parameters. If the proposed Core changes, it runs
  parameter optimization and repeats strict certification before publishing membership and parameters
  together; an unchanged Core skips the grid. There is no promotion delay, tenure, old-Core retention, star
  priority or minimum count.
- When tuning changes execution parameters, Observer reload waits for one membership consistency pass on the
  same complete generation. The sealed strategy revision activates new parameters and new Core together. Core
  search and portfolio tuning have no wall-clock cutoff; their finite candidate axes and move limits terminate
  the work without publishing a timed-out partial result.
- `follow_selection` is atomically published with the scan generation. Observer opens new positions only for
  enabled Core rows. Removed wallets with open positions remain exit-only until flat.
- Core has no minimum wallet quota and a maximum of sixteen. A complete scan may publish any count from zero to sixteen;
  final profit order/evidence and funded shared-account economics decide membership.

## Scheduled complete candidate reevaluation

Profiles are not re-downloaded from zero on every scheduled run.

- New candidates get a full configured profile window.
- Existing candidates use `candidate_fills` cursors and fetch only new fills, merging them into the 37-day
  cache.
- Only a newly discovered wallet or a missing/incomplete coverage marker bootstraps the full 37-day source
  window. Page-capped bootstraps persist a continuation cursor and resume from it on the next run.
- Leaderboard candidates require at least `$20,000` account value, `$250,000` leveraged 7-day notional volume,
  and positive 7-day and 30-day PnL. The cheap Portfolio precheck independently confirms at least `$250,000`
  of Perp-only 7-day volume, positive 30-day Perp PnL, at least 60% Perp-profit share and at least 20% dynamic
  30-day Perp return. Source quality and follower profitability are confirmed later from fills under a
  standardized `$10,000` starting equity with continuous compounding.
- Every survivor plus current Core/Challenger/open-position owners is evaluated in the same generation. There
  is no Top-N cap, rotation shard, recovery/exploration quota or deferred candidate tail.
- A valid generation is published atomically. A truncated/invalid leaderboard retains the old generation and
  cannot prune, publish, or tune.

Production schedules all jobs in `Asia/Shanghai`:

- Monday and Thursday 04:00: complete Leaderboard discovery and candidate-universe reevaluation.
- Tuesday, Wednesday, Friday, Saturday and Sunday 04:00: frozen Challenger/Core refresh via
  `python3 -m hyper.cli.discover --db ... challenger-refresh`.

The daily job clones the last complete generation's Leaderboard staging rows; it does not access the Leaderboard
API or discover a new wallet. It refreshes official Portfolio evidence, cached-fill deltas, positions, valuation
and required market paths, then reruns source gates, true 72-hour open/flip activity, canonical 30/14/7 Copy,
strict Top16 individual replay and strict shared-account replay. The first pass uses `retune=False`; a proposed
Core addition runs a parameter-grid pass only if every incumbent remains in the fixed-surface result, and the
tuned result must still be a strict superset before atomic publication. Daily refresh never removes or replaces
an incumbent: such a proposal carries the exact prior Core snapshot into the fresh evidence generation and
keeps proposed newcomers Challenger. Hard safety has two narrow exceptions: a recent source fill whose
`liquidatedUser` is that wallet plus fresh standard/affected-dex snapshots showing zero equity/no positions, or
one canonical Copy liquidation losing at least 5% of its episode-opening dynamic equity. Either removes new-open
authority immediately and becomes Exit-only while an existing copy is managed. Ordinary losses, sub-5% Paper
liquidations and rolling-return declines do not trigger these exceptions. Other Core
demotion is reserved for the Monday/Thursday complete scan. Missing or incomplete 37-day caches are deferred
to the next complete run. A current-Core data/path failure prevents publication; an individual Challenger
failure remains eligible for the next daily attempt because the pool stays anchored to the last successful
complete generation. Daily runs never prune discovery caches, and an unchanged-Core run preserves Core order
and does not reset the periodic parameter-retune age.

Previously known wallets remain history-incremental, and only complete discovery runs bootstrap or repair 37
days. The Dashboard rescan button queues the same complete reevaluation; changing scanner settings only persists
params and does not start a scan.

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

To rebuild source Episode quality and run the at-most-40 fills-only rough-Copy stage directly from a cached
published generation:

```bash
python3 -m hyper.cli.core_lab --db /path/to/production.db --max-rough 40
```

`core_lab` opens the database with SQLite `mode=ro` plus `query_only`, emits anonymous wallet labels, and never
migrates or writes the source database. A legacy generation without the current official Perp observed-history
return may be used only for an explicitly marked economic preview; it always reports that a new Portfolio scan
is required before publication.

For threshold research, the non-publishing distribution collector deliberately ignores the production ROI,
PnL, win-rate, activity, sample-depth and score gates. It recalls every Leaderboard row above the volume floor,
confirms `$250,000` of official Perp-only seven-day volume, retains only executable structure, catastrophic
source-risk and data-integrity checks, and runs the active Copy surface first fills-only and then against a
shared 15-minute price path:

```bash
python3 -m hyper.cli.discover --db /path/to/production.db profit-distribution \
  --week-perp-volume-min 250000 \
  --limit 600 \
  --strict-limit 0 \
  --max-pages 1 --recovery-pages 20 \
  --cache-db /private/profit-distribution-cache.db \
  --report /private/profit-distribution.json
```

The source database is opened read-only. The isolated cache and anonymous JSON report are created with mode
`0600`; the command never creates or publishes a generation, changes Core, updates parameters, or starts
Observer. A positive `--limit` keeps current Core/Challenger evidence and deterministically samples the complete
Perp-volume rank instead of taking a biased top-volume prefix; zero scans the entire recall set. Its strict
30/14/7 conservative-return quantiles and paired threshold matrix are the evidence used to choose new
profitability floors. For a complete-universe wallet hunt after that unbiased calibration, a positive
`--strict-limit` first ranks every structural survivor by its fills-only 70/30 conservative return and spends
the shared price-path replay budget only on that many leaders. Such a bounded result is intentionally biased
and may find candidates, but must not replace the unbiased sample when choosing thresholds.

## Copy replay and automatic tuning

The replay uses the same copyable-fill normalization and shared execution state used by the Observer. It models
shared available balance, isolated margin, volatility-tier sizing, leverage caps, deployment and per-coin caps,
fees/slippage, skipped opens, add pressure, and liquidation/price-path outcomes.

A replay starts with standardized `$10,000` equity and continuously compounds it. Conservative dynamic return
divides complete closed-Episode PnL minus all current open loss by the applicable window-start floating equity;
positive open PnL remains reference-only. Rough replay uses fills only and is capped at 40 source-quality
wallets. Strict replay loads K-line paths only for the profit-ordered Top16 and uses the final tuned parameters.
There is no Campaign, weekly-fold, per-close, cost-multiple, maximum-drawdown or score-floor admission rule.

The same 15-minute price path records wallet intratrade drawdown, underwater duration, time below
-5%, deep-loss events, recovery and conservative proxy liquidations at our leverage ceiling. Historical maximum
drawdown is diagnostic only. A wallet may enter tuning with sub-5% proxy liquidation evidence, then the final
tuned 30-day strict replay permits at most three proxy liquidations per Core wallet. One liquidated episode
losing 5% or more of its opening dynamic Copy equity is a hard rejection regardless of count. Live account loss
is controlled separately by the global equity high-water stop (15% by default), which pauses and flattens the
account.

Source-wallet profit high-water is not used as an admission or execution gate. Static per-wallet and
per-sector slices are also retired: wallets compete only when their positions actually overlap. New opens
stop at the account-wide 90% deployment line; adds may use the remaining real available cash. Per-coin
same-direction caps, liquidity checks, isolated liquidation and the global concurrency ceiling remain.

An explicit optimization run starts from the bounded pre-Core pool, searches wallet count and sizing together,
and then optimizes:

- stable/mid/high volatility first-open margins;
- leverage caps;
- smart-add gap, shrink, and hard-count parameters.

The search evaluates independent grid axes, finalist combinations, continuous-capital walk-forward folds,
holdout, and stress scenarios from fills. Each count node is tuned independently so an 8-wallet portfolio never
inherits parameters fitted to a congested 16-wallet account. It never changes Core membership using stale
profiles and never runs a candle replay for every parameter or membership proposal. After the winning parameters
and membership are fixed, the one final
strict 30-day portfolio certification supplies the estimated shared-account result shown above the “跟单中”
list. Publication also persists the exact final-surface individual 30/14/7 replay fields used for score and
admission, so Dashboard score and wallet economics never fall back to a different parameter surface.

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
