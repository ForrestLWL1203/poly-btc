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
- Every complete discovery scan may publish its currently proven score-prefix membership subject to the
  explicit incumbent-retention contract below. There is no promotion confirmation or forced minimum tenure.
  Challenger
  daily refresh is the deliberate exception: it may publish only the same Core set or a strict superset, never
  a removal or replacement. Expensive optimization runs only for a proven daily promotion or when explicitly
  requested; an evidence-only refresh must not silently start a parameter grid. Automatic daily removal has
  two hard-safety exceptions: a recent exchange-labelled self-liquidation followed by a fresh zero-equity/no-
  position clearinghouse snapshot, or a canonical Copy liquidation losing at least 8% of its episode-opening
  dynamic equity. Ordinary losses, sub-8% Paper liquidations and rolling-return declines never satisfy them.
- Dashboard business failures are not data errors. Reserve “数据异常” for collection, cache, replay, valuation-
  pipeline, or immutable-strategy integrity failures; an incomplete open-position mark is the explicit
  “开放仓位估值待确认” observation state, not a generic data-error badge.
- Reuse the Dashboard's shared `.btn` variants and nearby component patterns. Do not introduce one-off inline
  or private button skins when an existing neutral/accent/go/stop/danger variant expresses the action.
- Never expose, print, commit, or copy secrets, private keys, target files, live databases, or private VPS
  values. The active VPS connection is available locally; `secret/vps.txt` is its canonical source. Keep values
  there, re-read it before remote work, and never substitute a remembered or hardcoded target.

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

`Leaderboard staging → $250k Perp-volume/PnL-direction recall → executable-market fill cache → structural hard
gates → generation-frozen activity + fills-only conservative Copy/PF/lottery gates → scored Top32 →
current-surface strict path and profit-aligned-score Top16 tune seed → count-specific adaptive tune → tuned-surface
individual strict across the complete path-valid Top32 → score Top16/prefix shared replay → exact-Core
retune and bounded membership/parameter closure → atomic generation/selection/strategy revision publish →
Observer reload → replay-summary materialization`

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

- New-wallet Leaderboard recall requires leveraged 7d notional volume `$250,000` and non-negative 7d/30d PnL.
  Nominal leveraged volume is activity evidence, never a profitability denominator. Before fill history is
  downloaded, official `perpWeek` must independently confirm at least `$250,000` of Perp-only seven-day volume.
  Account value, official ROI magnitude, positive-equity history duration, Perp PnL share and official
  `perpMonth` profitability are not admission gates. Core, strict Challenger and open-position owners bypass
  cheap recall only so the same generation can refresh or safely remove them; they receive no final-qualification
  privilege from prior identity.
- Deep profiling uses one immutable executable universe for the generation. `hyper/copy/copy_data.py` normalizes symbols
  and removes spot, outcomes and opaque builder fills before cache, metrics and replay; publication audits the
  active cache for scope violations. Network APIs that cannot filter leaderboard rows by product scope are
  tolerated only at the coarse-harvest layer.
- `profit-distribution` is a non-publishing research path. It reads the source database in query-only mode and
  bypasses ROI/PnL, win-rate, sample-depth and score gates during broad collection, while preserving structural
  uncopyability, catastrophic source risk and data/path integrity checks. Before strict replay it separately
  requires recurring OID-deduplicated, source-notional-qualified open/flip opportunities: the latest seven days
  must be active, at least three of four rolling seven-day buckets must be active and the maximum 28-day opening
  gap must not exceed ten days. Sparse wallets remain in the research distribution but cannot consume strict
  replay slots. Every requested source artifact and derived profile is committed to a private 0600 research
  database, with an anonymous report checkpoint after rough collection and history repair; neither may be
  substituted for a scan generation or strategy revision. `--rough-only` must stop before price-path or strict
  work. `--strict-limit 0` is the unbiased strict-distribution mode after activity qualification. A positive
  strict limit is a candidate-hunt mode: it profiles the complete requested recall set, ranks operational
  structural survivors by rough 70/30 conservative return, and strictly replays only that bounded prefix; never
  use its biased quantiles to set policy.
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
- Complete discovery runs Monday and Thursday at 04:00 `Asia/Shanghai`. They refresh the complete Leaderboard,
  discover the candidate universe, repair missing 37-day caches, and evaluate every cheap-recall +
  Perp-volume survivor. Core, strict Challenger and open-position owners are also evaluated for safe
  removal/exit. The fills-only pre-strict queue has an independent maximum of 32; the strict/tune pool has an
  independent maximum of 16.
