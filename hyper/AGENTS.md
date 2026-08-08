# AGENTS.md

## Purpose and authority

This file is the current engineering and production contract for the Hyperliquid product. Read it completely
before changing anything under `hyper/` or any Hyperliquid projection/control under `dashboard/`.

- Verify details against current code and migrations; never revive behavior from an old note or screenshot.
- `hyper/CLAUDE.md` contains private operational access notes only. It must not redefine strategy behavior.
- Historical documents in `hyper/docs/` explain decisions but do not override this contract or current code.
- Avoid hardcoding tuned production values in documentation. Runtime strategy values come from the active
  immutable strategy revision; defaults come from `hyper/config.py`.

Repository ownership:

- `hyper/` owns Hyperliquid discovery, replay, selection, execution, tests, docs, and deployment tooling.
- Business code belongs in `discovery/`, `copy/`, `selection/`, `market/`, `execution/`, or `ops/`.
- `dashboard/` reads product state and writes only through the documented command/parameter control plane.
- `data/` and `secret/` are runtime locations, never source modules or commit material.

## Non-negotiable production invariants

### Secrets and remote access

- Never expose, print, commit, or copy private keys, credentials, target files, live databases, or private VPS
  values. Inspect structure without emitting values.
- Re-read `secret/vps.txt` before every remote operation. Never use a remembered or hardcoded target.
- Use the established `~/.ssh/id_ed25519` with `IdentitiesOnly=yes`. Password authentication is recovery-only.
- Do not create, install, rotate, replace, or remove SSH keys without explicit authorization for that exact key
  operation.

### State ownership and control

- The normal store is `data/hl.db`, using SQLite WAL.
- Scanner owns discovery/generation state. Observer and LiveExecutor own trading state. Dashboard must not
  directly mutate profiles, selections, positions, actions, signals, orders, fills, or sessions.
- Dashboard writes only `commands` and operator-editable `params`; workers validate and apply those requests.
- A deployment must preserve the current Observer state and selected Paper/Live mode. Restart only the affected
  service, and never start/stop/pause/drain trading unless the user requested it.
- `hl-scan.service` starts a real scan. Never include it in a broad restart just to load new code.

### Paper/Live boundary

- Paper and Live share target discovery, Core membership, strategy decisions, and sizing logic, but their books
  are completely isolated:
  - Paper: `copy_position`, `copy_action`;
  - Live: `live_copy_position`, `live_copy_action`, execution sessions/signals/intents/attempts/fills.
- The selected mode alone determines which held positions Observer polls and manages. A Paper position must
  never keep a Live wallet in the polling set, block a Live exit, or affect Live counts, and vice versa.
- Switching Paper/Live is forbidden while Observer is running. Stop Observer first, switch, then restart.
- The shared Core wallet set is not recollected when switching modes.

### Published strategy truth

- New entries come only from the complete, current, published `scan_generation`, its `follow_selection`, and the
  active immutable `strategy_revision` snapshot.
- Do not infer Core from `watchlist`, score thresholds, table order, Paper positions, or stale audit rows.
- A published generation may legitimately contain zero Core. Never fall back to an older score line.
- Every activated strategy revision must descend from the previously active revision. Normal scan, daily refresh,
  calibration, and manual parameter publication all preserve this chain.
- A valid descendant revision is hot-bound into a running Live session. Core or parameter publication must not
  pause entries, restart Observer, rescale existing positions, or require operator recovery.
- Hot binding attaches the immutable revision to the durable Live session before exposing that bundle to
  Observer signal handling. SQLite contention keeps source fills journalled, retries the bind in place, and
  must not strand the owning `reload_params` command in `acked`.
- Only the narrowly identified legacy parentless-publication repair may reconstruct lineage. It may auto-resume
  only after a fresh exchange reconcile and live-ledger projection prove zero unknown positions, unknown orders,
  and ambiguous intents. Real ambiguity, drift, or operator pause remains fail-closed.
- Missing/corrupt revision targets, sector policy, generation linkage, or parameter snapshot is an integrity
  failure; do not silently reconstruct it from mutable current tables.

## Runtime map

