# AGENTS.md

## Scope

This repository is organized as a multi-product copy-trade workspace. The active product is Hyperliquid:

- leaderboard discovery and wallet profiling;
- copyability scoring and canonical copy replay;
- a bounded quality pre-Core pool followed by count-specific adaptive portfolio tuning and strict publication;
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
- Every complete strict replay may publish exactly its currently proven score-prefix membership. There is no
  promotion confirmation, minimum tenure, soft-failure grace or prior-Core carryover. Expensive optimization
  runs only when explicitly requested; scheduled evidence refresh must not silently start a parameter grid.
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
30/14/7 Copy replay → individual Core/Challenger/reject classification → bounded top-16 pre-Core ranking →
count-specific adaptive tune (16→8→12...) → fixed-surface membership → one final strict shared-account replay +
strict LOO → atomic generation/selection/strategy revision publish → Observer reload → replay-summary materialization`

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

- New-wallet Leaderboard recall requires account value `$20,000`, leveraged 7d notional volume `$250,000`,
  and positive 7d and 30d PnL. Nominal leveraged volume is activity evidence, never a profitability denominator.
  Before fill history is downloaded, official `perpMonth` must show positive 30d Perp PnL, at least 60% Perp
  PnL share and at least 20% dynamic 30d Perp return. `history_under_28d`, `boundary_sample_gap`, and
  `zero_start_equity` are evidence gaps: they may remain Challenger but cannot enter Core. No prior role, star
  or retention state can bypass current qualification.
- Deep profiling uses one immutable executable universe for the generation. `hyper/copy/copy_data.py` normalizes symbols
  and removes spot, outcomes and opaque builder fills before cache, metrics and replay; publication audits the
  active cache for scope violations. Network APIs that cannot filter leaderboard rows by product scope are
  tolerated only at the coarse-harvest layer.
- A fresh candidate profile fetch covers `PROFILE_FETCH_DAYS` (currently 37 days: 30-day scoring window plus
  seven warm-up days). Reported copy evidence remains 30/14/7 days.
- Canonical 30/14/7 Copy evidence is one 37-day warm replay sliced at each reporting boundary, never three
  independently funded accounts. Timestamped open, capacity-block, add and deploy evidence is sliced with the
  same continuous capital path, so recent-window congestion uses the equity actually banked before that window.
- With no published generation, every scan request is forcibly upgraded to `cold_full`: it harvests a new
  Leaderboard, profiles the complete candidate workset, bootstraps each new wallet's 37-day history, and
  rebuilds sector specialization.
  A failed first generation remains cold on the next attempt.
- `candidate_fills` is the cache. Once `fill_cache_state` proves that the 37-day source window was completely
  fetched, all later scheduled evaluations fetch only the delta after that wallet's source cursor,
  merge it into the rolling window, and prune rows older than 37 days. Do not infer source completeness from
  the earliest retained fill: a wallet may simply have no trade near the boundary. Only new wallets and
  missing/incomplete/capped caches perform a resumable 37-day bootstrap or repair. A capped page saves its
  continuation cursor; it must not restart from the 37-day boundary on the next run.
- Every scheduled generation refreshes the complete Leaderboard and evaluates every official-ROI + Perp-precheck
  survivor. Core, Challenger and open-position owners are also evaluated for safe removal/exit. There is no
  300-wallet budget, rotation/recovery/exploration allocation, deferred tail, seven-day shard or weekly full
  refresh. Workset and fill transport remain separate: the workset is always `all`, while fills are `delta`,
  `full_refetch`, or `mixed`.

### 3. Market-sector specialization

- Crypto and stock/index/commodity evidence are evaluated independently. A complete/cold scan rebuilds each
  wallet's `sector_policy_json` from the current generation; an incremental scan may carry prior evidence for
  audit continuity only, never to preserve a current-generation weak sector's live permission.
- A wallet may be Crypto-only, Stock-only, or genuine Mix. A side with positive Copy economics may remain
  `watch` while samples grow; live permission requires sufficient sector evidence, positive canonical Copy
  economics and no structural hard failure. Path drawdown and proxy liquidation evidence remain available to
  tuning rather than vetoing a sector before sizing can be repaired.
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
- Selection price-path prefetch must apply each wallet's effective `allowed` sectors, or its `watch` sectors only
  when no sector is allowed, before validating against the sealed generation snapshot. A disabled specialty's
  cached fills may not require unrelated generation metadata or abort the whole bounded candidate batch. Path
  prefetch failure is a resumable generation data failure, never permission to publish a valid empty Core.

### 4. Quality gates and scores

`active`/`qualified` means the wallet has passed the quality and copyability requirements. It is not a promise
that every active wallet must fit into the funded Core account.

The canonical strict-Copy replay starts from a standardized `$10,000` window equity for comparable wallet
audits, then compounds continuously: every later open sizes from the floating equity available at that time.
For any window, dynamic return is `window net PnL / that window's start equity`; it is not a fixed-capital or
per-trade return. Thus a 30-day replay that grows `$10,000` to `$13,000` contributes 30%. The rolling 7-day
window is cut from the same continuous 30-day equity path and uses its own day-23 boundary equity.