- On Tuesday, Wednesday, Friday, Saturday and Sunday at 04:00 `Asia/Shanghai`,
  `python3 -m hyper.cli.discover --db ... challenger-refresh` refreshes only the Core + Challenger cohort frozen
  by the latest successful complete discovery generation, plus current Core and open-position owners. It clones
  that generation's Leaderboard snapshot for integrity but never calls the Leaderboard API, discovers wallets,
  bootstraps/repairs 37-day history or prunes discovery caches. Its promotion universe is exactly the latest
  complete new-model generation's Core plus strict Challenger rows. Current Core/open-position owners outside
  that universe may receive safety evidence but cannot first-time promote through daily work. A legacy or
  policy-mismatched complete generation makes daily fail closed. It reruns the same frozen activity, pre-strict,
  final individual strict and shared admission contract as complete discovery; daily is never a weaker route.
  It first uses the active strategy surface (`retune=False`) to replay the bounded Top16 and shared account
  strictly. Existing `active` and `draining` Core wallets form the effective membership floor; `requalify`
  wallets have released their seats but remain in the daily strict-requalification pool. Low and medium
  financial risk never removes an incumbent or revokes entry permission. Empty seats may be filled by the
  highest strict proposal; a full 16-wallet Core never auto-replaces an incumbent. Membership changes alone
  trigger exact-membership retuning. A current-Core data/path failure retains the previous generation and does
  not advance risk confirmation. Automatic removal is limited to durable high risk, recoverable zero-equity
  unavailability, structural uncopyability, or an already-resolved losing operator exit. High risk requires a
  verified source self-liquidation plus zero equity/no positions, or a Canonical/actual Copy liquidation losing
  at least 8% of episode opening equity; legacy positions without opening equity cannot satisfy the actual-Copy
  threshold.
- Complete scans, manual scans and Challenger refreshes share `data/run/scanner.lock`. A busy daily job records
  `skipped_scan_busy` and exits successfully. Workset and fill transport remain separate: complete worksets are
  `all`; daily worksets are `frozen_challenger_pool`; fill transport is delta unless a complete run repairs it.

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
For any qualification window, dynamic return is `qualificationPnl / that window's start equity`; it is not a fixed-capital or
per-trade return. Thus a 30-day replay that grows `$10,000` to `$13,000` contributes 30%. The rolling 7-day
window is cut from the same continuous 30-day equity path and uses its own day-23 boundary equity.

Profit qualification is deliberately one-sided. `closedPnl` is fee-paid PnL from complete closed Episodes,
`openLoss = abs(min(currentUnrealizedPnl, 0))`, and
`qualificationPnl = closedPnl - openLoss`. Positive unrealized PnL is stored only as
`openProfitReference`; it has zero qualification, scoring, ranking, tuning and shared-certification weight.
`closedPnl <= 0` is ineligible, and `openLoss / closedPnl > 50%` is a hard rejection. Dynamic conservative
return divides `qualificationPnl` by that window's start equity. Source, individual Copy, standardized shared
Copy and Paper-capital shared Copy all use the same contract.

Deep fills first enforce structural hard failures: HFT/OID robot density, systematic grid/heavy DCA, spot hedge,
opaque markets, extreme concurrency, confirmed source zeroing/major Copy liquidation, or incomplete fills,
valuation and market scope. Source and fills-only Copy must each have at least seven complete 30-day closed
Episodes and positive closed 30d/7d PnL. Negative unrealized PnL is fully charged and may not exceed 50% of
30-day closed profit; positive unrealized PnL has zero qualification weight.

Canonical activity is calculated once at generation start from OID-deduplicated, source-notional-qualified
flat-to-open/flip opportunities. The latest seven days must be active, at least three of four rolling seven-day
buckets must be active, and the maximum 28-day opening gap must not exceed ten days. 72-hour activity and fixed
7d/14d trade-count gates are retired from permission.

Every structural survivor receives fills-only rough Copy. Rough admission also requires positive conservative
30d/7d returns, Copy Profit Factor at least 1.25, at least 70% open follow and complete valuation. Fixed source
70%/85% and Copy 60% win-rate gates are retired. Conditional lottery protection rejects a sub-50% win wallet
when its post-Top3 body loses, or a wallet whose Top3 contributes at least 70% of gross profit while the body
loses or wins below 50%. A low-win, high-PF, non-concentrated wallet with a profitable body may pass.

