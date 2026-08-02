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
├── execution/  forward-only Observer, Paper/Live broker, order recovery, and risk assessment
├── ops/        process control, Paper reset, storage guard
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
    ↓ 7d notional volume ≥ $250k; 7d/30d PnL non-negative (ROI magnitude is ignored)
Official Portfolio Perp prefilter
    ↓ independently confirms Perp-only 7d notional volume ≥ $250k
Deep fills structure + source quality
    ↓ executable markets; no bot/grid/hedge/blow-up/data hard failure
Fills-only rough Copy
    ↓ 3/4 active weeks; max gap 10d; closed samples ≥7; PF ≥1.25; lottery protection; open follow ≥70%
Profit-aligned score (70/30 return with bounded confidence haircut)
    ↓ frozen Top32 → current-surface strict path → score Top16 tune seed
Final strict individual/shared Copy
    ↓ tuned-surface Top32 certification → final Top16/prefix → exact-Core retune/closure
    ↓ 30d/7d 10%/3%; PF ≥1.25; open-loss ratio ≤50%; no minimum quota
跟单中 (Core) · 候选 (Challenger) · exit-only for held positions
    ↓ forward-only Observer
Paper copy positions, PnL, and execution audit
```

The Dashboard and API expose the same published generation. `watchlist` is a derived ranked view; it is not the
final new-entry membership once an explicit selection generation exists.

## Live execution status

The repository contains the complete Paper/Live execution path, including the official SDK `0.24.0`, separate
Testnet/Mainnet venues, per-DEX metadata, precision-safe IOC orders, durable intents, deterministic CLOIDs,
fill-based accounting, restart reconciliation, encrypted Dashboard credentials, Mainnet preflight,
Draining and emergency close controls. Mainnet signing is available only through an explicitly activated Live
session; ordinary broker/client construction remains unable to sign Mainnet orders.

Paper and Mainnet Live use the same target signals, sizing, add/reduce/close logic and copy-ledger transitions.
Live replaces the Paper simulated fill with a signed Mainnet IOC and updates the Live ledger only from actual
fills. `MARGIN_EQUITY_PCT` scales each order's equity sizing base; it is not a reserved pool or total deployment
cap. Discovery data, the published Core and its immutable strategy revision are shared across modes; switching
Paper/Live never triggers a rescan or rebuilds Core. Every valid target opening signal is considered regardless
of the target's notional; our order scales with actual account equity down to Hyperliquid's 10 USD venue floor.
Every Live startup reconciles current exchange equity/available collateral, and each exposure increase refreshes
them again before sizing. The session-start equity is only the same drawdown-smoothing anchor used by Paper; a
prior Live session's ledger start or the standardized Paper balance can never become the new session's anchor.
Mainnet rollout still requires external VPS deployment, a separately authorized Mainnet Agent, funded
Unified account and a passing read-only preflight. A successful startup enters full Live execution immediately;
equity sizing and the normal deployment/per-coin/add/liquidity limits remain the risk boundaries.

The Dashboard exposes only the product modes: compact Paper and Mainnet Live. Testnet remains a developer CLI
verifier and is not shown as an operator account. Mainnet credential entry accepts the master address, Agent
address and Agent private key; the browser encrypts the key before transport, and verification reads the
Agent's authoritative `validUntil` from Hyperliquid `extraAgents` instead of accepting an operator-entered
expiry. The visible credential action verifies the private-key address, owner, Unified mode and authorization,
but never starts or stops Observer. The global top-right copy-trade control is the single runtime entry. In Live
mode it runs the full funding/strategy/Core/market/REST/WS/position/order preflight automatically, consumes its
short-lived grant and starts Observer; there is no Account-page start button or separate manual preflight panel.
Paper/Live mode may change only while Observer is stopped.

Testnet is deliberately not a product execution mode and does not maintain a Testnet copy ledger. It is only a
bounded functional verifier for Hyperliquid signing, leverage, order, cancel, query and WebSocket APIs. Testnet
books are not used to validate Mainnet strategy behavior, liquidity or expected slippage.

Public Testnet metadata can be checked without a wallet or signature after installing `hyper/requirements.txt`:

```bash
python3 -m hyper.cli.execution_verify public-metadata --network testnet \
  --require-coin xyz:XYZ100
