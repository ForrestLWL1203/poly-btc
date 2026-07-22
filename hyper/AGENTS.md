# AGENTS.md

## Scope

This repository is organized as a multi-product copy-trade workspace. The active product is Hyperliquid:

- leaderboard discovery and wallet profiling;
- copyability scoring and canonical copy replay;
- bounded joint Core-membership and portfolio-parameter formation;
- a forward-only paper Observer;
- a read-oriented Dashboard API and React dashboard;
- local/VPS process and deployment tooling.

Old non-Hyperliquid research scripts are not part of the active runtime. Read `CLAUDE.md` first for private
local notes, then verify any assumption against the current code and database schema.

Repository boundaries:

- `hyper/` owns Hyperliquid business logic, CLI entry points, tests, docs, and its deployment launcher.
- Keep new business modules inside the owning responsibility package: `discovery/`, `copy/`, `selection/`,
  `market/`, `execution/`, or `ops/`. The `hyper/` root is reserved for shared `config`, `params`, `storage`,
  and `util` primitives; do not add new flat business modules there.
- `dashboard/` owns the shared Dashboard server/API and frontend. It may present multiple products later.
- Future product implementations belong in their own top-level package, for example `polymarket/`.

## Non-negotiable invariants

- `data/hl.db` is the normal SQLite state store and runs in WAL mode.
- Dashboard code reads business state and writes only `commands` and `params`.
- Scanner and Observer are the writers of discovery/trading state. Do not make Dashboard routes mutate
  `profile`, `watchlist`, `follow_selection`, `copy_position`, or other business tables directly.
- A published, complete, current `scan_generation` plus its `follow_selection` rows is the source of truth
  for new copy opens. Do not infer production membership from `MIN_FOLLOW_SCORE`, row order, or the raw
  `watchlist` table.
- Once an immutable strategy revision exists, Observer executes its parameter and target snapshots. The
  revision's generation, Core rows, per-wallet sector policies, and follow parameters must agree; missing
  or corrupt execution context fails closed.
- A published generation may intentionally have zero Core wallets. Do not fall back to an old score line in
  that case. Before the first successful selection generation, Observer may run idle; existing open copies
  are still managed exit-only.
- Only executable product markets may reach profile economics or replay: standard Crypto perpetuals and the
  transparent `xyz:*` stock/index/commodity namespace. Spot, `#<id>` outcome/settlement rows, and opaque
  builder namespaces are out of scope.
- Settings saves must not start a scan. A scan starts from the explicit Dashboard action or the configured
  automatic cadence.
- Normal automatic formation has no 24/48-hour admission wait, multi-generation promotion confirmation, or
  one-change-per-run stability fence. Current complete evidence is published immediately; legacy lifecycle
  helpers/constants remain only for compatibility and offline tests.
- Dashboard business failures are not data errors. Reserve “数据异常” for collection, cache, replay, valuation-
  pipeline, or immutable-strategy integrity failures; an incomplete open-position mark is the explicit
  “开放仓位估值待确认” observation state, not a generic data-error badge.
- Reuse the Dashboard's shared `.btn` variants and nearby component patterns. Do not introduce one-off inline
  or private button skins when an existing neutral/accent/go/stop/danger variant expresses the action.
- Never expose, print, commit, or copy secrets, private keys, target files, live databases, or private VPS
  values. Keep those in local `CLAUDE.md` or ignored paths.

## Runtime map