| Concern | Primary files |
|---|---|
| Discovery CLI/orchestration | `hyper/cli/discover.py`, `hyper/discovery/scanner.py` |
| Generation staging/publication | `hyper/discovery/generation.py`, `hyper/selection/state.py` |
| Profiles and gates | `hyper/discovery/metrics.py`, `hyper/discovery/scanner_copy_bt.py`, `hyper/selection/follow_score.py` |
| Fill/cache/replay inputs | `hyper/copy/fills.py`, `hyper/copy/copy_data.py`, `hyper/copy/economics.py` |
| Canonical replay/sizing | `hyper/copy/copy_backtest.py`, `hyper/copy/copy_engine.py`, `hyper/copy/sizing.py` |
| Core formation/tuning | `hyper/selection/core_formation.py`, `hyper/selection/auto_tune.py` |
| Immutable revisions | `hyper/selection/strategy_revision.py` |
| Market data | `hyper/market/rest.py`, `hyper/market/ws.py`, `hyper/market/generation_market.py`, `hyper/market/volatility.py` |
| Observer/execution | `hyper/cli/observe.py`, `hyper/execution/observer.py`, `hyper/execution/live_executor.py` |
| Credentials/preflight/control | `hyper/execution/credentials.py`, `hyper/execution/live_preflight.py`, `hyper/execution/control.py` |
| Dashboard API/frontend | `dashboard/server.py`, `dashboard/api/*`, `dashboard/web/*` |
| Services/deployment | `hyper/ops/procman.py`, `hyper/launcher/core/*` |
| Schema/migrations/retention | `hyper/storage.py`, `hyper/ops/storage_guard.py` |
| Tunable values | `hyper/config.py`, `hyper/params.py`, SQLite `params` |

## Discovery, profiling, and Core formation

### Scheduled pipeline

The production path is:

```text
Leaderboard staging
→ cheap recall and permanent-automation blacklist subtraction
→ Perp/executable-market proof and incremental 37-day fill cache
→ structural/Profile/Rough Copy gates
→ Top32 evidence and Challenger pool
→ fresh finalizer process
→ freeze profit-aligned Top16
→ current-surface bounded Core-count search
→ optional local three-tier tuning around n±1 with n±2 guards
→ at most three strict finalists
→ one winning-surface individual strict pass over frozen Top16
→ same-surface final count and shared price-path certification
→ atomic generation + selection + revision publication
→ post-publication evidence cleanup
```

- Complete discovery runs Monday and Thursday at 04:00 Asia/Shanghai.
- Daily Challenger refresh runs on the other days at 04:00 and uses the latest complete full-scan evidence.
- Settings saves never start a scan. Only the explicit command or configured schedule does.
- No automatic scan/tuner/finalizer wall-clock deadline exists. Individual network requests still have finite
  timeouts, retries, deferred queues, and resumable checkpoints.
- A resource guard saves progress and defers before OOM. On hosts with at most 2 GiB RAM, replay remains
  single-process/serial and reuses the longest fill/path context instead of multiplying it across workers.

### Candidate and cache rules

- New-wallet cheap recall requires at least $250,000 leveraged seven-day volume and non-negative 7d/30d PnL.
  These are recall gates, not final economic qualification.
- Only standard Crypto perpetuals and transparent `xyz:*` stock/index/commodity perps are executable. Spot,
  `#<id>` outcomes/settlements, and opaque builder namespaces never enter metrics, replay, or execution.
- A fresh history covers 37 days: 30 scoring days plus seven warm-up days. A proven complete cache uses source
  cursor deltas and prunes the rolling window; it does not redownload all history each scan.
- Source completeness comes from `fill_cache_state`, never from the earliest retained timestamp. Interrupted
  pagination persists its continuation cursor.
- High-confidence whole-wallet HFT, market-making, grid/DCA automation, or non-executable-market decisions enter
  `wallet_scan_blacklist`. Future scans subtract them before Portfolio/history work and delete raw fill cache.
  Ambiguous automation, temporary economic weakness, heavy-DCA behavior, and data failures remain recoverable.
- Core, Challenger, Exit-only, and current-mode held-position wallets may bypass cheap recall for refresh/safe
  exit, but receive no privilege at final qualification.
- A fresh, explicit zero-equity and zero-position source snapshot is excluded before Rough Copy and checked
  again before final formation/publication. It is a recoverable availability failure, not a permanent blacklist;
  missing equity evidence is deferred and must never be coerced into the zero-equity decision.
- Each generation freezes its candidate order, fill evidence, market snapshot, and as-of time. Recovery must not
  splice in a later market surface or refetch already complete wallets.