Rough 30d/7d returns of at least 20%/5% form `primary`; otherwise-qualified wallets form `reserve`. These tiers
remain audit labels. Top32 and final formation share one profit-aligned score: conservative
`70%×30d + 30%×7d` return is mapped monotonically, then multiplied by an 85%–100% confidence factor from PF,
samples, execution, repeatability, cross-week activity and liquidation safety. Confidence can only haircut
profitability; it cannot award bonus points to a low-return wallet. Raw profit priority, 30d, 7d, PF and stable
address are tie-breaks. At most 32 receive a queue rank and strict path; current-surface strict reranks them by
the same score and sends at most 16 to unified tuning.

Final per-wallet strict admission requires conservative dynamic 30d/rolling-7d returns of 10%/3%, positive
closed PnL in both windows, a 30-day open-loss ratio at or below 50%, at least seven 30-day closed Copy
Episodes, Copy PF at least 1.25, conditional lottery protection, at least 70% open follow rate, the frozen
cross-week activity evidence, complete data/valuation/sector/path evidence and no more than three simulated
isolated liquidations. Count tolerance applies only to small isolated sizing events: any single liquidated Copy
episode whose net loss reaches 8% of the dynamic account equity recorded when that episode opened is a hard
rejection in both rough and strict qualification. Path/data gaps in a Top32 wallet may remain deferred
Challenger evidence and cannot enter Core. Campaigns, weekly Copy folds, per-close returns, cost
multiples/stress, LCB/probability, maximum drawdown and score floors are not Core gates. Payoff remains audit
telemetry; PF and conditional concentration protection are gates.

Formation ranks qualified wallets by the final profit-aligned score and considers only score prefixes up
to 16. No star,
prior Core role, tenure, minimum count, forced fill or lower-ranked substitution may alter that order. Each
prefix is tuned with one continuous floating-equity account. The optimizer first maximizes 30-day net profit,
then within the near-best profit band minimizes liquidations, reduces real congestion/missed opens, and jointly
adjusts tier margin, leverage and smart-add structure. Only the fixed winner receives final path-complete strict
replay. Publication requires the shared account to return at least 10% over 30 days and 3% over the latest
rolling 7 days, with both standardized `$10,000` and actual Paper starting-equity results persisted.
There is no second 85% total-margin slice: adds may use the remaining real available cash after fresh opens
stop, while per-coin caps and isolated-margin liquidation still bound exposure.
Historical replay assumes sufficient market liquidity; its effective open denominator excludes source opening
lifecycles whose cumulative flat-to-open position never reaches our tier minimum notional and counts every other
strategy/capacity rejection as a miss. A sub-floor source open remains one pending lifecycle while that position
grows. Once the floor is crossed, our open is sized independently by our margin, leverage and capacity surface;
the source notional does not cap it. Further fills from the OID that confirmed the opening extend the source
opening anchor and never become smart adds. Later source adds continue to use the existing smart-add rules.
Observer retains the live liquidity filter and persists those live-only skips separately.

Qualification never credits positive marked PnL. It counts complete closed-Episode PnL and charges negative
marked PnL in full from one canonical valuation snapshot. Recent repeatability is judged by closed source/Copy
Episode win rates plus the conservative dynamic rolling-7-day return from the same continuous 30-day equity
path; there are no weekly-fold or per-close-density admission rules. Individual
failures remain explicit Challenger evidence. Four or more proxy liquidations on the final tuned 30-day surface
remain Challenger evidence; the active pre-tune surface cannot reject a wallet that parameter optimization may
repair.

Official Portfolio return treats zero-equity transfer gaps as funding boundaries rather than trading resets.
Each positive-equity operating segment uses its own starting capital and the segment returns are compounded.
A full withdrawal and later redeposit therefore preserve prior trading evidence without creating a zero
denominator. A genuine PnL loss to zero remains a -100% segment, so redeposit cannot wash out liquidation loss.

Structural gates are sector-local. HFT, habitual grid/DCA, spot hedge, extreme concurrency (default maximum 15),
and uncopyable structures remain hard failures. Heavy-DCA uses a default threshold of more than 30 adds and only
counts complete round trips; a cache-window episode that starts already open cannot hard-reject a wallet.
Any complete Heavy-DCA violation rejects that sector and cannot be resurrected by a profitable Copy replay.
Execution density is based on distinct source OIDs per Episode, never exchange fill fragments. One followed add
OID may create at most one Copy add; later slices update source exposure without sending another order.