| Concern | Primary files |
|---|---|
| CLI discovery | `hyper/cli/discover.py`, `hyper/discovery/scanner.py` |
| Generation staging/publication | `hyper/discovery/generation.py`, `hyper/selection/state.py`, `hyper/selection/strategy_revision.py` |
| Profile metrics/gates | `hyper/discovery/metrics.py`, `hyper/discovery/scanner_copy_bt.py`, `hyper/selection/follow_score.py` |
| Cached fills/replay inputs | `hyper/copy/fills.py`, `hyper/copy/copy_data.py`, `hyper/copy/copy_evidence.py` |
| Generation market snapshot | `hyper/market/generation_market.py`, `hyper/market/volatility.py` |
| Sector specialization | `hyper/copy/sector.py`, `hyper/copy/copy_data.py` |
| Canonical copy replay | `hyper/copy/copy_backtest.py`, `hyper/copy/copy_engine.py`, `hyper/copy/fill_transition.py` |
| Core formation/tuning | `hyper/selection/core_formation.py`, `hyper/selection/auto_tune.py`, `hyper/copy/sizing.py` |
| Observer/paper execution | `hyper/cli/observe.py`, `hyper/execution/observer.py`, `hyper/market/rest.py`, `hyper/market/ws.py` |
| Dashboard API | `dashboard/server.py`, `dashboard/api/*` |
| Dashboard frontend | `dashboard/web/app.jsx`, `dashboard/web/components/*`, `dashboard/web/app.css`, compiled `dashboard/web/app.js` |
| Launcher/process control | `hyper/launcher/launcher.py`, `hyper/launcher/server.py`, `hyper/launcher/core/*`, `hyper/launcher/web/*` |
| Shared schema/migrations | `hyper/storage.py` |
| Safe Paper reset | `hyper/ops/paper_reset.py`, `hyper/cli/discover.py reset-paper` |
| Tunable values | `hyper/config.py`, `hyper/params.py`, SQLite `params` table |

## Discovery and selection pipeline

The production flow is:

`Leaderboard staging → candidate workset → executable-market fill cache → per-sector structure + canonical
30/14/7 Copy replay → individual Core/Challenger/reject classification → joint wallet-count/parameter formation →
final-parameter requalification → shared-account membership + strict LOO → atomic generation/selection/strategy
revision publish → Observer reload → replay-summary materialization`

### 1. Generation safety

Each scan gets a generation id. Leaderboard rows are written to staging and validated before profiles are
accepted. The default validation requires:

- at least 85% of the previous valid leaderboard row count (except the first non-empty generation);
- unique wallet addresses;
- at least 99% complete leaderboard windows;
- no malformed/empty snapshot.

An invalid or incomplete generation must retain the last published generation and must not publish a new
selection, prune discovery state, or activate new parameters. `scan_generation`, `pipeline_audit`,
`scan_progress`, `scan_runs`, and `strategy_revision` are the operational record.

### 2. Candidate workset and profiles

- New-wallet Leaderboard recall requires account value `$5,000`, leveraged 7d notional volume `$250,000`,
  positive 7d and 30d PnL, plus both 7d ROI `10%` and 30d ROI `20%`. All-time ROI remains score/audit only;
  nominal leveraged volume is never a profitability denominator and has no upper bound. Current Core,
  Challenger and open-position owners bypass discovery recall and always receive retention replay. Every new
  survivor then needs positive 30d Perp PnL and at least 60% 30d Perp PnL share; 7d/all-time Portfolio windows
  are audit-only and never form an AND rejection.
- Deep profiling uses one immutable executable universe for the generation. `hyper/copy/copy_data.py` normalizes symbols
  and removes spot, outcomes and opaque builder fills before cache, metrics and replay; publication audits the
  active cache for scope violations. Network APIs that cannot filter leaderboard rows by product scope are
  tolerated only at the coarse-harvest layer.
- A fresh candidate profile fetch covers `PROFILE_FETCH_DAYS` (currently 37 days: 30-day scoring window plus
  seven warm-up days). Reported copy evidence remains 30/14/7 days.
- With no published generation, every scan request is forcibly upgraded to `cold_full`: it harvests a new
  Leaderboard, profiles the complete candidate workset, bootstraps each new wallet's 37-day history, and
  rebuilds sector specialization.
  A failed first generation remains cold on the next attempt.
- `candidate_fills` is the cache. Once `fill_cache_state` proves that the 37-day source window was completely
  fetched, all later daily evaluations fetch only the delta after that wallet's source cursor,
  merge it into the rolling window, and prune rows older than 37 days. Do not infer source completeness from
  the earliest retained fill: a wallet may simply have no trade near the boundary. Only new wallets and
  missing/incomplete/capped caches perform a resumable 37-day bootstrap or repair. A capped page saves its
  continuation cursor; it must not restart from the 37-day boundary on the next run.