- Rough Copy must use a generation-start snapshot of every usable cached per-coin volatility value. A missing
  evidence coin is fetched/computed once through the shared daily-candle cache and persisted before that wallet
  is replayed. The neutral fallback is allowed only after a successful response proves insufficient closed
  history; ordinary Rough ranking must never assign 7% volatility merely to avoid a cold-cache request.

### Generation integrity

- Every scan has a generation id. Stage and validate Leaderboard data before accepting profiles.
- A valid Leaderboard has unique addresses, complete windows, no malformed/empty snapshot, and normally at least
  85% of the prior valid row count.
- Incomplete data, missing frozen paths, worker failure, or integrity mismatch retains the previous published
  generation. Never publish an omission as a complete result.
- Profile workers use independent query-only connections and return artifacts to the parent writer. They do not
  share the Scanner writer connection.
- `pipeline_audit` is resumable workspace for the active generation, not permanent historical evidence.

### Canonical Copy evidence

- 30/14/7 evidence is sliced from one continuous 37-day account path. Do not replay three independently funded
  accounts or add wallet-level profits together to simulate a portfolio.
- Shared portfolio replay uses a standardized account for selection/tuning. Paper and Live scale the published
  policy against their own current equity; selection must not depend on the current Paper balance.
- Profile and shared replay use the same executable universe, generation snapshot, fees, slippage, maintenance
  margin, liquidation accounting, and strategy semantics.
- A target flat-to-open transition is an actionable open regardless of target notional size. Hyperliquid's
  executable minimum and current account capacity are execution concerns, not a source-signal threshold.
- Split exchange fills from the same opening OID extend one opening anchor. Later add OIDs can create at most one
  followed add apiece; fill fragments do not become repeated adds.

### Quality and lifecycle rules

Keep gate definitions in their owning code. When changing them, update tests and operator-facing explanations
together. The current broad contract is:

- Structural exclusions reject proven bots/HFT/grid behavior, non-executable activity, corrupt history, and
  catastrophic source risk before expensive replay.
- Lottery concentration is conditional, not a Top3-profit ban: a concentrated wallet survives when its remaining
  body still has positive, repeatable economics. Do not revive the old rule that rejected every concentrated
  winner.
- Rough Copy requires positive 30d/7d economics, adequate samples, and executable open coverage before entering
  the bounded strict pool.
- New-generation Rough and Strict qualification split the latest 28 days into four fixed, non-overlapping
  seven-day buckets from the same continuous replay. Every bucket must contain at least one completed Copy
  Episode and have positive realized net PnL; missing evidence is not a zero-return bucket. Profit ordering is
  `60% × conservative 30d return + 25% × four-bucket average + 15% × worst bucket`. Immutable generations that
  predate this evidence remain readable with their original ranking formula.
- Strict individual and shared formation require positive recent windows, current sample depth, capacity/open
  coverage, acceptable open loss/drawdown/cost, complete price paths, and bounded isolated liquidations.
- Strict Core ordering applies a small, bounded penalty to unusually dense seven-day source opening activity.
  It is ranking-only: the wallet remains eligible/Challenger, is never blacklisted by this rule, and the penalty
  automatically disappears after a full scan or daily refresh observes its recent frequency below the start line.
- Exchange-labelled self-liquidation plus a fresh zero-equity/no-position account snapshot is a hard safety
  failure. Canonical Copy liquidation losing at least 8% of episode-opening dynamic equity is also hard safety.
  Ordinary losses, recent decline, or sub-8% isolated liquidation are not automatic daily removals.
- A complete scan is a membership reset: unstarred incumbents compete as current candidates. Only an actively
  operator-starred Core can use the bounded retention lane, and hard/incomplete evidence still revokes it.
- Daily Challenger refresh may keep the same Core or publish a strict superset; it cannot silently remove or
  replace Core. Manual Exit-only remains mode-specific for held-position completion.
- Dashboard labels weak economics as business rejection, not “数据异常”. Reserve data-error labels for cache,
  transport, replay, valuation, quarantine, or immutable-strategy integrity failures.

## Core count and automatic tuning

### Mode contract

- `AUTO_TUNE_MARGIN_ENABLE=false`: use the active parameter surface, perform all strict individual/shared gates,
  and choose a bounded profitable Core prefix. Membership changes must not re-enable tuning.