There is no zero-liquidation rule and no historical maximum-drawdown admission threshold. Source fills do not
disclose their true margin/leverage, so both values are conservative reconstructions at our leverage ceiling.
The active surface may enter tuning with any proxy count; the final tuned 30-day strict replay permits at most
three isolated proxy liquidations per Core wallet. Four or more remain Challenger-only. Independently of count,
a single liquidation loss of 8% or more of that episode's opening Copy equity is rejected before the bounded
candidate pool and can use the Challenger-daily hard-safety removal path. Confirmed source-account zeroing
liquidations and these major Copy liquidation events are persisted in `wallet_risk_event`; discovery cache
pruning and rolling-window expiry cannot make those wallets eligible again. Liquidation losses still reduce net PnL
and receive a bounded score penalty; path drawdown remains visible for diagnostics only. A 5%–8% proxy
liquidation is a non-catastrophic large-loss audit event: it remains fully included in PnL, PF and the ordinary
liquidation count, but is not a permanent veto. An allowed sub-8% proxy
liquidation must not trigger a hidden 24-hour/seven-day wallet freeze that then lowers open-follow rate and rejects
the wallet a second time. Live Paper execution likewise records the actual isolated loss without inventing a
source-wallet-wide re-entry ban.

`source_quality_score` is audit/tie-break evidence; official Portfolio return contributes zero score and no
Top40 source cap exists. `rough_copy_score` is likewise a quality tie-break rather than the primary queue
order. The composite score has no 70/75 permission line. After qualification, Core formation orders by
`0.70 × strict conservative 30d dynamic return + 0.30 × strict conservative 7d dynamic return`; 30d return,
7d return, composite score and
address are the deterministic tie-breaks. Final strict metrics recompute both score and profit priority for the
surviving Top16 against the material 10%/3% return floors.

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
- **Challenger** (`role=challenger`): a Top32 wallet that completed and passed individual strict but was not in
  the best shared Core prefix, or whose strict path/data evidence is temporarily deferred. Pre-strict reserve
  and proven economic/structure failures are not Challenger.
- **Exit-only** (`role=exit_only`): no new opens, but existing copies are managed to exit.
- **Rejected**: business value/structure is below the observation line and is not shown as Challenger.
- **Quarantine**: collection/cache/replay/valuation/strategy data is invalid and is not a new-entry target.

Membership is separate from live operator intent. `target_controls.intent` is `active`, `draining`, or
`requalify`. A draining wallet keeps its Core seat but cannot open or add; a requalify wallet is projected as
Challenger and has released the seat. Risk is separately projected from immutable
`wallet_risk_assessment` rows into `wallet_registry`: low/medium are advisory; single-event catastrophic high
is durable; rolling cumulative-loss high is recoverable; unavailable recovers after funding and strict
requalification; and structural/data blocks are not financial risk.

`CORE_INITIAL_MAX_N` and `CORE_TARGET_MAX_N` default to 16. There is no minimum Core count, service quota or
forced replacement count: zero to sixteen wallets may publish. Complete discovery formation is:

1. Freeze cross-week activity and fills-only economics for every structural survivor. Fill Top32 by the unified
   rough profit-aligned score; primary/reserve remains visible evidence, not a conflicting sort order.
2. Build current-surface individual evidence from cached fills and one refined 15-minute path per Top32
   candidate, rerank by the strict profit-aligned score, and use at most 16 as the initial tuning seed. Stars remain
   operator attention metadata only.
3. Search only strict score prefixes. Each count node uses the same normalized cached fills with continuous
   floating equity. A combination may shorten the lowest-profit suffix, but may never skip a higher-profit
   wallet to insert a lower-profit wallet.
4. Recompute every path-valid Top32 wallet's complete strict qualification on the tuned surface, rerank the
   qualified universe, and form a fresh score Top16. A wallet outside the initial seed may therefore
   enter when the tuned surface proves it stronger.
5. Search the shared strict score prefix, then full-tune the exact proposed Core. Replay the complete path-valid
   Top32 and shared prefix again on that surface. Membership/order and parameters must converge within two exact-
   Core rounds; a non-convergent generation fails closed and does not publish.
6. Run the final path-complete 30-day shared-account replay only after parameters and membership have converged.
   Require conservative dynamic 30-day return at least 10%, conservative rolling-7-day return at least 3%,
   a 30-day open-loss ratio at or below 50%, at least 70% open follow, positive closed and conservative PnL in
   both windows and complete price-path coverage. Every member must remain at or below three
   simulated liquidations and below 8% loss on every individual liquidated episode.