- Every daily generation refreshes the complete Leaderboard and evaluates every official-ROI + Perp-precheck
  survivor. Core, Challenger and open-position owners are also evaluated for safe removal/exit. There is no
  300-wallet budget, rotation/recovery/exploration allocation, deferred tail, seven-day shard or weekly full
  refresh. Workset and fill transport remain separate: the workset is always `all`, while fills are `delta`,
  `full_refetch`, or `mixed`.

### 3. Market-sector specialization

- Crypto and stock/index/commodity evidence are evaluated independently. A complete/cold scan rebuilds each
  wallet's `sector_policy_json` from the current generation; an incremental scan may carry prior evidence for
  audit continuity only, never to preserve a current-generation weak sector's live permission.
- A wallet may be Crypto-only, Stock-only, or genuine Mix. Each side must independently reach Challenger
  economics (5% 30d return, seven closes, five Campaigns, five evidence days), stay profitable after the two
  largest Campaigns and 1.5x costs, and avoid structural/deep-loss/liquidation hard risk. The complete Core
  12/5/3 sample, ten-Campaign, win-rate/Wilson, execution and capacity surface is applied once to the aggregate
  of those safe sectors. This keeps a bad side from contaminating a good side without requiring every side to
  be a standalone Core.
- A profitable sector with too few closed samples is `watch` evidence for Challenger ranking, not live-trading
  permission. Observer, individual replay, shared replay and Dashboard metrics use the same allowed/watch policy;
  an execution snapshot without an explicit allowed sector fails closed.
- Full/cold generation output therefore forms specialization every time. Do not restore whole-wallet portfolio
  PnL/volume/drawdown as a substitute for scoped fills and canonical Copy economics.
- Scanner economics use a sealed generation market snapshot, never the Observer's mutable `coin_vol`. After a
  wallet's executable fills are known and before its first strict Copy replay, its actual coins are resolved once
  per generation: closed-candle sigma as of generation start plus the generation's bulk Crypto/`xyz` context,
  max leverage and Crypto liquidity. An API failure defers affected wallets as a true data error; a valid market
  with fewer than five closed daily candles uses the explicit 7% `insufficient_history_default`.

### 4. Quality gates and scores

`active`/`qualified` means the wallet has passed the quality and copyability requirements. It is not a promise
that every active wallet must fit into the funded Core account.

Every public economic line is a percentage of the canonical replay's recorded `initial_margin_equity` (falling
back to configured account equity × `MARGIN_EQUITY_PCT`), never a fixed `$250/$500` dollar threshold. Current
default classification is:

- 30/14/7 observation floors remain 7/5/5, while new-open Core permission requires 12/5/3 closed episodes,
  ten 30d Campaigns and at least five independent evidence days. High ROI/PnL never creates a small-sample
  exception;
- aggregate 30d Campaign win rate must be at least 60% and its 80% one-sided Wilson lower confidence bound at
  least 50%. Once 14d has five Campaigns it needs 55% wins and positive net. Seven-day evidence has no fixed
  positive line; at five Campaigns, win rate below 40% together with negative net is a hard recent collapse;
- Challenger needs 30-day strict Copy return at least 10%; once 7-day evidence reaches five closes, 7-day
  total return must be at least 3%;
- normal Core needs 30-day return at least 10%, the aggregate sample/win surface above, five evidence days,
  complete open-position valuation, and no recent hard collapse;
- strong Core normally uses a 20% 30-day line with at least 20 closes and ten evidence days. It still needs
  the same 14-day/seven-day sample, win-rate, execution, capacity, valuation, structure and recent-risk checks;
- actionable open rate must be at least 70% and shared/individual capacity fit at least 75%;
- expected normalized margin return has a 2% Core line. A narrow default 1.5–2% miss may remain Challenger
  only when strict Copy totals, recent economics and samples are already strong; materially thinner or negative
  edge is rejected;
