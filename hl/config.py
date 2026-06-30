"""Shared constants — endpoints, hard limits, sim parameters. No logic here."""

# Hyperliquid endpoints
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"
UA = {"User-Agent": "hl-copytrade/0.3", "Accept": "application/json", "Content-Type": "application/json"}

# numeric
FLAT = 1e-6                 # |position| below this (coin units) counts as flat
MIN_POST_INTERVAL = 1.2     # global REST pace (s/POST). HL /info budget = 1200 WEIGHT/min/IP, and
#                             our heavy calls (userFillsByTime, frontendOpenOrders) cost weight 20
#                             each (+1 per 20 results) — so the real ceiling is ~60 weight-20/min,
#                             NOT a request count. 1.2s = 50/min ≈ 1000 weight/min: safely under
#                             1200, leaving headroom for the 8s-trickle scanner (~150 weight/min)
#                             on the same IP. (l2Book/clearinghouseState are only weight 2.)
#                             The scanner overrides this to --scan-interval in its own process.

# HL WS hard limits (per IP, official): the binding one is unique users.
MAX_WS_USERS = 10           # max unique users across user-specific subscriptions (WS only)

# Copy engine: SIGNAL via REST poll (per-wallet userFills — REST has no 10-user cap, so we can
# watch the whole watchlist); PRICING via WS bbo (per-COIN top-of-book — NOT subject to the
# 10-user cap, only the 1000-sub cap, and we touch only a few dozen coins). Targets are low-freq
# long-hold, so a few-seconds poll latency is fine; we execute against the live book at detection.
MIN_FOLLOW_SCORE = 0.6      # follow watchlist wallets with score >= this (quality threshold, UI-tunable).
#                             v6 (2026-06-30): 0.85→0.6 — the discipline GATES now keep 赌徒 out of the
#                             watchlist itself, so the score line can open up to admit more clean low/mid-
#                             freq strong traders (more copy signals) without re-admitting blow-up risk.
#                             v5 (2026-06-29): 1.2→0.85 — recalibrated for the new harvest box + de-bugged
#                             score; 0.85 yields ~30 CLEAN wallets (0 小赚大亏/扛单, win median 87%)

MAX_TARGETS = 40            # hard cap on followed wallets (bounds REST load even if many clear the score)
OBSERVER_UNIT = "hl-observe"  # systemd unit the scan-trigger supervisor starts/stops on dashboard command
WATCHLIST_RELOAD_S = 300   # re-read the watchlist table this often (track rolling discovery)
POLL_OVERLAP_MS = 5000     # re-fetch this far behind each wallet's in-memory cursor (tid-dedup absorbs
#                            it) so a fill landing between poll rounds isn't missed. This is the ONLY
#                            look-back — the observer is forward-only, it never catches up on history.
LIVE_FILLS_RETENTION_DAYS = 7  # prune live_fills older than this (tid-dedup only needs the overlap
#                                window; the rest is audit) — keeps the only unbounded table bounded

# Copy account & sizing (UI-tunable). Real-account paper model: a simulated wallet with an initial
# balance. Each copy commits isolated margin out of CURRENT AVAILABLE balance, sized by VOLATILITY
# TARGETING (below) — never a fixed $ amount, always a fraction of available. notional = margin *
# leverage; isolated liquidation (loss = margin). No stop-loss in v1.
INITIAL_BALANCE = 10000.0   # simulated wallet starting equity ($)
ADD_MARGIN_PCT = 0.01       # margin on each follow-on ADD (scale-in) = fraction of available
MAX_ADDS = 2                # follow the master's scale-ins up to this many adds/position (each ADD_MARGIN_PCT)