```

The local signed verifier requires a Testnet-only Agent key in a regular, non-symlink `0600` file. It verifies
that the derived Agent address and authorized master account match before trading; secret values are never
printed. Preflight requires the authoritative Info API account-abstraction value to be exactly
`unifiedAccount` and derives available collateral from Unified spot USDC (`total - hold`). A UI label alone is
not accepted. Use placeholders below rather than putting a private key on the command line:

```bash
python3 -m hyper.cli.execution_verify testnet-preflight \
  --account-address <rabby-address> --agent-address <testnet-agent-address> \
  --private-key-file <protected-testnet-key-file>

python3 -m hyper.cli.execution_verify testnet-roundtrip \
  --account-address <rabby-address> --agent-address <testnet-agent-address> \
  --private-key-file <protected-testnet-key-file>

python3 -m hyper.cli.execution_verify testnet-scenarios \
  --account-address <rabby-address> --agent-address <testnet-agent-address> \
  --private-key-file <protected-testnet-key-file>

python3 -m hyper.cli.execution_verify testnet-websocket \
  --account-address <rabby-address> --agent-address <testnet-agent-address> \
  --private-key-file <protected-testnet-key-file>

python3 -m hyper.cli.execution_verify testnet-idempotency \
  --account-address <rabby-address> --agent-address <testnet-agent-address> \
  --private-key-file <protected-testnet-key-file>

python3 -m hyper.cli.execution_verify testnet-reconcile \
  --account-address <rabby-address> --agent-address <testnet-agent-address> \
  --private-key-file <protected-testnet-key-file>

python3 -m hyper.cli.execution_verify testnet-signal-bridge \
  --account-address <rabby-address> --agent-address <testnet-agent-address> \
  --private-key-file <protected-testnet-key-file>

python3 -m hyper.cli.execution_verify testnet-all \
  --account-address <rabby-address> --agent-address <testnet-agent-address> \
  --private-key-file <protected-testnet-key-file>