- LCB and positive-profit probability are continuous ranking diagnostics after the sample floor, not a second
  hidden Core veto.

Profit concentration is a warning, not a standalone verdict. The replay removes the three largest positive
endpoints and measures the remaining trade body. A concentrated wallet may enter Core when that body has at
least five episodes, at least 60% wins, positive net and median PnL, and PF at least 1.0, while the normal
post-Top2, recent post-Top1 and cost-stress lines also pass. A sampled concentrated wallet whose remaining body
is mostly losing is rejected; an insufficient body remains Challenger observation. Public replay dollars always
include the large winners—the removal is qualification stress only, never a subtraction from displayed PnL.
A sampled allowed sector needs raw payoff ratio at least 0.60. Sample-complete strict Copy needs profit
  factor at least 1.30, 30-day net after removing its two largest winners at least 5% of
  `initial_margin_equity`, positive seven-day net after removing its largest winner, and positive 1.5x-cost
  stress net.

Qualification includes both realized and marked open PnL from one canonical valuation snapshot. Serious recent
collapse rejects the wallet/sector: sustained sampled 14-day and 7-day losses, a sampled negative 7-day loss at
least 25% of the positive 30-day edge, or a hard non-overlapping recent-distribution failure. A warning-level
decline can remain Challenger; a low-value or hard-loss wallet must not be used as candidate-list filler.
If both the seven-day and 14-day post-Top3 trade bodies contain at least ten episodes and remain negative, the
wallet/sector is Challenger-only even when total PnL is positive.

Structural gates are sector-local. HFT, habitual grid/DCA, spot hedge, extreme concurrency (default maximum 15),
and uncopyable structures remain hard failures. Heavy-DCA uses a default threshold of more than 30 adds and only
counts complete round trips; a cache-window episode that starts already open cannot hard-reject a wallet. One
complete Heavy-DCA outlier may enter the exact capped smart-add pressure replay. It is allowed only if that
sector still clears sample, PnL, recent, 70% open-rate and 75% capacity checks with no pressure-replay
liquidation; repeated/heavier failure remains rejected.

There is no lifetime zero-liquidation gate. Isolated liquidation losses already reduce net PnL and increase
drawdown, while liquidation frequency receives a bounded score penalty. Final-parameter 30-day strict replay
may contain at most one isolated liquidation for Core; repetition is Challenger-only. A currently losing 7-day
sector whose loss includes liquidation is still a hard recent failure, and Heavy-DCA pressure has its stricter
rule.

`profile.score` is the raw profile quality score. `watchlist.score` is the final copy-follow score, combining
10% raw quality, 40% normalized Copy quality, 40% account-normalized scalable economics and 10% activity,
plus bounded recent/liquidation penalties. The sample-confidence factor saturates at the actual qualification
floors. Scores order qualified candidates; neither raw score nor `MIN_FOLLOW_SCORE` is a production membership
gate after an explicit selection exists.

Smart-add replication uses `add_metrics_v2`. Each distinct target add order is finalized as `followed`,
`noise_merged`, `hard_cap_blocked`, `coin_cap_blocked`, `cash_blocked`, `min_margin_blocked`, or
`liquidity_blocked`; a later actionable fill slice may atomically replace an earlier noise classification for
the same order id. `noise_merged` is intentional denoising and never a miss penalty. Raw add-order follow rate
is audit-only. Ranking uses target/copy entry-VWAP divergence normalized by coin sigma plus genuinely blocked
actionable adds; with fewer than five add episodes this component remains audit-only. Legacy `missed_add_rate`
is retained only for backward-readable audit and must not feed qualification, selection, or tuning.

### 5. Core/Challenger lifecycle

The persistent `wallet_registry` retains identity, roles, good/bad confirmations, data errors, and reasons.
The user-facing roles are:

- **Core** (`role=core`): Observer may open new copy positions.
- **Challenger** (`role=challenger`): qualified candidate, no new copy opens.
- **Exit-only** (`role=exit_only`): no new opens, but existing copies are managed to exit.
- **Rejected**: business value/structure is below the observation line and is not shown as Challenger.
- **Quarantine**: collection/cache/replay/valuation/strategy data is invalid and is not a new-entry target.