# v8 SIZING (2026-06-30). Three VOLATILITY TIERS (by daily σ = high-low range, see volatility.py); each
# tier has its own margin% + leverage cap; WITHIN a tier, leverage scales continuously with σ. σ classifies
# AND fine-tunes — no coin lists. Anchored to AVAILABLE (self-throttles as positions fill). Tier by σ:
#   stable  σ ≤ STABLE_SIGMA_MAX        (BTC + anything calmer incl low-σ stocks like GOLD) → big
#   mid     STABLE_SIGMA_MAX < σ < HIGH_SIGMA_MIN  (ETH/SOL/HYPE/majors)
#   high    σ ≥ HIGH_SIGMA_MIN          (ZEC/memes/wild) → small
#   margin   = available × <tier>_MARGIN_PCT
#   leverage = floor(clip( STABLE_LEV_CAP × STABLE_SIGMA_MAX/σ , MIN_LEV , <tier>_LEV_CAP ))
#              the σ-ratio gives a continuous gradient (full at σ=STABLE_SIGMA_MAX, declining ∝1/σ); the
#              tier cap is the hard ceiling. So within mid, ETH(σ5.3%) hits the 10x cap while HYPE(σ9.6%)
#              gets ~8x. NOT mirrored from the master (their leverage choice no longer sizes us).
#   notional = margin × leverage. (Capped at the master's notional — moot at our size, kept as safety.)
STABLE_SIGMA_MAX = 0.04     # σ ≤ this → STABLE tier; also the leverage-formula reference (full lev at this σ)
HIGH_SIGMA_MIN   = 0.10     # σ ≥ this → HIGH-VOL tier; between the two → MID tier
STABLE_MARGIN_PCT = 0.10    # per-trade margin = this × available, for STABLE-tier coins
MID_MARGIN_PCT    = 0.08    # ...for MID-tier coins
HIGH_MARGIN_PCT   = 0.06    # ...for HIGH-VOL-tier coins (kept meaningful so memes aren't double-crushed)
STABLE_LEV_CAP = 20.0       # leverage ceiling for STABLE-tier coins
MID_LEV_CAP    = 10.0       # ...for MID-tier coins
HIGH_LEV_CAP   = 5.0        # ...for HIGH-VOL-tier coins
MIN_LEV = 1.0               # leverage floor — ultra-volatile coin → ~spot (isolated 1x ≈ unliquidatable)
COIN_MARGIN_CAP_PCT = 0.20  # per-COIN cap: total margin across all our open positions on ONE coin ≤ this
#                             fraction of the account (stops N wallets piling into the same coin/direction)
MIN_OPEN_MARGIN_PCT = 0.005 # skip a new copy if its formula margin (= MAX_MARGIN_PCT·scale·available) is below this
#                             fraction of equity: once free balance is too low to fund a MEANINGFUL
#                             position, just skip the signal (don't open dust). Existing positions stay
#                             managed/exited. High-conviction signals (bigger rf) still open later than
#                             low-conviction ones, which is intended. UI-tunable.
MAX_LEV = 20.0              # hard leverage cap (BTC + anything calmer pin here); also a stale-σ backstop

# Per-coin volatility (regime-aware) for the sizing above. A coin calm-then-erupting must NOT keep its
# old low σ and get over-levered into a blow-up — so we use TWO horizons and take the MAX (de-risk fast
# when vol rises, re-risk slowly when it falls). Refreshed periodically into the coin_vol TABLE off the
# signal hot path; sizing just reads the row. σ_used = max(σ_fast, σ_slow), both daily realized vol.
VOL_FAST_DAYS = 7           # recent window — catches a fresh volatility regime within ~a day
VOL_SLOW_DAYS = 30          # long baseline — stable; the floor we hold until calm is sustained
VOL_MIN_SAMPLES = 5         # need this many daily candles, else fall back
VOL_REFRESH_S = 3600        # re-fetch each tracked coin's σ at most this often (1h) — vol drifts slowly
VOL_FALLBACK_SIGMA = 0.10   # σ when candles unavailable (new/illiquid coin) → low lev, small notional
VOL_PREWARM_TOP = 30        # at startup, warm σ for the top-N by 24h volume in crypto + EACH builder dex

# PERIODIC orphan reconcile: forward-only polling normally catches a master's close in real time, but a
# missed fill (poll gap / aggregation quirk / blip) would leave us dumb-holding a position the master
# already exited. Re-run the startup reconcile this often so an orphan is closed within minutes.
RECONCILE_INTERVAL_S = 300  # 5 min
#                             (the liquid coins our targets most likely trade) → no first-open latency,
#                             warm restart. The long tail is still lazy-fetched on first fill.

# Copy-strategy knobs (UI-tunable; no hardcoded magic). None = disabled.
# Chase guard: on a fast spike the master eats the book with size and our taker fill lands worse.
# If our entry price is more than this % worse than the master's, SKIP that open (don't chase).
# Applies to taker opens only (maker rests passively; exits are never blocked — always follow out).
MAX_ENTRY_CHASE_PCT = None    # e.g. 0.5 => skip a taker open whose entry is >0.5% worse than master