The deep-fill source gate requires at least ten complete 30-day Episodes, at least 70% fee-paid Episode wins,
and a true actionable open within 72 hours. If the three largest winning Episodes contribute at least 70% of
gross profit, the remainder must itself have at least 70% wins and non-negative net PnL. Structural exclusions
cover HFT, systematic slicing, grid/heavy DCA, spot hedging, extreme concurrency and opaque markets.

At most 40 source-qualified wallets receive a fills-only rough Copy replay. Rough admission requires dynamic
30d/rolling-7d returns of 15%/5%, at least seven closed Copy Episodes, at least 60% Copy wins, at least 70% open
follow rate and complete valuation. The composite score ranks this pool only; it never vetoes a qualified wallet.
At most the first 16 proceed to unified tuning and strict path replay.

Final per-wallet strict admission requires dynamic 30d/rolling-7d returns of 10%/3%, at least 60% Copy wins,
at least 70% open follow rate, activity within 72 hours, complete data/valuation/sector/path evidence and no more
than three simulated isolated liquidations. Official evidence gaps remain Challenger and cannot enter Core.
Campaigns, weekly Copy folds, per-close returns, cost multiples/stress, LCB/probability/PF/payoff qualification,
maximum drawdown and score floors are not Core gates. Fees, slippage, average profit, concentration and drawdown
remain audit telemetry.

Formation ranks qualified wallets strictly by final score and considers only score prefixes up to 16. No star,
prior Core role, tenure, minimum count, forced fill or lower-ranked substitution may alter that order. Each
prefix is tuned with one continuous floating-equity account. The optimizer first maximizes 30-day net profit,
then within the near-best profit band minimizes liquidations, reduces real congestion/missed opens, and jointly
adjusts tier margin, leverage and smart-add structure. Only the fixed winner receives final path-complete strict
replay. Publication requires the shared account to return at least 10% over 30 days and 3% over the latest
rolling 7 days, with both standardized `$10,000` and actual Paper starting-equity results persisted.
There is no second 85% total-margin slice: adds may use the remaining real available cash after fresh opens
stop, while per-coin caps and isolated-margin liquidation still bound exposure.
Liquidity rejection and target-dust minimum-notional rejection are never tuned away.

Qualification includes both realized and marked open PnL from one canonical valuation snapshot. Recent
repeatability is judged by source/Copy Episode win rates plus the dynamic rolling-7-day return from the same
continuous 30-day equity path; there are no weekly-fold or per-close-density admission rules. Individual
failures remain explicit Challenger evidence. Four or more proxy liquidations on the final tuned 30-day surface
remain Challenger evidence; the active pre-tune surface cannot reject a wallet that parameter optimization may
repair.