`CORE_INITIAL_MAX_N` (default 16, bounded by `MAX_TARGETS=40`) is a user-set hard maximum, not a quota or
auto-tuned value. `CORE_TARGET_MIN_N` (default 10) is a service target, never a permission to weaken individual
quality gates: while below it, a daily generation may make portfolio-safe additions. Normal ranking replacement,
parameter retuning, and leave-one-out reshuffling run only after seven days since the last actual membership
change. Daily evidence still removes liquidation, Forward-loss, campaign-structure, or other individual hard
failures immediately while retaining every other qualified incumbent. Production automatic formation is:

1. Rank the current generation's individually qualified Core/Challenger pool under one parameter surface.
   Parameter-sensitive return/weekly/thin-edge Challengers and a hidden, tightly bounded 5–10% cold-start return
   probe may inform tuning, but cannot be published unless the final surface clears the real public gates.
2. Jointly search wallet count and a complete portfolio parameter surface. Pools of at most eight evaluate every
   count; larger pools use `search_quality_prefix` with the bounded `N → N/2 → boundary` search plus
   neighbours and the full prefix.
3. Re-run every candidate's canonical individual replay under the winning parameters, the same refined intratrade
   price path used by shared replay, and one valuation snapshot.
   Anyone no longer Core-eligible is removed before shared-account membership.
4. With that fixed surface, `search_quality_membership` evaluates every subset for pools of at most eight.
   Larger pools start from the winning prefix and run bounded add/swap closure so one congested wallet cannot
   block stronger wallets behind it.
5. Validate bounded final membership candidates on three non-overlapping ten-day folds, a profitable latest
   fold, 1.5x-cost stress, solvency, at least 70% actionable opens and at least 75% capacity fit. A replacement
   of still-qualified old Core also needs at least two improving folds, a non-degrading latest fold, at least
   5% risk-adjusted utility gain and net gain of 2% of `initial_margin_equity`. Current qualification or recent
   risk failure still removes immediately without that replacement hurdle.
6. Stress the final set after removing its largest one and two winning trades and after removing its largest
   contributing wallet. Normal and 1.5x-cost net must remain positive; only an all-strong-evidence set may
   publish with an explicit single-wallet-dependency warning. Persist wallet/coin/day/side concentration.
7. On a scheduled rebalance, repeatedly apply strict leave-one-out elimination only when removing a member raises
   funded net PnL by at least `$1` and the smaller set already passed the same membership robustness checks.
   Between rebalances, publish only hard-failure removals and validated additions toward the service target.

An operator may star a current Core wallet through the Dashboard. The durable `target_controls.pinned` flag
locks ordering and retention only while the wallet still passes the current Core business gates: an enabled,
qualified star is required in membership search and LOO, occupies the user Core maximum, and is ordered before
automatic members by `pinned_at`. A star cannot bypass strict-Copy win/sample, recent-body, liquidation,
economics or structure gates; a failing held wallet becomes exit-only. A true replay/cache/market-snapshot or
strategy-integrity failure still fails the generation closed and retains the prior complete strategy; it must not
silently clear the star or publish corrupt execution context. Disabling a starred wallet removes it from the
immutable execution target set until re-enabled. Removing the star returns it to normal
automatic selection on the next generation.

A wallet is not considered inactive merely because it has emitted no new flat-to-open event within 48 hours
only when the target still has a material, net-profitable open book and our forward-only copy book for that
wallet is also still open and net-profitable. A carried losing target or losing copy never receives this bypass.
This narrow long-hold activity exception does not waive current strict-Copy economics, recent-loss, structure,
valuation, market-snapshot, or data-integrity gates; once the mirrored episode closes or turns net-negative, the
normal activity clock applies again.