- `AUTO_TUNE_MARGIN_ENABLE=true`: run one `count_first_local_surface_v2` formation for the generation.
- Explicit `optimize`: research/operational full optimization, still subject to memory guards and checkpoints.

### Bounded automatic search

- Top32 is evidence/Challenger scope. Freeze at most the profit-aligned Top16 for automatic formation; ranks
  17–32 cannot be reabsorbed after tuning.
- Locate a count center with bounded jumps such as `16 → 8 → 12 → 10`, never `16 → 15 → 14 ...`.
- Test the three primary counts `n-1/n/n+1`; use `n-2/n+2` as cheap guards. Do not expand automatically to
  arbitrary wallet subsets or a full count-by-parameter Cartesian product.
- All three volatility tiers receive upward/downward margin evidence. New candidate margins use 0.5 percentage-
  point grid steps; the active exact surface remains a mandatory control.
- Final choice maximizes conservative shared economics subject to liquidation, drawdown, open-loss, capacity,
  concentration, price-path, and recent-window safety. Do not maximize headline ROI without risk constraints.
- Measure time-weighted deployment, deployment percentiles, tier economics, skip attribution, and the profit
  gained versus profit lost to additional congestion. A single historical P99 peak must not alone suppress the
  entire margin surface.
- After the winning surface's Top16 individual strict pass, a material membership/count reduction may trigger
  exactly one bounded **margin-only** final-membership calibration. It tests the exact control, a small fair
  three-tier grid, and at most three strict finalists. It does not search leverage, adds, wallet counts, or
  arbitrary subsets, and never recurses after another removal.
- Therefore the automatic path has one local tuner plus at most one final margin calibration; it never starts
  the retired per-count full tuners or exact-membership recursive closure.
- Final publication performs one same-surface count search and one shared price-path certification. A candidate
  that cannot pass actionable-open/capacity gates cannot publish merely because its PnL is high.

### Leverage and sizing semantics

- New opens use our tier leverage cap, clipped only by the venue's current maximum:
  - BTC is always stable tier;
  - non-BTC Crypto and transparent `xyz:*` are mid below 9% sigma and high at/above 9%;
  - unresolved/young valid markets temporarily use the configured mid fallback.
- Standard and HIP-3 market-context refreshes persist the official per-market maximum leverage beside volatility.
  A partial refresh must never erase a previously proven cap, and Live execution independently clamps the planned
  tier leverage to the broker's official market spec while preserving the planned margin.
- Target-wallet leverage is display/audit metadata only. It must remain visible when captured, but must never
  reduce or increase our planned leverage, affect qualification, or gate tuning.
- Existing positions are never retroactively rescaled by a revision. Adds remain tied to the position's opening
  strategy semantics; a fully closed later episode uses the new active revision.
- `MARGIN_EQUITY_PCT` is the single operator new-entry risk budget (default 90%). It scales the equity basis and
  caps aggregate fresh-entry margin. The remaining real cash can support existing-position adds and risk handling.
  Auto-tune must not modify it.
- First-open sizing must leave room under the per-coin cap for at least two full follow-on add units (open plus
  two adds), subject to real available balance and other hard caps.

## Observer and Live execution

### Target observation and durable signals

- A newly observed target starts at the current time; do not backfill old fills into a new copy book.
- Target fills come from REST `userFillsByTime`. Cursor movement and the complete batch commit are atomic. On
  SQLite/row failure, restore the old cursor and refetch the overlap; `tid` dedup prevents double execution.
- Target requests are sequential and start no faster than one wallet every five seconds. Ten tracked wallets
  therefore complete a healthy round in roughly 50 seconds; copying does not require sub-ten-second latency.
- Every Live fill is journaled in `execution_signal` before strategy mutation. Signals move through durable
  pending/processing/retryable/terminal states and survive worker or SQLite failures.
- A retryable source open is skipped when a later durable source close/flip already proves that episode ended;
  never create stale exposure solely to replay an obsolete signal before its queued close.
- The ordered signal worker owns strategy and execution mutation. Do not spawn competing per-fill book writers.
- While operator-paused or `reconcile_required`, new opens and exposure-increasing adds are blocked. Reductions,
  target full closes, and management of already-owned Live positions continue whenever exchange state is safe.
