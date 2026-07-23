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
- Normal automatic promotion requires two qualifying complete generations at least 24 hours apart. Ordinary
  membership/rank changes are weekly and a new Core has 14 days of soft minimum tenure; hard data/risk failures
  still act immediately and two consecutive generations confirm other soft failures.
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

- New-wallet Leaderboard recall requires account value `$20,000`, leveraged 7d notional volume `$250,000`,
  and positive 7d and 30d PnL. Leaderboard ROI windows are score/audit only; nominal leveraged volume is never
  a profitability denominator and has no upper bound. Before fill history is downloaded, the official
  `perpMonth` PnL/account-value series must provide four adjacent 7d folds and each must return at least 5%;
  incomplete time-series evidence is deferred, not rejected. The same Portfolio response must also show
  positive 30d Perp PnL and at least 60% 30d Perp PnL share. Current Core, Challenger and open-position owners
  bypass discovery recall and always receive retention replay. Fill-based strict Copy later requires at
  least 10% return over 30d and 5% over the latest rolling 7d on our own capital. Its four adjacent 7d folds
  must all contain Campaign evidence, at least three must be profitable, and the one permitted losing fold
  cannot exceed 25% of total 30d profit. Aggregate replay must remain profitable after taker fees are stressed
  to 1.5x. Average net per close is a ranking diagnostic, not a duplicate hard economic gate.
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
- A wallet may be Crypto-only, Stock-only, or genuine Mix. A side with positive strict-Copy economics may remain
  `watch` while samples grow; live permission requires sufficient sector evidence, positive 1.5x cost stress,
  and no structural/deep-loss/liquidation hard risk. Ten-Campaign, non-overlapping stability, activity,
  execution and capacity proof is applied once to the aggregate of safe sectors. This keeps a bad side from
  contaminating a good side without requiring every side to be a standalone Core.
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

Every public economic line is a percentage of the canonical replay's recorded `initial_margin_equity` (falling
back to configured account equity × `MARGIN_EQUITY_PCT`), never a fixed `$250/$500` dollar threshold. Current
default classification is:

- any positive 30-day strict-Copy result remains Challenger; insufficient samples, fold evidence, activity,
  outlier stress or cost stress are explicit Challenger reasons rather than economic rejection;
- normal Core needs ten independent 30-day Campaigns, at least five evidence days, complete valuation/path
  data, at least 10% 30-day strict-Copy return, at least 5% latest rolling 7-day strict-Copy return, and no
  hard risk;
- target-wallet stability uses official Portfolio for four adjacent non-overlapping 7-day folds covering the
  latest 28 days, each with at least 5% return. Strict Copy uses four matching follower folds as timing
  stability evidence: all four contain a Campaign, at least three are profitable, and the one permitted
  losing fold cannot exceed 25% of total 30-day profit. Aggregate net stays positive when already-modeled
  taker fees are increased to 1.5x. The preferred 0.5% average net per close remains in ranking so it can
  penalize thin/high-turnover economics without becoming a duplicate hard veto;
- the latest true flat-to-open signal must be within 72 hours for Core. Older wallets remain Challenger and
  existing copied positions remain managed exit-only;
- rolling 14-day return, PF, Wilson confidence and raw payoff are ranking/diagnostic signals. Latest rolling
  7-day return is the explicit 5% Core recency gate; Campaign/body win rates are explicit repeatability gates;
- actionable open rate must be at least 70% and shared/individual capacity fit at least 75%;
- expected normalized margin return has a 2% Core line; a miss remains Challenger while strict Copy stays
  profitable;
- LCB and positive-profit probability are continuous ranking diagnostics after the sample floor, not a second
  hidden Core veto.

Profit concentration has one hard Core stress only: remove the largest winning independent Campaign and require
the remaining 30-day net to stay positive. Top-two, body-after-top-three and top-wallet removals are retained as
diagnostics only because hard-gating all of them repeatedly judged the same outlier. Public replay dollars still
include the large winner. Positive 1.5x-cost stress remains a separate Core execution check.