A pure addition to a still-qualified Core is not an incumbent replacement: it needs positive funded marginal
net plus the fold/latest/stress safeguards, but not the 5% utility and 2%-of-equity anti-churn hurdle. Those larger
hurdles apply only when a candidate set removes or replaces a still-qualified old Core member.

Shared replay evaluates real balance contention, open capture, capacity, deployment, drawdown, fees/slippage and
per-coin limits. A high-scoring wallet can remain Challenger when it adds no funded-account value; a lower raw
rank may enter when the final shared combination is better. Core order is conditional leave-one-out portfolio
contribution, while Challenger order is current follow score. There is no fixed Core count and no stability
fence retaining a wallet that fails current Core qualification.

`FOLLOW_SELECTION_MODE=auto` lets the scanner publish this selection. `manual` carries the current selection
rows into the next generation and leaves membership operator-owned; it does not silently rewrite the Core.

### 6. Atomic publication and tuning

The scanner prefetches only the bounded candidate market path outside the final SQLite publication transaction.
Formation and tuning are synchronous. The winning surface, current-generation eligibility, explicit selection,
generation publication, follow history and immutable strategy revision are then sealed as one atomic decision.
Observer receives one `reload_params` command for the activated revision. Post-publication work only materializes
the effective portfolio and per-selection replay summaries used by the Dashboard; it is not a second async tuner.

The compact portfolio tuner searches all three volatility-tier upper margins and leverage caps,
`DEPLOY_FULL_PCT`, and smart-add `ADD_GAP_K`, `POS_ADD_GAP_K`, `ADD_GAP_SHRINK_G`, and `ADD_MAX_HARD`. It does
not tune the three lower margins, per-coin caps, `MAX_DEPLOY_PCT`, `MARGIN_EQUITY_PCT`, Core maximum, tail-close,
or stop/risk-owner settings. Candidate finalists run three non-overlapping ten-day folds, the latest fold as a
positive holdout, a 1.5x-cost stress replay, open/capacity checks, maintenance metadata and bounded price-path
coverage. Cold start additionally probes a few absolute margins at 50/75/100% of the four-add-safe ceiling;
it does not restore the old large Cartesian grid.

Current Paper defaults deliberately allow the full closed loop:

- `AUTO_TUNE_MODE=apply`;
- minimum shadow days, forward closed episodes and master-leverage coverage are zero for Paper; refined price
  path and maintenance-metadata coverage still default to 94% and 95%;
- a changed parameter candidate still must pass OOS/holdout/stress/risk gates;
- portfolio tuning has no wall-clock cutoff; finite axes and finalist limits bound completion;
- live-money deployments should use conservative shadow/coverage/forward thresholds instead.

Tuning must use only the same complete generation's cached fills, sector policies, marks and follow snapshot.
The generation market snapshot is immutable after profiling and its content hash is recorded in every scanner,
formation and auto-tune strategy revision. Profile replay, shared replay and tuning must all load that generation's
snapshot. A missing legacy snapshot blocks `regate`, `optimize`, selection repair and replay rematerialization until
a new scan succeeds; an already-published legacy strategy may continue executing unchanged.
Changing `MARGIN_EQUITY_PCT` during a run invalidates that run's finalization instead of allowing stale results to
overwrite the new operator policy. Any pre-publication formation/path/tuner/snapshot-consistency failure rolls
back the new membership and parameters, leaving the prior published generation and immutable strategy active.
Completed profiles/fill cache remain on the failed generation as `leaderboard_validated`, so `finalize-profiled`
can retry without another network sweep. A post-publication summary-replay failure is audited but cannot undo the
already atomic strategy. `auto_tune_state.effective_portfolio_replay` is valid only when its generation matches
the current published generation.

## Observer and execution model

- Observer is forward-only. It starts each target cursor at the current time and never backfills historical
  fills into a new copy book.
- Signal source is REST `userFillsByTime`; standard-perp pricing uses WS BBO and builder/stock pricing uses REST
  `l2Book`.
- Observer normally loads parameters, enabled Core targets, account context and sector policies from the active
  immutable strategy revision whose generation matches the current published selection. The direct published-
  selection/params loader is a rolling-migration fallback only. Existing positions for removed, disabled, or
  no-longer-Core wallets stay polled and managed exit-only.