# Execution model (paper fidelity). We ALWAYS price off the CURRENT book at detection (never the
# master's fill price — that's only a fallback when the book isn't ready). The only question is which
# SIDE: a copy reacts seconds LATE (forward-only REST poll), so we can't retroactively have rested at
# the master's maker price — to actually hold the position the master is in, we cross the spread (taker
# catch-up). Pricing a late maker fill at the passive side silently assumes an instant, never-missed
# rest = optimistic paper PnL. Default OFF = honest taker catch-up for ALL fills. Flip ON only once we
# proactively mirror a target's resting order we saw AHEAD of its fill (target_orders) — then a maker
# fill is legitimately reproducible. Until that exists, leave OFF so paper PnL doesn't flatter live.
EXEC_MAKER_MIRROR = False     # True = price master-maker fills at the passive book side (assumes our rest fills)

# Stage-1 leaderboard prefilter (UI-tunable). The leaderboard carries each wallet's 24h/7d/30d/allTime
# perf in ONE bulk fetch, so we pre-bias on what it CAN reliably say — multi-window profitability +
# return magnitude + 7d activity — BEFORE any per-wallet API profiling. What it CANNOT say (true week-
# to-week stability, copyability, loss-discipline) is the PROFILE stage's job (pos_day_ratio, grid gate,
# worst_loss gate). Key lessons baked in: (1) bots/grids are INVISIBLE here (volume/turnover/efficiency
# don't separate them from directional — proven), so don't try; profile catches them. (2) ACTIVITY uses
# the 7d window, NOT 24h — a 24h floor kills the holders we want (low 24h volume mid-hold) and biases to
# high-churn bots. (3) RETURN uses 30d magnitude + 7d magnitude TOGETHER: 30d alone can be one big early
# day then dormant; requiring the 7d to ALSO be earning blocks that, while the 30d requirement stops a
# single-week fluke. We copy by %/leverage so low-ROI wallets give us low returns (small capital).
# STAGE-1 leaderboard BOX (v5, 2026-06-29). Gate ONLY on what the leaderboard can HONESTLY say —
# real capital + genuine recent VOLUME + internal consistency. ROI/PnL MAGNITUDE is NOT a gate:
# leaderboard ROI is contaminated (deposits/withdrawals/spot/airdrop), empirically the top-ROI wallets
# are $0-volume HODLers/ghosts. The one field that can't be faked by holding is VOLUME. Profit
# JUDGMENT is deferred to the profile (real fills). Thresholds calibrated against 20 followed anchors +
# a clean-strength cohort (see memory hl-copytrade.md): strong wallets sit at $0.5–30M wk vol, pnl/vol
# 0.2–4%; ghosts pnl/vol >>8%; MMs vol >$100M & pnl/vol <0.1%.
HARVEST_MIN_ACCT = 10000.0          # real-capital floor (5k→10k; <10k mostly noise, but our proven
#                                     small-account %-traders sit at ~$11-20k so don't raise further)
HARVEST_WEEK_VLM_MIN = 500_000.0    # 7d VOLUME floor — genuinely trading this week (strong density is
#                                     thin below $1M, but $0.5-1M still holds real talent → floor $0.5M)
HARVEST_WEEK_VLM_MAX = 30_000_000.0 # 7d VOLUME ceiling — above ~$30M = market-maker/HFT-bot (billion-$
#                                     /wk, razor pnl/vol); 90% of strong wallets sit under $15M
HARVEST_PNL_VOL_MIN = 0.001         # 7d pnl/volume FLOOR (0.1%) — below = razor-thin MM, not directional
HARVEST_PNL_VOL_MAX = 0.08          # 7d pnl/volume CEILING (8%) — above = profit too big for the volume
#                                     = NOT from trading (deposit/spot/airdrop ghost); real traders 0.2-4%
# RETIRED (leaderboard ROI contaminated; daily turnover doesn't separate MMs from our high-churn keeps):
HARVEST_MON_ROI_MIN = 0.0           # was 0.15 — return magnitude is now a SCORE input, not a gate
HARVEST_MON_ROI_MAX = 1e9           # was 3.0
HARVEST_WEEK_ROI_MIN = 0.0          # was 0.02
HARVEST_MAX_TURNOVER = 1e9          # was 10.0 — volume ceiling + pnl/vol band handle MMs instead