Qualification includes both realized and marked open PnL from one canonical valuation snapshot. Recent
repeatability is judged by the non-overlapping folds above; rolling 7-day magnitude is a Core gate while
rolling 14-day return remains diagnostic. A magnitude/fold failure, stale activity or weak stress remains
Challenger, while current deep loss, repeated liquidation, invalid data and 30-day strict-Copy loss retain
their explicit hard outcomes.

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

`profile.score` is a discovery-only prior when Copy evidence is absent. Once canonical Copy exists,
`watchlist.score` uses 30% funded economics, 25% repeatability, 15% edge confidence, 15% operability, and
15% path risk. Funded economics combines explicit 30d and latest-7d return magnitude with non-overlapping
fold timing and median per-close density; overlapping 14d return and the legacy raw score contribute zero.
The sample-confidence factor saturates at the actual qualification floors. New Core must score at least
75/100 after all binary hard gates pass; score then orders those survivors and cannot compensate for a failed
weekly, win-rate, execution, capacity, valuation, or risk gate.

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
- **Challenger** (`role=challenger`): a bounded near-Core wallet that already clears the 30d strict-Copy
  return line but is still waiting on recent return, fold/sample/path/score or shared-portfolio proof; no new
  copy opens. Evidence-incomplete wallets stay here rather than being labeled as economic failures.
- **Exit-only** (`role=exit_only`): no new opens, but existing copies are managed to exit.
- **Rejected**: business value/structure is below the observation line and is not shown as Challenger.
- **Quarantine**: collection/cache/replay/valuation/strategy data is invalid and is not a new-entry target.

`CORE_INITIAL_MAX_N` and `CORE_TARGET_MAX_N` default to 16. There is no minimum Core count or service quota:
zero to sixteen wallets may publish, and no scheduled generation may add a wallet merely to reach a count. Normal ranking replacement,
parameter retuning, and leave-one-out reshuffling run only after seven days since the last actual membership
change. Scheduled evidence refresh still removes liquidation, Forward-loss, campaign-structure, or other individual hard
failures immediately while retaining every other qualified incumbent. Production automatic formation is:

1. First require every non-path Core gate. Then run only that bounded near-Core pool plus current Core through
   canonical individual Copy replay once with the refined 15-minute path (and finer candles only for ambiguous
   risk ranges). This is the authoritative liquidation/drawdown qualification pass. Only individually
   Core-eligible wallets may enter formation; other research evidence remains visible for audit.
2. On the currently active parameter surface, search wallet count and membership from the cached target fills.
   The fast replay still models shared cash, margin, deployment, coin caps, fees, open capture and 1.5x cost
   stress, but does not rescan the candle path inside every prefix/add/swap candidate.
3. The explicit `optimize` operation runs only after a qualified pool exists. Its parameter grid and independent
   folds are likewise fill-driven. It may propose higher-return sizing/Add parameters, but it cannot publish them
   by itself.
4. After parameters and actual publishable membership are fixed, run exactly one final path-complete 30-day
   portfolio Copy replay. Require positive net, at most 15% drawdown, at least 70% actionable opens, 75% capacity
   fit and complete price-path coverage. A failed final replay rolls back the proposal/publication; it must not
   restart a path-heavy parameter search.
5. The explicit `optimize` command treats incumbents as new entries so a policy correction cannot preserve an
   incorrectly published Core through normal soft-retention grace; scheduled scans keep that grace. The first
   funded generation is the sole exception to two-generation promotion confirmation because no earlier complete
   generation can exist after a clean factory reset.
6. Apply one individual outlier gate only: remove the largest winning independent Campaign and require remaining net
   positive. Top-two/body/top-wallet removals are diagnostic. Persist wallet/coin/day/side concentration.