- A critical background loop exit must make the process fail non-zero so systemd restarts it; it must not look
  like a clean intentional stop.

### Pricing and API budget

- Discovery jobs may snapshot `COLLECTION_SOURCE=quicknode` and route compatible `/info` calls through the
  protected QuickNode endpoint at a process-wide maximum of 10 RPS. The default remains `official`; Observer,
  trading, WebSocket, and execution-time L2 paths never receive or initialize the QuickNode credential.
- Leaderboard, `l2Book`, and `recentTrades` always use Hyperliquid official transport. A QuickNode 400/422
  disables only that request type for the current process. Credential/plan errors trip immediately; exhausted
  429, timeout, invalid-response, or 5xx retries lock the remaining job to official transport. A resumed
  finalizer inherits that lock. Every later scheduled or manually-triggered job probes its selected source again.
- Standard Crypto and `xyz:*` perps use the same public WS BBO and per-coin `activeAssetCtx` mark streams.
- REST `l2Book` is execution-only: fetch it immediately before an open/add/reduce/close for depth and bounded
  slippage. Never continuously poll books for Dashboard marks.
- Official-mark REST fallback is low-frequency and runs only for stale WS marks.
- Scanner and Observer share Hyperliquid REST weight. Target observation and real execution have priority;
  discovery pacing must yield under active execution load.

### Live execution and reconciliation

- Live is Paper strategy plus a real execution adapter; it is not a separate strategy engine.
- Self-account orders, fills, positions, open orders, equity, and available collateral are reconciled through
  REST only. There are no self-account user WebSocket subscriptions, account-WS modes, or account-WS health
  gates. Public BBO/mark WebSockets remain the independent pricing plane.
- Run a complete authoritative account reconcile at startup and every 30 seconds. Every exposure increase also
  requires a fresh complete reconcile before sizing and another after submission. A venue-enforced reduction
  may reuse a successful projection no older than 30 seconds, is clamped to the proven official per-coin size,
  and still synchronizes official fills; the periodic reconcile remains the independent backstop.
- Every exposure increase reads fresh exchange equity/available balance, reconciles exchange positions/orders
  against the Live ledger, then sizes and executes. Dashboard equity/available values come from the real account.
- LiveExecutor uses a dedicated WAL connection. Observer must commit its signal transition before LiveExecutor
  writes, and no coroutine/thread may borrow another owner's connection.
- Market execution is a bounded marketable IOC/taker flow, not an unbounded “market” assumption. Validate current
  book depth/slippage, use deterministic CLOIDs and durable intents, and record the exchange-confirmed fill before
  mutating the Live ledger.
- A timeout, 5xx, disconnected response, or SQLite lock is not proof that an order failed. Reconcile by CLOID,
  exchange orders/fills, and actual position delta before retrying. Never blindly resubmit an ambiguous open or
  close.
- Transient transport/lock errors remain visible and retry automatically; they do not silently set operator pause.
  Only proven exchange/ledger ambiguity enters persistent `reconcile_required`.
- A passing Mainnet preflight starts full Live immediately. The retired 1%-equity Canary cap must not be created
  for new sessions.
- Agent credentials authorize trading but not withdrawal. Private keys are browser-encrypted during transfer and
  stored encrypted on VPS; never return them through API/UI/logs. Authorization expiry is read from Hyperliquid,
  not user-entered.

### Position-management semantics

- Hyperliquid exposes one net position per account and market. If copied positions already own one direction,
  later opposite-direction opens/adds are skipped until that direction is flat; never represent simultaneous
  hedge-mode legs in the local Live ledger.
- An explicit exchange `Liquidated ...` self-account fill is an authoritative reduction even without our CLOID.
  Settle only the same-side ledger discrepancy proven by the fresh REST projection. Arbitrary/manual unmatched
  fills remain fail-closed and must never be silently attributed to a strategy position.
- Smart-add gaps compare target transaction prices. Our BBO is for execution/PnL, never mixed into target-motion
  qualification. One target add OID can execute at most one followed add.
- Target reductions accumulate until the configured mirrored threshold; a target full close always closes.
  Percentage reductions use target episode/peak size, not our absolute source-vs-copy size.
- A mirrored reduction that would leave less than Hyperliquid's executable minimum upgrades to a full close.
  Legacy Live dust must be closed by a venue-confirmed reduce-only order; never mark it closed only in the ledger.