- Copy state is persisted in `copy_position` and `copy_action`. Paper execution is taker-only; maker execution
  will be designed separately before a real-money deployment. Live `copy_position` PnL includes realized closed
  PnL plus unrealized PnL for open rows.
- The source-wallet membership high-water breaker is retired. Observer and canonical replay do not freeze,
  reduce or exit a wallet merely because it gave back prior profit; historical `wallet_risk_state` rows and
  `WALLET_HWM_*` values are migration-only and cannot affect qualification or execution. Deep-loss path risk,
  liquidation cooldowns, mirrored exits and portfolio/margin caps remain active.
- Sizing is equity/available-balance based and volatility-tiered. Profits compound; drawdown contracts sizing
  through the configured equity curve. Isolated margin, per-coin/deploy caps, liquidity filters, and add caps
  remain hard execution boundaries.
- BTC always uses the stable sizing tier, regardless of its measured sigma. Its real sigma still controls smart-add
  spacing and remains auditable. Every non-BTC Crypto and transparent `xyz:*` market uses mid below 9% sigma and
  high at or above 9%; unresolved/young valid markets temporarily use 7% (mid). `xyz:*` additionally obeys the hard
  stock leverage ceiling.
- `MARGIN_EQUITY_PCT` is a manual-only sizing base (default 100%, UI range 10–100%). It scales each new
  position's drawdown-adjusted equity base without freezing the remainder; real cash, per-coin caps and total
  deployment still use full risk equity. Auto-tune and Core-count selection must not modify this value.
- Below `DEPLOY_FULL_PCT`, a new open uses the tuned tier upper margin. It shrinks linearly toward the operator
  lower margin until `MAX_DEPLOY_PCT`; new opens stop at that cap, while follow-on adds may use remaining real
  cash because they preserve an already-entered episode.
- Smart-add spacing compares target transaction prices only; our BBO price is execution/PnL, never mixed into the
  target volatility gate. Adverse and positive adds have separate sigma gaps that expand after each followed
  add. One target order can consume at most one first-margin unit, the final reserved add may fill remaining
  same-coin room, and first-open sizing preserves at least four executable follow-on add slots before the hard
  `ADD_MAX_HARD` ceiling.
- Target reductions are percentage based: tiny fills accumulate until the target has unwound 10% since our last
  mirrored reduce, while a full close always executes. After a target reduce, a profitable tail at or below 20%
  of peak size exits; up to 35% may exit when its market-specific liquidation path could give back at least 50%
  of close-now episode profit. This is profit protection and never converts a losing episode into a stop-loss.
- Optional `SMART_TP_ENABLE` is off by default and is captured in the same immutable follow-parameter revision
  used by Observer and canonical replay. When enabled, each position arms a volatility-normalized high-water at
  `0.60σ/0.50σ/0.40σ` for stable/mid/high without selling; after 20%/35%/50% giveback it closes 20%/25%/25%
  of the arming size, rebasing the remaining high-water after each cut and preserving a 30% tail. Once that tail
  exists, target trims below 30% are observed but not mirrored; cumulative target reduction of at least 30%, a
  full close, or a flip exits the tail completely. Target adds after the first proactive cut never rebuild exposure.
  The legacy liquidation-risk tail rule is bypassed while smart take-profit owns the episode.
- A manual 100% close creates a 24-hour same-wallet/same-coin cooldown only when the realized episode is losing.
  A profitable/breakeven full close has no cooldown. Any partial manual close keeps the episode live so later
  target adds, reductions and close remain actionable.
- Copy execution has no hard-threshold stop-loss. Risk is bounded by selection, sizing, isolated margin,
  leverage/deployment caps, mirrored exits, and liquidation accounting.
- Core/strategy reloads are command-driven (`reload_params`) and do not copy historical fills or retroactively
  rescale existing positions.

## Dashboard contract

The dashboard reads the API and controls workers through the command/params plane. Important endpoints include:

- `/api/overview`, `/api/positions`, `/api/history`;
- `/api/wallets?tab=followed|challenger|dropped`;
- `/api/wallets/{address}` for lazy wallet details;
- `/api/positions/{id}` for lazy position detail;
- `/api/pipeline-audit` for generation, profile, selection, watchlist, and tuner reasons;
- `/api/params`, `/api/commands`, and process/scan status endpoints.

The wallet list is intentionally light. Detail and position-detail requests are lazy. The UI labels the current
roles as “跟单中”, “候选”, and “降级”; do not reintroduce internal role/model/data columns into the operator
table without a concrete decision use. Wallet profitability, sample counts and win rate must come from the
current immutable selection replay when available, filtered to the same allowed/watch sectors. Use profile replay
only as the explicit fallback; an unavailable strict-Copy win rate renders `—`, never a fabricated `0%` or the
target's raw account win rate.

Business qualification labels include return/sample/thin-edge/recent-decline/portfolio-candidate and
open-valuation-pending states. They must not map to “数据异常”. Only `deferred_data_error`, invalid cache/replay,
valuation-pipeline failure, corrupt strategy context, and quarantine are data-error states; rejected weak
economics are simply omitted from Challenger and remain explainable in audit/dropped history.

## Commands and local verification

Run from the repository root:

```bash
# Dashboard
python3 -m dashboard.server --db data/hl.db --static dashboard/web --host 127.0.0.1 --port 8810

# Scanner / maintenance
python3 -m hyper.cli.discover --db data/hl.db serve-rescan
python3 -m hyper.cli.discover --db data/hl.db scan --days 14 --scan-interval 8
python3 -m hyper.cli.discover --db data/hl.db scan --full --days 14 --scan-interval 8
python3 -m hyper.cli.discover --db data/hl.db regate
python3 -m hyper.cli.discover --db data/hl.db optimize
python3 -m hyper.cli.discover --db data/hl.db finalize-profiled --generation GENERATION_ID
python3 -m hyper.cli.discover --db data/hl.db repair-watchlist
python3 -m hyper.cli.discover --db data/hl.db watchlist --top 40
python3 -m hyper.cli.discover --db data/hl.db reset-paper --yes
# Add --factory-params only when operator settings should also return to code defaults.

# Observer
python3 -m hyper.cli.observe --db data/hl.db observe
python3 -m hyper.cli.observe --db data/hl.db report

# Launcher
python3 -m hyper.launcher.launcher --port 8799 --no-browser

# Mock dashboard
python3 dashboard/web/dev/seed_mock.py data/hl_mock.db
python3 dashboard/web/dev/mock_consumer.py data/hl_mock.db
DASH_PASSWORD=mock123 python3 -m dashboard.server --db data/hl_mock.db --static dashboard/web --host 127.0.0.1 --port 8810
```

`scan --full` means a full candidate-universe harvest and evaluation. It does not re-download a complete
wallet cache; only new or incomplete wallets fetch the 37-day bootstrap window. Except for the forced
first-generation `cold_full`, a Dashboard manual rescan is incremental unless its command payload requests
`full=true` or the CLI uses `--full`. `regate` re-applies current gates and rebuilds sector policy from cached evidence; `optimize` re-forms
and jointly tunes the current published generation without wallet fill refetch; `finalize-profiled` retries an
already-complete but unpublished generation after a finalization failure.

`reset-paper --yes` is the supported from-zero reset. Stop Observer and Scanner first. It clears discovery,
cache, selection, strategy, replay and Paper trading state, preserves operator `params` and encrypted provider
credentials, and recreates the `$10,000` Paper account. `--factory-params` is the explicit restore-defaults variant;
deleting the database file is also a factory reset, not a settings-preserving reset.

Before Python changes:

```bash
python3 -m compileall -q hyper dashboard
python3 -m unittest discover -s hyper/tests
```

After dashboard edits, edit JSX/CSS sources and rebuild; never hand-edit the compiled bundle:

```bash
dashboard/web/build.sh
hyper/launcher/web/build.sh
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