7. On a scheduled rebalance, repeatedly apply fill-driven leave-one-out elimination only when removing a member
   raises funded net PnL by at least `$1` and the smaller set passed the same membership robustness checks.
   Between rebalances, publish only hard-failure removals; ordinary additions wait for the next rebalance.

An operator may star a current Core wallet through the Dashboard. The durable `target_controls.pinned` flag
locks ordering and retention only while the wallet still passes the current Core business gates: an enabled,
qualified star is required in membership search and LOO, occupies the user Core maximum, and is ordered before
automatic members by `pinned_at`. A star cannot bypass strict-Copy win/sample, recent-body, liquidation,
economics or structure gates; a failing held wallet becomes exit-only. A true replay/cache/market-snapshot or
strategy-integrity failure still fails the generation closed and retains the prior complete strategy; it must not
silently clear the star or publish corrupt execution context. Disabling a starred wallet removes it from the
immutable execution target set until re-enabled. Removing the star returns it to normal
automatic selection on the next generation.

A wallet needs a true actionable flat-to-open signal within 72 hours for Core new-open permission. Missing or
stale activity never deletes an otherwise profitable Profile: it remains Challenger and can promote after a new
signal and confirmation. Existing copied positions remain managed exit-only.

A pure addition to a still-qualified Core is not an incumbent replacement: it needs positive funded marginal
net plus the fold/latest/stress safeguards, but not the 5% utility and 2%-of-equity anti-churn hurdle. Those larger
hurdles apply only when a candidate set removes or replaces a still-qualified old Core member.

Shared replay evaluates real balance contention, open capture, capacity, deployment, drawdown, fees/slippage and
per-coin limits. A high-scoring wallet can remain Challenger when it adds no funded-account value; a lower raw
rank may enter when the final shared combination is better. Core order is conditional leave-one-out portfolio
contribution, while Challenger order is current follow score. Core has no minimum count and a maximum of sixteen.
Promotion requires two complete generations at least 24 hours apart, ordinary changes are weekly, and
14-day soft tenure plus two-generation soft-failure confirmation prevents daily churn; hard failures are immediate.

`FOLLOW_SELECTION_MODE=auto` lets the scanner publish this selection. `manual` carries the current selection
rows into the next generation and leaves membership operator-owned; it does not silently rewrite the Core.

### 6. Atomic publication and tuning

The scanner prefetches only the bounded candidate market path outside the final SQLite publication transaction.
Normal cold/scheduled formation does not run a parameter grid. It strictly replays individual and shared-account
membership on the currently active execution surface, then seals eligibility, explicit selection, generation,
follow history and its immutable strategy revision as one atomic decision. Parameter search is the explicit
`optimize` operation; if it succeeds, its newly tuned surface and requalified membership are likewise sealed
atomically. A slow or failed optimizer must never delay an otherwise complete discovery generation or leave the
system with zero Core merely because no parameter proposal finished.

Repeated strict replays must reuse one normalized price path, filter it to the candidate's actual markets/time
range, and retain only compact portfolio summaries between membership candidates. Do not cache full position and
equity-curve results for every explored set: the production host is intentionally small and must fail boundedly
rather than reach the OOM killer.

The compact portfolio tuner searches all three volatility-tier upper margins and leverage caps,
`DEPLOY_FULL_PCT`, and smart-add `ADD_GAP_K`, `POS_ADD_GAP_K`, `ADD_GAP_SHRINK_G`, and `ADD_MAX_HARD`. It does
not tune the three lower margins, per-coin caps, `MAX_DEPLOY_PCT`, `MARGIN_EQUITY_PCT`, Core maximum, tail-close,
or stop/risk-owner settings. Parameter finalists use three non-overlapping fill-driven ten-day folds solely to
reject overfit sizing proposals, plus 1.5x-cost stress and open/capacity checks; these tuner folds do not decide
  wallet admission. The selected wallet set must separately pass the official four-week 5% target screen,
  strict-Copy 30d/rolling-7d magnitude lines, and the four-fold follower stability contract. Per-close
  economic density remains a ranking diagnostic.
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