7. Persist both `recommendedCore` (pure score/replay proposal) and `effectiveCore` (incumbent/risk/intent overlay),
   plus standardized `$10,000` and current-Paper-equity results. Incomplete paths, valuation or immutable
   context abort publication. A data-complete economic-only failure publishes `operator_review_degraded` with
   the existing effective membership and parameters; it does not promote or retune.

An operator may star a wallet through the Dashboard for attention and manual review. The durable
`target_controls.pinned` flag is never a selection permission. A conditional exit with no positions moves
immediately to `requalify`. With positions it captures the complete current position-ID cohort and enters
`draining`: new opens/adds stop while reductions, closes and risk management continue. Once every captured
position is terminal, positive aggregate post-fee PnL with no liquidation/high/system block restores `active`;
otherwise it resolves to `requalify`. There is no permanent manual-disable state.

A wallet needs the generation-frozen activity proof defined above. A 72-hour signal is shown as freshness
context but has no permission effect. Existing copied positions whose source loses Core authority remain
managed exit-only.

Shared replay evaluates real balance contention, open capture, capacity, deployment, drawdown, fees/slippage and
per-coin limits. Complete and daily assessments use the same risk state machine. The first ordinary
7d/return/PF/activity/sample/open-rate or low-sample actual-Copy failure is low risk. A second independent
successful assessment at least 72 hours later is medium risk; non-positive 30-day closed/conservative PnL,
open loss above 50% of 30-day closed profit, or actual 30-day conservative loss with at least three closed
positions is immediately medium. Low/medium remain Core with entry permission, and one complete healthy
assessment clears them. Data/path/valuation failures do not advance confirmation. Structural failures block
execution without masquerading as high financial risk. Zero equity/no positions without liquidation proof is
recoverable unavailable. Verified source zeroing and Canonical/actual Copy liquidation loss at or above 8% of
opening equity are durable high-risk events and immediately remove new-open authority.
Actual Copy also accumulates closed realized PnL plus open realized PnL and all negative unrealized PnL over
30 days against the earliest recorded opening account equity in that window. At least two closed positions and
an 8% cumulative conservative loss is recoverable high risk: later profit below 8% lowers it to medium, and a
healthy net-positive assessment clears it. Observer refreshes this projection after settlements and every
five-minute account snapshot; daily refresh persists it before selection work, so a later publication failure
cannot erase the day's risk label. Ordinary losses and 5%–8% isolated liquidations remain advisory evidence.
Observer persists `pending/confirmed/cleared`
execution freezes separately from permanent risk evidence: a target self-liquidation fill blocks new opens/adds
until standard and affected-DEX clearinghouse snapshots prove either recovery or zero equity with no positions.

`FOLLOW_SELECTION_MODE=auto` lets the scanner publish this selection. `manual` carries the current selection
rows into the next generation and leaves membership operator-owned; it does not silently rewrite the Core.

### 6. Atomic publication and tuning

The scanner prefetches only the bounded candidate market path outside the final SQLite publication transaction.
Complete formation uses the current-surface Top16 only as its bounded initial tune seed. It then strictly
replays the complete path-valid Top32 on the winning execution surface, reranks the final Top16, chooses a shared
profit prefix, and full-tunes that exact proposed Core. This membership/parameter closure is bounded to two
rounds and must stabilize before eligibility, explicit selection, generation, follow history and the immutable
strategy revision are sealed atomically. A failed or non-convergent optimizer aborts a promotion or complete-
scan publication; it may not publish a partial promoted list.
An evidence-only Challenger generation may explicitly carry the prior Core snapshot, but cannot claim a new
portfolio certification or apply tuned parameters.

Repeated strict replays must reuse one normalized price path, filter it to the candidate's actual markets/time
range, and retain only compact portfolio summaries between membership candidates. Do not cache full position and
equity-curve results for every explored set: the production host is intentionally small and must fail boundedly
rather than reach the OOM killer.

The compact portfolio tuner searches all three volatility-tier margins and leverage caps,
and smart-add `ADD_GAP_K`, `POS_ADD_GAP_K`, `ADD_GAP_SHRINK_G`, and `ADD_MAX_HARD`. It does
not tune per-coin caps, `MAX_DEPLOY_PCT`, `MARGIN_EQUITY_PCT`, Core maximum, tail-close,
or stop/risk-owner settings. Production Core formation uses the efficient profile: it tests every tier
independently, keeps current/highest-profit/fewest-liquidation directions, but does not expand them into a
three-tier Cartesian product. Its 30-day search retains Pareto representatives for highest profit, fewest
liquidations, best capacity/open capture and the active baseline; only four distinct surfaces enter expensive
path/walk-forward validation. Risk and capacity representatives must remain inside the configured near-best
profit band, so an under-deployed surface cannot win merely by avoiding trades. The legacy full profile remains
available for offline diagnostics; coarse count probes remain sparse. Any optimizer walk-forward folds compare
parameter robustness only; they do not
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