```

The aggregate suite starts only from a clean Testnet account and finishes with another clean-account preflight.
It covers long/add/reduce/close, short, close-before-flip, BTC/ETH, standard and `xyz:*` REST reads, IOC reject
normalization, GTC order/status/cancel paths, user fills, actual WebSocket market/user streams, process-local
duplicate suppression, reconstructed-client recovery, and a real public Mainnet target fill mapped to a bounded
Testnet roundtrip. Hyperliquid accepts more than one order with the same CLOID, so CLOID is a reconciliation key,
not an exchange-side idempotency guarantee. The verifier does not create a Testnet strategy ledger.

Current Testnet `xyz:XYZ100` quotes can be outside the exchange's oracle protection. The suite therefore proves
HIP-3 routing and signing with an accepted resting GTC plus status/cancel, and separately verifies the real
aggressive IOC `oracle_reject`; it does not claim a HIP-3 taker fill under an untradeable Testnet book. See the
private local rollout and external-gate checklist in `hyper/docs/live-trading-rollout-plan.md`.

## Selection model

Wallet quality and funded-account membership are separate decisions.

- Cheap recall uses only useful activity and PnL direction: Leaderboard 7-day leveraged notional must be at
  least `$250,000`, and 7-day/30-day PnL may not be negative. Official Portfolio independently confirms
  Perp-only 7-day notional of at least `$250,000`. Account balance, official ROI magnitude, positive-equity
  history length, and Perp-profit share are not admission gates.
- Deep fills first reject structural or catastrophic risk: second-scale HFT, OID-level robot density,
  systematic grid/heavy DCA, spot hedge, opaque markets, extreme concurrency, confirmed source-account
  zeroing, a major Copy liquidation, or incomplete data/valuation/market scope. Fill fragments sharing one
  OID cannot manufacture entries, adds, activity, or samples.
- Profitability is proved only by complete closed Episodes. Positive unrealized PnL is displayed as a reference
  and has zero weight. Negative unrealized PnL is charged in full:
  `qualificationPnl = closedPnl - abs(min(unrealizedPnl, 0))`. Closed PnL must be positive in both 30-day and
  7-day windows, conservative PnL must remain positive, and 30-day open loss may not exceed 50% of closed
  profit. This definition is shared by source profiles, individual Copy, shared replay and tuning.
- Activity is frozen once per generation from OID-deduplicated flat-to-open/flip
  opportunities: the latest seven days must contain one opportunity, at least three of four rolling seven-day
  buckets must be active, and the maximum 28-day opening gap may not exceed ten days. The 72-hour value remains
  display/ranking context only; it is not an admission veto.
- Rough Copy uses one continuously compounded `$10,000` comparison account and requires positive closed and
  conservative 30d/7d PnL, a 30-day open-loss ratio at or below 50%, at least seven complete closed source and
  Copy Episodes over 30 days, Copy Profit Factor at least 1.25, at least 70% open follow, complete valuation,
  and conditional lottery protection. Fixed 60%/70%/85% win-rate floors are retired: a sub-50% win wallet may
  pass when its PF is sound, Top3 profit is not dominant, and the post-Top3 body remains profitable.
  Historical replay assumes liquidity was executable. Every source flat-to-open lifecycle is eligible without
  a source-notional threshold; our open is sized independently by our equity, margin, leverage and capacity
  rules. Further fills from the opening OID extend the opening anchor and are never treated as
  smart adds. A later add OID may wait for cumulative slices to become actionable, but its first Copy execution
  seals the OID and later slices cannot submit another add. Structural execution density counts distinct OIDs,
  not exchange fill fragments. Live Observer liquidity skips remain separate audit evidence.
  Return magnitude owns the score and does not pre-empt the later unified parameter tune.
- Qualified wallets form both Top32 and Core through one profit-aligned score. Conservative
  `70% × 30d + 30% × 7d` Copy return is mapped monotonically, then multiplied by an 85%–100% confidence factor
  derived from PF, samples, execution, repeatability, cross-week activity and liquidation safety. Confidence can
  only haircut profitability and cannot manufacture a high score for a weak-return wallet. Raw profit priority,
  30d, 7d, PF and address are stable tie-breaks. Rough 20%/5% wallets remain labelled `primary`; other qualified
  wallets are `reserve`, but those tiers no longer create a conflicting sort order. Current-surface strict path
  replay reranks candidates by the same score and its Top16 seeds unified tuning. The winning surface then
  certifies the complete path-valid Top32 before the final Top16 is formed.
- Final individual strict replay requires conservative dynamic 30d/7d returns of 10%/3%, positive closed PnL,
  a 30-day open-loss ratio at or below 50%, at least seven complete 30-day Copy Episodes, Copy PF at least
  1.25, conditional lottery protection, 70% open follow, the frozen cross-week activity proof,
  complete data/path evidence and at most three proxy liquidations. A separate severity gate rejects a wallet
  from the rough candidate pool as soon as any liquidated Copy episode loses at least 8% of the dynamic account
  equity recorded when that episode opened, even if its count is only one. Final shared replay requires
  conservative 10%/3%, a 30-day open-loss ratio at or below 50%, and 70% open follow on both standardized and
  actual Paper capital. Campaign, weekly-fold, per-close,
  cost-multiple, maximum-drawdown and 75-point gates do not exist.
- Confirmed source-account zeroing liquidations and those >=8% Copy liquidation events are persisted in
  `wallet_risk_event`. Discovery cache pruning and 30/37-day window expiry cannot make the wallet eligible again.
- Wallet count and parameters are tuned together over profit-aligned score prefixes. The initial winning surface
  requalifies every path-valid Top32 wallet. After the shared prefix chooses the proposed Core, that exact
  membership is full-tuned and the Top32 ranking/prefix is certified again. Membership/order and parameters must
  stabilize within two rounds before publication.
- Final moves must pass the dynamic 30d/7d shared-account return and path-completeness contract.
  Complete candidate discovery runs Monday and Thursday; the frozen Challenger cohort is refreshed on the other
  five days. Daily refresh first certifies with the active parameters. Low and medium financial risk remain
  Core with new-open/add authority; a first ordinary failure is low, an independent confirmation at least
  72 hours later is medium, and one complete healthy assessment clears either. Severe 30-day loss is
  immediately medium but still advisory. Actual Copy conservative loss accumulates closed/open realized PnL
  plus negative unrealized PnL against the 30-day opening-equity reference. Two or more closes reaching 8% are
  recoverable high risk: later profits below 8% lower the wallet to medium and a healthy net-positive
  assessment clears it. Single-event catastrophic high remains durable. Only high risk, recoverable
  zero-equity unavailability,
  structural uncopyability, incomplete system context, or a completed losing operator exit blocks execution.
  A full 16-wallet Core is never auto-replaced; an actual empty seat may be filled by the highest strict
  Challenger. There is no promotion delay, star priority or minimum count.
- When tuning changes execution parameters, Observer reload waits for one membership consistency pass on the
  same complete generation. The sealed strategy revision activates new parameters and new Core together. Core
  search and portfolio tuning have no wall-clock cutoff; their finite candidate axes and move limits terminate
  the work without publishing a timed-out partial result.
- `follow_selection` is atomically published with the scan generation. `recommendedCore` stores the pure
  score/replay recommendation, while `effectiveCore` overlays incumbency, risk and operator intent. Observer
  opens only for effective Core with `intent=active`.
- Dashboard conditional exit uses `active → draining/requalify`. The request reads only the currently selected
  Paper or Live ledger, so an inactive-ledger position cannot block immediate requalification. Draining captures
  every current-ledger position ID present at
  the click and reserves its Core seat. Once all captured positions close, aggregate post-fee profit with no
  liquidation/high/system block restores active automatically; otherwise the wallet moves to requalify.
  While a captured position remains open, clicking the same control again may cancel the unresolved drain,
  preserving every existing position while restoring new-open/add authority. High/system-blocked wallets cannot
  use cancellation to bypass their block. Flat requests move to requalify immediately. There is no permanent
  manual disable.
- Core has no minimum wallet quota and a maximum of sixteen. A complete scan may publish any count from zero to sixteen;
  final profit order/evidence and funded shared-account economics decide membership.

## Scheduled complete candidate reevaluation

Profiles are not re-downloaded from zero on every scheduled run.

- New candidates get a full configured profile window.
- Existing candidates use `candidate_fills` cursors and fetch only new fills, merging them into the 37-day
  cache.
- Only a newly discovered wallet or a missing/incomplete coverage marker bootstraps the full 37-day source
  window. Page-capped bootstraps persist a continuation cursor and resume from it on the next run.
- Leaderboard candidates require at least `$250,000` leveraged 7-day notional volume and non-negative 7-day
  and 30-day PnL. The cheap Portfolio precheck independently confirms at least `$250,000` of Perp-only 7-day
  volume. Official ROI magnitude and account size are not used. Source quality and follower profitability are
  confirmed later from fills under a
  standardized `$10,000` starting equity with continuous compounding.
- Every recall survivor plus current Core/strict-Challenger/open-position owner is refreshed in the complete
  generation. The pre-strict replay queue is independently capped at 32; only the score Top16 can tune.
  Generation-scoped reserve evidence stays auditable but is not a user-facing Challenger.
- A valid generation is published atomically. A truncated/invalid leaderboard retains the old generation and
  cannot prune, publish, or tune.

Production schedules all jobs in `Asia/Shanghai`:

- Monday and Thursday 04:00: complete Leaderboard discovery and candidate-universe reevaluation.
- Tuesday, Wednesday, Friday, Saturday and Sunday 04:00: frozen Challenger/Core refresh via
  `python3 -m hyper.cli.discover --db ... challenger-refresh`.

The daily job clones the last complete generation's Leaderboard staging rows; it does not access the Leaderboard
API or discover a new wallet. Its promotion universe is exactly that complete generation's new-model Core plus
strict Challenger rows. Current Core and open-position owners outside that universe may receive safety/exit
evidence but can never use daily work as an alternative first-time promotion path. A legacy or policy-mismatched
complete generation makes the daily job fail closed. Daily uses the same frozen activity, pre-strict, final
individual strict, unified-retune and shared-account admission contract as complete discovery—anything that
cannot enter Core under complete-scan criteria cannot enter through daily refresh.

It refreshes Portfolio evidence, cached-fill deltas, actual Copy 7/30-day conservative PnL, positions, valuation
and required market paths, then reruns the pre-strict Top32 and profit-aligned Top16 path. Active and draining
incumbents form the effective membership floor; requalify wallets remain eligible for strict recovery but no
longer reserve a seat. Actual-Copy risk is persisted before membership formation, independently of whether the
later daily publication succeeds; Observer also refreshes it after settlements and every five minutes.
Low/medium observations never remove, freeze or replace an incumbent. When Core has fewer
than 16 seats, daily may append the highest strict proposal and retune only that changed exact membership.
Hard financial safety is limited to a verified source self-liquidation plus fresh zero-equity/no-position
snapshots, one Canonical/actual Copy liquidation losing at least 8% of its recorded opening equity, or at least
two actual Copy closes whose 30-day cumulative conservative loss reaches 8% of the opening-equity reference.
The cumulative case may recover through later profits; the single-event cases remain durable. Legacy
positions with no opening-equity value cannot prove that threshold. A zero-equity/no-position wallet without
liquidation evidence is recoverable unavailable; structural HFT/DCA/hedge/unexecutable-market failures use a
separate system block. Missing data/path/valuation evidence never advances risk confirmation and prevents
publication. An economic-only shared replay failure retains the current membership and parameters as
`operator_review_degraded`, pauses promotions/tuning, and appears as a Dashboard warning.

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

To rebuild source Episode quality and run a bounded fills-only research preview directly from a cached
published generation:

```bash
python3 -m hyper.cli.core_lab --db /path/to/production.db --max-rough 40
```

`core_lab` opens the database with SQLite `mode=ro` plus `query_only`, emits anonymous wallet labels, and never
migrates or writes the source database. Its `--max-rough` bound is a research budget, not the production
primary/reserve Top32 contract. A legacy generation may be used only for an explicitly marked economic preview;
it is never valid promotion evidence.

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
wallets. Strict replay reuses the bounded Top32 K-line path cache. The current-surface Top16 is only the tune
seed; the final tuned surface certifies the complete path-valid Top32 before forming the final Top16.
There is no Campaign, weekly-fold, per-close, cost-multiple, maximum-drawdown or score-floor admission rule.

The same 15-minute price path records wallet intratrade drawdown, underwater duration, time below
-8%, deep-loss events, recovery and conservative proxy liquidations at our leverage ceiling. Historical maximum
drawdown is diagnostic only. A wallet may enter tuning with sub-8% proxy liquidation evidence, then the final
tuned 30-day strict replay permits at most three proxy liquidations per Core wallet. One liquidated episode
losing 8% or more of its opening dynamic Copy equity is a hard rejection regardless of count. A 5%–8% liquidation
remains fully reflected in profit, PF and ordinary liquidation count but is not a permanent veto. Live account loss
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

`python3 -m hyper.cli.discover --db data/hl.db optimize` first applies the deployed score model to the current
generation's frozen pre-strict evidence and rebuilds Top32, then replays Strict/Core formation at that
generation's sealed as-of time. It does not refetch Leaderboard, Portfolio, or wallet fills. A newly ranked
wallet may require a bounded public K-line path completion. Before strict/grid work begins, only missing markets
are retried up to five times at ten-second intervals; the scan is not stopped while retrying. The
`--reuse-tuned-surface` repair mode replays on the published parameter surface and tunes only the exact changed
Core membership, avoiding a second coarse wallet-count grid after a transient path repair. The command shares
the scanner lock with full and
daily refreshes, but it can run beside Observer; Observer keeps the old immutable strategy revision until the
new selection, tuned parameters, and revision publish atomically.

The search evaluates independent parameter axes, a bounded Pareto finalist set, continuous-capital walk-forward
folds, holdout, and stress scenarios from fills. Production formation does not enumerate arbitrary wallet
subsets, leave-one-out variants, every `1..N` prefix, or the three-tier leverage Cartesian product. It uses a
bounded strict-prefix boundary search; each tested count gets a sparse 30-day probe, and the winning exact
membership gets an efficient tune with at most four path finalists representing profit, liquidation risk,
capacity/open capture, and the active baseline. Each count node is still tuned independently so an 8-wallet
portfolio never inherits parameters fitted to a congested 16-wallet account. Completed strict-prefix summaries
are cached by generation, parameter surface and membership hash, so a retry resumes without keeping full
trajectories in memory. It never changes Core membership using stale profiles and never runs a candle replay for
every parameter or membership proposal. After the winning parameters and membership are fixed, the one final
strict 30-day portfolio certification supplies the estimated shared-account result shown above the “跟单中”
list. Publication also persists the exact final-surface individual 30/14/7 replay fields used for score and
admission, so Dashboard score and wallet economics never fall back to a different parameter surface.

An exact-membership tune memoizes duplicate parameter surfaces across its axis, combination, margin-polish and
finalist stages. Final walk-forward certification replays the active baseline once and all unique finalists in
one CPU-aware batch over the same prepared fills/candle path; it does not rerun the identical baseline for every
candidate. One-core hosts execute the same batch serially, while larger hosts scale to their configured worker
ceiling without changing ordering or results.
Its adaptive leverage, margin and smart-add batches keep one worker session alive and reuse the same immutable
30-day fills/market context, avoiding repeated process startup and context serialization without pruning a
candidate surface.

最终 Strict 评分采用“60分完整资格基线 + 35分严格盈利映射 + 5分综合可信度”。这样已通过源画像、
跨周活跃、PF、执行、路径和清算认证的钱包不会显示成不及格，同时仍按盈利能力拉开顺序；
Pre-strict 粗评分没有60分基线，评分也始终不能替代任何准入门槛。
旧 generation 已封存的 Strict 构成可由 Dashboard 按当前公式即时投影，原始审计分仍保留且不改写；
下一次完整扫描会把新公式作为正式不可变分数发布。

Leverage candidates preserve approximate tier exposure by pairing lower leverage with reciprocally higher
margin (`margin × leverage` stays near the active notional before caps). Profit remains the primary objective;
the exact best-profit surface is always retained. Only inside the configured near-best profit band does the tuner
prefer fewer liquidations, less balance congestion, better open capture, and then stronger measured add fidelity;
a low-risk surface outside that band cannot win merely by under-deploying. A profit-retaining proposal that
strictly reduces liquidation evidence can be accepted as a safety repair even when it does not claim the ordinary
minimum relative gain.

The current Paper defaults allow automatic application after the validation gates:

```text
FOLLOW_SELECTION_MODE=auto
AUTO_TUNE_MODE=apply
```

Paper uses zero-day/zero-forward-count exploration thresholds so the complete loop can be tested from a cold
database. For real-money deployment, use conservative shadow and forward-evidence thresholds and review the
persisted `params` values before enabling any live execution.

Observer liquidation checks use only the exchange `markPx` returned by `metaAndAssetCtxs`. BBO/mid prices remain
execution and display fallbacks and cannot initiate an isolated Paper liquidation; a missing official mark holds
the position and retries. In Dashboard fill details, entry rows show locked margin while reduce/close rows show
the actual available-funds return: released entry-basis margin plus realized PnL net of the exit fee.

## Runtime components

| Area | Entry points |
|---|---|
| Scanner/discovery | `hyper/cli/discover.py`, `hyper/discovery/scanner.py`, `hyper/discovery/metrics.py` |
| Generation/selection | `hyper/discovery/generation.py`, `hyper/selection/state.py`, `hyper/selection/follow_score.py` |
| Replay/tuning | `hyper/copy/copy_backtest.py`, `hyper/copy/copy_engine.py`, `hyper/selection/auto_tune.py` |
| Market data | `hyper/market/rest.py`, `hyper/market/ws.py`, `hyper/market/price_path.py` |
| Observer/paper copy | `hyper/cli/observe.py`, `hyper/execution/observer.py` |
| Paper/Live execution and Testnet API verification | `hyper/execution/live_executor.py`, `hyper/execution/hyperliquid_broker.py`, `hyper/cli/execution_verify.py` |
| Runtime operations | `hyper/ops/procman.py`, `hyper/ops/paper_reset.py`, `hyper/ops/storage_guard.py` |
| Dashboard API | `dashboard/server.py`, `dashboard/api/*` |
| Dashboard frontend | `dashboard/web/app.jsx`, `dashboard/web/components/*`, compiled `dashboard/web/app.js` |
| Launcher/ops | `hyper/launcher/launcher.py`, `hyper/launcher/server.py`, `hyper/launcher/core/*`, `hyper/launcher/web/*` |
| Schema/migrations | `hyper/storage.py` |

Important durable tables include `scan_generation`, `profile`, `candidate_fills`, `episode`, `wallet_registry`,
`watchlist`, `follow_selection`, `pipeline_audit`, `copy_position`, `copy_action`, `auto_tune_runs`, and
`auto_tune_state`. `leaderboard_staging` keeps only the current/protected generation set plus the latest 30
generations. Compact selection/risk/tuner audit remains durable; high-volume per-wallet pipeline detail has a
90-day TTL.

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
python3 -m hyper.cli.discover --db data/hl.db storage-maintenance
python3 -m hyper.cli.discover --db data/hl.db watchlist --top 40

# Stop Scanner/Observer first. Clear Paper history + generations/selections while retaining
# candidate fills, path caches and durable source-risk vetoes for a fresh full generation.
python3 -m hyper.cli.discover --db data/hl.db reset-paper --preserve-discovery-cache --yes

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
- With no executable Observer work, Scanner switches from a fixed request interval to a 95%-budget weighted
  token bucket and backs off automatically on HTTP 429. Once Observer has a Core target or open position, Scanner
  immediately returns to its configured slow interval.
- Profile workers return cache/profile/Episode artifacts; the scanner parent commits them in bounded SQLite
  batches. Strict per-wallet replay and independent tune candidates use CPU-affinity-aware process workers
  (`1 core → serial`, up to four workers), while all database writes remain in the parent process.
- Do not restart `hl-scan.service` to deploy code: it starts a real scan when activated. Restart only the
  affected long-running service, normally `hl-dashboard.service` and/or `hl-observe.service`.
- Every scheduled full or Challenger scan runs `storage-maintenance` as `ExecStopPost`. It shares the scanner
  lock, expires only per-wallet audit detail older than 90 days, preserves the current/full/in-progress and
  latest 30 generation snapshots, and records disk/DB/WAL growth in `storage_guard_run`. Dashboard warns at
  70% disk use, more than 1 GB normalized database growth per 24 hours, or a WAL above 512 MB; 85% disk use is
  critical. The task relies on SQLite freelist reuse and never runs `VACUUM` or forces a WAL checkpoint.
- Before diagnosing a manual “full” scan, verify the command payload has `full=true`, the CLI used `--full`, or
  the completed run records `full=1`; explicit full bypasses the two-hour Portfolio decision cache but still
  uses incremental fills for wallets whose complete 37-day history is already present.
- Never commit `data/`, `secret/`, `hyper/launcher/data/keys/`, `hyper/launcher/data/targets.json`, or live database
  snapshots. Keep private deployment details in local ignored notes.