# v3 score shape (interpretable, UI-tunable — NOT arbitrary quality cutoffs). The watchlist is
# top-N by SCORE = Quality(RAR × day-consistency) × Survival × Health(current-underwater depth).
SCORE_K = 5.0          # daily-stats confidence: w = active_days/(active_days+K). Low-freq → lean overall ROI
SCORE_GAMMA = 2.0      # day-consistency strictness: consistency = pos_day_ratio^(w·GAMMA). Higher = stricter
UW_TOL = 0.02          # ignore current open underwater below this (fresh/small dips fine)
UW_REF = 0.10          # open-underwater treated as fully dangerous (Health snap → 0 here). Decoupled
#                        from MAX_LEV (the copy cap) on purpose — this is a scoring-shape param.
# EVIDENCE handling (paired with the now-soft activity gate). Relaxing `irregular` admits genuine
# low-freq swing/trend traders, but a 3-trade +100% wallet must NOT rank like a proven one. So the
# score discounts thin evidence AT THE SOURCE instead of via a hard gate: shrink roi toward 0 by
# sample size, and cap the risk-adjusted ratio so no wallet rides one lucky low-drawdown streak to an
# unbounded score. Low-evidence wallets then sit BELOW the follow line (observed by the scanner, not
# yet copied) and climb as round-trips accumulate across re-scans — graduation with no tier machinery.
SCORE_SHRINK_K = 10.0  # roi trusted as roi×n/(n+K) for n closed round-trips: a wallet needs ~K trades
#                        for its return to be half-believed (n=10→×0.5, n=3→×0.23, n=100→×0.91)
SCORE_RAR_CAP = 3.0    # ceiling on risk-adjusted return (roi_eff/(dd+0.05)) — tiny observed drawdown at
#                        low sample is not real safety, so one extreme ratio can't dominate the score

# LOSS-DISCIPLINE demote ("扛单降权"). Measures NOT cutting losses DIRECTLY — never via win rate. The
# score multiplies by 1/(1+K·disc), where disc = 5×(current losing-bag burden: depth×count×duration) +
# 1×(historical forced liquidations). A clean fast-cutter (no open loss, never liquidated) is untouched
# however high its win rate; a wallet sitting on several deep bags for days, or that's been force-closed,
# is demoted. SOFT: sinks the worst toward/below the follow line, never zeroes a profitable wallet. 0 =
# off. Tunable via dashboard (apply_scanner_params pushes it onto config so scan + regate both honor it).
DISP_PENALTY_K = 0.6   # demote strength (0 = disabled; higher = harsher). score *= 1/(1+K·disc)
# REALIZED-asymmetry sub-term of disc — catches "小赚大亏 / 不及时止损" (the twins, #17, RESOLV) by the
# tail directly: |worst realized loss| vs the median win. v5 (2026-06-29): the OLD win-rate gate
# (defer = 1-loss_rate/LOSS_RATE_REF) is REMOVED — it zeroed this penalty for any wallet with win<85%,
# so a 60%-win churner with a 4× tail loss sailed through. loss_pain now bites at ANY win rate; a clean
# fast-cutter (small symmetric losses) has loss_pain≤TAIL_FREE → still untouched.
TAIL_FREE     = 1.5    # worst loss up to this × median win is fine; beyond = asymmetric (小赚大亏)
ASYM_W        = 1.5    # weight of the asymmetry term inside disc (0.8→1.5: 小赚大亏 sinks below the line)
LOSS_RATE_REF = 0.15   # (retired — the asym win-rate gate is gone; kept only so stale refs don't break)
PAIN_MIN_TRADES = 15   # ≥ this many closed trades with ZERO realized losses = extreme deferrer
PAIN_NOLOSS   = 4.0    # loss_pain assigned to a never-realized-a-loss wallet over a large sample
# HOLD-SKEW sub-term of disc — 扛单 by DURATION: median losing-hold / median winning-hold. >1 = holds
# losers longer than winners (disposition effect). Only EXTREME skew is penalized (the dangerous combo is
# high skew WITH a big tail loss, already caught by loss_pain); moderate skew on small losses is benign.
HOLD_SKEW_FREE = 3.0   # skew up to 3× is tolerated (holding small losers a bit longer ≠ blow-up risk)
HOLD_SKEW_W    = 0.5   # weight of the (hold_skew - FREE) term inside disc