Core membership remains a strict profit-ranked prefix: production formation never searches arbitrary wallet
subsets, never runs leave-one-out membership elimination, and never exhaustively replays every `1..N` prefix.
It uses bounded boundary search plus neighbouring counts, then exact-membership efficient retune and at most two
closure rounds when the final surface changes ranking. Generation-scoped prefix evidence stores only compact
metrics keyed by the membership hash and parameter surface, so a retry resumes completed strict prefix work
without retaining full trajectories or raw membership addresses.

Within one exact-membership tune, identical parameter surfaces are replayed once even if several search stages
rediscover them. Final path validation prepares one immutable fills/candle context, replays the active baseline
once, and evaluates all distinct Pareto finalists as one CPU-bounded batch. Baseline and finalist paths still use
the complete continuous account, liquidation, capacity and fold contract; batching changes scheduling and reuse,
not the objective or evidence.
The leverage, margin, and smart-add batches in that tune also reuse one worker session and immutable 30-day
context. Do not recreate a spawn pool for each dependent axis; a one-core host must remain serial-safe.

Final-Strict display/ranking score is qualification-anchored: 60% records completion of the full strict
contract, 35% is the bounded profit-priority mapping, and 5% is PF/sample/execution/repeatability/activity/
liquidation reliability. Rough/pre-strict scores remain unanchored so uncertified wallets never inherit the
strict baseline. Score orders already-qualified wallets and never grants admission.
Dashboard may project a legacy immutable Strict score detail through the current formula for display, but must
retain and expose the generation's original score as audit evidence; never rewrite an old selection row.

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
Before strict replay or any parameter grid starts, bounded selection-path prefetch retries only still-missing
markets five times at ten-second intervals, bypassing the longer background cache backoff. The scan continues
through those retries; only markets still missing after all five attempts are classified incomplete.

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
  high at or above 9%; unresolved/young valid markets temporarily use 7% (mid). Stock/index/commodity markets use
  the same tier leverage cap, plus the venue maximum and source-wallet leverage cap.
- `MARGIN_EQUITY_PCT` is a manual-only sizing base (default 100%, UI range 10–100%). It scales each new
  position's drawdown-adjusted equity base without freezing the remainder; real cash, per-coin caps and total
  deployment still use full risk equity. Auto-tune and Core-count selection must not modify this value.
- A new open uses the tuned tier margin until `MAX_DEPLOY_PCT` (default 90%); new opens stop at that cap, while follow-on
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
`full=true` or the CLI uses `--full`. `regate` re-applies current gates and rebuilds sector policy from cached evidence; `optimize` first
re-scores/re-ranks the current generation's frozen pre-strict evidence with the deployed model, then re-forms
and jointly tunes that generation at its sealed as-of time without Leaderboard, Portfolio, or wallet-fill
refetch. A missing bounded public K-line path may still be completed before strict replay. Optimize,
selection-repair, and finalize commands share the full/daily scanner process lock but may run with Observer.
`optimize --reuse-tuned-surface` is the narrow post-scan repair path: it reuses the current tuned surface,
replays the repaired bounded pool, and runs full tuning only for the exact changed membership instead of
repeating the coarse count grid.
`finalize-profiled` retries an
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
- Before remote work, read the current connection from `secret/vps.txt`; never reuse a host, user, password,
  key, or SSH target from memory. The local agent should attempt the canonical configuration before declaring
  the VPS inaccessible.
- For complex remote SQL/Python, send a local script through the current connection without embedding or
  printing any secret value.
- Never use a destructive Git reset on a user worktree without explicit approval. Preserve unrelated changes.
- If a command fails before reaching the VPS, re-read `secret/vps.txt`, retry with that exact current
  configuration, and report the concrete failure before drawing conclusions from remote state.

## Data and audit retention

Raw fill cache is bounded to the configured profile window plus warm-up. `wallet_registry`, generation history,
selection history, and pipeline audit are durable decision history. Live fill dedup data and account snapshots
are retained with explicit TTLs. Do not prune the old generation manually while a scan or tuner is active.