Structural gates are sector-local. HFT, habitual grid/DCA, spot hedge, extreme concurrency (default maximum 15),
and uncopyable structures remain hard failures. Heavy-DCA uses a default threshold of more than 30 adds and only
counts complete round trips; a cache-window episode that starts already open cannot hard-reject a wallet.
Any complete Heavy-DCA violation rejects that sector and cannot be resurrected by a profitable Copy replay.

There is no zero-liquidation rule and no historical maximum-drawdown admission threshold. Source fills do not
disclose their true margin/leverage, so both values are conservative reconstructions at our leverage ceiling.
The active surface may enter tuning with any proxy count; the final tuned 30-day strict replay permits at most
three isolated proxy liquidations per Core wallet. Four or more remain Challenger-only. Liquidation losses still
reduce net PnL and receive a bounded score penalty; path drawdown remains visible for diagnostics only.

`source_quality_score` orders deep-fill survivors before the Top40 cap. `rough_copy_score` then orders the rough
Copy-qualified pool using exactly: official Perp 30d return 10%; rough Copy 30d/7d dynamic returns 20%/10%;
source/Copy Episode win rates 20%/10%; open-follow/add-reduction replication 15%/5%; and recency/open count
5%/5%. Components are monotonic and capped. Scores only establish strict descending order and have no 70/75
permission line. Final strict metrics recompute the same score surface for the surviving Top16.

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
- **Challenger** (`role=challenger`): a research wallet waiting on official/source/rough/strict evidence or a
  failed business threshold; no new copy opens. Evidence gaps stay distinct from economic failures.
- **Exit-only** (`role=exit_only`): no new opens, but existing copies are managed to exit.
- **Rejected**: business value/structure is below the observation line and is not shown as Challenger.
- **Quarantine**: collection/cache/replay/valuation/strategy data is invalid and is not a new-entry target.

`CORE_INITIAL_MAX_N` and `CORE_TARGET_MAX_N` default to 16. There is no minimum Core count, service quota,
incumbent grace or forced replacement count: zero to sixteen wallets may publish. Production formation is:

1. Build final-surface individual evidence from cached fills and one refined 15-minute path per bounded
   candidate. Qualification uses the complete current rules above, not current/previous role.
2. Sort every qualified candidate by final score descending and retain at most the first 16. Stars remain
   operator attention metadata only. They do not affect admission, ordering, membership or suffix removal.
3. Search only score prefixes. Each count node is tuned from the same normalized cached fills with continuous
   floating equity. A combination may shorten the lowest-scoring suffix, but may never skip a higher score to
   insert a lower score.
4. Recompute each wallet's strict qualification on the tuned surface. Failures are removed without pulling
   replacements from rank 17 or below.
5. Run exactly one final path-complete 30-day shared-account replay after parameters and membership are fixed.
   Require dynamic 30-day return at least 10%, rolling-7-day return at least 3%, at least 70% open follow,
   positive net in both windows and complete price-path coverage. Every member must remain at or below three
   simulated liquidations.
6. Persist both the standardized `$10,000` starting-account result and the result using current Paper equity.
   Any failed qualification or final replay aborts the proposal atomically; it cannot publish a partial list.

An operator may star a wallet through the Dashboard for attention and manual review. The durable
`target_controls.pinned` flag is never a selection permission. Manual disable remains authoritative, and an
open copied position whose source loses Core authority is managed exit-only.

A wallet needs a true actionable flat-to-open signal within 72 hours for Core new-open permission. Missing or
stale activity never deletes an otherwise profitable Profile: it remains Challenger and can promote after a new
signal and confirmation. Existing copied positions remain managed exit-only.

Shared replay evaluates real balance contention, open capture, capacity, deployment, drawdown, fees/slippage and
per-coin limits. Core and Challenger order is final score order. Leave-one-out economics may remove only the
current low-score suffix and remains audit telemetry for every other member. There is no promotion confirmation,
soft tenure, stable-retention bypass or minimum count.