# ── DISCIPLINE GATES (2026-06-30) — promote the SOFT score sub-terms above to HARD watchlist-entry
# gates, so a 赌徒 never enters the watchlist at all (not merely ranked low). These use metrics ALREADY
# stored on the profile (loss_pain / hold_skew / profit_conc) → `regate` applies them instantly with no
# re-fetch. Plus a LIFETIME-net check (the one new datum, from the full-history fetch) that catches a
# wallet whose blow-up is OLDER than the 14d scoring window (e.g. #47: clean 14d, but -123k over 287d).
# All UI-tunable (params.py → apply_scanner_params overlays onto the scan/regate namespace).
GATE_LOSS_PAIN_MAX   = 1.5   # reject if |worst realized loss| / median win ≥ this (小赚大亏). 0 = off.
GATE_HOLD_SKEW_MAX   = 1.5   # reject if median losing-hold / winning-hold ≥ this (抗单). 0 = off.
GATE_PROFIT_CONC_MAX = 0.8   # reject if one day ≥ this share of gross profit (一把行情/未经验证). 0 = off.
GATE_REQUIRE_LIFETIME_NET = True   # reject if full-history realized net ≤ 0 (长期净亏). Skipped if the
#                                    net_life field is absent (old profiles) so regate is safe pre-rescan.
GATE_REQUIRE_30D_NET      = True   # reject if 30d realized net ≤ 0 (近一月在走下坡). Same absent-skip.
# How far back the profiler pulls fills (paginated, sorted, capped at max_pages*2000). Covers the 14d
# scoring slice + the 7/14/30d multi-window nets + a ~6-month "long-term" net_life. Longer = catches
# older blow-ups but more pages/wallet (slower); shorter = faster but net_life sees less history.
PROFILE_FETCH_DAYS = 180

# ── FOLLOW-TIME STATE FILTER (observer, not a watchlist gate) — a high-quality wallet that is currently
# un-followable is BENCHED (kept in watchlist, marked dormant), not dropped, so it revives instantly when
# it trades again (no re-harvest, no lost history). Tunable in the follow params.
DORMANT_DAYS       = 7.0     # no fill within N days → bench (no NEW copies; existing copies still managed)
OPEN_BAG_MAX_FRAC  = 0.03    # currently carrying an unrealized loss worse than this fraction of account →
#                              bench (don't open a fresh copy of a wallet that is right now 扛深亏单)

# TREND-trader inclusion: a winning OPEN position worth ≥ this fraction of the wallet's account = a real
# trend hold, so the wallet is kept even if low-frequency (exempt from the `irregular` activity floor).
TREND_OPEN_MIN = 0.05

# Unrealized gains are return NOT yet locked (can reverse) → count this fraction of a wallet's winning
# open position as RISK in the score denominator, so an unproven unrealized pump can't top the board
# over wallets that actually realized the same return. Trend traders stay included, just ranked behind.
UNREAL_RISK_W = 0.5

# SPOT-HEDGE exclusion: if more than this fraction of a wallet's perp-short notional is offset by a spot
# long of the same token, it's hedging spot (market-neutral), not trading directionally — reject. Its
# perp 'profit' is cancelled by spot, so copying the naked perp leg is a loss for us.
HEDGE_MAX_FRAC = 0.5

# COPY-SIDE STOP — a flat ADVERSE-PRICE cut, our isolated-account tail guard. Cut a copy when price
# runs COPY_STOP_PCT against entry. Calibrated WIDE on purpose (default 18%) from the twins' winner-MAE
# (5m candles): the normal reverting winners — the actual edge — take a median 1.7% / quick heat and
# recover in <1-4h, so an 18% line NEVER touches them. It only governs the deep-bag minority (>10% MAE:
# ~8% of winners), which on an ISOLATED small account is exactly where the damage is — those bags lock
# our margin 12h-to-4-DAYS while risking the 3x liquidation (+33%). The master rides them on $40k cross
# + patience; we can't, so we cap the tail: realize a bounded ~COPY_STOP_PCT×lev margin loss and free
# the capital, instead of bag-holding to liquidation. Paired with the master-leverage cap (which
# already removed the 6x premature-liq bug). UI-tunable (set very high to effectively disable).
COPY_STOP_ENABLE = True
COPY_STOP_PCT    = 0.18     # adverse price move from entry that triggers a cut (≈ this × lev of margin)

# paper-copy simulation
LATENCIES = [0.5, 2.0, 5.0]  # (legacy) latency bands — schema columns; REST signal has one
TAKER_FEE = 0.00045          # detection latency, so all three resolve to the same live-book price
NOTIONAL = 1000.0            # fixed paper notional per copied trade ($)
BOOK_HIST_S = max(LATENCIES) + 3  # (legacy) bbo history depth — REST mode prices off current bbo only

DEFAULT_DB = "data/hl.db"