- Manual 100% close creates the same-wallet/same-coin cooldown only for a losing realized episode. Partial manual
  close leaves the episode managed for later target add/reduce/close.
- Isolated liquidation is charged to that position. There is no portfolio-wide hard PnL stop that flattens or
  pauses unrelated positions.
- Optional smart take-profit and tail-protection rules must use the immutable strategy revision shared by replay,
  Paper, and Live. Do not add a Live-only profit rule.

## Dashboard contract

- The Account tab configures Paper/Live mode and Mainnet credentials; Testnet is development-only and does not
  belong in production mode selection or persistent product bookkeeping.
- The Account tab also configures the scanner-only QuickNode endpoint. Browser code encrypts it with the
  existing public wrap key; a dedicated protected worker validates `meta` and atomically writes only an HTTPS
  `*.quiknode.pro` `/info` endpoint to `secret/quicknode` with mode `0600`. Dashboard/API/SQLite/logs must never
  expose or persist the plaintext endpoint. Source changes are rejected while a scan is active; endpoint
  replacement remains allowed but applies only to the next job.
- Paper mode is compact. Live reveals owner address, agent address, and agent private-key input. “加密保存并验证”
  validates ownership/authorization/Unified/account basics and stores credentials; it does not start Observer.
- The global top-right player-style controls are the only normal start/pause/stop controls. Their state comes
  from execution control and process state, not a client-side assumption. Live strategy publication must not
  make them display stopped.
- Agent expiry is fetched from `extraAgents`; never add an editable expiry field.
- Mode switching is disabled while Observer is active.
- Position close controls remain loading until every position captured by the command reaches terminal state or
  a concrete failure is returned. Do not clear loading on a fixed timer.
- Wallet counts and roles come from the current published selection plus the selected-mode held-position overlay.
  Manual Exit-only in Live must not be blocked by Paper holdings.
- Target/source entry price and captured target leverage may be displayed as audit context. Our planned/actual
  leverage is separate; never label a missing target leverage as our execution leverage.
- Reuse shared `.btn` semantic variants and nearby component patterns. Do not create one-off button skins.
- Edit JSX/CSS sources and rebuild. Never hand-edit compiled `dashboard/web/app.js` or launcher bundles.

## Data lifecycle and SQLite discipline

### Durable business data

Retain Paper/Live positions and actions, Live execution sessions, order intents/attempts/fills, referenced
strategy revisions, Core/Exit-only/manual controls, blacklist decisions, and safety events. These are business
history and are not scan-cache cleanup targets.

### Bounded cache and temporary evidence

- `candidate_fills`: rolling 37 days. Remove raw fills for permanent automation blacklist addresses. Protect
  current Core/Challenger/Exit-only/current-mode held-position evidence as required for operation.
- Price candles use their configured bounded per-interval windows.
- Retain generation cache only for the current published generation, latest complete full scan, and unfinished/
  resumable generations. Keep the latest five compact scan summaries for operator history.
- `pipeline_audit` and `formation_prefix_evidence` are temporary resumable workspace. Delete them after atomic
  publication or terminal failure; retain only compact success/failure summaries.
- Normal execution snapshots, completed signals/commands/preflights, and successful reconcile checkpoints retain
  seven days. Anomalous reconcile records retain 90 days. Durable order/fill/position ledgers are unaffected.

### Maintenance and locking

- Both scheduled scan services run `storage-maintenance` from `ExecStopPost`, including after failure.
- Deletes are indexed, bounded, and committed in small batches. Do not hold a large write transaction while
  Observer is running.
- All long work must release SQLite write transactions before network calls or replay.
- Connections set a 64 MiB `journal_size_limit`. Maintenance performs PASSIVE checkpoint and truncates only when
  all frames are checkpointed and no reader blocks it. Never delete the WAL file manually.
- Do not routinely run `VACUUM`. Freelist pages are reusable; use a separately planned `VACUUM INTO` only when
  physical compaction is explicitly needed and trading/scanning safety is established.
- `database is locked` is recoverable contention, not a reason to lose a cursor/signal or silently stop Observer.

## Commands and verification

Run from repository root with `.venv/bin/python` when available:

```bash
# Dashboard
.venv/bin/python -m dashboard.server --db data/hl.db --static dashboard/web --host 127.0.0.1 --port 8810

# Scanner / maintenance
.venv/bin/python -m hyper.cli.discover --db data/hl.db serve-rescan
.venv/bin/python -m hyper.cli.discover --db data/hl.db scan --full --days 14 --scan-interval 6
.venv/bin/python -m hyper.cli.discover --db data/hl.db challenger-refresh
.venv/bin/python -m hyper.cli.discover --db data/hl.db regate
.venv/bin/python -m hyper.cli.discover --db data/hl.db optimize
.venv/bin/python -m hyper.cli.discover --db data/hl.db calibrate-current-core
.venv/bin/python -m hyper.cli.reform_current_core --db data/hl.db
.venv/bin/python -m hyper.cli.discover --db data/hl.db finalize-profiled --generation GENERATION_ID
.venv/bin/python -m hyper.cli.discover --db data/hl.db storage-maintenance --dry-run
.venv/bin/python -m hyper.cli.discover --db data/hl.db storage-maintenance
.venv/bin/python -m hyper.cli.discover --db data/hl.db reset-paper --yes

# Observer
.venv/bin/python -m hyper.cli.observe --db data/hl.db observe
.venv/bin/python -m hyper.cli.observe --db data/hl.db report

# Launcher
.venv/bin/python -m hyper.launcher.launcher --port 8799 --no-browser
```

Important command semantics:

- `scan --full` rediscovers the candidate universe but still uses delta fetch for complete wallet caches.
- `regate` rebuilds current gates/policies from frozen cached evidence; it does not fetch a new Leaderboard.
- `optimize` deliberately performs the heavier research path on the current sealed generation.
- `calibrate-current-core` freezes membership and adjusts only the bounded first-open margin surface. Use
  `--apply` only when the user authorized publication.
- `reform_current_core` re-ranks frozen current-generation evidence, publishes membership on the unchanged
  active parameter surface, and then certifies/stores the resulting shared strict replay for Dashboard display.
- Explicit `repair-selection --strict-candidate-limit 32` may replay the frozen Top32 in two Top16 path
  batches to backfill strict failures. It must not raise the independent Core cap or alter scheduled Top16
  automatic formation.
- `finalize-profiled` resumes an already profiled unpublished generation without redoing network collection.
- `reset-paper --yes` is the supported Paper reset and requires Scanner/Observer stopped. It must not touch Live
  ledgers or credentials. `--factory-params` additionally resets operator parameters.

Required verification:

```bash
.venv/bin/python -m compileall -q hyper dashboard
.venv/bin/python -m unittest discover -s hyper/tests
dashboard/web/build.sh
hyper/launcher/web/build.sh
```

For a narrow documentation-only edit, inspect links/commands and `git diff --check`; code/build tests are not
required unless documentation changes accompany code.

## Deployment checklist

1. Preserve unrelated worktree changes; never use destructive local Git reset.
2. Read the current target from `secret/vps.txt` immediately before connecting.
3. Deploy from the Git source of truth.
4. Run migrations/verification appropriate to the changed component.
5. Restart only affected long-running services. Never restart `hl-scan.service` as a generic deploy step.
6. Confirm Observer process state, execution control state, selected mode, active session/revision, durable signal
   backlog, reconciliation health, and recent errors after any execution change.
7. A strategy/Core publication does not require an Observer restart and must preserve `live_running`.
8. Do not claim Live healthy from process status alone; verify both control/session state and exchange/ledger
   reconciliation without exposing account secrets.

## Retired behavior: do not restore

- Testnet as a product mode or persistent product ledger.
- Paper holdings included in Live monitoring, counts, exits, or wallet retirement decisions.
- Shared Paper/Live position tables or balances.
- Target leverage as an input to our sizing or tuning.
- The 1%-equity Live Canary cap.
- Continuous REST `l2Book` polling for marks.
- Blind order retry after an ambiguous API response.
- Core/parameter publication pausing a healthy running Live session.
- Dashboard routes directly mutating business tables.
- Total scan/tune timeout that discards a resumable generation.
- Per-count full tuner grids, recursive exact-membership closure, or sequential Core-count enumeration.
- Permanent 90-day retention of detailed `pipeline_audit`/Profile payloads.
- Automatic Core inference from `MIN_FOLLOW_SCORE`, raw score, or watchlist order.
- Source-wallet profit high-water forced exits or portfolio-wide copy stop-losses.