`FOLLOW_SELECTION_MODE=auto` lets the scanner publish this selection. `manual` carries the current selection
rows into the next generation and leaves membership operator-owned; it does not silently rewrite the Core.

### 6. Atomic publication and tuning

The scanner prefetches only the bounded candidate market path outside the final SQLite publication transaction.
Formation runs the bounded Top16 unified parameter search required by the current generation, then strictly
replays individual and shared-account membership on the winning execution surface and seals eligibility,
explicit selection, generation, follow history and its immutable strategy revision as one atomic decision. A
failed optimizer aborts publication; it may not publish a partial list or retain an old Core through a bypass.

Repeated strict replays must reuse one normalized price path, filter it to the candidate's actual markets/time
range, and retain only compact portfolio summaries between membership candidates. Do not cache full position and
equity-curve results for every explored set: the production host is intentionally small and must fail boundedly
rather than reach the OOM killer.

The compact portfolio tuner searches all three volatility-tier margins and leverage caps,
and smart-add `ADD_GAP_K`, `POS_ADD_GAP_K`, `ADD_GAP_SHRINK_G`, and `ADD_MAX_HARD`. It does
not tune per-coin caps, `MAX_DEPLOY_PCT`, `MARGIN_EQUITY_PCT`, Core maximum, tail-close,
or stop/risk-owner settings. Any optimizer walk-forward folds compare parameter robustness only; they do not
decide wallet admission. The selected set separately passes the official 30-day Perp screen, source Episode
contract and dynamic strict-Copy 30d/rolling-7d returns. There is no weekly, Campaign, per-close-density or
cost-stress admission rule.
Price-path and maintenance-risk validation belongs to the one final strict 30-day replay, not every parameter candidate. Cold start may probe a
few absolute margins at 50/75/100% of the four-add-safe ceiling; it does not restore the old large Cartesian grid.
Leverage probes pair a lower leverage with reciprocal margin so each tier's `margin × leverage` notional stays
approximately constant before capacity caps. Selection is profit-led, but candidates within the configured
near-best profit band are ordered by fewer liquidations, better capacity/open fit, then measured add fidelity.
A proposal which retains the configured share of profit and strictly reduces liquidation evidence may apply as a
safety repair without pretending to clear the ordinary relative-profit-gain hurdle.

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
  `WALLET_HWM_*` values are migration-only and cannot affect qualification or execution. Path-risk telemetry,
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
- A new open uses the tuned tier margin until `MAX_DEPLOY_PCT`; new opens stop at that cap, while follow-on
  adds may use the remaining real cash because they preserve an already-entered episode. The former
  `DEPLOY_FULL_PCT` linear-shrink line and second 85% total-margin slice are compatibility-only/retired.
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
- Copy execution has no per-position hard-threshold stop-loss. The Paper account has a separate global
  high-water equity stop (`PORTFOLIO_DRAWDOWN_STOP_PCT`, default 15%): once hit, Observer pauses new opens,
  persists the trip and repeatedly flattens all remaining positions. A manual resume clears the trip and
  rebases the high-water to current equity. Risk is otherwise bounded by selection, sizing, isolated margin,
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

`scan --full` means a full candidate-universe harvest and evaluation and bypasses the short-lived official
Portfolio prefilter cache. It does not re-download a complete wallet fill cache; only new or incomplete
wallets fetch the 37-day bootstrap window. Except for the forced
first-generation `cold_full`, a Dashboard manual rescan is incremental unless its command payload requests
`full=true` or the CLI uses `--full`. `regate` re-applies current gates and rebuilds sector policy from cached evidence; `optimize` re-forms
and jointly tunes the current published generation without wallet fill refetch; `finalize-profiled` retries an
already-complete but unpublished generation after a finalization failure. `finalize-profiled --no-retune` is the
explicit operational fallback for sealing the active parameter surface when expensive tuning exceeds host
capacity; it does not skip strict individual, path, cost, capacity, or shared-membership gates.

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
