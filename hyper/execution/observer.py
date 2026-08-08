"""Live copy-trade observer + paper-copy driver.

Two decoupled data planes, by design:
  • SIGNAL (who traded what) — a continuous REST poll over the FULL watchlist (per-wallet
    userFillsByTime, cursor + small overlap, idempotent by tid). REST has no 10-user cap, so we
    can follow the whole watchlist; our targets are low-freq long-hold, so a bounded tens-of-seconds
    poll latency is acceptable. This is the primary engine.
  • PRICING (what we'd fill at) — a WS bbo subscription PER COIN plus a size-aware L2 check at
    each new exposure. bbo subs are
    per-coin, NOT subject to the 10-user cap (only the 1000-sub cap, and we touch a few dozen
    coins). Every copy is priced as an honest taker catch-up off the LIVE book at detection:
    the planned order must fit current depth/spread/impact and Paper uses its L2 average fill.
    (No user subscriptions on this WS, so no 10-user concern.)

A partial-aware state machine persists every target open/add/reduce/close and our mirrored fill.
Open copies reload after restart. New exposure is equity- and volatility-tier-sized, smart adds are
price-spaced and capped, and target reductions are mirrored by percentage with profitable-tail protection.
"""
import asyncio
import contextvars
import json
import logging
import os
import sqlite3
import time

import websockets

from hyper import config
from hyper.copy.copy_engine import (OpenSizingParams, isolated_liq_px, plan_open_sizing,
                          profit_tail_close_decision, reduce_leaves_dust,
                          rebase_isolated_position,
                          smart_add_order_margin, smart_take_profit_decision, tier_for_sigma,
                          margin_cap_room, wallet_margin,
                          wallet_sector_side_effective_cap_pct, wallet_sector_side_margin,
                          wallet_sector_side_margin_room, wallet_sector_side_position_count)
from hyper.copy.fill_transition import classify_fill_transition
from hyper.copy.sector import parse_json_obj, policy_allows_coin
from hyper.market import rest, volatility, ws
from hyper.market.coin_filter import coin_is_blocked, parse_coin_blacklist
from hyper.selection import state as selection, strategy_revision, wallet_risk
from hyper.ops import storage_guard
from hyper.util import f, now_iso, now_ms
from .liquidity import assess_order_book
from .live_executor import LiveExecutor

logging.getLogger("websockets").setLevel(logging.CRITICAL)
STALE_MS = 30_000          # a detected fill older than this priced at master px (book unreliable)
MARK_WRITE_MIN_MS = 1_000  # dashboard mark freshness: persist at most once/sec/coin from live book ticks
MANUAL_CLOSE_COOLDOWN_S = 24 * 60 * 60
_SOURCE_EVENT_ID = contextvars.ContextVar("observer_source_event_id", default=None)
_LEDGER_RECOVERY_COIN = contextvars.ContextVar("observer_ledger_recovery_coin", default=None)


class RetryableSignalError(RuntimeError):
    """The target fill was received, but its strategy action is not terminal yet."""


class TerminalSignalError(RuntimeError):
    """A verified venue/policy outcome makes retrying this target fill unsafe or pointless."""


def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Book:
    """The isolated Paper account and its restart-safe in-memory position state."""
    def __init__(self, name, pos_table, act_table, acct_table):
        self.name = name
        self.pos_table = pos_table
        self.act_table = act_table
        self.acct_table = acct_table
        self.balance = config.INITIAL_BALANCE
        self.initial_balance = config.INITIAL_BALANCE
        self.sizing_anchor = config.INITIAL_BALANCE
        self.open_ep: dict = {}             # (addr,coin) -> position state
        self._acct_lock = None              # created lazily inside the running loop (sync inspection creates none)
        # Lifetime dashboard counters. Initialized once from history at startup, then maintained per action/close
        # so the 5-minute stats snapshot never rescans the ever-growing action/position tables.
        self.closed_n = 0
        self.wins_n = 0
        self.gross_traded = 0.0
        self.fees_cum = 0.0
        self.stats_loaded = False
        self.available_balance = None       # exchange-authoritative in Live; derived from rows in Paper

    @property
    def acct_lock(self):
        """Serialize margin allocation across opens without creating an orphan event loop at construction."""
        loop = asyncio.get_running_loop()
        if self._acct_lock is None or getattr(self._acct_lock, "_loop", loop) not in (None, loop):
            self._acct_lock = asyncio.Lock()
        return self._acct_lock


class Observer:
    # self.balance / self.open_ep / self.acct_lock delegate to the PRIMARY (taker) book so all existing
    # non-apply code (logs, equity stats, poll/stop loops) keeps working unchanged; the apply/helper methods
    # take an explicit `book` to operate on either account.
    @property
    def balance(self):
        return self.taker.balance

    @balance.setter
    def balance(self, v):
        self.taker.balance = v

    @property
    def open_ep(self):
        return self.taker.open_ep

    @property
    def acct_lock(self):
        return self.taker.acct_lock

    def __init__(self, db, addrs: list, seed_coins: dict, top_n: int = None, add_frac: float = None):
        self.db = db
        db_info = list(self.db.execute("PRAGMA database_list"))
        self.db_path = next((str(row[2]) for row in db_info if row[1] == "main" and row[2]), None)
        self._live_executor_db = None
        self.addrs = addrs
        self.seed_coins = seed_coins
        self.strategy_revision_id = None
        self.top_n = top_n or config.MAX_TARGETS    # hard cap on followed wallets (REST-rate ceiling)
        # v8 sizing (UI-tunable): 3 σ-tiers, each with margin% + lev cap. Margin uses the adaptive strategy
        # equity base; real risk equity and available cash enforce coin/deployment caps.
        self.add_frac = config.ADD_FRAC if add_frac is None else add_frac  # each ADD = first-open margin × this
        self.high_sigma_min = config.HIGH_SIGMA_MIN       # σ≥this → high-vol tier; between → mid tier
        self.tier_margin = {"stable": config.STABLE_MARGIN_PCT, "mid": config.MID_MARGIN_PCT, "high": config.HIGH_MARGIN_PCT}
        self.tier_lev_cap = {"stable": config.STABLE_LEV_CAP, "mid": config.MID_LEV_CAP, "high": config.HIGH_LEV_CAP}
        # UI-tunable sizing knobs (refreshed from the params table by _reload_params; config = fallback)
        self.max_lev = config.MAX_LEV
        self.min_lev = config.MIN_LEV
        self.coin_blacklist = parse_coin_blacklist(config.COIN_BLACKLIST)
        self.block_korean_stocks = bool(config.BLOCK_KOREAN_STOCKS)
        self.low_liquidity_filter_enable = config.LOW_LIQUIDITY_FILTER_ENABLE
        self.live_book_max_spread_bps = config.LIVE_BOOK_MAX_SPREAD_BPS
        self.live_book_max_impact_bps = config.LIVE_BOOK_MAX_IMPACT_BPS
        self.min_coin_day_ntl_vlm = config.MIN_COIN_DAY_NTL_VLM
        self.min_coin_oi_notional = config.MIN_COIN_OI_NOTIONAL
        self.wallet_margin_cap_pct = config.WALLET_MARGIN_CAP_PCT
        self.wallet_sector_side_cap_pct = config.WALLET_SECTOR_SIDE_CAP_PCT
        self.wallet_sector_side_caps = {
            "stable": config.WALLET_CRYPTO_STABLE_SIDE_CAP_PCT,
            "mid": config.WALLET_CRYPTO_MID_SIDE_CAP_PCT,
            "high": config.WALLET_CRYPTO_HIGH_SIDE_CAP_PCT,
            "stock": config.WALLET_STOCK_SIDE_CAP_PCT,
        }
        self.wallet_max_open_positions = config.WALLET_MAX_OPEN_POSITIONS
        self.wallet_stock_side_max_positions = config.WALLET_STOCK_SIDE_MAX_POSITIONS
        self.margin_equity_pct = config.MARGIN_EQUITY_PCT    # manual per-open sizing base; full cash remains available
        self.min_open_margin_pct = config.MIN_OPEN_MARGIN_PCT
        self.tier_max_adds = {"stable": config.STABLE_MAX_ADDS, "mid": config.MID_MAX_ADDS,
                              "high": config.HIGH_MAX_ADDS}   # per-σ-tier scale-in cap (hardcap mode)
        # ── 加仓策略引擎 (B 逆向): smart(σ波动闸+比例镜像+三档预算) vs hardcap(次数cap+ADD_FRAC) ──
        self.add_strategy = config.ADD_STRATEGY
        self.add_gap_k = config.ADD_GAP_K                       # 波动闸 x = k×σ
        self.pos_add_gap_k = config.POS_ADD_GAP_K               # 顺势加仓也要过价差闸,避免小碎单全跟
        self.add_shrink_g = config.ADD_GAP_SHRINK_G             # 每加一次 x×此
        self.add_max_hard = config.ADD_MAX_HARD                 # smart 硬顶
        self.follow_pos_add = config.FOLLOW_POS_ADD             # A 正向加仓开关(开=过 POS_ADD_GAP_K 才跟)
        self.tier_coin_cap = {"stable": config.STABLE_COIN_CAP_PCT, "mid": config.MID_COIN_CAP_PCT,
                              "high": config.HIGH_COIN_CAP_PCT}  # 三档单币最大保证金占用%
        self.max_entry_chase_pct = config.MAX_ENTRY_CHASE_PCT
        self.vol_fallback_sigma = config.VOL_FALLBACK_SIGMA
        # (v10: RISK_BUDGET / σ-scaled leverage removed — leverage is now the σ-tier's cap, see _sizing_for)
        self.tail_close_enable = config.TAIL_CLOSE_ENABLE
        self.tail_close_hard_remain_pct = config.TAIL_CLOSE_HARD_REMAIN_PCT
        self.tail_close_risk_remain_pct = config.TAIL_CLOSE_RISK_REMAIN_PCT
        self.tail_close_profit_giveback_pct = config.TAIL_CLOSE_PROFIT_GIVEBACK_PCT
        self.smart_tp_enable = config.SMART_TP_ENABLE
        self.smart_tp_arm_sigma = {
            "stable": config.SMART_TP_STABLE_ARM_SIGMA,
            "mid": config.SMART_TP_MID_ARM_SIGMA,
            "high": config.SMART_TP_HIGH_ARM_SIGMA,
        }
        self.smart_tp_giveback_pcts = (
            config.SMART_TP_GIVEBACK_1_PCT,
            config.SMART_TP_GIVEBACK_2_PCT,
            config.SMART_TP_GIVEBACK_3_PCT,
        )
        self.smart_tp_close_pcts = (
            config.SMART_TP_CLOSE_1_PCT,
            config.SMART_TP_CLOSE_2_PCT,
            config.SMART_TP_CLOSE_3_PCT,
        )
        self.smart_tp_tail_remain_pct = config.SMART_TP_TAIL_REMAIN_PCT
        self.smart_tp_target_reduce_exit_pct = config.SMART_TP_TARGET_REDUCE_EXIT_PCT
        self.smart_tp_min_fee_mult = config.SMART_TP_MIN_FEE_MULT
        self.vol: dict = {}              # coin -> σ (read-cache mirror of coin_vol; refreshed off hot path)
        self.vol_coins: set = set()      # coins we've encountered -> the periodic σ-refresh work set
        self.held_off: set = set()       # wallets polled ONLY because we hold a copy (off-watchlist) ->
        #                                  EXIT-ONLY: follow their reduce/close, never open a NEW position
        self.entry_frozen: set = set()   # probation Core: manage exits, but originate no open/add exposure
        self.target_acct: dict = {}      # addr -> target's account value (conviction denominator)
        self.target_sector_policy: dict = {}  # addr -> sector allow/deny policy from watchlist
        self.bbo: dict = {}              # coin -> (bid, ask) current top-of-book (any source)
        self.bbo_ms: dict = {}           # coin -> local receive time; stale cached quotes cannot execute
        self.mark_mid: dict = {}         # coin -> latest official markPx used for display/risk
        self.official_mark_ms: dict = {} # coin -> local receive time of activeAssetCtx/REST fallback
        self.mark_write_ms: dict = {}    # coin -> last DB mark write ms (throttle BBO-triggered writes)
        self.hb: dict = {}               # per-heartbeat-interval tally (fills seen / copied / skipped-by-reason);
        #                                  reset each _announce. Answers "why no trades".
        self.sub_coins: set = set()      # executable coins with WS bbo + activeAssetCtx subscriptions
        self.last_fill_ms: dict = {}     # addr -> cursor (latest processed fill time)
        self.valid_coins: set = set()    # COPYABLE universe (crypto perps + transparent builder)
        self.crypto_coins: set = set()   # standard crypto perps (these price via WS bbo)
        execution = self.db.execute(
            "SELECT selected_mode,state,active_session_id FROM execution_control WHERE id=1"
        ).fetchone()
        selected_mode = str(execution[0] or "paper") if execution else "paper"
        if selected_mode == "live":
            if not execution[2] or execution[1] not in {
                "live_canary", "live_running", "paused", "draining", "reconcile_required",
            }:
                raise RuntimeError("live_mode_without_active_session")
            self.execution_mode = "live"
            self.execution_state = str(execution[1])
            self.execution_session_id = str(execution[2])
            self.taker = Book("live", "live_copy_position", "live_copy_action", "live_copy_account")
        else:
            self.execution_mode = "paper"
            self.execution_state = "paper"
            self.execution_session_id = "paper"
            self.taker = Book("paper", "copy_position", "copy_action", "copy_account")
        self.live_executor = None
        self.live_reconcile_error = None
        self.live_reconcile_error_at = None
        self.selection_generation = None
        self.ws = None
        self.stop = False
        self._strategy_reload_lock = None
        self._strategy_bind_pending = False
        self._background_tasks: dict[str, asyncio.Task] = {}
        self._signal_tasks: set[asyncio.Task] = set()
        self._critical_background_failure: BaseException | None = None
        self._live_order_inflight = 0
        prior_state = self.db.execute(
            "SELECT state FROM process_status WHERE name='observer'"
        ).fetchone()
        prior_state = str(prior_state[0] or "") if prior_state else ""
        # Pause is operator intent, not process-local state. Preserve it across deploys/restarts so a worker
        # cannot briefly originate new positions before its command loop receives another pause command.
        self.paused = (
            prior_state in {"paused", "pausing"}
            or self.execution_state in {"paused", "draining", "reconcile_required"}
        )
        self.draining = self.execution_state == "draining"
        self._proc_state = "paused" if self.paused else "running"
        self._proc_owner = f"observer:{os.getpid()}"
        self.safety_frozen = {
            str(row[0] or "").lower()
            for row in self.db.execute(
                "SELECT addr FROM execution_wallet_safety "
                "WHERE state IN ('pending','confirmed')"
            ).fetchall()
            if row[0]
        }

    def _assert_mode_binding(self) -> None:
        """Fail closed if an out-of-band writer changes mode/session under this process."""
        row = self.db.execute(
            "SELECT selected_mode,state,active_session_id FROM execution_control WHERE id=1"
        ).fetchone()
        selected = str(row[0] or "paper") if row else "paper"
        if selected != self.execution_mode:
            raise RuntimeError("execution_mode_changed_while_observer_running")
        if self.execution_mode == "live":
            if not row or str(row[2] or "") != self.execution_session_id:
                raise RuntimeError("execution_session_changed_while_observer_running")
            if str(row[1] or "") not in {
                "live_canary", "live_running", "paused", "draining", "reconcile_required",
            }:
                raise RuntimeError("execution_state_invalid_while_observer_running")

    def _spawn_background(self, coro, name: str, *, critical: bool = False):
        """Track long-lived loops and turn an unexpected critical exit into a visible stop."""
        task = asyncio.create_task(coro, name=f"observer:{name}")
        task_key = name
        if task_key in self._background_tasks:
            task_key = f"{name}:{id(task)}"
        self._background_tasks[task_key] = task

        def _done(completed):
            if self._background_tasks.get(task_key) is completed:
                self._background_tasks.pop(task_key, None)
            if completed.cancelled() or self.stop:
                return
            try:
                exc = completed.exception()
            except asyncio.CancelledError:
                return
            if not critical and exc is None:
                return
            message = str(exc or f"{name}_exited")[:160]
            _log(f"background task {name} failed: {message}")
            if not critical:
                return
            self._critical_background_failure = exc or RuntimeError(f"{name}_exited")
            self.stop = True
            try:
                if self.execution_mode == "live":
                    stamp = now_iso()
                    self.execution_state = "reconcile_required"
                    self.paused = True
                    self.db.execute(
                        "UPDATE execution_session SET state='reconcile_required',updated_at=? "
                        "WHERE session_id=?",
                        (stamp, self.execution_session_id),
                    )
                    self.db.execute(
                        "UPDATE execution_control SET state='reconcile_required',last_error_code=?,"
                        "last_error_at=?,updated_at=? WHERE id=1 AND selected_mode='live' "
                        "AND active_session_id=?",
                        (f"TASK_{name.upper()}_FAILED", stamp, stamp, self.execution_session_id),
                    )
                    self.db.commit()
                self._write_proc_status("failed")
            except Exception as status_exc:  # noqa: BLE001 - original failure remains authoritative
                self._rollback_db()
                _log(f"failed to persist {name} task failure: {str(status_exc)[:120]}")
            self._interrupt_ws_for_stop()

        task.add_done_callback(_done)
        return task

    def _raise_critical_background_failure(self) -> None:
        """Make systemd's Restart=on-failure contract see critical loop exits.

        The main WebSocket loop previously observed ``self.stop`` and returned
        normally after a critical task crashed.  The CLI therefore exited 0 and
        systemd left Live positions unmanaged.  An intentional drain/stop does
        not set this field and still exits normally.
        """
        failure = self._critical_background_failure
        if failure is None:
            return
        raise RuntimeError(
            f"critical_background_task_failed:{type(failure).__name__}:{str(failure)[:120]}"
        ) from failure

    def _signal_row(self, signal_id: int):
        return self.db.execute(
            "SELECT signal_id,state,attempt_count,payload_json FROM execution_signal "
            "WHERE signal_id=? AND mode=? AND session_id=?",
            (int(signal_id), self.execution_mode, self.execution_session_id),
        ).fetchone()

    def _mark_signal(self, signal_id: int, state: str, *, code=None, error=None, retry=False) -> None:
        stamp = now_iso()
        if retry:
            row = self._signal_row(signal_id)
            attempts = int(row[2] or 0) if row else 1
            delay_s = min(
                float(config.LIVE_SIGNAL_RETRY_MAX_S),
                float(config.LIVE_SIGNAL_RETRY_BASE_S) * (2 ** max(0, attempts - 1)),
            )
            next_ms = now_ms() + int(delay_s * 1000)
            self.db.execute(
                "UPDATE execution_signal SET state='retryable',next_attempt_ms=?,decision_code=?,"
                "last_error=?,updated_at=?,completed_at=NULL WHERE signal_id=?",
                (next_ms, code, str(error or "")[:300] or None, stamp, int(signal_id)),
            )
        else:
            self.db.execute(
                "UPDATE execution_signal SET state=?,next_attempt_ms=0,decision_code=?,last_error=?,"
                "updated_at=?,completed_at=? WHERE signal_id=?",
                (state, code, str(error or "")[:300] or None, stamp, stamp, int(signal_id)),
            )
        self.db.commit()

    def _schedule_signal_task(self, signal_id, coro, *, name: str):
        """Run one strategy transition and durably finalize or retry its source signal."""
        if signal_id is None:
            return self._spawn_background(coro, f"event:{name}", critical=False)
        self.db.execute(
            "UPDATE execution_signal SET state='processing',attempt_count=attempt_count+1,"
            "updated_at=? WHERE signal_id=?",
            (now_iso(), int(signal_id)),
        )
        # Release the main connection's write transaction before the runner calls LiveExecutor,
        # which deliberately uses a separate connection.  Leaving this UPDATE uncommitted would
        # make Observer contend with itself and can turn every signed attempt into `database is locked`.
        self.db.commit()

        async def _runner():
            source_event_id = f"signal:{self.execution_session_id}:{int(signal_id)}"
            token = _SOURCE_EVENT_ID.set(source_event_id)
            signal = self.db.execute(
                "SELECT coin FROM execution_signal WHERE signal_id=?", (int(signal_id),),
            ).fetchone()
            recovery = self.db.execute(
                "SELECT coin FROM execution_order_intent WHERE session_id=? AND source_fill_id=? "
                "LIMIT 1",
                (self.execution_session_id, source_event_id),
            ).fetchone()
            recovery_token = _LEDGER_RECOVERY_COIN.set(
                str(signal[0]) if signal and recovery and str(signal[0]) == str(recovery[0]) else None
            )
            try:
                await coro
            except TerminalSignalError as exc:
                try:
                    self._restore_live_book_from_db()
                except Exception as restore_exc:  # noqa: BLE001 - terminal audit remains authoritative
                    self._rollback_db()
                    _log(f"signal #{signal_id} terminal state reload failed: {str(restore_exc)[:120]}")
                self._mark_signal(
                    signal_id, "failed_terminal", code="EXECUTION_TERMINAL", error=exc,
                )
                _log(f"signal #{signal_id} {name} terminal: {str(exc)[:120]}")
            except RetryableSignalError as exc:
                try:
                    self._restore_live_book_from_db()
                except Exception as restore_exc:  # noqa: BLE001 - durable inbox will retry
                    self._rollback_db()
                    _log(f"signal #{signal_id} state reload failed: {str(restore_exc)[:120]}")
                self._mark_signal(signal_id, "retryable", code="TRANSIENT", error=exc, retry=True)
            except Exception as exc:  # noqa: BLE001 - inbox owns retry and audit
                self._rollback_db()
                try:
                    self._restore_live_book_from_db()
                except Exception as restore_exc:  # noqa: BLE001 - durable inbox will retry
                    self._rollback_db()
                    _log(f"signal #{signal_id} state reload failed: {str(restore_exc)[:120]}")
                self._mark_signal(
                    signal_id, "retryable", code=type(exc).__name__, error=exc, retry=True,
                )
                _log(f"signal #{signal_id} {name} deferred: {str(exc)[:120]}")
            else:
                self._mark_signal(signal_id, "completed", code="OK")
            finally:
                _LEDGER_RECOVERY_COIN.reset(recovery_token)
                _SOURCE_EVENT_ID.reset(token)

        task = asyncio.create_task(_runner(), name=f"observer:signal:{signal_id}:{name}")
        self._signal_tasks.add(task)
        task.add_done_callback(self._signal_tasks.discard)
        return task

    def _restore_live_book_from_db(self) -> None:
        """Discard partially-mutated in-memory episode state after a failed Live transition."""
        if self.execution_mode != "live":
            return
        self._rollback_db()
        self.taker.open_ep.clear()
        self._load_account(self.taker)
        self._reload_open(self.taker)

    # -- paper account ------------------------------------------------------
    def _available(self, book=None) -> float:
        """Balance not currently tied up as isolated margin (margin scales with rem_size/size as a
        position is partially closed). Per-book; defaults to the taker book."""
        book = book or self.taker
        if book.name == "live" and book.available_balance is not None:
            return max(0.0, float(book.available_balance))
        locked = self.db.execute(
            f"SELECT COALESCE(SUM(margin * rem_size / size),0) FROM {book.pos_table} "
            "WHERE status='open' AND size>0").fetchone()[0]
        return book.balance - (locked or 0.0)

    def _load_account(self, book=None):
        book = book or self.taker
        row = self.db.execute(
            f"SELECT initial_balance,balance FROM {book.acct_table} WHERE id=1"
        ).fetchone()
        if row:
            book.initial_balance = row[0] or config.INITIAL_BALANCE
            book.balance = row[1]
        else:
            if book.name == "live":
                raise RuntimeError("live_account_projection_missing")
            self.db.execute(
                f"INSERT INTO {book.acct_table} (id,initial_balance,balance,updated_at) "
                "VALUES (1,?,?,?)",
                (config.INITIAL_BALANCE, config.INITIAL_BALANCE, now_iso()),
            )
            self.db.commit()
        # The Live ledger keeps its first funding anchor for audit, while each new Live session freezes the
        # then-current real equity as its own drawdown/sizing anchor. Dashboard Live returns are derived from
        # confirmed copy fills and open PnL so later deposits/withdrawals cannot masquerade as performance.
        book.sizing_anchor = (
            float(self.live_executor.session["sizing_anchor"])
            if book.name == "live" and self.live_executor is not None
            else float(book.initial_balance)
        )
        closed = self.db.execute(
            f"SELECT COUNT(*),COALESCE(SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END),0) "
            f"FROM {book.pos_table} WHERE status!='open'"
        ).fetchone()
        traded = self.db.execute(
            f"SELECT COALESCE(SUM(ABS(our_qty_delta*our_px)),0) FROM {book.act_table}"
        ).fetchone()[0]
        book.closed_n, book.wins_n = int(closed[0] or 0), int(closed[1] or 0)
        book.gross_traded = float(traded or 0.0)
        book.fees_cum = book.gross_traded * config.TAKER_FEE
        book.stats_loaded = True
        _log(f"account[{book.name}]: balance ${book.balance:,.2f} / available ${self._available(book):,.2f}")

    def _save_account(self, book=None):
        book = book or self.taker
        if book.name == "live":
            return
        self.db.execute(f"UPDATE {book.acct_table} SET balance=?, updated_at=? WHERE id=1",
                        (book.balance, now_iso()))

    def _book_unrealized(self, book=None) -> float:
        book = book or self.taker
        total = 0.0
        for (_addr, coin), ep in book.open_ep.items():
            if not ep.get("rem_size"):
                continue
            entry = ep.get("entry_px") or 0.0
            mark = self._mark_px(coin, entry)
            sign = 1 if ep.get("side") == "long" else -1
            total += ep["rem_size"] * (mark - entry) * sign
        return total

    def _risk_equity(self, book=None) -> float:
        book = book or self.taker
        if book.name == "live":
            return max(0.0, book.balance)
        return max(0.0, book.balance + min(0.0, self._book_unrealized(book)))

    def _risk_available(self, book=None) -> float:
        book = book or self.taker
        if book.name == "live":
            return max(0.0, self._available(book))
        return max(0.0, self._available(book) + min(0.0, self._book_unrealized(book)))

    def _sync_live_account(self) -> None:
        if self.execution_mode != "live" or self.live_executor is None:
            return
        self.taker.balance = max(0.0, float(self.live_executor.equity))
        self.taker.available_balance = max(0.0, float(self.live_executor.available))
        self.db.execute(
            "UPDATE live_copy_account SET balance=?,available=?,equity_projection_version=2,"
            "updated_at=? WHERE id=1",
            (self.taker.balance, self.taker.available_balance, now_iso()),
        )
        self.db.commit()

    def _settle_forced_liquidations(self) -> int:
        """Apply explicit self-account liquidation fills to the Live ledger.

        Hyperliquid liquidation fills have no strategy CLOID. Attribution is
        therefore limited to the exact same-side ledger discrepancy proven by
        the latest REST account projection. Unrelated manual fills remain
        unmatched and fail closed through normal reconciliation.
        """
        if self.execution_mode != "live" or self.live_executor is None:
            return 0
        exchange_sizes = {
            str(coin): float(size or 0.0)
            for coin, size in self.db.execute(
                "SELECT coin,COALESCE(SUM(signed_size),0) FROM execution_position_projection "
                "WHERE session_id=? GROUP BY coin",
                (self.execution_session_id,),
            ).fetchall()
        }
        rows = self.db.execute(
            "SELECT f.tid,f.coin,f.side,f.size,f.px,f.fee,f.closed_pnl,f.fill_time_ms,f.raw_json "
            "FROM execution_fill f WHERE f.session_id=? AND NOT EXISTS ("
            "SELECT 1 FROM execution_order_intent i WHERE i.session_id=f.session_id AND ("
            "(f.cloid IS NOT NULL AND lower(i.cloid)=lower(f.cloid)) OR "
            "(f.oid IS NOT NULL AND i.oid=f.oid))) ORDER BY f.fill_time_ms,f.tid",
            (self.execution_session_id,),
        ).fetchall()
        settled = 0
        for tid, coin, side, fill_size, px, fee, closed_pnl, fill_time_ms, raw_json in rows:
            if not LiveExecutor._is_forced_liquidation(raw_json):
                continue
            side = str(side or "").upper()
            fill_sign = 1.0 if side == "B" else -1.0 if side == "A" else 0.0
            if not fill_sign:
                continue
            candidates = [
                (addr, ep) for (addr, ep_coin), ep in self.taker.open_ep.items()
                if ep_coin == str(coin)
                and ((fill_sign > 0 and ep.get("side") == "short")
                     or (fill_sign < 0 and ep.get("side") == "long"))
                and int(fill_time_ms or 0) >= int(ep.get("master_open_ms") or 0)
                and float(ep.get("rem_size") or 0.0) > config.FLAT
            ]
            if not candidates:
                continue
            ledger_signed = sum(
                float(ep.get("rem_size") or 0.0) * float(ep.get("sign") or 0.0)
                for (_addr, ep_coin), ep in self.taker.open_ep.items()
                if ep_coin == str(coin)
            )
            correction = exchange_sizes.get(str(coin), 0.0) - ledger_signed
            if correction * fill_sign <= 1e-8:
                continue
            candidate_total = sum(float(ep.get("rem_size") or 0.0) for _addr, ep in candidates)
            apply_total = min(abs(correction), abs(float(fill_size or 0.0)), candidate_total)
            if apply_total <= config.FLAT:
                continue
            net_pnl = (float(closed_pnl or 0.0) - abs(float(fee or 0.0))) * (
                apply_total / max(abs(float(fill_size or 0.0)), 1e-12)
            )
            remaining = apply_total
            pnl_remaining = net_pnl
            for index, (addr, ep) in enumerate(candidates):
                if remaining <= config.FLAT:
                    break
                old_rem = float(ep.get("rem_size") or 0.0)
                if index + 1 == len(candidates):
                    close_size = min(old_rem, remaining)
                    allocated_pnl = pnl_remaining
                else:
                    close_size = min(old_rem, apply_total * old_rem / max(candidate_total, 1e-12))
                    allocated_pnl = net_pnl * close_size / max(apply_total, 1e-12)
                if close_size <= config.FLAT:
                    continue
                remaining -= close_size
                pnl_remaining -= allocated_pnl
                ep["rem_size"] = max(0.0, old_rem - close_size)
                ep["realized_pnl"] = float(ep.get("realized_pnl") or 0.0) + allocated_pnl
                closing = ep["rem_size"] <= config.FLAT
                if not closing:
                    basis = rebase_isolated_position(
                        ep["entry_px"], ep["side"], ep["rem_size"], ep["leverage"],
                        ep.get("maintenance_leverage"),
                    )
                    ep.update(
                        size=basis["size"], margin=basis["margin"],
                        notional=basis["notional"], liq_px=basis["liq_px"],
                    )
                self._record_action(
                    ep, addr, str(coin), int(fill_time_ms or now_ms()),
                    "close" if closing else "reduce", None, float(px or 0.0), 0.0,
                    float(ep.get("master_current") or 0.0),
                    -close_size * float(ep.get("sign") or 0.0), float(px or 0.0),
                    allocated_pnl, 0.0, book=self.taker,
                )
                self.db.execute(
                    "UPDATE live_copy_position SET size=?,rem_size=?,margin=?,notional=?,liq_px=?,"
                    "realized_pnl=?,was_liq=1,status=?,closed_at=?,mark_px=?,unrealized_pnl=? "
                    "WHERE pos_id=? AND status='open'",
                    (
                        ep.get("size"), ep["rem_size"], ep.get("margin"), ep.get("notional"),
                        ep.get("liq_px"), ep["realized_pnl"], "liquidated" if closing else "open",
                        now_iso() if closing else None, float(px or 0.0), 0.0 if closing else None,
                        ep["pos_id"],
                    ),
                )
                ep["was_liq"] = 1
                if closing:
                    if self.taker.stats_loaded:
                        self.taker.closed_n += 1
                        self.taker.wins_n += 1 if ep["realized_pnl"] > 0 else 0
                    self.taker.open_ep.pop((addr, str(coin)), None)
                    self._resolve_draining_intent(addr)
                settled += 1
                _log(
                    f"[live] EXCHANGE-LIQUIDATION {addr[:10]} {coin} {ep['side']} "
                    f"qty={close_size:g} tid={str(tid)[:16]} pnl=${allocated_pnl:+,.2f}"
                )
            self.db.commit()
        if settled:
            self._sync_live_account()
            self._finish_live_session_if_drained()
        return settled

    def _verify_live_ledger_projection(self) -> bool:
        """Require the managed Live ledger to net to the exchange projection.

        The execution adapter can prove that every exchange position came from
        one of our durable order intents.  This second projection proves that
        those confirmed fills were also applied to ``live_copy_position``.  A
        crash between the two commits is therefore visible and blocks increases
        instead of leaving an unmanaged real position behind.
        """
        if self.execution_mode != "live":
            return True
        ledger = {
            str(coin): float(size or 0.0)
            for coin, size in self.db.execute(
                "SELECT coin,COALESCE(SUM(CASE WHEN side='long' THEN rem_size ELSE -rem_size END),0) "
                "FROM live_copy_position WHERE status='open' GROUP BY coin"
            ).fetchall()
        }
        exchange = {
            str(coin): float(size or 0.0)
            for coin, size in self.db.execute(
                "SELECT coin,COALESCE(SUM(signed_size),0) FROM execution_position_projection "
                "WHERE session_id=? GROUP BY coin",
                (self.execution_session_id,),
            ).fetchall()
        }
        drift = []
        for coin in sorted(set(ledger) | set(exchange)):
            expected = ledger.get(coin, 0.0)
            actual = exchange.get(coin, 0.0)
            tolerance = max(1e-8, abs(actual) * 1e-7)
            if abs(expected - actual) > tolerance:
                drift.append(coin)
        if not drift:
            return True
        # Ignore only the exact interval in which LiveExecutor has confirmed a managed order but the awaiting
        # coroutine has not resumed to commit its ledger transition.  A durable signal recovering an existing
        # intent may also repair drift on that signal's coin; unrelated drift still fails closed.
        recovery_coin = _LEDGER_RECOVERY_COIN.get()
        if self._live_order_inflight > 0 or (
            recovery_coin is not None and set(drift) == {recovery_coin}
        ):
            return True
        stamp = now_iso()
        self.paused = True
        self.execution_state = "reconcile_required"
        self.live_reconcile_error = "LIVE_LEDGER_PROJECTION_DRIFT"
        self.live_reconcile_error_at = stamp
        self.db.execute(
            "UPDATE execution_session SET state='reconcile_required',updated_at=? WHERE session_id=?",
            (stamp, self.execution_session_id),
        )
        self.db.execute(
            "UPDATE execution_control SET state='reconcile_required',last_error_code=?,last_error_at=?,"
            "updated_at=? WHERE id=1",
            ("LIVE_LEDGER_PROJECTION_DRIFT", stamp, stamp),
        )
        self.db.commit()
        _log(f"live ledger projection drift on {len(drift)} coin(s); exposure increases frozen")
        return False

    @staticmethod
    def _is_db_contention(exc: Exception) -> bool:
        return isinstance(exc, sqlite3.OperationalError) and any(
            token in str(exc).lower() for token in ("locked", "busy")
        )

    async def _reconcile_live_with_retry(self, *, attempts: int, retry_all: bool) -> dict:
        """Reconcile exchange truth and mirror it to the Observer book as one recoverable operation."""
        for attempt in range(max(1, int(attempts))):
            try:
                result = await asyncio.to_thread(self.live_executor.reconcile)
                self._sync_live_account()
                return result
            except Exception as exc:  # noqa: BLE001 - caller decides whether the terminal error pauses work
                self.live_executor.rollback_after_error()
                self._rollback_db()
                if attempt + 1 >= attempts or (not retry_all and not self._is_db_contention(exc)):
                    raise
                await asyncio.sleep(0.1 * (2 ** attempt))
        raise RuntimeError("live_reconcile_retry_exhausted")

    async def _refresh_live_sizing_state(self) -> None:
        """Refresh authoritative Mainnet equity before calculating any exposure increase."""
        if self.execution_mode != "live" or self.live_executor is None:
            return
        try:
            result = await self._reconcile_live_with_retry(attempts=4, retry_all=True)
        except Exception as exc:
            self.live_reconcile_error = str(exc)[:120]
            self.live_reconcile_error_at = now_iso()
            raise
        self.live_reconcile_error = None
        self.live_reconcile_error_at = None
        self._settle_forced_liquidations()
        if result.get("ok") and not self._verify_live_ledger_projection():
            raise RuntimeError("live_ledger_projection_drift")
        if not result.get("ok"):
            self.paused = True
            self.execution_state = "reconcile_required"
            self._write_proc_status("paused")
            raise RuntimeError("live_reconcile_required")

    def _open_live_executor_db(self):
        """Open a connection never shared with Observer signal/command coroutines.

        Live reconciliation runs in worker threads. Sharing ``self.db`` lets a worker commit or roll back the
        Observer's transaction, which caused false lease loss and a hidden permanent pause in production.
        File-backed databases therefore receive an independent WAL connection. In-memory unit-test databases
        retain the supplied connection because they cannot be reopened as the same database.
        """
        if not self.db_path or self.db_path == ":memory:":
            return self.db
        db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        # This connection is used only from worker threads, so a normal SQLite writer wait does not freeze
        # target polling.  Reusing the Observer event-loop connection's 1.5s timeout made harmless cursor and
        # mark commits surface as repeated Live reconciliation failures under an otherwise healthy WAL.
        db.execute(f"PRAGMA busy_timeout={int(config.LIVE_EXECUTOR_DB_BUSY_TIMEOUT_MS)}")
        return db

    def _refresh_vol_worker(self, coin: str, asset_ctx=None):
        """Refresh volatility without lending Observer's transaction to a worker thread."""
        if not self.db_path or self.db_path == ":memory:":
            return volatility.refresh(self.db, coin, asset_ctx)
        db = sqlite3.connect(self.db_path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        try:
            return volatility.refresh(db, coin, asset_ctx)
        finally:
            db.close()

    def _target_snapshot(self, addr, coin):
        """Return the source's current margin, entry and leverage for episode audit.

        Source leverage is display/audit metadata only. It must never enter our sizing calculation: our
        versioned tier leverage remains the sole strategy input, matching historical replay where fills do not
        reliably carry source leverage.
        """
        dex = coin.split(":")[0] if ":" in coin else None
        cs = rest.clearinghouse_state(addr, dex)
        margin = entry = leverage = None
        if isinstance(cs, dict):
            for ap in cs.get("assetPositions", []):
                pos = ap.get("position", {})
                if pos.get("coin") == coin:
                    entry = f(pos.get("entryPx"))
                    margin = f(pos.get("marginUsed"))
                    leverage_value = pos.get("leverage")
                    if isinstance(leverage_value, dict):
                        leverage_value = leverage_value.get("value")
                    leverage = f(leverage_value) or None
                    break
        return margin, entry, leverage

    def _copyable(self, coin: str) -> bool:
        """A coin we can copy + price: crypto perp, or transparent builder perp (stock/commodity).
        Opaque/unknown names are skipped (and subscribing their bbo would close the WS anyway)."""
        return bool(coin) and (not self.valid_coins or coin in self.valid_coins)

    def _sector_allowed(self, addr: str, coin: str) -> bool:
        # Published targets must carry an explicit immutable sector policy.  Missing/corrupt context is
        # not permission to trade every sector.
        return policy_allows_coin(self.target_sector_policy.get((addr or "").lower()), coin, default=False)

    def _manual_close_cooldown_until(self, addr: str, coin: str):
        """Return the active manual-close cooldown expiry for wallet+coin, or None.

        A loss-cutting manual flatten is an operator risk override: stay out of that wallet+coin for a full
        day even if the master adds, flips, or reopens. Profitable manual exits do not create this row.
        """
        addr = (addr or "").lower()
        if not addr or not coin:
            return None
        row = self.db.execute(
            "SELECT expires_at FROM execution_manual_close_cooldown "
            "WHERE mode=? AND addr=? AND lower(coin)=lower(?) "
            "AND reason IN ('manual_close','manual_stop_loss')",
            (self.execution_mode, addr, coin),
        ).fetchone()
        if not row:
            return None
        expires_at = row[0]
        if expires_at > now_iso():
            return expires_at
        self.db.execute(
            "DELETE FROM execution_manual_close_cooldown "
            "WHERE mode=? AND addr=? AND lower(coin)=lower(?) "
            "AND reason IN ('manual_close','manual_stop_loss')",
            (self.execution_mode, addr, coin),
        )
        self.db.commit()
        return None

    def _clear_manual_close_cooldown(self, addr: str, coin: str):
        self.db.execute(
            "DELETE FROM execution_manual_close_cooldown "
            "WHERE mode=? AND addr=? AND lower(coin)=lower(?) "
            "AND reason IN ('manual_close','manual_stop_loss')",
            (self.execution_mode, (addr or "").lower(), coin),
        )
        self.db.commit()

    def _prune_legacy_profit_cooldowns(self):
        """Remove cooldowns written by the old all-manual-full-closes policy for non-losing exits."""
        cur = self.db.execute(
            "DELETE FROM execution_manual_close_cooldown WHERE mode=? AND reason='manual_close' AND EXISTS ("
            f"SELECT 1 FROM {self.taker.pos_table} p "
            "WHERE p.pos_id=execution_manual_close_cooldown.pos_id "
            "AND COALESCE(p.realized_pnl,0)>=0)",
            (self.execution_mode,),
        )
        self.db.commit()
        if cur.rowcount:
            _log(f"cleared {cur.rowcount} legacy profitable manual-close cooldowns")

    def _add_manual_close_cooldown(self, addr: str, coin: str, pos_id: int):
        addr = (addr or "").lower()
        created_at = now_iso()
        expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + MANUAL_CLOSE_COOLDOWN_S))
        self.db.execute(
            "INSERT INTO execution_manual_close_cooldown "
            "(mode,addr,coin,pos_id,reason,created_at,expires_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(mode,addr,coin) DO UPDATE SET "
            "pos_id=excluded.pos_id,reason=excluded.reason,created_at=excluded.created_at,"
            "expires_at=excluded.expires_at",
            (self.execution_mode, addr, coin, pos_id, "manual_stop_loss", created_at, expires_at),
        )
        self.db.commit()
        return expires_at

    def _new_exposure_block_reason(self, addr: str, coin: str, book=None, side=None):
        book = book or self.taker
        addr = str(addr or "").lower()
        if addr in self.safety_frozen:
            return "wallet_safety_frozen"
        risk = self.db.execute(
            "SELECT COALESCE(risk_level,'normal'),risk_block_reason "
            "FROM wallet_registry WHERE lower(addr)=lower(?)",
            (addr,),
        ).fetchone()
        if risk and (risk[0] in {wallet_risk.HIGH, wallet_risk.UNAVAILABLE} or risk[1]):
            return "wallet_risk_blocked"
        if addr in self.entry_frozen:
            return "retention_probation"
        if side and any(
            str(position.get("coin") or "") == str(coin)
            and str(position.get("side") or "") != str(side)
            and f(position.get("rem_size")) > config.FLAT
            for position in book.open_ep.values()
        ):
            # Hyperliquid maintains one net position per account/market, not
            # independent hedge-mode legs. Use deterministic first-direction-
            # wins semantics until the existing copied direction is flat.
            return "coin_direction_conflict"
        wallet_open_n = sum(
            1 for position in book.open_ep.values()
            if str(position.get("addr") or "").lower() == addr
        )
        if wallet_open_n >= self.wallet_max_open_positions:
            return "wallet_position_cap"
        if side and str(coin).lower().startswith("xyz:") and wallet_sector_side_position_count(
            book.open_ep.values(), addr=addr, coin=coin, side=side,
        ) >= self.wallet_stock_side_max_positions:
            return "wallet_stock_side_position_cap"
        return None

    def _refresh_live_wallet_risks(self, addrs=None):
        """Refresh actual-copy risk independently of scanner publication."""
        if addrs is None:
            owners = {
                str(row[0] or "").lower()
                for row in self.db.execute(
                    f"SELECT DISTINCT addr FROM {self.taker.pos_table} WHERE status='open'"
                ).fetchall()
                if row[0]
            }
            generation = selection.latest_published_generation(self.db)
            if generation:
                owners |= {
                    str(row[0] or "").lower()
                    for row in self.db.execute(
                        "SELECT addr FROM follow_selection WHERE generation=? "
                        "AND lower(role)='core' AND COALESCE(enabled,1)=1",
                        (generation,),
                    ).fetchall()
                    if row[0]
                }
        else:
            owners = {
                str(addr or "").lower() for addr in addrs if str(addr or "").strip()
            }
        if not owners:
            return {}
        stamp = now_iso()
        live_generation = f"live-{stamp[:10]}"
        results = {}
        for addr in sorted(owners):
            previous = wallet_risk.registry_state(self.db, addr)
            assessment, evidence = wallet_risk.assess_actual_copy(
                self.db,
                generation=live_generation,
                addr=addr,
                source="observer_live",
                assessed_at=stamp,
                complete=True,
                min_confirmation_hours=config.CORE_RETENTION_MIN_CONFIRMATION_HOURS,
                cumulative_low_loss_pct=config.ACTUAL_COPY_LOW_RISK_LOSS_PCT,
                cumulative_medium_loss_pct=config.ACTUAL_COPY_MEDIUM_RISK_LOSS_PCT,
                cumulative_high_loss_pct=config.COPY_CATASTROPHIC_LIQUIDATION_LOSS_PCT,
                position_table=self.taker.pos_table,
            )
            results[addr] = assessment
            if (
                assessment.level != previous["level"]
                or assessment.reasons != previous["reasons"]
            ):
                reason = assessment.reasons[0] if assessment.reasons else "healthy"
                _log(
                    f"wallet risk {addr[:10]}: {previous['level']} → "
                    f"{assessment.level} ({reason}, "
                    f"30d={float(evidence.get('cumulativeLossPct30d') or 0.0):.2%})"
                )
        self.db.commit()
        return results

    @staticmethod
    def _target_self_liquidation(addr: str, fill: dict) -> bool:
        liquidation = fill.get("liquidation")
        return bool(
            isinstance(liquidation, dict)
            and str(liquidation.get("liquidatedUser") or "").lower()
            == str(addr or "").lower()
        )

    def _set_wallet_safety(
        self, addr: str, state: str, *, event_key=None, occurred_at=None,
        reason=None, evidence=None,
    ):
        addr = str(addr or "").lower()
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO execution_wallet_safety "
            "(addr,state,event_key,occurred_at,reason,evidence_json,first_seen_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(addr) DO UPDATE SET "
            "state=excluded.state,event_key=COALESCE(excluded.event_key,execution_wallet_safety.event_key),"
            "occurred_at=COALESCE(excluded.occurred_at,execution_wallet_safety.occurred_at),"
            "reason=excluded.reason,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at",
            (
                addr, state, str(event_key) if event_key is not None else None,
                int(occurred_at) if occurred_at is not None else None,
                reason, json.dumps(evidence or {}, sort_keys=True, separators=(",", ":")),
                stamp, stamp,
            ),
        )
        if state in {"pending", "confirmed"}:
            self.safety_frozen.add(addr)
        else:
            self.safety_frozen.discard(addr)
        self.db.commit()

    async def _confirm_wallet_safety(self, addr: str, coin: str = None):
        """Confirm target self-liquidation without turning API failure into a blacklist."""
        addr = str(addr or "").lower()
        dexes = [None]
        if coin and ":" in coin:
            dexes.append(coin.split(":", 1)[0])
        states = []
        try:
            for dex in dexes:
                state = await asyncio.to_thread(rest.clearinghouse_state, addr, dex)
                if not isinstance(state, dict):
                    return False
                states.append(state)
        except Exception:  # noqa: BLE001 - pending remains fail-closed and is retried
            self._rollback_db()
            return False
        equity = sum(
            max(0.0, f((state.get("marginSummary") or {}).get("accountValue")))
            for state in states
        )
        positions = [
            position
            for state in states
            for position in (state.get("assetPositions") or ())
            if abs(f((position.get("position") or {}).get("szi"))) >= config.FLAT
        ]
        if equity > 1e-9 or positions:
            self._set_wallet_safety(
                addr, "cleared", reason="source_liquidation_not_zero",
                evidence={"equity": equity, "positionCount": len(positions)},
            )
            return True

        row = self.db.execute(
            "SELECT event_key,occurred_at FROM execution_wallet_safety WHERE addr=?",
            (addr,),
        ).fetchone()
        event_key = row[0] if row else f"observer:{now_ms()}"
        occurred_at = row[1] if row else now_ms()
        self._set_wallet_safety(
            addr, "confirmed", event_key=event_key, occurred_at=occurred_at,
            reason="source_account_liquidated_zero",
            evidence={"equity": equity, "positionCount": 0},
        )
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO wallet_risk_event "
            "(addr,event_type,event_key,occurred_at,evidence_json,first_seen_at,last_seen_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(addr,event_type,event_key) DO UPDATE SET "
            "last_seen_at=excluded.last_seen_at,evidence_json=excluded.evidence_json",
            (
                addr, "source_account_liquidated_zero", str(event_key), int(occurred_at),
                json.dumps({"confirmedBy": "observer", "equity": equity, "positionCount": 0},
                           sort_keys=True, separators=(",", ":")),
                stamp, stamp,
            ),
        )
        self.db.commit()
        await self._reconcile_open()
        _log(f"SAFETY-FROZEN {addr[:10]} confirmed source liquidation + zero perp equity")
        return True

    async def wallet_safety_retry_loop(self):
        while not self.stop:
            active = {
                str(row[0] or "").lower()
                for row in self.db.execute(
                    "SELECT addr FROM execution_wallet_safety "
                    "WHERE state IN ('pending','confirmed')"
                ).fetchall()
                if row[0]
            }
            self.safety_frozen = active
            rows = self.db.execute(
                "SELECT addr,evidence_json FROM execution_wallet_safety WHERE state='pending'"
            ).fetchall()
            for addr, evidence_json in rows:
                try:
                    evidence = json.loads(evidence_json or "{}")
                except (TypeError, ValueError):
                    evidence = {}
                await self._confirm_wallet_safety(addr, evidence.get("coin"))
            await asyncio.sleep(60)

    def _wallet_group_cap_pct(self, book, addr, coin, side, tier, *, exclude=None):
        positions = (position for position in book.open_ep.values() if position is not exclude)
        return wallet_sector_side_effective_cap_pct(
            positions, addr=addr, coin=coin, side=side, candidate_tier=tier,
            tier_for_coin=lambda current_coin: tier_for_sigma(
                self._sigma(current_coin), self.high_sigma_min, current_coin,
            ),
            crypto_stable=self.wallet_sector_side_caps["stable"],
            crypto_mid=self.wallet_sector_side_caps["mid"],
            crypto_high=self.wallet_sector_side_caps["high"],
            stock=self.wallet_sector_side_caps["stock"],
        )

    # -- pricing off the live book -------------------------------------------
    def _fill_px(self, coin, is_buy, fallback):
        ba = self.bbo.get(coin)
        age = now_ms() - int(self.bbo_ms.get(coin) or 0)
        if (not ba or not ba[0] or not ba[1]
                or age > int(getattr(config, "EXECUTION_QUOTE_MAX_AGE_MS", 5_000))):
            return fallback                       # book not ready -> master px (slippage ~0 anyway)
        bid, ask = ba
        return ask if is_buy else bid             # honest taker catch-up across the spread, CURRENT book

    async def _execution_px(self, coin, is_buy, fallback):
        """Return the public WS execution quote or the target fill as a safe fallback.

        This path plans Paper/Live intent only.  LiveExecutor always requests and
        validates a fresh L2 book immediately before submitting a real order, so
        the Observer must not duplicate that REST call merely because WS is stale.
        """
        cached = self._fill_px(coin, is_buy, None)
        if cached:
            return cached
        return fallback

    def _mark_px(self, coin: str, fallback=None):
        mid = self.mark_mid.get(coin)
        mark_stamp = int(self.official_mark_ms.get(coin) or 0)
        mark_fresh = not mark_stamp or now_ms() - mark_stamp <= config.AUTHORITATIVE_MARK_WS_STALE_MS
        if mid and mid > 0 and mark_fresh:
            return mid
        ba = self.bbo.get(coin)
        if ba and ba[0] and ba[1]:
            return (ba[0] + ba[1]) / 2
        return fallback

    def _tally(self, key, book=None):
        """Count one heartbeat event for the diagnostic rollup without per-fill log growth."""
        self.hb[key] = self.hb.get(key, 0) + 1

    def _record_live_policy_skip(self, addr, coin, action, reason, stamp_ms):
        """Persist a bounded live-only policy decision without changing replay qualification."""
        stamp_ms = int(stamp_ms or now_ms())
        day = time.strftime("%Y-%m-%d", time.gmtime(stamp_ms / 1000.0))
        try:
            self.db.execute(
                "INSERT INTO live_policy_skip "
                "(day,addr,coin,action,reason,count,first_ms,last_ms,strategy_revision_id) "
                "VALUES (?,?,?,?,?,1,?,?,?) "
                "ON CONFLICT(day,addr,coin,action,reason) DO UPDATE SET "
                "count=live_policy_skip.count+1,last_ms=excluded.last_ms,"
                "strategy_revision_id=excluded.strategy_revision_id",
                (
                    day, str(addr or "").lower(), str(coin or ""), str(action or ""),
                    str(reason or ""), stamp_ms, stamp_ms, self.strategy_revision_id,
                ),
            )
        except Exception:  # noqa: BLE001 - audit telemetry must never interrupt execution safety
            pass

    # -- restart recovery: reload open copies from db ------------------------
    def _reload_params(self, follow_values=None):
        """Refresh UI-tunable strategy params from the params table (engine units; config = fallback).
        Called at startup + each watchlist reload so dashboard edits take effect on the NEXT new copy.
        Fully defensive: any failure keeps the current values (never disrupts the live engine)."""
        try:
            from hyper import params as P
            f = dict(follow_values) if follow_values is not None else P.load_follow(self.db)
            if f.get("COIN_BLACKLIST") is not None: self.coin_blacklist = parse_coin_blacklist(f["COIN_BLACKLIST"])
            if f.get("BLOCK_KOREAN_STOCKS") is not None: self.block_korean_stocks = bool(f["BLOCK_KOREAN_STOCKS"])
            if f.get("LOW_LIQUIDITY_FILTER_ENABLE") is not None: self.low_liquidity_filter_enable = bool(f["LOW_LIQUIDITY_FILTER_ENABLE"])
            if f.get("LIVE_BOOK_MAX_SPREAD_BPS") is not None: self.live_book_max_spread_bps = f["LIVE_BOOK_MAX_SPREAD_BPS"]
            if f.get("LIVE_BOOK_MAX_IMPACT_BPS") is not None: self.live_book_max_impact_bps = f["LIVE_BOOK_MAX_IMPACT_BPS"]
            if f.get("MIN_COIN_DAY_NTL_VLM") is not None: self.min_coin_day_ntl_vlm = f["MIN_COIN_DAY_NTL_VLM"]
            if f.get("MIN_COIN_OI_NOTIONAL") is not None: self.min_coin_oi_notional = f["MIN_COIN_OI_NOTIONAL"]
            if f.get("MAX_TARGETS"): self.top_n = int(f["MAX_TARGETS"])
            # (v10: FOLLOW_MIN_TRADES/FOLLOW_MIN_ACTIVE_DAYS dropped — evidence enforced once at profile time)
            if f.get("ADD_FRAC") is not None: self.add_frac = f["ADD_FRAC"]
            if f.get("MAX_LEV"): self.max_lev = f["MAX_LEV"]
            if f.get("MIN_LEV"): self.min_lev = f["MIN_LEV"]
            if f.get("MARGIN_EQUITY_PCT") is not None: self.margin_equity_pct = f["MARGIN_EQUITY_PCT"]
            if f.get("HIGH_SIGMA_MIN") is not None: self.high_sigma_min = f["HIGH_SIGMA_MIN"]
            for tier, mk, lk, ak in (
                ("stable", "STABLE_MARGIN_PCT", "STABLE_LEV_CAP", "STABLE_MAX_ADDS"),
                ("mid", "MID_MARGIN_PCT", "MID_LEV_CAP", "MID_MAX_ADDS"),
                ("high", "HIGH_MARGIN_PCT", "HIGH_LEV_CAP", "HIGH_MAX_ADDS"),
            ):
                if f.get(mk) is not None: self.tier_margin[tier] = f[mk]
                if f.get(lk): self.tier_lev_cap[tier] = f[lk]
                if f.get(ak) is not None: self.tier_max_adds[tier] = int(f[ak])
            if f.get("MIN_OPEN_MARGIN_PCT") is not None: self.min_open_margin_pct = f["MIN_OPEN_MARGIN_PCT"]
            if f.get("SMART_ADD") is not None: self.add_strategy = "smart" if f["SMART_ADD"] else "hardcap"
            if f.get("ADD_GAP_K") is not None: self.add_gap_k = f["ADD_GAP_K"]
            if f.get("POS_ADD_GAP_K") is not None: self.pos_add_gap_k = f["POS_ADD_GAP_K"]
            if f.get("ADD_GAP_SHRINK_G"): self.add_shrink_g = f["ADD_GAP_SHRINK_G"]
            if f.get("ADD_MAX_HARD") is not None: self.add_max_hard = int(f["ADD_MAX_HARD"])
            if f.get("FOLLOW_POS_ADD") is not None: self.follow_pos_add = bool(f["FOLLOW_POS_ADD"])
            for tier, ck in (("stable", "STABLE_COIN_CAP_PCT"), ("mid", "MID_COIN_CAP_PCT"), ("high", "HIGH_COIN_CAP_PCT")):
                if f.get(ck) is not None: self.tier_coin_cap[tier] = f[ck]
            self.max_entry_chase_pct = f.get("MAX_ENTRY_CHASE_PCT")     # None = chase guard off
            if f.get("VOL_FALLBACK_SIGMA"): self.vol_fallback_sigma = f["VOL_FALLBACK_SIGMA"]
            # (v10: RISK_BUDGET removed — leverage = σ-tier cap)
            if f.get("TAIL_CLOSE_ENABLE") is not None: self.tail_close_enable = bool(f["TAIL_CLOSE_ENABLE"])
            if f.get("TAIL_CLOSE_HARD_REMAIN_PCT") is not None: self.tail_close_hard_remain_pct = f["TAIL_CLOSE_HARD_REMAIN_PCT"]
            if f.get("TAIL_CLOSE_RISK_REMAIN_PCT") is not None: self.tail_close_risk_remain_pct = f["TAIL_CLOSE_RISK_REMAIN_PCT"]
            if f.get("TAIL_CLOSE_PROFIT_GIVEBACK_PCT") is not None: self.tail_close_profit_giveback_pct = f["TAIL_CLOSE_PROFIT_GIVEBACK_PCT"]
            if f.get("SMART_TP_ENABLE") is not None: self.smart_tp_enable = bool(f["SMART_TP_ENABLE"])
            for tier, key in (("stable", "SMART_TP_STABLE_ARM_SIGMA"),
                              ("mid", "SMART_TP_MID_ARM_SIGMA"),
                              ("high", "SMART_TP_HIGH_ARM_SIGMA")):
                if f.get(key) is not None: self.smart_tp_arm_sigma[tier] = f[key]
            self.smart_tp_giveback_pcts = tuple(
                f.get(key, current) for key, current in zip(
                    ("SMART_TP_GIVEBACK_1_PCT", "SMART_TP_GIVEBACK_2_PCT", "SMART_TP_GIVEBACK_3_PCT"),
                    self.smart_tp_giveback_pcts,
                )
            )
            self.smart_tp_close_pcts = tuple(
                f.get(key, current) for key, current in zip(
                    ("SMART_TP_CLOSE_1_PCT", "SMART_TP_CLOSE_2_PCT", "SMART_TP_CLOSE_3_PCT"),
                    self.smart_tp_close_pcts,
                )
            )
            if f.get("SMART_TP_TAIL_REMAIN_PCT") is not None: self.smart_tp_tail_remain_pct = f["SMART_TP_TAIL_REMAIN_PCT"]
            if f.get("SMART_TP_TARGET_REDUCE_EXIT_PCT") is not None: self.smart_tp_target_reduce_exit_pct = f["SMART_TP_TARGET_REDUCE_EXIT_PCT"]
            if f.get("SMART_TP_MIN_FEE_MULT") is not None: self.smart_tp_min_fee_mult = f["SMART_TP_MIN_FEE_MULT"]
            if not self.smart_tp_enable:
                self._clear_smart_take_profit_state()
        except Exception as exc:  # noqa: BLE001
            _log(f"param reload failed (keeping current): {exc}")

    def _clear_smart_take_profit_state(self):
        """Disabling the strategy starts any later re-enable from a fresh live high-water."""
        for ep in self.open_ep.values():
            ep.update(
                smart_tp_armed=False,
                smart_tp_stage=0,
                smart_tp_peak_pnl=0.0,
                smart_tp_base_size=0.0,
                smart_tp_master_anchor=0.0,
            )
        self.db.execute(
            f"UPDATE {self.taker.pos_table} SET smart_tp_armed=0,smart_tp_stage=0,smart_tp_peak_pnl=0,"
            "smart_tp_base_size=NULL,smart_tp_master_anchor=NULL WHERE status='open'"
        )
        self.db.commit()

    def _reload_open(self, book=None):
        book = book or self.taker
        if book is self.taker:
            self._prune_legacy_profit_cooldowns()
        rows = self.db.execute(
            "SELECT pos_id,addr,coin,side,master_open_ms,master_open_px,master_peak_sz,leverage,"
            "margin,notional,entry_px,size,rem_size,peak_size,liq_px,realized_pnl,add_count,mae_pct,num_actions,"
            "master_margin,master_leverage,master_open_notional,master_current_sz,smart_tp_armed,smart_tp_stage,"
            f"smart_tp_peak_pnl,smart_tp_base_size,smart_tp_master_anchor FROM {book.pos_table} "
            "WHERE status='open'").fetchall()
        loaded = 0
        closed_dust = 0
        reconstructed_peaks = []
        rebased_positions = []
        maintenance_by_coin = {
            coin: max_leverage
            for coin, max_leverage in self.db.execute(
                "SELECT coin,max_leverage FROM coin_vol"
            ).fetchall()
        }
        for r in rows:
            (pid, addr, coin, side, mo, mpx, peak, lev, mgn, notl, epx, sz, rem, peak_sz, liq, rpnl, adds, mae, na,
             m_mgn, m_lev, master_open_notional, master_current, smart_armed, smart_stage, smart_peak, smart_base,
             smart_master_anchor) = r
            rem = rem or 0.0
            sz = sz or 0.0
            dust_px = epx or ((notl or 0.0) / sz if sz else 0.0)
            # Paper dust can be settled locally because Paper is the ledger.
            # A Live row is only a projection of a real exchange position: it
            # must stay open until LiveExecutor submits a reduce-only close and
            # authoritative reconciliation proves the venue position is flat.
            live_exchange_dust = book is self.taker and self.execution_mode == "live"
            if (dust_px > 0 and reduce_leaves_dust(rem, 0.0, dust_px)
                    and not live_exchange_dust):
                self._close_reloaded_dust(book, pid, addr, coin, side, rem, dust_px)
                closed_dust += 1
                continue
            maintenance_leverage = maintenance_by_coin.get(coin)
            if epx is not None and f(epx) > 0.0 and f(lev) > 0.0 and rem > 0.0:
                basis = rebase_isolated_position(
                    epx, side, rem, lev, maintenance_leverage,
                )
                if any(
                    abs(f(current) - f(basis[key])) > 1e-9
                    for current, key in (
                        (sz, "size"), (mgn, "margin"),
                        (notl, "notional"), (liq, "liq_px"),
                    )
                ):
                    rebased_positions.append((
                        basis["size"], basis["margin"], basis["notional"],
                        basis["liq_px"], pid,
                    ))
                sz = basis["size"]
                mgn = basis["margin"]
                notl = basis["notional"]
                liq = basis["liq_px"]
            ev = asyncio.Event()
            if epx is not None:
                ev.set()
            if peak_sz is None:
                running_size = 0.0
                reconstructed_peak = 0.0
                for (qty_delta,) in self.db.execute(
                    f"SELECT our_qty_delta FROM {book.act_table} WHERE pos_id=? ORDER BY act_id",
                    (pid,),
                ).fetchall():
                    running_size += f(qty_delta) or 0.0
                    reconstructed_peak = max(reconstructed_peak, abs(running_size))
                peak_sz = max(reconstructed_peak, abs(rem))
                reconstructed_peaks.append((peak_sz, pid))
            first_entry_row = self.db.execute(
                f"SELECT SUM(ABS(our_qty_delta)*our_px) FROM {book.act_table} "
                "WHERE pos_id=? AND action='open' AND ABS(our_qty_delta)>1e-12 AND our_px IS NOT NULL",
                (pid,),
            ).fetchone()
            exact_first_margin = (
                (f(first_entry_row[0]) / f(lev))
                if first_entry_row and f(first_entry_row[0]) > 0 and f(lev) > 0
                else (mgn or 0.0) / (1 + (adds or 0) * self.add_frac)
            )
            last_followed_add = self.db.execute(
                f"SELECT master_px FROM {book.act_table} WHERE pos_id=? AND action IN ('open','add') "
                "AND ABS(our_qty_delta)>1e-12 AND master_px IS NOT NULL ORDER BY act_id DESC LIMIT 1",
                (pid,),
            ).fetchone()
            exact_last_target_add_px = f(last_followed_add[0]) if last_followed_add else mpx
            source_open_oids = {
                o for (o,) in self.db.execute(
                    f"SELECT DISTINCT master_oid FROM {book.act_table} "
                    "WHERE pos_id=? AND action='open'",
                    (pid,),
                ).fetchall() if o is not None
            }
            book.open_ep[(addr, coin)] = {
                "pos_id": pid, "addr": addr, "coin": coin,
                "side": side, "sign": 1 if side == "long" else -1,
                "master_open_ms": mo, "master_open_px": mpx, "master_peak": peak or 0.0,
                "master_current": abs(master_current if master_current is not None else (peak or 0.0)),
                "open_oid": None, "leverage": lev or 0.0, "margin": mgn or 0.0,
                "notional": notl or 0.0, "entry_px": epx, "size": sz, "rem_size": rem,
                "peak_size": peak_sz or abs(rem),
                "liq_px": liq or 0.0, "realized_pnl": rpnl or 0.0,
                "add_count": adds or 0, "entries_ready": ev, "lock": asyncio.Lock(),
                # Reconstruct smart-add anchors from the immutable action audit.  Using total margin/add_count
                # is wrong when proportional smart adds differ from ADD_FRAC and can oversize after restart.
                "first_margin": exact_first_margin,
                "master_first_notl": (
                    master_open_notional
                    if master_open_notional is not None
                    else (m_mgn or 0.0) * (m_lev or 0.0)
                ),
                "source_open_oids": source_open_oids,
                "last_target_add_px": exact_last_target_add_px,
                "maintenance_leverage": maintenance_leverage,
                "mae": mae or 0.0, "num_actions": na or 0, "gap": False, "add_orders": {},
                "smart_tp_armed": bool(smart_armed),
                "smart_tp_stage": int(smart_stage or 0),
                "smart_tp_peak_pnl": float(smart_peak or 0.0),
                "smart_tp_base_size": float(smart_base or 0.0),
                "smart_tp_master_anchor": float(smart_master_anchor or 0.0),
                "smart_tp_inflight": False,
                "seen_oids": {o for (o,) in self.db.execute(   # orders already consumed (restart-safe)
                    f"SELECT DISTINCT master_oid FROM {book.act_table} WHERE pos_id=? AND action IN "
                    "('open','add')", (pid,)).fetchall() if o is not None}}
            loaded += 1
        if reconstructed_peaks:
            self.db.executemany(
                f"UPDATE {book.pos_table} SET peak_size=? WHERE pos_id=? AND peak_size IS NULL",
                reconstructed_peaks,
            )
            self.db.commit()
        if rebased_positions:
            self.db.executemany(
                f"UPDATE {book.pos_table} SET size=?,margin=?,notional=?,liq_px=? "
                "WHERE pos_id=? AND status='open'",
                rebased_positions,
            )
            self.db.commit()
        if loaded or closed_dust:
            extra = f", closed {closed_dust} dust" if closed_dust else ""
            if rebased_positions:
                extra += f", rebased {len(rebased_positions)} liquidation bases"
            _log(f"reloaded {loaded} open {book.name} copy positions from db{extra}")

    def _close_reloaded_dust(self, book, pos_id, addr, coin, side, rem_size, px):
        sign = 1 if side == "long" else -1
        t = now_ms()
        self.db.execute(
            f"INSERT INTO {book.act_table} "
            "(pos_id,addr,coin,ts,recv_ms,action,master_oid,master_px,master_sz_delta,"
            "master_pos_after,our_qty_delta,our_px,realized_pnl,slippage_bps,strategy_revision_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pos_id, addr, coin, t, t, "close", None, px, 0.0, 0.0,
             -rem_size * sign, px, 0.0, 0.0, self.strategy_revision_id),
        )
        self.db.execute(
            f"UPDATE {book.pos_table} SET rem_size=0,status='closed',closed_at=?,"
            "num_actions=COALESCE(num_actions,0)+1,mark_px=?,unrealized_pnl=0 WHERE pos_id=?",
            (now_iso(), px, pos_id),
        )
        if book.stats_loaded:
            book.closed_n += 1
            book.gross_traded += abs(rem_size * px)
            book.fees_cum += abs(rem_size * px) * config.TAKER_FEE
        self.db.commit()

    async def _reconcile_open(self):
        """Startup state-reconcile (replaces the deleted time-based backfill for EXITS). Forward-only
        means we can't see a master's close that happened while we were down → a reloaded copy could
        orphan-hold. So for ONLY the wallets we still hold a copy on, fetch the master's CURRENT
        positions (clearinghouseState); if the master no longer holds ours (flat on that coin, or
        flipped to the opposite side), close our copy now at the live book. Masters still in the
        position (same side) are left untouched — forward polling follows their next action."""
        book = self.taker
        held = sorted({addr for (addr, _) in book.open_ep})
        for addr in held:
            # standard perp + each builder dex we hold a position on (stock perps aren't in the
            # standard clearinghouseState — without the dex they'd read as flat and get wrong-closed).
            dexes = sorted({c.split(":")[0] for (a, c) in book.open_ep if a == addr and ":" in c})
            source_positions, all_ok = {}, True
            for dex in [None] + dexes:
                st = await asyncio.to_thread(rest.clearinghouse_state, addr, dex)
                if not isinstance(st, dict):
                    all_ok = False
                    break                             # a fetch failed — safer to hold than wrong-close
                for ap in st.get("assetPositions", []):
                    p = ap.get("position", {}) or {}
                    if p.get("coin") is not None:
                        leverage_value = p.get("leverage")
                        if isinstance(leverage_value, dict):
                            leverage_value = leverage_value.get("value")
                        source_positions[p["coin"]] = {
                            "size": f(p.get("szi")) or 0.0,
                            "entry": f(p.get("entryPx")) or None,
                            "margin": f(p.get("marginUsed")) or None,
                            "leverage": f(leverage_value) or None,
                        }
            if not all_ok:
                continue
            for (a, coin), ep in list(book.open_ep.items()):
                if a != addr:
                    continue
                source = source_positions.get(coin) or {}
                m = source.get("size", 0.0)          # master's signed size on this coin, now
                still = (m > config.FLAT) if ep["side"] == "long" else (m < -config.FLAT)
                if still:
                    # Backfill source metadata for current/legacy rows. This audit path deliberately does not
                    # alter our leverage or sizing state.
                    self.db.execute(
                        f"UPDATE {book.pos_table} SET master_leverage=COALESCE(?,master_leverage),"
                        "master_margin=COALESCE(?,master_margin),"
                        "master_open_px=COALESCE(?,master_open_px) WHERE pos_id=?",
                        (source.get("leverage"), source.get("margin"), source.get("entry"), ep["pos_id"]),
                    )
                    continue                          # master still in it (same side) -> keep & follow
                ba = await asyncio.to_thread(rest.book_top, coin)
                mid = ((ba[0] + ba[1]) / 2) if ba else ep["entry_px"]
                await self._apply_reduce(addr, coin, ep, now_ms(), mid, 0.0, 0.0,
                                         closing=True, liq=False, gap=True, forced_px=mid, book=book)
                if (addr, coin) not in book.open_ep:
                    _log(f"RECONCILE-CLOSE {addr[:10]} {coin} {ep['side']} @ {mid:g} "
                         f"pnl=${ep['realized_pnl']:+,.1f}  bal=${book.balance:,.0f} "
                         "(master no longer holds it)")
            self.db.commit()

    async def reconcile_loop(self):
        """Periodic safety net for the startup reconcile. Forward polling should catch a master's close
        live, but a missed fill would orphan-hold; this re-checks every held wallet's CURRENT positions
        every RECONCILE_INTERVAL_S and closes any copy whose master has gone flat/flipped. Runs even when
        paused (an orphan whose master already left is pure risk with no copy value)."""
        while not self.stop:
            await asyncio.sleep(config.RECONCILE_INTERVAL_S)
            try:
                await self._reconcile_open()
            except Exception as exc:  # noqa: BLE001
                self._rollback_db()
                _log(f"reconcile loop error: {exc}")

    # -- watchlist sync (the copy engine tracks rolling discovery) -----------
    def _reload_targets(self, init=False, target_snapshot=None):
        if target_snapshot is None:
            addrs, seed = load_targets(self.db, self.top_n)
            target_acct = {a: v for a, v in                 # conviction denominator (target's account)
                           self.db.execute("SELECT addr, acct_value FROM watchlist").fetchall()}
            target_sector_policy = {
                (r[0] or "").lower(): parse_json_obj(r[1])
                for r in self.db.execute("SELECT addr, sector_policy_json FROM watchlist").fetchall()
            }
            entry_frozen = {
                str(row[0] or "").lower()
                for row in self.db.execute(
                    "SELECT addr FROM follow_selection WHERE generation=? "
                    "AND lower(role)='core' AND COALESCE(enabled,1)=1 "
                    "AND COALESCE(entry_eligible,1)=0",
                    (self.selection_generation,),
                ).fetchall()
                if row[0]
            }
        else:
            rows = list(target_snapshot)[:max(0, int(self.top_n))]
            addrs = [(row.get("addr") or "").lower() for row in rows if row.get("addr")]
            seed = {row["addr"].lower(): set(row.get("seedCoins") or []) for row in rows}
            target_acct = {row["addr"].lower(): row.get("acctValue") for row in rows}
            target_sector_policy = {
                row["addr"].lower(): dict(row.get("sectorPolicy") or {}) for row in rows
            }
            entry_frozen = {
                row["addr"].lower()
                for row in rows
                if row.get("entryEligible") is False
            }
        self.seed_coins = seed
        self.target_acct = target_acct
        self.target_sector_policy = target_sector_policy
        self.entry_frozen = entry_frozen
        # SAFEGUARD: never stop polling a wallet we still hold a copy on, even if it fell off the
        # watchlist this scan — else we'd miss its exit and dumb-hold the position to liquidation.
        held_off = [a for a in {addr for (addr, _) in self.open_ep} if a not in addrs]
        addrs = addrs + held_off
        self.held_off = set(held_off)         # EXIT-ONLY set: poll them to unwind, but open nothing new
        new = [a for a in addrs if a not in self.last_fill_ms]
        stamp_ms = now_ms()
        for a in new:
            cursor = None
            if self.execution_mode == "live":
                row = self.db.execute(
                    "SELECT last_fill_ms FROM observer_target_cursor "
                    "WHERE mode='live' AND session_id=? AND lower(addr)=lower(?)",
                    (self.execution_session_id, a),
                ).fetchone()
                cursor = int(row[0]) if row and row[0] is not None else None
            self.last_fill_ms[a] = cursor if cursor is not None else stamp_ms
            if self.execution_mode == "live" and cursor is None:
                self.db.execute(
                    "INSERT OR IGNORE INTO observer_target_cursor "
                    "(mode,session_id,addr,last_fill_ms,updated_at) VALUES ('live',?,?,?,?)",
                    (self.execution_session_id, a, stamp_ms, now_iso()),
                )
        dropped = [a for a in self.addrs if a not in addrs]
        self.addrs = addrs
        if init or new or dropped or held_off:
            extra = f", {len(held_off)} held-off-list" if held_off else ""
            probation = f", {len(entry_frozen)} probation-frozen" if entry_frozen else ""
            _log(
                f"watchlist: tracking {len(addrs)} wallets "
                f"(+{len(new)} new, -{len(dropped)} dropped{extra}{probation})"
            )

    def _read_strategy_snapshot(self):
        """Read one immutable Core+params bundle without changing process state."""
        try:
            if self.db.in_transaction:
                self.db.commit()
            self.db.execute("BEGIN")
            bundle = strategy_revision.load_active(self.db)
            published_generation = selection.latest_published_generation(self.db)
            if bundle and bundle.get("selectionGeneration") == published_generation:
                follow = dict(bundle.get("params") or {})
                targets = strategy_revision.resolved_targets(self.db, bundle, self.top_n)
                revision = bundle["revision"]
            else:
                # Rolling-deploy compatibility only.  Once the first revision exists, mutable global params
                # can no longer race independently against a published Core generation.
                from hyper import params as P
                follow = P.load_follow(self.db)
                targets = None
                revision = None
            self.db.commit()
        except Exception:
            if self.db.in_transaction:
                self.db.rollback()
            raise
        return {
            "follow": follow,
            "targets": targets,
            "revision": revision,
            "published_generation": published_generation,
        }

    def _apply_strategy_snapshot(self, snapshot, *, init=False):
        """Publish a previously read strategy snapshot to Observer memory."""
        revision = snapshot["revision"]
        changed = revision != self.strategy_revision_id
        self.strategy_revision_id = revision
        self.selection_generation = snapshot["published_generation"]
        self._reload_params(snapshot["follow"])
        self._reload_targets(init=init, target_snapshot=snapshot["targets"])
        if changed:
            _log(f"strategy revision: {revision or 'legacy-fallback'}")

    def _reload_strategy(self, init=False):
        """Load one immutable Core+params bundle from a single SQLite read snapshot."""
        self._apply_strategy_snapshot(self._read_strategy_snapshot(), init=init)

    async def _hot_reload_strategy(self, init=False):
        """Bind a Live session before exposing a new immutable bundle to signal handling.

        Scanner publication and Observer reconciliation share SQLite.  A temporary writer lock must
        therefore delay the switch, not leave Observer memory ahead of the durable Live session.  Source
        fills continue to be journalled while this waits; the ordered durable-signal worker resumes only
        after the exact bound snapshot has been installed.
        """
        if self._strategy_reload_lock is None:
            self._strategy_reload_lock = asyncio.Lock()
        async with self._strategy_reload_lock:
            self._strategy_bind_pending = True
            applied = False
            retry_delay = 0.25
            last_log_ms = 0
            try:
                while not self.stop:
                    snapshot = self._read_strategy_snapshot()
                    try:
                        if self.execution_mode == "live" and self.live_executor is not None:
                            self._bind_live_strategy_revision(snapshot["revision"])
                            # A narrowly allowed lineage repair can replace the revision while binding.
                            # Re-read until the snapshot exactly matches the durable session binding.
                            bound = str(self.live_executor.session.get("strategy_revision") or "")
                            if bound != str(snapshot["revision"] or ""):
                                continue
                        self._apply_strategy_snapshot(snapshot, init=init)
                        if self.db.in_transaction:
                            self.db.commit()
                        applied = True
                        return True
                    except Exception as exc:
                        self._rollback_db()
                        if self._is_db_contention(exc):
                            current_ms = now_ms()
                            if current_ms - last_log_ms >= 5_000:
                                _log("strategy hot-bind database busy; retaining prior in-memory revision")
                                last_log_ms = current_ms
                            await asyncio.sleep(retry_delay)
                            retry_delay = min(5.0, retry_delay * 2.0)
                            continue
                        if str(exc) in {
                            "live_strategy_revision_not_active",
                            "live_strategy_revision_changed",
                        }:
                            # A newer descendant won the publication race. Read and bind that exact bundle.
                            await asyncio.sleep(0)
                            continue
                        raise
                raise RuntimeError("observer_stopping_during_strategy_bind")
            finally:
                # An integrity/application failure must fail closed. A later periodic reload may repair it;
                # until then the poller can journal fills but the signal worker cannot mutate strategy state.
                if applied or self.stop:
                    self._strategy_bind_pending = False

    def _bind_live_strategy_revision(self, revision_override=None):
        """Advance the active Live session along the immutable revision chain.

        Core refreshes and operator parameter edits are deliberately hot-reloadable while Live is
        running.  The session binding must advance with the bundle actually loaded by Observer, or a
        later worker restart will correctly reject the otherwise-stale session revision.  Only a
        descendant of the session's current revision may be adopted; a lateral/replaced history still
        fails closed.
        """
        if self.execution_mode != "live" or self.live_executor is None:
            return False
        revision = revision_override or self.strategy_revision_id
        if not revision:
            raise RuntimeError("live_strategy_revision_missing")
        session_id = self.live_executor.session["session_id"]
        row = self.db.execute(
            "SELECT strategy_revision FROM execution_session WHERE session_id=?",
            (session_id,),
        ).fetchone()
        previous = str(row[0] or "") if row else ""
        if not previous:
            raise RuntimeError("live_session_strategy_revision_missing")
        bundle = strategy_revision.load_revision(self.db, revision)
        if not bundle or strategy_revision.active_revision_id(self.db) != revision:
            raise RuntimeError("live_strategy_revision_not_active")
        if previous == revision:
            margin_pct = float(
                (bundle.get("params") or {}).get("MARGIN_EQUITY_PCT", self.margin_equity_pct)
            )
            self.live_executor.session["strategy_revision"] = revision
            self.live_executor.session["margin_equity_pct"] = margin_pct
            return False
        if previous != revision:
            def _ancestor(bundle_row):
                cursor = bundle_row
                seen = set()
                while cursor and cursor.get("revision") not in seen:
                    current = cursor.get("revision")
                    if current == previous:
                        return cursor
                    seen.add(current)
                    parent = cursor.get("parentRevision")
                    cursor = strategy_revision.load_revision(self.db, parent) if parent else None
                return None

            cursor = _ancestor(bundle)
            if not cursor:
                if self.db.in_transaction:
                    self.db.commit()
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    repaired = strategy_revision.repair_parentless_active_revision(
                        self.db,
                        live_parent_revision=previous,
                        enqueue_reload=False,
                    )
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise
                if not repaired:
                    raise RuntimeError("live_strategy_revision_not_descendant")
                if revision_override is None:
                    self._reload_strategy()
                    revision = self.strategy_revision_id
                else:
                    revision = repaired["revision"]
                bundle = strategy_revision.load_revision(self.db, revision)
                if not bundle or not _ancestor(bundle):
                    raise RuntimeError("live_strategy_revision_not_descendant")
        margin_pct = float(
            (bundle.get("params") or {}).get("MARGIN_EQUITY_PCT", self.margin_equity_pct)
        )
        if self.db.in_transaction:
            self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if strategy_revision.active_revision_id(self.db) != revision:
                raise RuntimeError("live_strategy_revision_changed")
            updated = self.db.execute(
                "UPDATE execution_session SET strategy_revision=?,margin_equity_pct=?,updated_at=? "
                "WHERE session_id=? AND strategy_revision=?",
                (revision, margin_pct, now_iso(), session_id, previous),
            ).rowcount
            if updated != 1:
                raise RuntimeError("live_session_strategy_revision_changed")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.live_executor.session["strategy_revision"] = revision
        self.live_executor.session["margin_equity_pct"] = margin_pct
        return previous != revision

    def _strategy_revision_recovery_requested(self) -> bool:
        """Recognise only the restart state caused by a strategy-lineage mismatch.

        A real exchange ambiguity, ledger drift, or operator pause must never be
        auto-resumed.  Capture this before ``LiveExecutor.reconcile`` converts a
        successfully reconciled ``reconcile_required`` session to ``paused`` and
        clears the original error code.
        """
        if self.execution_mode != "live" or not self.execution_session_id:
            return False
        row = self.db.execute(
            "SELECT ec.selected_mode,ec.state,ec.active_session_id,ec.last_error_code,es.state "
            "FROM execution_control ec LEFT JOIN execution_session es "
            "ON es.session_id=ec.active_session_id WHERE ec.id=1"
        ).fetchone()
        return bool(
            row
            and str(row[0] or "") == "live"
            and str(row[1] or "") == "reconcile_required"
            and str(row[2] or "") == self.execution_session_id
            and str(row[3] or "") == "STRATEGY_REVISION_MISMATCH"
            and str(row[4] or "") == "reconcile_required"
        )

    def _resume_after_strategy_revision_recovery(
        self,
        *,
        requested: bool,
        reconcile_result: dict,
        ledger_projection_ok: bool,
    ) -> bool:
        """Resume Live only after a lineage-only failure is completely repaired.

        This is deliberately narrower than the operator ``resume`` command.  It
        prevents a harmless Core/parameter publication from permanently freezing
        new entries, while preserving fail-closed behaviour for every trading or
        reconciliation uncertainty.
        """
        if not requested or self.execution_mode != "live" or self.live_executor is None:
            return False
        if not reconcile_result.get("ok") or not ledger_projection_ok:
            return False
        if any(int(reconcile_result.get(key) or 0) for key in (
            "unknownPositions", "unknownOrders", "ambiguousIntents",
        )):
            return False

        session_id = self.live_executor.session["session_id"]
        if self.db.in_transaction:
            self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            active_revision = strategy_revision.active_revision_id(self.db)
            if not active_revision or active_revision != self.strategy_revision_id:
                raise RuntimeError("strategy_recovery_active_revision_changed")
            session = self.db.execute(
                "SELECT state,canary,strategy_revision FROM execution_session WHERE session_id=?",
                (session_id,),
            ).fetchone()
            control_row = self.db.execute(
                "SELECT selected_mode,state,active_session_id FROM execution_control WHERE id=1"
            ).fetchone()
            if not session or not control_row:
                raise RuntimeError("strategy_recovery_state_missing")
            if (
                str(session[0] or "") != "paused"
                or str(session[2] or "") != active_revision
                or str(control_row[0] or "") != "live"
                or str(control_row[1] or "") != "paused"
                or str(control_row[2] or "") != session_id
            ):
                raise RuntimeError("strategy_recovery_state_changed")
            next_state = "live_canary" if bool(session[1]) else "live_running"
            stamp = now_iso()
            session_updated = self.db.execute(
                "UPDATE execution_session SET state=?,updated_at=? "
                "WHERE session_id=? AND state='paused' AND strategy_revision=?",
                (next_state, stamp, session_id, active_revision),
            ).rowcount
            control_updated = self.db.execute(
                "UPDATE execution_control SET state=?,last_error_code=NULL,last_error_at=NULL,updated_at=? "
                "WHERE id=1 AND selected_mode='live' AND state='paused' AND active_session_id=?",
                (next_state, stamp, session_id),
            ).rowcount
            if session_updated != 1 or control_updated != 1:
                raise RuntimeError("strategy_recovery_compare_and_swap_failed")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        self.live_executor.session["state"] = next_state
        self.execution_state = next_state
        self.paused = False
        self._proc_state = "running"
        self.live_reconcile_error = None
        self.live_reconcile_error_at = None
        _log("strategy revision lineage repaired and fully reconciled; Live entries resumed")
        return True

    # -- WS bbo (pricing only; no user subscriptions) ------------------------
    async def _sub(self, subscription: dict):
        await self.ws.send(ws.sub_msg(subscription))
        await asyncio.sleep(0.05)

    async def subscribe_bbo(self):
        self.sub_coins.clear()                    # subs are gone on a fresh connection — re-add
        coins = {"BTC", "ETH", "SOL", "HYPE"}     # majors warm so most fills price off a live book
        for a in self.addrs:
            coins |= self.seed_coins.get(a, set())
        for (_, c) in self.open_ep:
            coins.add(c)
        for c in coins:
            await self.ensure_coin(c)

    # -- per-coin volatility (regime-aware σ for risk-targeted sizing) --------
    def _sigma(self, coin: str) -> float:
        """Latest σ for coin from the read-cache (mirrors coin_vol); fallback if not refreshed yet."""
        return self.vol.get(coin) or self.vol_fallback_sigma

    def _market_max_leverage(self, coin: str):
        """Return the most conservative proven Hyperliquid leverage cap.

        ``coin_vol`` is the warm scanner/Observer cache. Live additionally has
        the broker's official ``meta`` market spec, which is the final guard at
        order time and covers a cold or temporarily incomplete cache.
        """
        caps = []
        row = self.db.execute(
            "SELECT max_leverage FROM coin_vol WHERE coin=?", (coin,),
        ).fetchone()
        cached = f(row[0]) if row and row[0] is not None else 0.0
        if cached > 0.0:
            caps.append(cached)
        if self.execution_mode == "live" and self.live_executor is not None:
            try:
                official = f(self.live_executor.broker.market_spec(coin).max_leverage)
            except Exception:  # broker submission still fails closed if unsupported
                official = 0.0
            if official > 0.0:
                caps.append(official)
        return min(caps) if caps else None

    def _tier(self, sigma: float, coin: str = None) -> str:
        """BTC alone may enter stable; every other market starts at mid and can rise to high by σ."""
        return tier_for_sigma(sigma, self.high_sigma_min, coin)

    def _open_sizing_params(self, book=None):
        book = book or self.taker
        return OpenSizingParams(
            high_sigma_min=self.high_sigma_min,
            tier_margin=self.tier_margin,
            tier_lev_cap=self.tier_lev_cap,
            tier_coin_cap=self.tier_coin_cap,
            min_lev=self.min_lev,
            min_open_margin_pct=self.min_open_margin_pct,
            capital_anchor=book.sizing_anchor,
            drawdown_exponent=config.SIZING_DRAWDOWN_EXPONENT,
            drawdown_max_multiplier=config.SIZING_DRAWDOWN_MAX_MULTIPLIER,
            margin_equity_pct=self.margin_equity_pct,
        )

    async def _live_liquidity_book(self, coin: str):
        if not self.low_liquidity_filter_enable or not coin or ":" in coin:
            return None
        try:
            return await asyncio.to_thread(
                rest.realtime_book_snapshot, coin, config.LIVE_BOOK_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 - market-context fallback remains available
            return None

    async def _execute_live_order(
        self, *, ep, addr, coin, action, is_buy, size, leverage, reduce_only,
        source_time_ms, source_order_id,
    ):
        if self.execution_mode != "live" or self.live_executor is None:
            return None
        source_fill_id = _SOURCE_EVENT_ID.get() or (
            f"{str(addr).lower()}:{int(source_time_ms)}:"
            f"{source_order_id if source_order_id is not None else 'none'}:"
            f"{action}:{int(ep.get('num_actions') or 0) + 1}"
        )
        self._live_order_inflight += 1
        try:
            worker = asyncio.create_task(
                asyncio.to_thread(
                    self.live_executor.execute,
                    coin=coin,
                    is_buy=bool(is_buy),
                    size=float(size),
                    leverage=float(leverage),
                    reduce_only=bool(reduce_only),
                    action=action,
                    source_address=addr,
                    source_fill_id=source_fill_id,
                    source_order_id=str(source_order_id) if source_order_id is not None else None,
                    source_time_ms=int(source_time_ms),
                    action_seq=int(ep.get("num_actions") or 0) + 1,
                ),
                name=f"observer:live_order:{action}:{coin}",
            )
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Cancelling an await does not cancel the signed worker thread.  Finish recovery and return
                # its confirmed result so the caller can commit the matching Live ledger transition.
                return await worker
        finally:
            self._live_order_inflight = max(0, self._live_order_inflight - 1)

    def _coin_liquidity_decision(
        self, coin: str, *, book_snapshot=None, is_buy=None, planned_notional=None,
    ) -> dict:
        """Assess our actual live order, falling back to coarse context only when L2 is unavailable."""
        if not self.low_liquidity_filter_enable or not coin or ":" in coin:
            return {"reason": None, "source": "disabled"}
        if is_buy is not None and f(planned_notional) > 0.0:
            assessment = assess_order_book(
                book_snapshot,
                is_buy=bool(is_buy),
                planned_notional=f(planned_notional),
                max_spread_bps=self.live_book_max_spread_bps,
                max_impact_bps=self.live_book_max_impact_bps,
            )
            if assessment.get("available"):
                return {**assessment, "source": "live_l2"}
        row = self.db.execute(
            "SELECT day_ntl_vlm,oi_notional FROM coin_vol WHERE coin=?",
            (coin,),
        ).fetchone()
        if not row:
            return {"reason": None, "source": "context_unavailable"}
        day_ntl_vlm, oi_notional = row[0], row[1]
        if day_ntl_vlm is None or oi_notional is None:
            return {"reason": None, "source": "context_incomplete"}
        # A quiet day alone does not prove that our small order is unfillable. Only reject the fallback when
        # both turnover and open interest are weak; a live L2 snapshot remains authoritative when available.
        weak_volume = day_ntl_vlm < self.min_coin_day_ntl_vlm
        weak_oi = oi_notional < self.min_coin_oi_notional
        return {
            "reason": "fallback_volume_and_open_interest" if weak_volume and weak_oi else None,
            "source": "market_context_fallback",
            "day_ntl_vlm": day_ntl_vlm,
            "oi_notional": oi_notional,
        }

    @staticmethod
    def _liquidity_log_detail(decision: dict) -> str:
        if decision.get("source") != "live_l2":
            return str(decision.get("source") or "unknown")
        return (
            f"order ${f(decision.get('planned_notional')):,.0f}, "
            f"spread {f(decision.get('spread_bps')):.1f}bps, "
            f"impact {f(decision.get('impact_bps')):.1f}bps, "
            f"depth {f(decision.get('fill_ratio')) * 100:.0f}%"
        )

    async def _ensure_vol(self, coin: str):
        """Track coin for the periodic σ refresh, and fetch it NOW if we have no fresh value (so a
        first-seen coin gets a real σ within seconds; sizing uses the fallback only in the meantime)."""
        if not coin:
            return
        self.vol_coins.add(coin)
        row = self.db.execute(
            "SELECT day_ntl_vlm,oi_notional,max_leverage FROM coin_vol WHERE coin=?",
            (coin,),
        ).fetchone()
        needs_market_ctx = (
            self.low_liquidity_filter_enable and ":" not in coin
            and ((not row) or row[0] is None or row[1] is None)
        )
        # Every market, including HIP-3, must have the official leverage cap
        # before sizing. A warm sigma alone is not enough.
        needs_leverage = (not row) or row[2] is None or f(row[2]) <= 0.0
        # A coin_vol placeholder with NULL sigma is not warm.  This is common for builder/stock markets whose
        # market-context row was staged before candle volatility was collected; refresh it immediately instead
        # of sizing the first order with a temporary fallback (currently neutral 7% / mid tier).
        if not self.vol.get(coin) or needs_market_ctx or needs_leverage:
            self.vol[coin] = await asyncio.to_thread(self._refresh_vol_worker, coin)

    async def prewarm_vol(self):
        """Warm σ for the top-N-by-24h-volume crypto + each builder dex at startup (background, gentle):
        the liquid coins our targets are likeliest to trade get σ before their first fill — no first-open
        latency, warm restart. The long tail is still lazy-fetched on first fill. Skips already-warm coins."""
        for dex in (None, *rest.BUILDER_DEXES):
            ctxs = await asyncio.to_thread(rest.asset_contexts, dex)
            def _day_vlm(item):
                try:
                    return float((item[1] or {}).get("dayNtlVlm") or 0.0)
                except (TypeError, ValueError):
                    return 0.0
            for raw_coin, ctx in sorted(ctxs.items(), key=_day_vlm, reverse=True)[:config.VOL_PREWARM_TOP]:
                coin = (
                    str(raw_coin) if dex is None or ":" in str(raw_coin)
                    else f"{dex}:{raw_coin}"
                )
                if self.vol.get(coin) or self.stop:
                    continue
                self.vol_coins.add(coin)
                try:
                    self.vol[coin] = await asyncio.to_thread(self._refresh_vol_worker, coin, ctx)
                except Exception:  # noqa: BLE001
                    pass
        _log(f"vol prewarmed: {len(self.vol)} coins (top {config.VOL_PREWARM_TOP}/pool by 24h vol)")

    async def vol_refresh_loop(self):
        """Periodically re-compute σ for every tracked coin into coin_vol — OFF the signal hot path, so
        sizing only ever reads the cache. Catches a calm→volatile regime change within VOL_REFRESH_S."""
        while not self.stop:
            await asyncio.sleep(config.VOL_REFRESH_S)
            ctxs = {}
            for dex in (None, *rest.BUILDER_DEXES):
                scoped = await asyncio.to_thread(rest.asset_contexts, dex)
                for raw_coin, ctx in scoped.items():
                    coin = (
                        str(raw_coin) if dex is None or ":" in str(raw_coin)
                        else f"{dex}:{raw_coin}"
                    )
                    ctxs[coin] = ctx
            for coin in list(self.vol_coins):
                try:
                    self.vol[coin] = await asyncio.to_thread(
                        self._refresh_vol_worker, coin, ctxs.get(coin),
                    )
                except Exception:  # noqa: BLE001
                    pass

    async def ensure_coin(self, coin: str):
        """Subscribe every executable standard/HIP-3 perp to identical BBO and official-mark feeds."""
        if not coin or coin in self.sub_coins:
            return
        self._spawn_background(
            self._ensure_vol(coin), f"ensure_vol:{coin}", critical=False,
        )
        if self._copyable(coin) and self.ws is not None:
            try:
                await self._sub(ws.bbo(coin))
                await self._sub(ws.active_asset_ctx(coin))
                self.sub_coins.add(coin)
            except Exception:  # noqa: BLE001
                self.sub_coins.discard(coin)

    async def heartbeat(self):
        while not self.stop:
            await asyncio.sleep(30)
            try:
                await self.ws.send(ws.PING)
            except Exception:  # noqa: BLE001
                return

    def _write_stats(self):
        """Snapshot the paper account into account_stats — the DASHBOARD time-series (equity curve, ROI,
        win rate, hedge ratio = net/gross, fee drag). Mark-to-market open positions off the live book."""
        init = config.INITIAL_BALANCE or 1.0
        upnl = locked = gross = net = 0.0
        mark_updates = []
        for pos_id, coin, side, rem, size, entry, margin, notional in self.db.execute(
                "SELECT pos_id,coin,side,rem_size,size,entry_px,margin,notional FROM copy_position "
                "WHERE status='open' AND size>0").fetchall():
            mark = self._mark_px(coin, entry or 0)
            sgn = 1 if side == "long" else -1
            pos_upnl = rem * (mark - (entry or 0)) * sgn
            upnl += pos_upnl
            locked += margin * rem / size
            cur_notl = notional * rem / size
            gross += cur_notl
            net += cur_notl * sgn
            mark_updates.append((mark, pos_upnl, pos_id))
        if mark_updates:
            self.db.executemany(                      # one prepared statement/transaction for all live positions
                "UPDATE copy_position SET mark_px=?, unrealized_pnl=? WHERE pos_id=?", mark_updates)
        open_n = self.db.execute("SELECT count(*) FROM copy_position WHERE status='open'").fetchone()[0]
        if not self.taker.stats_loaded:
            self._load_account(self.taker)
        closed_n = self.taker.closed_n
        win_rate = self.taker.wins_n / closed_n if closed_n else 0.0
        equity = self.balance + upnl
        self.db.execute(
            "INSERT INTO account_stats (ts,balance,unrealized_pnl,equity,realized_pnl_cum,roi,open_n,"
            "closed_n,win_rate,locked_margin,available,gross_notional,net_notional,fees_cum) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), self.balance, upnl, equity, self.balance - init, equity / init - 1,
             open_n, closed_n, win_rate, locked, self.balance - locked, gross, net, self.taker.fees_cum))
        self.db.commit()
        self._refresh_live_wallet_risks()

    def _refresh_marks(self, book=None):
        """Mark-to-market open positions into the book's position table (mark_px/unrealized_pnl) WITHOUT
        appending an account_stats row. Lets the read-only dashboard show near-real-time浮盈. Per-book."""
        book = book or self.taker
        updates = []
        for pos_id, coin, side, rem, size, entry in self.db.execute(
                f"SELECT pos_id,coin,side,rem_size,size,entry_px FROM {book.pos_table} "
                "WHERE status='open' AND size>0").fetchall():
            mark = self._mark_px(coin)
            if not mark:
                continue
            sgn = 1 if side == "long" else -1
            updates.append((mark, rem * (mark - (entry or 0)) * sgn, pos_id))
        if updates:
            self.db.executemany(
                f"UPDATE {book.pos_table} SET mark_px=?, unrealized_pnl=? WHERE pos_id=?", updates)
        self.db.commit()

    def _refresh_coin_marks(self, coin: str, book=None) -> int:
        """Persist live mark/unrealized PnL for one coin only. Used by BBO/l2Book ticks so the dashboard
        does not wait for the slower full mark_refresh_loop."""
        book = book or self.taker
        mark = self._mark_px(coin)
        if not (coin and mark):
            return 0
        n = 0
        updates = []
        for pos_id, side, rem, entry in self.db.execute(
                f"SELECT pos_id,side,rem_size,entry_px FROM {book.pos_table} "
                "WHERE status='open' AND coin=? AND size>0", (coin,)).fetchall():
            sgn = 1 if side == "long" else -1
            updates.append((mark, rem * (mark - (entry or 0)) * sgn, pos_id))
            n += 1
        if n:
            self.db.executemany(
                f"UPDATE {book.pos_table} SET mark_px=?, unrealized_pnl=? WHERE pos_id=?", updates)
            self.db.commit()
        return n

    def _refresh_coin_marks_throttled(self, coin: str):
        now = now_ms()
        if now - self.mark_write_ms.get(coin, 0) < MARK_WRITE_MIN_MS:
            return
        try:
            wrote = self._refresh_coin_marks(coin, self.taker)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            # Mark persistence is replaceable telemetry. Keep the fresh in-memory BBO and let the next tick
            # retry instead of tearing down the price socket because Scanner temporarily owns the write lock.
            self._rollback_db()
            return
        if wrote:
            self.mark_write_ms[coin] = now

    async def mark_refresh_loop(self):
        """Frequent mark refresh for dashboard freshness (between the 5-min account_stats snapshots)."""
        while not self.stop:
            await asyncio.sleep(25)
            try:
                self._refresh_marks(self.taker)
            except Exception as exc:  # noqa: BLE001 — never let dashboard freshness kill the engine
                self._rollback_db()
                _log(f"mark refresh failed: {exc}")

    def _rollback_db(self):
        """Release an interrupted SQLite transaction so another loop can recover after write contention."""
        try:
            self.db.rollback()
        except Exception:  # noqa: BLE001 - recovery must not mask the original loop error
            pass

    async def _announce(self):
        while not self.stop:
            await asyncio.sleep(300)
            if not self.taker.stats_loaded:
                self._load_account(self.taker)
            o, c = len(self.open_ep), self.taker.closed_n
            h, self.hb = self.hb, {}           # snapshot + reset this interval's diagnostic tally
            seen = h.get("seen", 0)
            acts = {k[4:]: v for k, v in h.items() if k.startswith("act_")}   # open/add/reduce/stop/close
            skips = {k[5:]: v for k, v in h.items() if k.startswith("skip_")}
            act_s = ", ".join(f"{k} {v}" for k, v in sorted(acts.items(), key=lambda x: -x[1])) or "-"
            skip_s = ", ".join(f"{k} {v}" for k, v in sorted(skips.items(), key=lambda x: -x[1])) or "-"
            _log(f"heartbeat: {o} open / {c} closed | 本轮看到 {seen} → 跟 {sum(acts.values())} ({act_s}), "
                 f"跳 {sum(skips.values())} ({skip_s})")
            try:
                if self.execution_mode != "live" or self.live_executor is None:
                    self._write_stats()             # append the Paper dashboard snapshot every 5 min
            except Exception as exc:  # noqa: BLE001
                _log(f"stats snapshot failed: {exc}")

    async def _reconcile_live_once(self):
        """Refresh exchange truth without turning a transient error into a hidden permanent pause.

        Every exposure increase performs its own mandatory reconciliation, so a failed background refresh is
        already fail-closed for the next order. Only a successful exchange response proving drift enters
        persistent ``reconcile_required``; transport/SQLite errors remain visible and retry automatically.
        """
        try:
            # A writer that commits between retries is not an exchange-health failure. Keep this recovery
            # inside the attempt so a normal WAL collision never becomes a false red Live control-plane state.
            result = await self._reconcile_live_with_retry(attempts=3, retry_all=False)
        except Exception as exc:  # noqa: BLE001
            self.live_reconcile_error = str(exc)[:120]
            self.live_reconcile_error_at = now_iso()
            try:
                self._write_proc_status(self._proc_state)
            except Exception:  # noqa: BLE001 - telemetry must not mask reconciliation recovery
                self._rollback_db()
            _log(f"live reconcile transient error: {self.live_reconcile_error}")
            return None
        self.live_reconcile_error = None
        self.live_reconcile_error_at = None
        self._settle_forced_liquidations()
        if result.get("ok") and not self._verify_live_ledger_projection():
            result = dict(result)
            result.update(ok=False, status="reconcile_required", ledgerProjectionDrift=True)
        if not result.get("ok"):
            self.paused = True
            self.execution_state = "reconcile_required"
            self._write_proc_status("paused")
        return result

    async def live_reconcile_loop(self):
        """Continuously refresh exchange truth and freeze increases on drift."""
        while not self.stop and self.execution_mode == "live":
            try:
                await self._reconcile_live_once()
            except Exception as exc:  # noqa: BLE001 - a short SQLite writer race is recoverable
                if not self._is_db_contention(exc):
                    raise
                self._rollback_db()
                _log("live reconcile database busy; rolled back and will retry")
            await asyncio.sleep(config.LIVE_ACCOUNT_RECONCILE_INTERVAL_S)

    async def prune_live_fills(self):
        """Bound replaceable execution diagnostics; business ledgers are never pruned."""
        while not self.stop:
            removed = storage_guard.prune_execution_transients(self.db)
            total = sum(int(value or 0) for value in removed.values())
            if total:
                _log(f"pruned {total} expired execution diagnostics")
            await asyncio.sleep(6 * 3600)

    # -- dashboard control plane (command channel) ---------------------------
    def _write_proc_status(self, state):
        """Upsert this process's liveness + state machine row for the dashboard. heartbeat_at lets the
        UI flag a dead observer (stale) and lets the command channel self-heal."""
        self._proc_state = state
        pid = None if state == "stopped" else os.getpid()
        signal_states = {}
        if self.execution_mode == "live":
            signal_states = {
                str(row[0]): int(row[1])
                for row in self.db.execute(
                    "SELECT state,COUNT(*) FROM execution_signal WHERE mode='live' AND session_id=? "
                    "GROUP BY state",
                    (self.execution_session_id,),
                ).fetchall()
            }
        self.db.execute(
            "INSERT INTO process_status (name,state,pid,heartbeat_at,detail_json) VALUES "
            "('observer',?,?,?,?) ON CONFLICT(name) DO UPDATE SET state=excluded.state,"
            "pid=excluded.pid,heartbeat_at=excluded.heartbeat_at,detail_json=excluded.detail_json",
            (state, pid, now_iso(),
             json.dumps({"paused": self.paused, "targets": len(self.addrs),
                         "open": len(self.open_ep), "strategyRevision": self.strategy_revision_id,
                         "executionMode": self.execution_mode,
                         "executionState": self.execution_state,
                         "reconcileHealthy": self.live_reconcile_error is None,
                         "reconcileError": self.live_reconcile_error,
                         "reconcileErrorAt": self.live_reconcile_error_at,
                         "signalStates": signal_states})))
        self.db.commit()

    def _interrupt_ws_for_stop(self):
        """Wake the main WebSocket receive loop after an in-process drain completes.

        With no subscribed BBO traffic, ``async for raw in conn`` can otherwise wait forever even after
        ``self.stop`` is set.  That leaves systemd active and the Dashboard stuck on ``paused`` although the
        Live session has already been finalized and has no exposure.
        """
        conn = self.ws
        if conn is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def close_connection():
            try:
                await conn.close()
            except Exception:  # noqa: BLE001 — shutdown must still finish if transport close races
                pass

        loop.create_task(close_connection())

    async def consume_commands(self):
        """Poll the command channel and execute the commands this process OWNS (pause/resume/close/
        toggle). Each: acked -> done/failed. Scanner-owned commands (rescan) are left untouched. Also
        refreshes process_status heartbeat each loop so the dashboard sees the observer alive."""
        OWNED = ("pause", "resume", "close_position", "close_all", "drain", "emergency_close_all", "wallet_toggle",
                 "wallet_exit_request", "wallet_exit_cancel", "wallet_star", "reload_params")
        # A process may die after acknowledgement but before completing a signed order/ledger commit.
        # Requeue only Observer-owned commands; command-id-derived source event ids make Live closes
        # deterministic across the restart.
        initialized = False
        last_hb = 0.0
        while not self.stop:
            try:
                if not initialized:
                    self.db.execute(
                        "UPDATE commands SET status='pending',acked_at=NULL,error=NULL "
                        "WHERE status='acked' AND type IN (" + ",".join("?" * len(OWNED)) + ")",
                        OWNED,
                    )
                    self.db.commit()
                    initialized = True
                rows = self.db.execute(
                    "SELECT id,type,payload_json FROM commands WHERE status='pending' AND type IN "
                    "(" + ",".join("?" * len(OWNED)) + ") ORDER BY id", OWNED).fetchall()
                for cmd_id, ctype, payload_json in rows:
                    self.db.execute("UPDATE commands SET status='acked',acked_at=? WHERE id=?",
                                    (now_iso(), cmd_id))
                    self.db.commit()
                    try:
                        result = await self._dispatch_command(
                            ctype, json.loads(payload_json or "{}"), command_id=cmd_id,
                        )
                        await self._persist_command_completion(
                            cmd_id, status="done", result=result,
                        )
                        _log(f"command #{cmd_id} {ctype} -> done {result}")
                    except Exception as exc:  # noqa: BLE001 — a bad command must not kill the engine
                        self._rollback_db()
                        await self._persist_command_completion(
                            cmd_id, status="failed", error=str(exc),
                        )
                        _log(f"command #{cmd_id} {ctype} -> FAILED {exc}")
                if time.time() - last_hb > 15:        # refresh liveness heartbeat (throttled)
                    self._write_proc_status(self._proc_state)
                    last_hb = time.time()
            except Exception as exc:  # noqa: BLE001
                self._rollback_db()
                _log(f"command loop error: {exc}")
            await asyncio.sleep(1.5)

    async def _persist_command_completion(self, command_id, *, status, result=None, error=None):
        """Finish an acknowledged command without redispatching its side effects on SQLite contention."""
        delay = 0.25
        last_log_ms = 0
        while not self.stop:
            try:
                if status == "done":
                    self.db.execute(
                        "UPDATE commands SET status='done',done_at=?,result_json=?,error=NULL WHERE id=?",
                        (now_iso(), json.dumps(result), command_id),
                    )
                else:
                    self.db.execute(
                        "UPDATE commands SET status='failed',done_at=?,error=? WHERE id=?",
                        (now_iso(), error, command_id),
                    )
                self.db.commit()
                return
            except Exception as exc:
                self._rollback_db()
                if not self._is_db_contention(exc):
                    raise
                current_ms = now_ms()
                if current_ms - last_log_ms >= 5_000:
                    _log(f"command #{command_id} completion database busy; retrying")
                    last_log_ms = current_ms
                await asyncio.sleep(delay)
                delay = min(5.0, delay * 2.0)
        raise RuntimeError("observer_stopping_before_command_completion")

    async def _dispatch_command(self, ctype, payload, *, command_id=None):
        if ctype == "pause":
            self.paused = True
            if self.execution_mode == "live":
                self.execution_state = "paused"
                self.db.execute(
                    "UPDATE execution_session SET state='paused',updated_at=? WHERE session_id=?",
                    (now_iso(), self.live_executor.session["session_id"]),
                )
                self.db.execute(
                    "UPDATE execution_control SET state='paused',updated_at=? WHERE id=1",
                    (now_iso(),),
                )
                self.db.commit()
            self._write_proc_status("paused")
            return {"paused": True}
        if ctype == "resume":
            if self.execution_mode == "live":
                row = self.db.execute(
                    "SELECT state,canary FROM execution_session WHERE session_id=?",
                    (self.live_executor.session["session_id"],),
                ).fetchone()
                if not row or row[0] != "paused":
                    raise ValueError("live_session_not_resumable")
                next_state = "live_canary" if row[1] else "live_running"
                self.db.execute(
                    "UPDATE execution_session SET state=?,updated_at=? WHERE session_id=?",
                    (next_state, now_iso(), self.live_executor.session["session_id"]),
                )
                self.db.execute(
                    "UPDATE execution_control SET state=?,updated_at=? WHERE id=1",
                    (next_state, now_iso()),
                )
                self.db.commit()
                self.execution_state = next_state
            self.paused = False
            self._write_proc_status("running")
            return {"paused": False}
        if ctype == "close_position":
            return await self._cmd_close(
                int(payload["positionId"]), float(payload.get("fraction", 1.0)),
                source_event_id=f"command:{command_id}:position:{int(payload['positionId'])}",
            )
        if ctype == "close_all":
            return await self._cmd_close_all(source_event_id=f"command:{command_id}:close_all")
        if ctype == "drain":
            if self.execution_mode != "live":
                raise ValueError("drain_requires_live_mode")
            self.paused = True
            self.draining = True
            self.execution_state = "draining"
            self.db.execute(
                "UPDATE execution_session SET state='draining',updated_at=? WHERE session_id=?",
                (now_iso(), self.live_executor.session["session_id"]),
            )
            self.db.execute(
                "UPDATE execution_control SET state='draining',updated_at=? WHERE id=1",
                (now_iso(),),
            )
            self.db.commit()
            self._write_proc_status("paused")
            self._finish_live_session_if_drained()
            return {"draining": True, "open": len(self.open_ep)}
        if ctype == "emergency_close_all":
            if self.execution_mode != "live":
                raise ValueError("emergency_close_all_requires_live_mode")
            await self._dispatch_command("drain", {}, command_id=command_id)
            await asyncio.to_thread(self.live_executor.cancel_managed_orders)
            result = await self._cmd_close_all(
                source_event_id=f"command:{command_id}:emergency_close_all",
            )
            result["emergency"] = True
            self._finish_live_session_if_drained()
            return result
        if ctype == "wallet_toggle":
            result = self._cmd_wallet_toggle(
                payload["address"], bool(payload["enabled"]), reload_strategy=False,
            )
            await self._hot_reload_strategy()
            return result
        if ctype == "wallet_exit_request":
            result = self._cmd_wallet_exit_request(payload["address"], reload_strategy=False)
            await self._hot_reload_strategy()
            return result
        if ctype == "wallet_exit_cancel":
            result = self._cmd_wallet_exit_cancel(payload["address"], reload_strategy=False)
            await self._hot_reload_strategy()
            return result
        if ctype == "wallet_star":
            result = self._cmd_wallet_star(payload["address"], bool(payload["starred"]))
            await self._hot_reload_strategy()
            return result
        if ctype == "reload_params":               # UI saved follow params or Core membership changed
            created = None
            if payload.get("createStrategyRevision"):
                if self.db.in_transaction:
                    self.db.commit()
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    created = strategy_revision.materialize_current(
                        self.db,
                        source=str(payload.get("by") or "manual_params"),
                        reason=payload.get("reason") or "operator_follow_params_changed",
                        enqueue_reload=False,
                    )
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise
            await self._hot_reload_strategy()
            return {"reloaded": True, "source": "strategy_revision", "targets": len(self.addrs),
                    "revision": self.strategy_revision_id, "created": created}
        raise ValueError(f"unhandled command type {ctype}")

    def _ep_by_pos(self, pos_id):
        for (addr, coin), ep in self.open_ep.items():
            if ep.get("pos_id") == pos_id:
                return addr, coin, ep
        return None

    async def _cmd_close(self, pos_id, frac=1.0, *, source_event_id=None):
        """Manual close of one live copy at the current book (operator exit). `frac` ∈ (0,1] closes that
        fraction of the remaining size — <100% is a partial reduce (position stays open; freed margin
        returns to available via rem_size/size). Reuses the normal reduce path so PnL/account/status
        finalize identically to a master-driven close."""
        found = self._ep_by_pos(pos_id)
        if not found:
            if self.execution_mode == "live" and source_event_id:
                recovered = self.db.execute(
                    "SELECT 1 FROM execution_order_intent WHERE session_id=? "
                    "AND (source_fill_id=? OR source_fill_id LIKE ?) AND filled_size>0 "
                    "AND state IN ('filled','partial') LIMIT 1",
                    (self.execution_session_id, source_event_id, f"{source_event_id}:%"),
                ).fetchone()
                closed_row = self.db.execute(
                    "SELECT status,realized_pnl FROM live_copy_position WHERE pos_id=?",
                    (int(pos_id),),
                ).fetchone()
                if recovered and closed_row and closed_row[0] != "open":
                    return {
                        "positionId": pos_id, "fraction": frac, "closed": True,
                        "realizedPnl": round(f(closed_row[1]), 2), "remSize": 0.0,
                        "recovered": True, "cooldownUntil": None,
                    }
            raise ValueError(f"position {pos_id} not open/live")
        addr, coin, ep = found
        if ep.get("entry_px") is None:
            raise ValueError(f"position {pos_id} still opening")
        frac = max(0.0, min(1.0, frac))
        if frac <= 0:
            raise ValueError("fraction must be > 0")
        ba = self.bbo.get(coin)
        if ba and ba[0] and ba[1]:
            exit_px = ba[0] if ep.get("sign", 1) > 0 else ba[1]
        else:
            exit_px = self._mark_px(coin, ep["entry_px"])
        full = frac >= 0.999
        # LiveExecutor already owns bounded IOC attempts and deterministic
        # recovery. Re-entering it here repeats full account reconciliation
        # after a verified failure without making the close safer.
        attempts = 1
        last_error = None
        for attempt in range(attempts):
            token = None
            if source_event_id:
                # Keep one logical action id across outer retries. LiveExecutor's deterministic attempt
                # indexes recover a prior ambiguous/partial fill and submit only the verified remainder.
                token = _SOURCE_EVENT_ID.set(source_event_id)
            try:
                await self._apply_reduce(
                    addr, coin, ep, now_ms(), exit_px, 0.0, 0.0,
                    closing=full, liq=False, forced_px=exit_px, forced_frac=frac,
                )
                last_error = None
            except RetryableSignalError as exc:
                last_error = exc
            finally:
                if token is not None:
                    _SOURCE_EVENT_ID.reset(token)
            if not full or (addr, coin) not in self.open_ep:
                break
            if attempt + 1 < attempts:
                await asyncio.sleep(min(2.0, 0.25 * (2 ** attempt)))
        if last_error is not None or (full and (addr, coin) in self.open_ep):
            raise RuntimeError("manual_close_incomplete") from last_error
        # Partial manual profit-taking/stop-loss keeps this episode live: subsequent target adds and reduces
        # continue through the same ``ep``. A full manual loss is a risk veto and gets 24h cooldown; a full
        # profitable/breakeven exit is merely early profit-taking and must not block the target's next episode.
        cooldown_until = None
        if full and f(ep.get("realized_pnl")) < 0:
            cooldown_until = self._add_manual_close_cooldown(addr, coin, pos_id)
        elif full:
            self._clear_manual_close_cooldown(addr, coin)
        _log(f"MANUAL-{'CLOSE' if full else f'REDUCE {int(round(frac*100))}%'} {addr[:10]} {coin} {ep['side']} "
             f"@ {exit_px:g}  pnl=${ep['realized_pnl']:+,.1f}  bal=${self.balance:,.0f}")
        actually_closed = (addr, coin) not in self.open_ep
        return {"positionId": pos_id, "exit": exit_px, "fraction": frac, "closed": actually_closed,
                "realizedPnl": round(ep["realized_pnl"], 2), "remSize": round(ep["rem_size"], 8),
                "cooldownUntil": cooldown_until}

    async def _cmd_close_all(self, *, source_event_id=None):
        pos_ids = [ep["pos_id"] for ep in self.open_ep.values()]
        closed = []
        failed = []
        for pid in pos_ids:
            try:
                await self._cmd_close(
                    pid, source_event_id=f"{source_event_id or 'close_all'}:position:{pid}",
                )
                closed.append(pid)
            except Exception as exc:  # noqa: BLE001
                failed.append(pid)
                _log(f"close_all: position {pid} incomplete: {str(exc)[:120]}")
        if self.execution_mode == "live":
            result = await self._reconcile_live_once()
            if not result or not result.get("ok"):
                failed.extend(pid for pid in pos_ids if pid not in failed and pid not in closed)
        if failed or self.open_ep:
            raise RuntimeError(f"close_all_incomplete:{len(set(failed)) or len(self.open_ep)}")
        return {"closed": closed, "count": len(closed), "exchangeFlat": True}

    def _finish_live_session_if_drained(self):
        if self.execution_mode != "live" or not self.draining or self.open_ep:
            return False
        session_id = self.live_executor.session["session_id"]
        active_orders = self.db.execute(
            "SELECT COUNT(*) FROM execution_order_intent WHERE session_id=? "
            "AND state IN ('created','submitting','resting','ambiguous')",
            (session_id,),
        ).fetchone()[0]
        projection = self.db.execute(
            "SELECT COUNT(*) FROM execution_position_projection WHERE session_id=? AND ABS(signed_size)>1e-12",
            (session_id,),
        ).fetchone()[0]
        if active_orders or projection:
            return False
        stamp = now_iso()
        self.db.execute(
            "UPDATE execution_session SET state='stopped',stopped_at=?,stop_reason='drained',updated_at=? "
            "WHERE session_id=?",
            (stamp, stamp, session_id),
        )
        self.db.execute(
            "UPDATE execution_control SET state='live_ready',active_session_id=NULL,updated_at=? WHERE id=1",
            (stamp,),
        )
        self.db.commit()
        self.execution_state = "live_ready"
        self.paused = False
        self.draining = False
        self.stop = True
        self._write_proc_status("stopped")
        self._interrupt_ws_for_stop()
        return True

    def _cmd_wallet_exit_request(self, addr, *, reload_strategy=True):
        """Capture the current execution ledger's cohort and start a conditional exit.

        Paper and Live positions are independent.  A position in the inactive
        ledger must never keep a wallet in Core for the selected execution mode.
        Once released, any inactive-ledger position is still picked up by that
        ledger's normal held-off/exit-only reload path when it next runs.
        """
        addr = addr.lower()
        ts = now_iso()
        position_ids = sorted(
            int(row[0]) for row in self.db.execute(
                f"SELECT pos_id FROM {self.taker.pos_table} "
                "WHERE lower(addr)=lower(?) AND status='open'",
                (addr,),
            ).fetchall()
        )
        intent = "draining" if position_ids else "requalify"
        captured = json.dumps(position_ids, separators=(",", ":"))
        cur = self.db.execute(
            "UPDATE target_controls SET enabled=0,intent=?,intent_requested_at=?,"
            "intent_position_ids_json=?,intent_resolved_at=NULL,intent_resolution=NULL,updated_at=? "
            "WHERE lower(addr)=lower(?)",
            (intent, ts, captured, ts, addr),
        )
        if cur.rowcount == 0:
            self.db.execute(
                "INSERT INTO target_controls "
                "(addr,enabled,intent,intent_requested_at,intent_position_ids_json,updated_at) "
                "VALUES (?,0,?,?,?,?)",
                (addr, intent, ts, captured, ts),
            )
        self.db.commit()
        if reload_strategy:
            self._reload_strategy()
        return {
            "address": addr, "intent": intent, "enabled": False,
            "executionMode": self.execution_mode,
            "capturedPositionIds": position_ids,
        }

    def _cmd_wallet_exit_cancel(self, addr, *, reload_strategy=True):
        """Cancel an unresolved draining request and restore normal Core execution."""
        addr = (addr or "").lower()
        generation = selection.latest_published_generation(self.db)
        current_core = self.db.execute(
            "SELECT 1 FROM follow_selection WHERE generation=? AND lower(addr)=lower(?) "
            "AND lower(role)='core' AND COALESCE(enabled,1)=1",
            (generation, addr),
        ).fetchone() if generation else None
        if not current_core:
            raise ValueError("wallet_exit_cancel_requires_current_core")
        blocked = self.db.execute(
            "SELECT risk_level,risk_block_reason FROM wallet_registry WHERE lower(addr)=lower(?)",
            (addr,),
        ).fetchone()
        if blocked and (blocked[0] == "high" or blocked[1]):
            raise ValueError("wallet is blocked by durable risk state")
        control = self.db.execute(
            "SELECT COALESCE(intent,'active') FROM target_controls WHERE lower(addr)=lower(?)",
            (addr,),
        ).fetchone()
        if not control or control[0] != "draining":
            raise ValueError("wallet_exit_not_draining")
        ts = now_iso()
        self.db.execute(
            "UPDATE target_controls SET enabled=1,intent='active',"
            "intent_position_ids_json=NULL,intent_resolved_at=?,"
            "intent_resolution='operator_cancelled_exit',updated_at=? "
            "WHERE lower(addr)=lower(?) AND intent='draining'",
            (ts, ts, addr),
        )
        self.db.commit()
        if reload_strategy:
            self._reload_strategy()
        return {
            "address": addr, "intent": "active", "enabled": True,
            "resolution": "operator_cancelled_exit",
        }

    def _cmd_wallet_toggle(self, addr, enabled, *, reload_strategy=True):
        """Compatibility adapter for pre-migration Dashboard clients."""
        if not enabled:
            return self._cmd_wallet_exit_request(addr, reload_strategy=reload_strategy)
        addr = addr.lower()
        blocked = self.db.execute(
            "SELECT risk_level,risk_block_reason FROM wallet_registry WHERE lower(addr)=lower(?)",
            (addr,),
        ).fetchone()
        if blocked and (blocked[0] == "high" or blocked[1]):
            raise ValueError("wallet is blocked by durable risk state")
        ts = now_iso()
        cur = self.db.execute(
            "UPDATE target_controls SET enabled=1,intent='active',"
            "intent_resolved_at=?,intent_resolution='legacy_manual_reenable',updated_at=? "
            "WHERE lower(addr)=lower(?)",
            (ts, ts, addr),
        )
        if cur.rowcount == 0:
            self.db.execute(
                "INSERT INTO target_controls "
                "(addr,enabled,intent,intent_resolved_at,intent_resolution,updated_at) "
                "VALUES (?,1,'active',?,'legacy_manual_reenable',?)",
                (addr, ts, ts),
            )
        self.db.commit()
        if reload_strategy:
            self._reload_strategy()
        return {"address": addr, "enabled": True, "intent": "active"}

    def _resolve_draining_intent(self, addr, *, reload_strategy=True):
        """Resolve one captured cohort once every captured position is terminal."""
        addr = (addr or "").lower()
        row = self.db.execute(
            "SELECT intent_position_ids_json FROM target_controls "
            "WHERE lower(addr)=lower(?) AND intent='draining'",
            (addr,),
        ).fetchone()
        if not row:
            return None
        try:
            position_ids = [
                int(value) for value in json.loads(row[0] or "[]")
            ]
        except (TypeError, ValueError):
            position_ids = []
        if not position_ids:
            return None
        marks = ",".join("?" for _ in position_ids)
        positions = self.db.execute(
            f"SELECT pos_id,status,COALESCE(realized_pnl,0),COALESCE(was_liq,0) "
            f"FROM {self.taker.pos_table} WHERE pos_id IN ({marks})",
            tuple(position_ids),
        ).fetchall()
        by_id = {int(item[0]): item for item in positions}
        if any(
            pos_id not in by_id or by_id[pos_id][1] == "open"
            for pos_id in position_ids
        ):
            return {
                "address": addr, "intent": "draining",
                "remaining": sum(
                    1 for pos_id in position_ids
                    if pos_id not in by_id or by_id[pos_id][1] == "open"
                ),
            }
        net_pnl = sum(float(by_id[pos_id][2] or 0.0) for pos_id in position_ids)
        liquidated = any(bool(by_id[pos_id][3]) for pos_id in position_ids)
        risk = self.db.execute(
            "SELECT risk_level,risk_block_reason FROM wallet_registry WHERE lower(addr)=lower(?)",
            (addr,),
        ).fetchone()
        high_or_blocked = bool(risk and (risk[0] == "high" or risk[1]))
        recovered = net_pnl > 0 and not liquidated and not high_or_blocked
        intent = "active" if recovered else "requalify"
        resolution = (
            "captured_cohort_profitable_recovered" if recovered
            else "captured_cohort_high_risk" if high_or_blocked
            else "captured_cohort_liquidated" if liquidated
            else "captured_cohort_not_profitable"
        )
        ts = now_iso()
        self.db.execute(
            "UPDATE target_controls SET enabled=?,intent=?,intent_resolved_at=?,"
            "intent_resolution=?,updated_at=? WHERE lower(addr)=lower(?)",
            (1 if recovered else 0, intent, ts, resolution, ts, addr),
        )
        self.db.commit()
        if reload_strategy:
            self._reload_strategy()
        return {
            "address": addr, "intent": intent, "resolution": resolution,
            "capturedNetPnl": net_pnl, "liquidated": liquidated,
        }

    def _resolve_all_draining_intents(self):
        resolved = []
        for (addr,) in self.db.execute(
            "SELECT addr FROM target_controls WHERE intent='draining'"
        ).fetchall():
            result = self._resolve_draining_intent(addr, reload_strategy=False)
            if result and result.get("intent") != "draining":
                resolved.append(result)
        return resolved

    def _cmd_wallet_star(self, addr, starred):
        """Persist an operator-owned Core lock without mutating the published generation.

        Star creation is intentionally limited to a wallet already present in the current Core.  The next
        scanner formation consumes this durable control as a required member; removing a star merely hands
        the wallet back to normal automatic selection on the following generation.
        """
        addr = (addr or "").strip().lower()
        if not addr:
            raise ValueError("wallet address is required")
        if starred:
            generation = selection.latest_published_generation(self.db)
            row = self.db.execute(
                "SELECT 1 FROM follow_selection WHERE generation=? AND lower(addr)=? "
                "AND lower(role)='core' LIMIT 1",
                (generation, addr),
            ).fetchone() if generation else None
            if not row:
                raise ValueError("only a current Core wallet can be starred")
        ts = now_iso()
        cur = self.db.execute(
            "UPDATE target_controls SET pinned=?,"
            "pinned_at=CASE WHEN ?=1 AND COALESCE(pinned,0)=0 THEN ? "
            "               WHEN ?=0 THEN NULL ELSE pinned_at END,updated_at=? WHERE lower(addr)=?",
            (1 if starred else 0, 1 if starred else 0, ts, 1 if starred else 0, ts, addr),
        )
        if cur.rowcount == 0:
            self.db.execute(
                "INSERT INTO target_controls (addr,enabled,pinned,pinned_at,updated_at) VALUES (?,?,?,?,?)",
                (addr, 1, 1 if starred else 0, ts if starred else None, ts),
            )
        self.db.commit()
        row = self.db.execute(
            "SELECT pinned_at FROM target_controls WHERE lower(addr)=?", (addr,)
        ).fetchone()
        return {
            "address": addr, "starred": bool(starred),
            "starredAt": row[0] if row and starred else None,
        }

    # -- SIGNAL: continuous REST poll of the whole watchlist -----------------
    async def poll_loop(self):
        """Poll targets sequentially, starting at most one REST request every five seconds.

        Cursor overlap and TID dedup retain the same no-miss/restart guarantees.
        The lower cadence deliberately trades sub-minute copy latency for stable
        REST headroom: ten wallets take roughly 50 seconds per healthy round.
        """
        last_reload = now_ms()

        async def _poll_one(addr):
            since = self.last_fill_ms.get(addr, now_ms()) - config.POLL_OVERLAP_MS
            try:
                cursor = await self._poll_fills(
                    addr, since, persist_cursor=self.execution_mode != "live",
                )
                return (addr, cursor) if cursor is not None else None
            except Exception as exc:  # noqa: BLE001 — one wallet's failure must not abort the whole round
                self._rollback_db()
                self._tally("poll_error")
                _log(f"poll_fills {addr[:10]} error: {exc}")
                return None

        while not self.stop:
            self._assert_mode_binding()
            if now_ms() - last_reload > config.WATCHLIST_RELOAD_S * 1000:
                await self._hot_reload_strategy()
                last_reload = now_ms()
            addresses = list(self.addrs)
            if not addresses:
                await asyncio.sleep(config.TARGET_POLL_START_INTERVAL_S)
                continue
            updates = []
            for addr in addresses:
                started = time.monotonic()
                update = await _poll_one(addr)
                if update is not None:
                    updates.append(update)
                if self.stop:
                    break
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(0.0, config.TARGET_POLL_START_INTERVAL_S - elapsed))
            if self.execution_mode == "live":
                if updates:
                    try:
                        self._persist_live_cursors(updates)
                    except sqlite3.OperationalError as exc:
                        message = str(exc).lower()
                        if "locked" not in message and "busy" not in message:
                            raise
                        # The previous durable cursor remains a safe replay point.  The next round retries a
                        # newer batch, while live_fills/execution_signal dedup any already-journalled receipts.
                        self._rollback_db()

    async def signal_retry_loop(self):
        """Resume journalled Live target fills until strategy handling is terminal.

        ``processing`` is process-local ownership.  A restart converts abandoned
        rows back to retryable; deterministic CLOIDs make recovery safe even when
        the exchange filled immediately before the previous process died.
        """
        if self.execution_mode != "live":
            return
        initialized = False
        last_busy_log_ms = 0
        while not self.stop:
            try:
                if not initialized:
                    self.db.execute(
                        "UPDATE execution_signal SET state='retryable',next_attempt_ms=0,"
                        "last_error=COALESCE(last_error,'observer_restarted'),updated_at=? "
                        "WHERE mode='live' AND session_id=? AND state='processing'",
                        (now_iso(), self.execution_session_id),
                    )
                    self.db.commit()
                    initialized = True
                await self._signal_retry_once()
            except Exception as exc:  # noqa: BLE001 - SQLite writer races are locally recoverable
                if not self._is_db_contention(exc):
                    raise
                self._rollback_db()
                current_ms = now_ms()
                if current_ms - last_busy_log_ms >= 5_000:
                    _log("signal retry database busy; rolled back and will retry")
                    last_busy_log_ms = current_ms
                await asyncio.sleep(0.25)

    def _source_episode_already_superseded(
        self, signal_id: int, addr: str, coin: str, source_time_ms: int,
    ) -> bool:
        """Whether a later journalled source fill already closed/flipped this episode.

        Retrying an exposure-increasing open after the target has gone flat
        creates a position the source no longer owns. The later close/flip is
        durable evidence that the stale open must be skipped, not replayed.
        """
        later = self.db.execute(
            "SELECT payload_json FROM execution_signal WHERE mode='live' AND session_id=? "
            "AND lower(addr)=lower(?) AND coin=? AND "
            "(source_time_ms>? OR (source_time_ms=? AND signal_id>?)) "
            "ORDER BY source_time_ms,signal_id",
            (
                self.execution_session_id, addr, coin, int(source_time_ms),
                int(source_time_ms), int(signal_id),
            ),
        ).fetchall()
        for (payload_json,) in later:
            try:
                payload = json.loads(payload_json or "{}")
                signed = f(payload.get("sz")) if payload.get("side") == "B" else -f(payload.get("sz"))
                pos0 = f(payload.get("startPosition"))
                transition = classify_fill_transition(pos0, pos0 + signed)
            except Exception:  # malformed rows remain owned by normal durable-signal validation
                continue
            if transition in {"close", "flip"}:
                return True
        return False

    async def _signal_retry_once(self):
        """Process at most one durable signal; caller owns DB-contention retry."""
        self._assert_mode_binding()
        if self._strategy_bind_pending:
            # The poller still journals fills. Ordered strategy mutation resumes only after the exact
            # process bundle has been durably attached to the Live session.
            await asyncio.sleep(0.05)
            return
        if self._signal_tasks:
            await asyncio.sleep(0.05)
            return
        # With a single consumer, `processing` and no in-memory task can only mean the runner died
        # between state changes.  Reclaim it immediately instead of waiting for another process restart.
        reclaimed = self.db.execute(
            "UPDATE execution_signal SET state='retryable',next_attempt_ms=0,"
            "last_error=COALESCE(last_error,'signal_runner_abandoned'),updated_at=? "
            "WHERE mode='live' AND session_id=? AND state='processing'",
            (now_iso(), self.execution_session_id),
        ).rowcount
        if reclaimed:
            self.db.commit()
        rows = self.db.execute(
            "SELECT s.signal_id,s.payload_json FROM execution_signal s "
            "WHERE s.mode='live' AND s.session_id=? AND s.state IN ('pending','retryable') "
            "AND s.next_attempt_ms<=? AND NOT EXISTS ("
            "SELECT 1 FROM execution_signal prior WHERE prior.mode=s.mode "
            "AND prior.session_id=s.session_id AND prior.addr=s.addr "
            "AND prior.coin=s.coin AND prior.state IN ('pending','retryable','processing') "
            "AND (prior.source_time_ms<s.source_time_ms OR "
            "(prior.source_time_ms=s.source_time_ms AND prior.signal_id<s.signal_id))) "
            "ORDER BY s.source_time_ms,s.signal_id LIMIT 1",
            (
                self.execution_session_id, now_ms(),
            ),
        ).fetchall()
        for signal_id, payload_json in rows:
            try:
                x = json.loads(payload_json)
                addr = str(x.get("_addr") or "").lower()
                if not addr:
                    source = self.db.execute(
                        "SELECT addr FROM execution_signal WHERE signal_id=?", (signal_id,),
                    ).fetchone()
                    addr = str(source[0] or "").lower() if source else ""
                coin = x.get("coin")
                if not addr or not coin or x.get("tid") is None:
                    raise ValueError("invalid_signal_payload")
                oid = x.get("oid")
                actions = {
                    str(row[0] or "")
                    for row in self.db.execute(
                        "SELECT action FROM live_copy_action "
                        "WHERE lower(addr)=lower(?) AND coin=? AND ts=? "
                        "AND ((master_oid=?) OR (master_oid IS NULL AND ? IS NULL))",
                        (addr, coin, int(x["time"]), oid, oid),
                    ).fetchall()
                }
                signed = f(x.get("sz")) if x.get("side") == "B" else -f(x.get("sz"))
                pos0 = f(x.get("startPosition"))
                pos1 = pos0 + signed
                transition = classify_fill_transition(pos0, pos1)
                if transition == "open" and self._source_episode_already_superseded(
                    int(signal_id), addr, coin, int(x["time"]),
                ):
                    self._mark_signal(
                        int(signal_id), "policy_skipped",
                        code="SOURCE_EPISODE_ALREADY_FLAT",
                        error="later_source_close_or_flip_journalled",
                    )
                    continue
                terminal_action = (
                    (transition == "open" and "open" in actions)
                    or (transition == "add" and "add" in actions)
                    or (transition == "reduce" and bool(actions & {"reduce", "close"}))
                    # A flip is two-phase.  A close action alone must resume the reverse open.
                    or (transition == "flip" and "open" in actions)
                )
                if terminal_action:
                    self._mark_signal(signal_id, "completed", code="LEDGER_ACTION_PRESENT")
                    continue
                self._dispatch_fill(
                    addr, coin, (addr, coin), int(x["time"]), signed, pos0, pos1,
                    f(x.get("px")), bool(x.get("liquidation")), oid,
                    signal_id=int(signal_id),
                )
            except Exception as exc:  # noqa: BLE001 - keep corrupt/transient rows visible
                self._rollback_db()
                self._mark_signal(
                    int(signal_id), "retryable", code=type(exc).__name__, error=exc, retry=True,
                )
        await asyncio.sleep(0.05 if rows else 0.25)

    def _apply_authoritative_marks(self, contexts: dict, coins) -> int:
        """Apply fresh exchange markPx values and evaluate mark-based risk.

        Hyperliquid liquidation is mark-triggered.  midPx, allMids and BBO values are deliberately not
        accepted here: they remain execution/display fallbacks and can never initiate a Paper liquidation.
        """
        applied = 0
        for coin in coins:
            ctx = contexts.get(coin) if isinstance(contexts, dict) else None
            mark = f((ctx or {}).get("markPx"))
            if mark <= 0:
                continue
            self.mark_mid[coin] = mark
            self.official_mark_ms[coin] = now_ms()
            self._refresh_coin_marks_throttled(coin)
            for (a, c), ep in self.open_ep.items():
                if c == coin and ep["master_open_px"]:
                    adv = ((ep["master_open_px"] - mark) if ep["side"] == "long"
                           else (mark - ep["master_open_px"])) / ep["master_open_px"]
                    ep["mae"] = max(ep.get("mae", 0.0), adv)
            self._maybe_liquidate(coin, mark, self.taker)
            self._queue_smart_take_profit(coin, mark, self.taker)
            applied += 1
        return applied

    # -- Official mark REST fallback (normal realtime path is WS activeAssetCtx) --
    async def _refresh_stale_authoritative_marks_once(self) -> int:
        open_coins = {coin for (_, coin) in self.open_ep}
        stale_before = now_ms() - int(config.AUTHORITATIVE_MARK_WS_STALE_MS)
        stale_coins = {
            coin for coin in open_coins
            if int(self.official_mark_ms.get(coin) or 0) < stale_before
        }
        groups = {}
        for coin in stale_coins:
            dex = None if coin in self.crypto_coins else coin.split(":", 1)[0] if ":" in coin else None
            groups.setdefault(dex, set()).add(coin)
        if not groups:
            return 0
        dexes = list(groups)
        results = await asyncio.gather(*(
            asyncio.to_thread(rest.asset_contexts, dex, False) for dex in dexes
        ))
        return sum(
            self._apply_authoritative_marks(contexts, groups[dex])
            for dex, contexts in zip(dexes, results)
        )

    async def poll_authoritative_marks(self):
        """Low-frequency REST safety fallback when official WS marks become stale.

        Both standard and HIP-3 markets normally use per-coin ``activeAssetCtx``. REST is paced and used only
        after the stream is stale long enough to preserve target-signal and order capacity.
        """
        last_log = 0
        while not self.stop:
            await asyncio.sleep(config.AUTHORITATIVE_MARK_REST_FALLBACK_S)
            try:
                applied = await self._refresh_stale_authoritative_marks_once()
                if applied and time.time() - last_log > 300:
                    _log(f"official mark REST fallback refreshed: {applied} stale open coins")
                    last_log = time.time()
            except Exception as exc:  # noqa: BLE001
                self._rollback_db()
                _log(f"official mark REST fallback failed: {exc}")

    @staticmethod
    def _quiet(loop, context):
        msg = str(context.get("exception") or context.get("message"))
        if "SSL" in msg or "closed" in msg.lower():
            return
        loop.default_exception_handler(context)

    # -- run: REST signal tasks + a WS connection for bbo pricing ------------
    async def run(self):
        asyncio.get_event_loop().set_exception_handler(self._quiet)
        strategy_recovery_requested = False
        if self.execution_mode == "live":
            strategy_recovery_requested = self._strategy_revision_recovery_requested()
            self._live_executor_db = self._open_live_executor_db()
            self.live_executor = LiveExecutor.from_db(self._live_executor_db)
            # Startup uses the same recoverable exchange+ledger boundary as steady-state Live. A harmless
            # Dashboard/maintenance commit during systemd restart must not crash-loop the real-position owner.
            result = await self._reconcile_live_with_retry(attempts=4, retry_all=True)
            if not result.get("ok"):
                self.paused = True
                self.execution_state = "reconcile_required"
        # Use the same public executable universe boundary as Scanner/Profile.  ``copyable_universe``
        # fails closed if either standard Crypto or the transparent builder/stock universe is missing;
        # starting with a partial set would silently ignore one whole sector of Core signals.
        self.valid_coins = rest.copyable_universe(force=True)
        self.crypto_coins = rest.perp_universe()           # classification only; every perp prices via WS
        if not self.crypto_coins or not self.crypto_coins.issubset(self.valid_coins):
            raise RuntimeError("copyable universe mismatch — refusing partial Observer market scope")
        _log(f"universe: {len(self.crypto_coins)} crypto + "
             f"{len(self.valid_coins) - len(self.crypto_coins)} builder/stock (unified WS pricing)")
        self._load_account(self.taker)
        self._reload_open(self.taker)
        ledger_projection_ok = True
        if self.execution_mode == "live":
            self._settle_forced_liquidations()
            ledger_projection_ok = self._verify_live_ledger_projection()
        self._resolve_all_draining_intents()
        self.vol = volatility.load_all(self.db)    # warm the σ read-cache from coin_vol (restart-safe)
        await self._reconcile_open()               # close any copy whose master went flat while we were down
        self._reload_strategy(init=True)           # atomic Core + exact follow-param revision
        try:
            self._bind_live_strategy_revision()
        except Exception:
            control_state = "STRATEGY_REVISION_MISMATCH"
            self.db.execute(
                "UPDATE execution_session SET state='reconcile_required',updated_at=? WHERE session_id=?",
                (now_iso(), self.live_executor.session["session_id"]),
            )
            self.db.execute(
                "UPDATE execution_control SET state='reconcile_required',last_error_code=?,last_error_at=?,"
                "updated_at=? WHERE id=1",
                (control_state, now_iso(), now_iso()),
            )
            self.db.commit()
            raise RuntimeError("live_strategy_revision_mismatch")
        if strategy_recovery_requested:
            # Reconcile once more after startup close/recovery work and after the immutable
            # revision binding has advanced.  Only this fresh, fully consistent state may
            # reopen exposure after a lineage-only restart failure.
            recovery_result = await self._reconcile_live_with_retry(attempts=4, retry_all=True)
            self._settle_forced_liquidations()
            ledger_projection_ok = (
                bool(recovery_result.get("ok")) and self._verify_live_ledger_projection()
            )
            self._resume_after_strategy_revision_recovery(
                requested=True,
                reconcile_result=recovery_result,
                ledger_projection_ok=ledger_projection_ok,
            )
        try:
            if self._refresh_live_wallet_risks():
                self._reload_strategy()
        except Exception as exc:  # noqa: BLE001 — risk refresh is retried by live stats
            self._rollback_db()
            _log(f"wallet risk startup refresh failed: {exc}")
        try:
            self._write_proc_status(self._proc_state)  # preserve operator pause across worker restarts
        except Exception as exc:  # noqa: BLE001 — status is non-essential; never block the engine
            _log(f"proc status init failed: {exc}")
        self._spawn_background(self.consume_commands(), "commands", critical=True)
        self._spawn_background(self.mark_refresh_loop(), "marks", critical=False)
        if self.execution_mode == "live":
            self._spawn_background(self.live_reconcile_loop(), "live_reconcile", critical=True)
            self._spawn_background(self.signal_retry_loop(), "signal_retry", critical=True)
        self._spawn_background(self._announce(), "announce", critical=False)
        self._spawn_background(self.prewarm_vol(), "prewarm_vol", critical=False)
        self._spawn_background(self.vol_refresh_loop(), "vol_refresh", critical=False)
        self._spawn_background(self.reconcile_loop(), "target_reconcile", critical=True)
        self._spawn_background(self.wallet_safety_retry_loop(), "wallet_safety", critical=False)
        self._spawn_background(self.prune_live_fills(), "prune", critical=False)
        self._spawn_background(self.poll_authoritative_marks(), "authoritative_marks", critical=False)
        self._spawn_background(self.poll_loop(), "poll", critical=True)
        while not self.stop:                        # WS: PRICING only (per-coin bbo, no user subs)
            try:
                async with websockets.connect(config.WS_URL, ping_interval=None, max_size=None) as conn:
                    self.ws = conn
                    hb = asyncio.create_task(self.heartbeat(), name="observer:ws_heartbeat")
                    subscribe = asyncio.create_task(self.subscribe_bbo(), name="observer:ws_subscribe")
                    try:
                        _log(
                            f"bbo ws connected ({len(self.addrs)} wallets polled, "
                            f"{len(self.open_ep)} open copies)"
                        )
                        async for raw in conn:
                            self.on_message(raw)
                    finally:
                        hb.cancel()
                        subscribe.cancel()
                        await asyncio.gather(hb, subscribe, return_exceptions=True)
            except Exception as exc:  # noqa: BLE001
                self.ws = None
                self._rollback_db()
                _log(f"bbo ws error: {exc}; reconnecting in 3s")
                await asyncio.sleep(3)
        for task in list(self._background_tasks.values()):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*list(self._background_tasks.values()), return_exceptions=True)
        # A signal task may be awaiting a signed order in asyncio.to_thread; cancelling the coroutine does
        # not stop that worker thread.  Let the bounded exchange call return and persist its terminal intent
        # before releasing the lease/closing the executor DB.
        if self._signal_tasks:
            await asyncio.gather(*list(self._signal_tasks), return_exceptions=True)
        if self.live_executor is not None:
            try:
                self.live_executor.release_lease()
            except Exception:  # noqa: BLE001 - process exit must not mask the completed drain
                pass
        if self._live_executor_db is not None and self._live_executor_db is not self.db:
            try:
                self._live_executor_db.close()
            except Exception:  # noqa: BLE001 - shutdown must not mask the completed drain
                pass
        self._raise_critical_background_failure()

    # -- WS message router: unified standard/HIP-3 BBO + official marks ------
    def on_message(self, raw: str):
        m = json.loads(raw)
        if m.get("channel") == "bbo":
            self.on_bbo(m.get("data", {}))
        elif m.get("channel") == "activeAssetCtx":
            data = m.get("data") or {}
            coin = data.get("coin")
            if coin:
                self._apply_authoritative_marks({coin: data.get("ctx") or {}}, {coin})

    def on_bbo(self, d: dict):
        coin = d.get("coin")
        ba = d.get("bbo") or []
        if not coin or len(ba) < 2 or not ba[0] or not ba[1]:
            return
        bid, ask = f(ba[0].get("px")), f(ba[1].get("px"))
        if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:   # crossed/zero book → junk tick, ignore
            return
        self.bbo[coin] = (bid, ask)
        self.bbo_ms[coin] = now_ms()
        self._refresh_coin_marks_throttled(coin)
        mid = (bid + ask) / 2
        for (a, c), ep in self.open_ep.items():     # track worst adverse excursion while open
            if c == coin and ep["master_open_px"]:
                adv = ((ep["master_open_px"] - mid) if ep["side"] == "long"
                       else (mid - ep["master_open_px"])) / ep["master_open_px"]
                ep["mae"] = max(ep.get("mae", 0.0), adv)
        self._queue_smart_take_profit(coin, mid, self.taker)

    def _record_fill(self, addr, x) -> bool:
        """Insert the (aggregated, trade-level) fill; True if NEW, False if this tid was already seen
        (dedup) — what makes process_fill idempotent so overlapping poll rounds can't double-copy."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO live_fills (addr,tid,time_ms,coin,side,dir,px,sz,closed_pnl,crossed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (addr, x.get("tid"), x.get("time"), x.get("coin"), x.get("side"), x.get("dir"),
             f(x.get("px")), f(x.get("sz")), f(x.get("closedPnl")), 1 if x.get("crossed") else 0))
        return cur.rowcount > 0

    def _ensure_execution_signal(self, addr: str, x: dict) -> int:
        addr = str(addr or "").lower()
        stamp = now_iso()
        self.db.execute(
            "INSERT OR IGNORE INTO execution_signal "
            "(mode,session_id,addr,coin,tid,source_time_ms,source_order_id,payload_json,state,"
            "received_at,updated_at) VALUES ('live',?,?,?,?,?,?,?,'pending',?,?)",
            (
                self.execution_session_id, addr, str(x["coin"]), int(x["tid"]), int(x["time"]),
                str(x.get("oid")) if x.get("oid") is not None else None,
                json.dumps(x, sort_keys=True, separators=(",", ":")), stamp, stamp,
            ),
        )
        row = self.db.execute(
            "SELECT signal_id FROM execution_signal WHERE mode='live' AND session_id=? "
            "AND lower(addr)=lower(?) AND tid=?",
            (self.execution_session_id, addr, int(x["tid"])),
        ).fetchone()
        if not row:
            raise RuntimeError("live_signal_journal_failed")
        return int(row[0])

    # -- core: master fills -> copy actions ----------------------------------
    def process_fill(self, addr: str, x: dict):
        coin = x.get("coin")
        if not coin or x.get("tid") is None:
            return
        inserted = self._record_fill(addr, x)
        signal_id = None
        if self.execution_mode == "live":
            if inserted:
                signal_id = self._ensure_execution_signal(addr, x)
            else:
                # A durable pending/retryable row is owned by signal_retry_loop.  A terminal row is the
                # real idempotency boundary; raw receipt alone is never proof of execution completion.
                return
        elif not inserted:
            return                          # already processed this tid (poll overlap) — idempotent
        self._tally("seen")                 # a fresh target fill reached us (proves ingestion is alive)
        self.last_fill_ms[addr] = max(self.last_fill_ms.get(addr, 0), x["time"])  # advance cursor
        if self.execution_mode == "live":
            # Live ingestion owns only durable receipt.  A single ordered consumer below owns all strategy,
            # exchange and ledger mutation, so two target fills cannot interleave commits on self.db.
            if self._target_self_liquidation(addr, x):
                event_key = x.get("tid") or f"{coin}:{x['time']}"
                self._set_wallet_safety(
                    addr, "pending", event_key=event_key, occurred_at=x["time"],
                    reason="source_liquidation_pending",
                    evidence={"coin": coin, "tid": x.get("tid")},
                )
                self._spawn_background(
                    self._confirm_wallet_safety(addr, coin),
                    f"wallet_safety:{addr[:8]}:{coin}", critical=False,
                )
            if coin not in self.sub_coins:
                self._spawn_background(
                    self.ensure_coin(coin), f"ensure_coin:{coin}", critical=False,
                )
            return signal_id
        t = x["time"]
        sz = f(x.get("sz"))
        signed = sz if x.get("side") == "B" else -sz
        pos0 = f(x.get("startPosition"))
        pos1 = pos0 + signed
        px = f(x.get("px"))
        key = (addr, coin)
        liq = bool(x.get("liquidation"))
        oid = x.get("oid")
        if self._target_self_liquidation(addr, x):
            event_key = x.get("tid") or f"{coin}:{t}"
            self._set_wallet_safety(
                addr, "pending", event_key=event_key, occurred_at=t,
                reason="source_liquidation_pending",
                evidence={"coin": coin, "tid": x.get("tid")},
            )
            self._spawn_background(
                self._confirm_wallet_safety(addr, coin),
                f"wallet_safety:{addr[:8]}:{coin}", critical=False,
            )
        if coin not in self.sub_coins:
            self._spawn_background(
                self.ensure_coin(coin), f"ensure_coin:{coin}", critical=False,
            )

        self._dispatch_fill(
            addr, coin, key, t, signed, pos0, pos1, px, liq, oid, signal_id=signal_id,
        )

    def _dispatch_fill(self, addr, coin, key, t, signed, pos0, pos1, px, liq, oid, *, signal_id=None):
        book = self.taker
        transition = classify_fill_transition(pos0, pos1)
        target_in_position = abs(pos1) >= config.FLAT
        cooldown_until = self._manual_close_cooldown_until(addr, coin) if target_in_position else None
        side = "long" if pos1 > 0 else "short"
        risk_block = self._new_exposure_block_reason(addr, coin, book, side=side) if target_in_position else None
        ep = book.open_ep.get(key)
        if ep is not None and ep.get("entry_px") is None and transition in ("open", "flip"):
            ep["open_oid"] = oid
            self._schedule_signal_task(
                signal_id, self._resolve_entry(addr, coin, ep, t, px, book), name="resume_open",
            )
            return
        if ep is None:
            opening_lifecycle = transition in ("open", "flip")
            if opening_lifecycle and target_in_position:
                if cooldown_until:
                    self._tally("skip_manual_cooldown", book)
                    if signal_id is not None:
                        self._mark_signal(signal_id, "policy_skipped", code="manual_cooldown")
                elif risk_block:
                    self._tally(f"skip_{risk_block}", book)
                    if signal_id is not None:
                        self._mark_signal(signal_id, "policy_skipped", code=risk_block)
                elif (addr not in self.held_off       # held-off (off-watchlist) = exit-only, no new opens
                        and not self.paused           # dashboard pause = no new opens (existing keep to close)
                        and self._sector_allowed(addr, coin)):
                    self._start_source_open(
                        addr, coin, t, px, pos1, oid, book, signal_id=signal_id,
                    )
                else:
                    reason = ("paused" if self.paused else
                              "heldoff" if addr in self.held_off else
                              "sector_disabled" if not self._sector_allowed(addr, coin) else
                              "midway")
                    self._tally("skip_paused" if self.paused else
                                "skip_heldoff" if addr in self.held_off else
                                "skip_sector_disabled" if not self._sector_allowed(addr, coin) else
                                "skip_midway", book)
                    if signal_id is not None:
                        if self.execution_state == "reconcile_required":
                            self._mark_signal(
                                signal_id, "retryable", code="RECONCILE_REQUIRED",
                                error="live reconciliation required", retry=True,
                            )
                        else:
                            self._mark_signal(signal_id, "policy_skipped", code=reason)
            elif not target_in_position:
                if signal_id is not None:
                    self._mark_signal(signal_id, "completed", code="NO_MANAGED_POSITION")
            else:                                       # a fresh open we chose not to take → tally the reason
                reason = ("manual_cooldown" if cooldown_until else risk_block if risk_block else
                          "paused" if self.paused else "heldoff" if addr in self.held_off else
                          "sector_disabled" if not self._sector_allowed(addr, coin) else "midway")
                self._tally(f"skip_{reason}", book)
                if signal_id is not None:
                    if self.execution_state == "reconcile_required":
                        self._mark_signal(
                            signal_id, "retryable", code="RECONCILE_REQUIRED",
                            error="live reconciliation required", retry=True,
                        )
                    else:
                        self._mark_signal(signal_id, "policy_skipped", code=reason)
            return
        # Persist every observed target size, including tiny reductions that the 10% mirror step suppresses.
        # The protected-tail exit therefore measures the target's true cumulative reduction, not our actions.
        ep["master_current"] = abs(pos1)
        self.db.execute(
            f"UPDATE {book.pos_table} SET master_current_sz=? WHERE pos_id=?",
            (abs(pos1), ep["pos_id"]),
        )
        if transition == "flip":
            ep["master_peak"] = max(ep["master_peak"], abs(pos0))
            self._schedule_signal_task(
                signal_id,
                self._apply_flip(addr, coin, ep, t, px, pos0, pos1, liq, oid, book),
                name="flip",
            )
            return
        ep["master_peak"] = max(ep["master_peak"], abs(pos1))
        if transition == "add":
            # One target order may fill in many slices over tens of seconds. Accumulate it until the first
            # actionable Copy execution, then seal the OID. Later source slices only update source exposure;
            # they can never send a second Copy add. Old finalised orders loaded from disk remain idempotent.
            add_orders = ep.setdefault("add_orders", {})
            if oid is not None and oid in ep.get("source_open_oids", ()):
                source_open_notional = f(ep.get("master_first_notl")) + abs(signed) * px
                ep["master_first_notl"] = source_open_notional
                if abs(pos1) > 0:
                    ep["master_open_px"] = source_open_notional / abs(pos1)
                self.db.execute(
                    f"UPDATE {book.pos_table} SET master_open_px=?,master_peak_sz=?,"
                    "master_current_sz=?,master_open_notional=? WHERE pos_id=?",
                    (
                        ep["master_open_px"], ep["master_peak"], abs(pos1),
                        source_open_notional, ep["pos_id"],
                    ),
                )
                self.db.commit()
                if signal_id is not None:
                    self._mark_signal(signal_id, "completed", code="OPEN_ORDER_EXTENSION")
                return
            if oid is not None and oid in ep.get("seen_oids", ()) and oid not in add_orders:
                m_now = abs(pos1)
                if m_now > 0 and px and ep.get("master_open_px"):
                    m_prev = abs(pos1 - signed)
                    ep["master_open_px"] = (
                        m_prev * ep["master_open_px"] + abs(signed) * px
                    ) / m_now
                    self.db.execute(
                        f"UPDATE {book.pos_table} SET master_open_px=?,master_peak_sz=?,"
                        "master_current_sz=? WHERE pos_id=?",
                        (ep["master_open_px"], ep["master_peak"], abs(pos1), ep["pos_id"]),
                    )
                    self.db.commit()
                if signal_id is not None:
                    self._mark_signal(signal_id, "completed", code="ORDER_ALREADY_CONSUMED")
                return
            if (
                self.paused
                or addr in self.held_off
                or addr in self.entry_frozen
                or not self._sector_allowed(addr, coin)
            ):
                self._tally("skip_paused_add" if self.paused else
                            "skip_heldoff_add" if addr in self.held_off else
                            "skip_retention_probation_add" if addr in self.entry_frozen else
                            "skip_sector_add", book)
                if signal_id is not None:
                    if self.execution_state == "reconcile_required":
                        self._mark_signal(
                            signal_id, "retryable", code="RECONCILE_REQUIRED",
                            error="live reconciliation required", retry=True,
                        )
                    else:
                        self._mark_signal(signal_id, "policy_skipped", code="ADD_BLOCKED")
                return
            self._schedule_signal_task(
                signal_id, self._apply_add(addr, coin, ep, t, px, signed, pos1, oid, book),
                name="add",
            )
        else:
            self._schedule_signal_task(
                signal_id,
                self._apply_reduce(addr, coin, ep, t, px, signed, pos1,
                                   closing=abs(pos1) < config.FLAT, liq=liq, oid=oid, book=book),
                name="reduce",
            )

    def _start_source_open(self, addr, coin, t, px, pos1, oid, book=None, *, forced_entry_px=None,
                           signal_id=None):
        """Follow every source opening signal; our own sizing surface owns the resulting order amount."""
        book = book or self.taker
        opening_oids = set()
        if oid is not None:
            opening_oids.add(oid)
        ep = self._open_position(
            addr, coin, t, px, pos1, oid, book,
            forced_entry_px=forced_entry_px,
            source_open_oids=opening_oids,
            schedule_entry=signal_id is None,
        )
        if signal_id is not None:
            if ep is None:
                self._mark_signal(signal_id, "policy_skipped", code="OPEN_POLICY")
            else:
                self._schedule_signal_task(
                    signal_id, self._resolve_entry(addr, coin, ep, t, px, book), name="open",
                )
        return ep

    async def _apply_flip(self, addr, coin, ep, t, master_px, pos0, pos1, liq, oid,
                          book=None, forced_px=None):
        book = book or self.taker
        await self._apply_reduce(addr, coin, ep, t, master_px, -pos0, 0.0,
                                 closing=True, liq=liq, oid=oid, book=book, forced_px=forced_px)
        if (addr, coin) in book.open_ep:
            return
        if (addr in self.held_off or self.paused or not self._sector_allowed(addr, coin)
                or self._manual_close_cooldown_until(addr, coin)):
            return
        risk_block = self._new_exposure_block_reason(
            addr, coin, book, side="long" if pos1 > 0 else "short",
        )
        if risk_block:
            self._tally(f"skip_{risk_block}", book)
            return
        new_ep = self._open_position(
            addr, coin, t, master_px, pos1, oid, book,
            forced_entry_px=forced_px, source_open_oids=({oid} if oid is not None else set()),
            schedule_entry=False,
        )
        if new_ep is not None:
            await self._resolve_entry(addr, coin, new_ep, t, master_px, book)

    def _open_position(self, addr, coin, t, px, pos1, oid, book=None, forced_entry_px=None,
                       source_open_oids=None, schedule_entry=True):
        book = book or self.taker
        side = "long" if pos1 > 0 else "short"
        risk_block = self._new_exposure_block_reason(addr, coin, book, side=side)
        if risk_block:
            self._tally(f"skip_{risk_block}", book)
            return
        if coin_is_blocked(coin, self.coin_blacklist, block_korean_stocks=self.block_korean_stocks):
            self._tally("skip_coin_blacklist", book)
            return
        if not self._copyable(coin):
            self._tally("skip_opaque", book)
            return              # copy crypto + transparent builder (stocks); skip opaque/unknown
        lag_sec = max(0.0, (now_ms() - t) / 1000.0)   # copy latency: master fill -> our detection (dashboard)
        cur = self.db.execute(
            f"INSERT INTO {book.pos_table} (addr,coin,side,status,master_open_ms,master_open_px,"
            "master_peak_sz,master_current_sz,opened_at,num_actions,open_lag_sec,strategy_revision_id) "
            "VALUES (?,?,?,'open',?,?,?,?,?,0,?,?)",
            (addr, coin, side, t, px, abs(pos1), abs(pos1), now_iso(), lag_sec, self.strategy_revision_id))
        ep = {"pos_id": cur.lastrowid, "addr": addr, "coin": coin,
              "side": side, "sign": 1 if side == "long" else -1,
              "master_open_ms": t, "master_open_px": px, "master_peak": abs(pos1), "master_current": abs(pos1),
              "open_oid": oid, "leverage": 0.0, "margin": 0.0, "notional": 0.0,
              "entry_px": None, "size": 0.0, "rem_size": 0.0, "liq_px": 0.0, "realized_pnl": 0.0,
              "add_count": 0, "entries_ready": asyncio.Event(), "lock": asyncio.Lock(), "mae": 0.0,
              "num_actions": 0, "gap": False,
              "seen_oids": ({oid} if oid is not None else set()) | set(source_open_oids or ()),
              "source_open_oids": ({oid} if oid is not None else set()) | set(source_open_oids or ()),
              "add_orders": {},
              "smart_tp_armed": False, "smart_tp_stage": 0, "smart_tp_peak_pnl": 0.0,
              "smart_tp_base_size": 0.0, "smart_tp_master_anchor": 0.0,
              "smart_tp_inflight": False}  # order accumulators
        if forced_entry_px is not None:
            ep["forced_entry_px"] = forced_entry_px
        book.open_ep[(addr, coin)] = ep
        if schedule_entry:
            self._spawn_background(
                self._resolve_entry(addr, coin, ep, t, px, book),
                f"entry:{addr[:8]}:{coin}", critical=False,
            )
        return ep

    async def _resolve_entry(self, addr, coin, ep, t, master_px, book=None):
        book = book or self.taker
        is_buy = ep["side"] == "long"                # opening a long => we buy
        stale = (now_ms() - t) > STALE_MS            # backfilled-late: book is no longer the fill's
        forced_entry_px = ep.get("forced_entry_px")
        px = forced_entry_px if forced_entry_px is not None else (
            master_px if stale else await self._execution_px(coin, is_buy, master_px)
        )
        if not px or px <= 0 or not master_px or master_px <= 0:   # can't price it -> don't hold a 0-price
            self.db.execute(f"DELETE FROM {book.pos_table} WHERE pos_id=?", (ep["pos_id"],))  # position (also
            self.db.commit()                                                              # avoids /0 below)
            book.open_ep.pop((addr, coin), None)
            self._tally("skip_unpriceable", book)
            _log(f"skip {coin}: unpriceable (px={px}, master_px={master_px}) — not followed")
            return
        chase = (px - master_px) / master_px * 1e4 * ep["sign"]   # bps worse than master (+ = worse)
        if (self.max_entry_chase_pct is not None
                and chase > self.max_entry_chase_pct * 100):       # spike too far past master -> skip
            self.db.execute(f"DELETE FROM {book.pos_table} WHERE pos_id=?", (ep["pos_id"],))
            self.db.commit()
            book.open_ep.pop((addr, coin), None)
            self._tally("skip_chase", book)
            return                                            # chase-skip: price ran past master before we detected
        await self._ensure_vol(coin)                 # fetch THIS coin's real σ once (else first open = fallback)
        # Fetch source position audit and L2 concurrently outside the account lock. The L2 is assessed only after
        # sizing determines OUR actual notional, but it must not add a second network round trip to entry lag.
        (m_mgn, m_entry, m_lev), liquidity_book = await asyncio.gather(
            asyncio.to_thread(self._target_snapshot, addr, coin),
            self._live_liquidity_book(coin),
        )
        # v10 sizing: σ → tier (stable/mid/high) → margin% + leverage = the tier's LEV CAP
        #  margin = adaptive sizing equity × <tier>_margin_pct
        #  lev    = <tier>_lev_cap (clipped MIN/MAX_LEV, then ≤ venue max leverage)
        #  notional = margin·lev. NOT mirrored from the master (σ alone sizes us). A calm coin (BTC, GOLD)
        #  lands in the stable tier with big margin + high lev; a wild one (ZEC/meme) in high tier, small.
        sigma = self._sigma(coin)
        async with book.acct_lock:                   # serialize margin allocation across opens
            # Paper reads its latest local balance here. Live first refreshes exchange-authoritative equity
            # and available collateral so the sizing formula never starts from the 30-second projection cache.
            try:
                await self._refresh_live_sizing_state()
            except Exception as exc:  # no order was submitted, so the placeholder is safe to remove
                self.db.execute(f"DELETE FROM {book.pos_table} WHERE pos_id=?", (ep["pos_id"],))
                book.open_ep.pop((addr, coin), None)
                self.db.commit()
                self._tally("skip_live_reconcile", book)
                _log(f"live open {coin} deferred: reconciliation unavailable ({str(exc)[:120]})")
                if self.execution_mode == "live" and _SOURCE_EVENT_ID.get():
                    raise RetryableSignalError("live_open_reconciliation_unavailable") from exc
                return
            # MARGIN_EQUITY_PCT owns both the per-order base and aggregate fresh-entry budget. Once the
            # budget is full, adds may still use remaining real cash because they preserve copy fidelity.
            risk_equity = self._risk_equity(book)
            avail = self._risk_available(book)           # cash gate after recognizing floating losses
            # PER-COIN cap (catastrophe backstop, NOT a per-wallet tax): total margin across our open positions
            # on this coin IN THE SAME DIRECTION ≤ the σ-tier's per-coin cap (STABLE/MID/HIGH_COIN_CAP_PCT).
            # Bounds how much of the account one coin's single move can destroy when N wallets pile the SAME way
            # (e.g. all short BTC). Same-direction ONLY on purpose: an opposite-side signal (a wallet flips long
            # while we hold shorts) OFFSETS our exposure, it doesn't stack it, so it must NOT be blocked.
            existing_coin = sum(e.get("margin", 0.0) * (e["rem_size"] / e["size"] if e.get("size") else 1.0)
                                for (a2, c2), e in book.open_ep.items()   # EFFECTIVE margin (partial-close aware)
                                if c2 == coin and e.get("side") == ep["side"] and e is not ep)
            group_existing = wallet_sector_side_margin(
                (e for e in book.open_ep.values() if e is not ep),
                addr=addr, coin=coin, side=ep["side"],
            )
            group_cap = self._wallet_group_cap_pct(
                book, addr, coin, ep["side"],
                tier_for_sigma(sigma, self.high_sigma_min, coin),
                exclude=ep,
            )
            group_room = wallet_sector_side_margin_room(
                cap_pct=group_cap,
                risk_equity=risk_equity,
                existing_margin=group_existing,
            )
            source_room = margin_cap_room(
                cap_pct=self.wallet_margin_cap_pct,
                risk_equity=risk_equity,
                existing_margin=wallet_margin(
                    (e for e in book.open_ep.values() if e is not ep), addr=addr,
                ),
            )
            target_notl = abs(ep["master_peak"]) * master_px if master_px else 0.0
            master_notl = target_notl
            # The configured tier leverage is a product cap, never permission
            # to exceed Hyperliquid's per-market maximum. Live also consults
            # the broker's official meta cache so a missing warm-cache row
            # cannot produce an impossible order.
            maintenance_leverage = self._market_max_leverage(coin)
            plan = plan_open_sizing(
                coin=coin,
                side=ep["side"],
                entry_px=px,
                sigma=sigma,
                balance=risk_equity,
                available=avail,
                existing_coin_margin=existing_coin,
                master_notional=master_notl,
                master_leverage=None,
                params=self._open_sizing_params(book),
                maintenance_leverage=maintenance_leverage,
                wallet_sector_side_room=group_room,
                wallet_room=source_room,
            )
            if not plan.ok:
                self._tally(f"skip_{plan.reason}", book)
                if plan.reason == "small_notl":
                    why = (
                        f"below Hyperliquid min order ${plan.notional:,.2f} < "
                        f"${config.HYPERLIQUID_MIN_PERP_NOTIONAL_USD:,.0f} "
                        f"(source signal notl ${plan.master_notional:,.0f})"
                    )
                else:
                    why = plan.reason
                _log(f"skip {coin} {ep['side']} {addr[:10]}: {why} (room ${plan.room:,.0f} / deploy ${plan.deploy_room:,.0f} / cash ${plan.available:,.0f} / want ${plan.wanted_margin:,.0f})")
                self.db.execute(f"DELETE FROM {book.pos_table} WHERE pos_id=?", (ep["pos_id"],))  # -> skip
                self.db.commit()
                book.open_ep.pop((addr, coin), None)
                return
            liquidity = self._coin_liquidity_decision(
                coin, book_snapshot=liquidity_book, is_buy=is_buy,
                planned_notional=plan.notional,
            )
            liquidity_reason = liquidity.get("reason")
            if liquidity_reason:
                self.db.execute(f"DELETE FROM {book.pos_table} WHERE pos_id=?", (ep["pos_id"],))
                book.open_ep.pop((addr, coin), None)
                self._tally("skip_low_liquidity", book)
                self._record_live_policy_skip(
                    addr, coin, "open", liquidity_reason, now_ms(),
                )
                self.db.commit()
                _log(
                    f"skip {coin} {ep['side']} {addr[:10]}: live liquidity "
                    f"{liquidity_reason} ({self._liquidity_log_detail(liquidity)})"
                )
                return
            # Paper fills at the size-weighted live L2 price, not merely the best quote. Rebuild sizing so
            # position size and isolated liquidation price use that same honest execution price.
            l2_average_px = f(liquidity.get("average_px"))
            if forced_entry_px is None and l2_average_px > 0.0:
                px = l2_average_px
                plan = plan_open_sizing(
                    coin=coin,
                    side=ep["side"],
                    entry_px=px,
                    sigma=sigma,
                    balance=risk_equity,
                    available=avail,
                    existing_coin_margin=existing_coin,
                    master_notional=master_notl,
                    master_leverage=None,
                    params=self._open_sizing_params(book),
                    maintenance_leverage=maintenance_leverage,
                    wallet_sector_side_room=group_room,
                    wallet_room=source_room,
                )
                chase = (px - master_px) / master_px * 1e4 * ep["sign"]
                if self.max_entry_chase_pct is not None and chase > self.max_entry_chase_pct * 100:
                    self.db.execute(
                        f"DELETE FROM {book.pos_table} WHERE pos_id=?", (ep["pos_id"],),
                    )
                    self.db.commit()
                    book.open_ep.pop((addr, coin), None)
                    self._tally("skip_chase", book)
                    return
            lev = plan.leverage
            margin = plan.margin
            notional = plan.notional
            size = plan.size
            liq_px = plan.liq_px
            if self.execution_mode == "live":
                try:
                    execution = await self._execute_live_order(
                        ep=ep, addr=addr, coin=coin, action="open", is_buy=is_buy,
                        size=size, leverage=lev, reduce_only=False,
                        source_time_ms=t, source_order_id=ep.get("open_oid"),
                    )
                except Exception as exc:  # noqa: BLE001 - state machine owns the sanitized code
                    self.db.execute(
                        f"DELETE FROM {book.pos_table} WHERE pos_id=?", (ep["pos_id"],),
                    )
                    book.open_ep.pop((addr, coin), None)
                    self.db.commit()
                    self._tally("skip_live_execution", book)
                    _log(f"live open {coin} failed closed: {str(exc)[:120]}")
                    if _SOURCE_EVENT_ID.get():
                        raise RetryableSignalError("live_open_execution_failed") from exc
                    return
                if not execution or execution.filled_size <= config.FLAT or not execution.average_px:
                    self.db.execute(f"DELETE FROM {book.pos_table} WHERE pos_id=?", (ep["pos_id"],))
                    book.open_ep.pop((addr, coin), None)
                    self.db.commit()
                    self._tally("skip_live_unfilled", book)
                    if _SOURCE_EVENT_ID.get():
                        raise TerminalSignalError(
                            f"live_open_unfilled:{getattr(execution, 'error_code', None) or 'no_fill'}"
                        )
                    return
                px = float(execution.average_px)
                size = float(execution.filled_size)
                if getattr(execution, "leverage", None):
                    lev = float(execution.leverage)
                notional = size * px
                margin = notional / lev
                liq_px = isolated_liq_px(px, ep["side"], size, margin, maintenance_leverage)
                self._sync_live_account()
            ep.update(leverage=lev, margin=margin, notional=notional, entry_px=px, first_margin=margin,
                      size=size, rem_size=size, peak_size=size, liq_px=liq_px,
                      maintenance_leverage=maintenance_leverage,
                      master_first_notl=target_notl,      # confirmed source opening → smart-add ratio anchor
                      last_target_add_px=master_px)       # 波动闸只比较目标成交价；我方BBO只负责执行/PnL
            self.db.execute(                         # source audit only; our sizing never reads m_lev
                f"UPDATE {book.pos_table} SET leverage=?,margin=?,notional=?,entry_px=?,size=?,rem_size=?,peak_size=?,"
                "liq_px=?,master_leverage=?,master_margin=?,master_open_notional=?,"
                "master_open_px=COALESCE(?,master_open_px),opening_account_equity=? "
                "WHERE pos_id=?",
                (
                    lev, margin, notional, px, size, size, size, liq_px, m_lev, m_mgn,
                    target_notl, m_entry, risk_equity, ep["pos_id"],
                ))
            if self.execution_mode != "live":
                book.balance -= abs(size * px) * config.TAKER_FEE
            self._save_account(book)
            self.db.commit()
        ep["entries_ready"].set()
        msz = ep["master_peak"] * ep["sign"]
        self._record_action(ep, addr, coin, t, "open", ep["open_oid"], master_px,
                            msz, msz, size * ep["sign"], px, 0.0, chase, book=book)
        self.db.commit()                                      # the open is in copy_position/copy_action

    async def _apply_add(self, addr, coin, ep, t, master_px, signed, pos1, oid,
                         book=None, forced_px=None):
        """Master scaled in -> we follow (average down/up) up to MAX_ADDS, each add committing
        first_margin × ADD_FRAC (half the first-open by default) at the current price; avg entry + liq_px recompute.

        Hyperliquid can fill one order as many same-oid slices. Smart mode keeps an order-level accumulator so
        tiny early slices may wait until the aggregate price/size is actionable. The first successful Copy add
        then seals the OID; later fills update source exposure but never top the Copy add up again.
        """
        book = book or self.taker
        async with ep["lock"]:
            prior_master_open_px = ep.get("master_open_px")
            prior_master_peak = ep.get("master_peak", 0.0)
            prior_add_order = None
            if oid is not None and oid in ep.setdefault("add_orders", {}):
                prior_add_order = dict(ep["add_orders"][oid])

            def _restore_retry_state():
                ep["master_open_px"] = prior_master_open_px
                ep["master_peak"] = prior_master_peak
                if oid is not None:
                    if prior_add_order is None:
                        ep.setdefault("add_orders", {}).pop(oid, None)
                    else:
                        ep.setdefault("add_orders", {})[oid] = prior_add_order

            if str(addr or "").lower() in self.safety_frozen:
                self._tally("skip_wallet_safety_frozen", book)
                return False
            if str(addr or "").lower() in self.entry_frozen:
                self._tally("skip_retention_probation_add", book)
                return False
            try:
                await asyncio.wait_for(ep["entries_ready"].wait(), timeout=12)
            except asyncio.TimeoutError:
                pass
            if ep.get("entry_px") is None or (addr, coin) not in book.open_ep:
                return False
            # Several fill callbacks for one OID may be queued before the first async add acquires this lock.
            # Re-check finality inside the lock so those queued slices cannot recreate the accumulator.
            if (
                oid is not None
                and oid in ep.get("seen_oids", ())
                and oid not in ep.setdefault("add_orders", {})
            ):
                m_now = abs(pos1)
                if m_now > 0 and master_px and ep.get("master_open_px"):
                    m_prev = abs(pos1 - signed)
                    ep["master_open_px"] = (
                        m_prev * ep["master_open_px"] + abs(signed) * master_px
                    ) / m_now
                    self.db.execute(
                        f"UPDATE {book.pos_table} SET master_open_px=?,master_peak_sz=?,"
                        "master_current_sz=? WHERE pos_id=?",
                        (ep["master_open_px"], ep.get("master_peak", 0.0), abs(pos1), ep["pos_id"]),
                    )
                    self.db.commit()
                return False
            ep["master_peak"] = max(ep.get("master_peak", 0.0), abs(pos1))
            # 源(目标)加权均价:每次目标加仓都把 master_open_px 更新为其 size 加权均价(此前只存首开价 →
            # 多次加仓的"源"价没更新、和我们的均价没法比)。用目标的真实仓位量(pos1 = 加仓后仓位,signed = 本笔量)。
            # 即使超过我们的跟随上限(下面 observe-only 分支)也更新 —— 目标仍在摊他的均价。
            m_now = abs(pos1)
            if m_now > 0 and master_px and ep.get("master_open_px"):
                m_prev = abs(pos1 - signed)           # 目标加仓前的仓位量
                ep["master_open_px"] = (m_prev * ep["master_open_px"] + abs(signed) * master_px) / m_now

            order = None
            decision_master_px = master_px
            target_add_notl = abs(signed) * master_px
            if oid is not None and self.add_strategy == "smart":
                order = ep.setdefault("add_orders", {}).setdefault(oid, {
                    "target_notl": 0.0,
                    "target_abs_sz": 0.0,
                    "target_px_notl": 0.0,
                    "followed_margin": 0.0,
                    "counted": False,
                    "base_add_count": ep.get("add_count", 0),
                })
                order["target_notl"] += target_add_notl
                order["target_abs_sz"] += abs(signed)
                order["target_px_notl"] += abs(signed) * master_px
                target_add_notl = order["target_notl"]
                if order["target_abs_sz"] > 0:
                    decision_master_px = order["target_px_notl"] / order["target_abs_sz"]

            is_buy = ep["side"] == "long"             # adding to a long => buy more
            stale = (now_ms() - t) > STALE_MS
            px = forced_px if forced_px is not None else (
                master_px if stale else await self._execution_px(coin, is_buy, master_px)
            )
            lev = ep["leverage"]
            sigma = self._sigma(coin); tier = self._tier(sigma, coin)
            fm = ep.get("first_margin", ep["margin"])

            def _observe_only(final=False):           # record his slice, DON'T follow; keep source state fresh
                self._record_action(ep, addr, coin, t, "add", oid, master_px, signed, pos1,
                                    0.0, master_px, 0.0, 0.0, book=book)
                self.db.execute(
                    f"UPDATE {book.pos_table} SET master_open_px=?,master_peak_sz=? WHERE pos_id=?",
                    (ep["master_open_px"], ep["master_peak"], ep["pos_id"]),
                )
                if final and oid is not None:
                    ep.setdefault("seen_oids", set()).add(oid)
                    ep.setdefault("add_orders", {}).pop(oid, None)
                self.db.commit()
                return False

            if self.smart_tp_enable and int(ep.get("smart_tp_stage") or 0) > 0:
                # Profit already banked by our own policy is never re-risked because the target adds again.
                self._tally("skip_smart_tp_readd", book)
                return _observe_only(final=True)

            if coin_is_blocked(coin, self.coin_blacklist, block_korean_stocks=self.block_korean_stocks):
                self._tally("skip_coin_blacklist_add", book)
                return _observe_only(final=True)

            if self.add_strategy == "smart":
                # 逆向(adv>0=价格朝我们不利方向,摊低)走 ADD_GAP_K;
                # 正向(adv<0=顺势加仓)也要过 POS_ADD_GAP_K,避免 1.01/1.02/1.03 这类小碎追单全跟。
                last = ep.get("last_target_add_px") or ep.get("master_open_px") or master_px
                adv = (((last - decision_master_px) if is_buy else (decision_master_px - last)) / last) if last else 0.0
                base_add_count = order["base_add_count"] if order else ep["add_count"]
                gap_mult = self.add_shrink_g ** base_add_count
                x = self.add_gap_k * sigma * gap_mult
                pos_x = self.pos_add_gap_k * sigma * gap_mult
                already_counted = bool(order and order["counted"])
                if not already_counted:
                    if adv >= x:                                 # ① B 逆向:摊低幅度够 → 跟
                        pass
                    elif adv < 0 and self.follow_pos_add and abs(adv) >= pos_x:
                        pass
                    else:                                        # later same-oid slices may make weighted px actionable
                        return _observe_only()
                    if ep["add_count"] >= self.add_max_hard:     # 硬顶(A/B 共用)
                        return _observe_only(final=True)
                # ③ 比例镜像，但单个目标加仓订单最多消耗一个我方首仓额度；不足整笔时填满单币余量。
                ratio = target_add_notl / ep["master_first_notl"] if ep.get("master_first_notl") else self.add_frac
                async with book.acct_lock:
                    try:
                        await self._refresh_live_sizing_state()
                    except Exception as exc:
                        self._tally("skip_live_reconcile_add", book)
                        _log(f"live add {coin} deferred: reconciliation unavailable ({str(exc)[:120]})")
                        if self.execution_mode == "live" and _SOURCE_EVENT_ID.get():
                            _restore_retry_state()
                            raise RetryableSignalError("live_add_reconciliation_unavailable") from exc
                        return _observe_only()
                    risk_equity = self._risk_equity(book)
                    coin_cap = self.tier_coin_cap[tier] * risk_equity
                    existing = sum(e.get("margin", 0.0) * (e["rem_size"] / e["size"] if e.get("size") else 1.0)
                                   for (a2, c2), e in book.open_ep.items()
                                   if c2 == coin and e.get("side") == ep["side"])   # incl THIS ep (its current margin)
                    group_existing = wallet_sector_side_margin(
                        book.open_ep.values(), addr=addr, coin=coin, side=ep["side"],
                    )
                    group_room = wallet_sector_side_margin_room(
                        cap_pct=self._wallet_group_cap_pct(book, addr, coin, ep["side"], tier),
                        risk_equity=risk_equity,
                        existing_margin=group_existing,
                    )
                    source_room = margin_cap_room(
                        cap_pct=self.wallet_margin_cap_pct,
                        risk_equity=risk_equity,
                        existing_margin=wallet_margin(book.open_ep.values(), addr=addr),
                    )
                    total_room = self._risk_available(book)
                    followed_margin = order["followed_margin"] if order else 0.0
                    add_margin = smart_add_order_margin(
                        first_margin=fm,
                        target_ratio=ratio,
                        followed_margin=followed_margin,
                        coin_room=max(0.0, coin_cap - existing),
                        risk_available=self._risk_available(book),
                        wallet_sector_side_room=group_room,
                        wallet_room=source_room,
                        total_margin_room=total_room,
                    )
                if add_margin < self.min_open_margin_pct * risk_equity * self.margin_equity_pct:  # 预算用尽 / 太小
                    if source_room <= min(group_room, max(0.0, coin_cap - existing)) + 1e-12:
                        self._tally("skip_wallet_add", book)
                    elif group_room <= max(0.0, coin_cap - existing) + 1e-12:
                        self._tally("skip_wallet_sector_side_add", book)
                    return _observe_only()
            else:                                     # hardcap: 分档次数上限 + 固定 ADD_FRAC(老逻辑)
                if ep["add_count"] >= self.tier_max_adds.get(tier, 0):
                    return _observe_only(final=True)
                async with book.acct_lock:
                    try:
                        await self._refresh_live_sizing_state()
                    except Exception as exc:
                        self._tally("skip_live_reconcile_add", book)
                        _log(f"live add {coin} deferred: reconciliation unavailable ({str(exc)[:120]})")
                        if self.execution_mode == "live" and _SOURCE_EVENT_ID.get():
                            _restore_retry_state()
                            raise RetryableSignalError("live_add_reconciliation_unavailable") from exc
                        return _observe_only()
                    risk_equity = self._risk_equity(book)
                    coin_cap = self.tier_coin_cap[tier] * risk_equity
                    existing = sum(
                        e.get("margin", 0.0) * (e["rem_size"] / e["size"] if e.get("size") else 1.0)
                        for (a2, c2), e in book.open_ep.items()
                        if c2 == coin and e.get("side") == ep["side"]
                    )
                    group_existing = wallet_sector_side_margin(
                        book.open_ep.values(), addr=addr, coin=coin, side=ep["side"],
                    )
                    group_room = wallet_sector_side_margin_room(
                        cap_pct=self._wallet_group_cap_pct(book, addr, coin, ep["side"], tier),
                        risk_equity=risk_equity,
                        existing_margin=group_existing,
                    )
                    source_room = margin_cap_room(
                        cap_pct=self.wallet_margin_cap_pct,
                        risk_equity=risk_equity,
                        existing_margin=wallet_margin(book.open_ep.values(), addr=addr),
                    )
                    total_room = self._risk_available(book)
                    add_margin = max(0.0, min(
                        fm * self.add_frac,
                        coin_cap - existing,
                        self._risk_available(book),
                        group_room,
                        source_room,
                        total_room,
                    ))
                if add_margin <= 0:
                    if source_room <= min(group_room, max(0.0, coin_cap - existing)) + 1e-12:
                        self._tally("skip_wallet_add", book)
                    elif group_room <= max(0.0, coin_cap - existing) + 1e-12:
                        self._tally("skip_wallet_sector_side_add", book)
                    return _observe_only(final=True)
            planned_add_notional = add_margin * lev
            liquidity_book = await self._live_liquidity_book(coin)
            liquidity = self._coin_liquidity_decision(
                coin, book_snapshot=liquidity_book, is_buy=is_buy,
                planned_notional=planned_add_notional,
            )
            add_liquidity_reason = liquidity.get("reason")
            if add_liquidity_reason:
                self._tally("skip_low_liquidity_add", book)
                self._record_live_policy_skip(
                    addr, coin, "add", add_liquidity_reason, now_ms(),
                )
                _log(
                    f"skip {coin} {ep['side']} add {addr[:10]}: live liquidity "
                    f"{add_liquidity_reason} ({self._liquidity_log_detail(liquidity)})"
                )
                return _observe_only(final=True)
            l2_average_px = f(liquidity.get("average_px"))
            if forced_px is None and l2_average_px > 0.0:
                px = l2_average_px
            venue_max_leverage = self._market_max_leverage(coin)
            maintenance_leverage = (
                venue_max_leverage
                if venue_max_leverage is not None
                else ep.get("maintenance_leverage")
            )
            if venue_max_leverage is not None:
                lev = min(lev, venue_max_leverage)
            live_add_size = None
            if self.execution_mode == "live":
                requested_add_size = (add_margin * lev / px) if px else 0.0
                try:
                    execution = await self._execute_live_order(
                        ep=ep, addr=addr, coin=coin, action="add", is_buy=is_buy,
                        size=requested_add_size, leverage=lev, reduce_only=False,
                        source_time_ms=t, source_order_id=oid,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._tally("skip_live_add_execution", book)
                    _log(f"live add {coin} failed closed: {str(exc)[:120]}")
                    if _SOURCE_EVENT_ID.get():
                        _restore_retry_state()
                        raise RetryableSignalError("live_add_execution_failed") from exc
                    return _observe_only(final=True)
                if not execution or execution.filled_size <= config.FLAT or not execution.average_px:
                    self._tally("skip_live_add_unfilled", book)
                    if _SOURCE_EVENT_ID.get():
                        _restore_retry_state()
                        raise TerminalSignalError(
                            f"live_add_unfilled:{getattr(execution, 'error_code', None) or 'no_fill'}"
                        )
                    return _observe_only(final=True)
                px = float(execution.average_px)
                live_add_size = float(execution.filled_size)
                if getattr(execution, "leverage", None):
                    lev = float(execution.leverage)
                add_margin = live_add_size * px / lev
                self._sync_live_account()
            basis = rebase_isolated_position(
                ep["entry_px"], ep["side"], ep["rem_size"], lev,
                maintenance_leverage,
            )
            ep.update(
                leverage=lev,
                size=basis["size"], margin=basis["margin"],
                notional=basis["notional"], liq_px=basis["liq_px"],
                maintenance_leverage=maintenance_leverage,
            )
            add_size = live_add_size if live_add_size is not None else ((add_margin * lev / px) if px else 0.0)
            new_size = ep["rem_size"] + add_size
            ep["entry_px"] = ((ep["rem_size"] * ep["entry_px"] + add_size * px) / new_size
                              if new_size else px)    # size-weighted average entry
            ep["rem_size"] = new_size
            ep["size"] += add_size
            ep["peak_size"] = max(ep.get("peak_size", 0.0), new_size)
            ep["margin"] += add_margin
            ep["notional"] += add_margin * lev
            ep["liq_px"] = isolated_liq_px(
                ep["entry_px"], ep["side"], ep["size"], ep["margin"],
                maintenance_leverage,
            )
            first_copy_for_order = not (order and order["counted"])
            if first_copy_for_order:
                ep["add_count"] += 1
            ep["last_target_add_px"] = decision_master_px  # target VWAP anchor; never an execution quote
            ep["reduce_anchor"] = None                # master grew → invalidate the reduce-step window
            ep.update(
                smart_tp_armed=False,
                smart_tp_stage=0,
                smart_tp_peak_pnl=0.0,
                smart_tp_base_size=0.0,
                smart_tp_master_anchor=0.0,
            )
            slip = (px - master_px) / master_px * 1e4 * ep["sign"] if master_px else 0.0
            self._record_action(ep, addr, coin, t, "add", oid, master_px, signed, pos1,
                                add_size * ep["sign"], px, 0.0, slip, book=book)
            if order is not None:
                order["followed_margin"] += add_margin
                order["counted"] = True
                if oid is not None:
                    ep.setdefault("add_orders", {}).pop(oid, None)
            if oid is not None:
                ep.setdefault("seen_oids", set()).add(oid)
            if self.execution_mode != "live":
                book.balance -= abs(add_size * px) * config.TAKER_FEE
            self._save_account(book)
            self.db.execute(
                f"UPDATE {book.pos_table} SET leverage=?,margin=?,notional=?,entry_px=?,size=?,rem_size=?,peak_size=?,liq_px=?,"
                "add_count=?,master_open_px=?,smart_tp_armed=0,smart_tp_stage=0,smart_tp_peak_pnl=0,"
                "smart_tp_base_size=NULL,smart_tp_master_anchor=NULL WHERE pos_id=?",
                (ep["leverage"], ep["margin"], ep["notional"], ep["entry_px"], ep["size"], ep["rem_size"], ep["peak_size"],
                 ep["liq_px"], ep["add_count"], ep["master_open_px"], ep["pos_id"]))
            self.db.commit()                                  # the add is in the action table
            return True

    async def _apply_reduce(self, addr, coin, ep, t, master_px, signed, pos1, closing, liq,
                            oid=None, gap=False, forced_px=None, forced_frac=None, book=None,
                            smart_tp_stage=None):
        book = book or self.taker
        async with ep["lock"]:
            try:
                await asyncio.wait_for(ep["entries_ready"].wait(), timeout=12)
            except asyncio.TimeoutError:
                pass
            if ep.get("entry_px") is None or (addr, coin) not in book.open_ep:
                return
            is_buy = ep["side"] == "short"           # closing a long => sell; closing a short => buy
            stale = (now_ms() - t) > STALE_MS
            exit_px = (forced_px if forced_px is not None
                       else master_px if stale else await self._execution_px(coin, is_buy, master_px))
            old_rem = ep["rem_size"]
            smart_cut = smart_tp_stage is not None
            smart_tail_close = False
            # delta-based: close the SAME fraction of our position the master just closed of his —
            # correct for any build-up (adds we followed, adds we skipped past the cap, or none).
            if smart_cut:
                if int(ep.get("smart_tp_stage") or 0) != int(smart_tp_stage):
                    return
                decision = self._smart_take_profit_decision(coin, ep, exit_px)
                if decision is None or not decision.trigger:
                    return
                reduce_frac = min(1.0, decision.close_size / max(ep["rem_size"], 1e-12))
            elif forced_frac is not None:                     # operator manual close: EXACT fraction of rem_size
                reduce_frac = max(0.0, min(1.0, forced_frac))
                closing = reduce_frac >= 0.999                # <100% keeps the position OPEN (partial reduce)
            elif closing or abs(pos1 - signed) < config.FLAT:
                reduce_frac = 1.0                             # full close always executes → exact flat
            else:
                if (self.smart_tp_enable
                        and int(ep.get("smart_tp_stage") or 0) >= len(self.smart_tp_close_pcts)):
                    anchor = float(ep.get("smart_tp_master_anchor") or 0.0)
                    if (anchor > 0
                            and abs(pos1) <= anchor * (1.0 - self.smart_tp_target_reduce_exit_pct) + config.FLAT):
                        reduce_frac = 1.0
                        closing = True
                        smart_tail_close = True
                    else:
                        # Keep the 30% tail whole.  Target trims below the cumulative line are observed
                        # through master_current_sz but are not mirrored into a sequence of residue fills.
                        return
                else:
                    # STEP-mirror: an algo master unwinds a big position in 100s of tiny orders. Instead of
                    # mirroring every dust reduce, only act once his cumulative unwind since our last reduce
                    # reaches REDUCE_STEP_FRAC of his position (→ ≤~10 partial reduces). `reduce_anchor` = his
                    # |position| at our last executed reduce (re-anchored to pre-fill size if he grew via an add).
                    pos0 = pos1 - signed
                    anchor = ep.get("reduce_anchor")
                    if not anchor or anchor <= abs(pos1):     # first reduce, or he added since → re-anchor
                        anchor = abs(pos0)
                    cum_frac = (anchor - abs(pos1)) / anchor if anchor else 0.0
                    if cum_frac < config.REDUCE_STEP_FRAC:
                        ep["reduce_anchor"] = anchor          # accumulate; skip this sub-step (no fill/log)
                        return
                    reduce_frac = min(1.0, cum_frac)          # rem still matches `anchor` → cut the whole ratio
                    ep["reduce_anchor"] = abs(pos1)           # open a fresh 10% window from here
            tail_close = False
            tail_decision = None
            dust_close = not closing and reduce_leaves_dust(ep["rem_size"], reduce_frac, exit_px)
            if dust_close:
                reduce_frac = 1.0
                closing = True
            elif (not closing and forced_frac is None and not smart_cut and not self.smart_tp_enable
                  and not liq and not gap):
                tail_decision = profit_tail_close_decision(
                    rem_size=ep["rem_size"],
                    peak_size=ep.get("peak_size") or max(ep.get("size", 0.0), ep["rem_size"]),
                    reduce_frac=reduce_frac,
                    execution_px=exit_px,
                    risk_px=self.mark_mid.get(coin) or exit_px,
                    entry_px=ep["entry_px"],
                    side=ep["side"],
                    realized_pnl=ep.get("realized_pnl", 0.0),
                    liq_px=ep.get("liq_px", 0.0),
                    fee_rate=config.TAKER_FEE,
                    enabled=self.tail_close_enable,
                    hard_remain_pct=self.tail_close_hard_remain_pct,
                    risk_remain_pct=self.tail_close_risk_remain_pct,
                    max_profit_giveback_pct=self.tail_close_profit_giveback_pct,
                )
                if tail_decision.close:
                    reduce_frac = 1.0
                    closing = True
                    tail_close = True
            close_size = ep["rem_size"] * reduce_frac
            requested_full_close = bool(closing)
            live_fee = None
            if self.execution_mode == "live":
                try:
                    execution = await self._execute_live_order(
                        ep=ep, addr=addr, coin=coin,
                        action="close" if closing else "reduce", is_buy=is_buy,
                        size=close_size, leverage=ep["leverage"], reduce_only=True,
                        source_time_ms=t, source_order_id=oid,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._tally("skip_live_reduce_execution", book)
                    _log(f"live reduce {coin} failed closed: {str(exc)[:120]}")
                    if _SOURCE_EVENT_ID.get():
                        raise RetryableSignalError("live_reduce_execution_failed") from exc
                    if requested_full_close:
                        self._schedule_live_full_close_retry(addr, coin, ep, book)
                    return
                if not execution or execution.filled_size <= config.FLAT or not execution.average_px:
                    self._tally("skip_live_reduce_unfilled", book)
                    if _SOURCE_EVENT_ID.get():
                        raise RetryableSignalError("live_reduce_unfilled")
                    if requested_full_close:
                        self._schedule_live_full_close_retry(addr, coin, ep, book)
                    return
                exit_px = float(execution.average_px)
                close_size = min(ep["rem_size"], float(execution.filled_size))
                reduce_frac = close_size / max(ep["rem_size"], 1e-12)
                closing = bool(closing and close_size >= ep["rem_size"] - config.FLAT)
                live_fee = max(0.0, float(execution.fee))
                self._sync_live_account()
            fee = live_fee if live_fee is not None else abs(close_size * exit_px) * config.TAKER_FEE
            pnl = close_size * (exit_px - ep["entry_px"]) * ep["sign"] - fee    # NET of our exit fee
            ep["rem_size"] -= close_size
            ep["realized_pnl"] += pnl
            if self.execution_mode != "live":
                book.balance += pnl                   # realize (net of fee) into the paper account
            if not closing and ep["rem_size"] > config.FLAT:
                basis = rebase_isolated_position(
                    ep["entry_px"], ep["side"], ep["rem_size"], ep["leverage"],
                    ep.get("maintenance_leverage"),
                )
                ep.update(
                    size=basis["size"], margin=basis["margin"],
                    notional=basis["notional"], liq_px=basis["liq_px"],
                )
            if smart_cut:
                ep["smart_tp_stage"] = int(smart_tp_stage) + 1
                if int(smart_tp_stage) == 0 and not ep.get("smart_tp_master_anchor"):
                    ep["smart_tp_master_anchor"] = float(ep.get("master_current") or abs(pos1) or 0.0)
                ep["smart_tp_peak_pnl"] = max(
                    0.0, ep["rem_size"] * (exit_px - ep["entry_px"]) * ep["sign"],
                )
            elif not closing and ep.get("smart_tp_armed") and old_rem > 0:
                ep["smart_tp_peak_pnl"] = max(
                    0.0, float(ep.get("smart_tp_peak_pnl") or 0.0) * ep["rem_size"] / old_rem,
                )
            slip = (master_px - exit_px) / master_px * 1e4 * ep["sign"] if master_px else 0.0
            action = "close" if closing else "reduce"
            self._record_action(ep, addr, coin, t, action, oid, master_px, signed, pos1,
                                -close_size * ep["sign"], exit_px, pnl, slip, book=book)
            tail_close = tail_close or smart_tail_close
            status = ("liquidated" if (closing and liq) else "tail_closed" if (closing and tail_close)
                      else "gap_closed" if (closing and gap) else "closed" if closing else "open")
            was_liq = 1 if (closing and liq) else 0
            ep["was_liq"] = was_liq
            self.db.execute(
                f"UPDATE {book.pos_table} SET size=?,rem_size=?,margin=?,notional=?,liq_px=?,"
                "realized_pnl=?,mae_pct=?,was_liq=?,status=?,"
                "closed_at=?,smart_tp_armed=?,smart_tp_stage=?,smart_tp_peak_pnl=?,smart_tp_base_size=?,"
                "smart_tp_master_anchor=? WHERE pos_id=?",
                (ep["size"], ep["rem_size"], ep["margin"], ep["notional"], ep.get("liq_px", 0.0),
                 ep["realized_pnl"], ep["mae"], was_liq, status,
                 now_iso() if closing else None, 1 if ep.get("smart_tp_armed") else 0,
                 int(ep.get("smart_tp_stage") or 0), float(ep.get("smart_tp_peak_pnl") or 0.0),
                 float(ep.get("smart_tp_base_size") or 0.0) or None,
                 float(ep.get("smart_tp_master_anchor") or 0.0) or None, ep["pos_id"]))
            self._save_account(book)
            self.db.commit()
            if book is self.taker:
                try:
                    self._refresh_live_wallet_risks({addr})
                except Exception as exc:  # noqa: BLE001 — execution settlement already committed
                    self._rollback_db()
                    _log(f"wallet risk close refresh failed for {addr[:10]}: {exc}")
            if closing:
                if book.stats_loaded:
                    book.closed_n += 1
                    book.wins_n += 1 if ep["realized_pnl"] > 0 else 0
                book.open_ep.pop((addr, coin), None)         # normal closes are in the position table; only
                if book is self.taker:
                    self._resolve_draining_intent(addr)
                    self._finish_live_session_if_drained()
                if liq:                                       # liquidation (our isolated stop-out) is logged
                    _log(f"[{book.name}] LIQUIDATED {addr[:10]} {coin} {ep['side']} -${ep['margin']:,.0f}  bal=${book.balance:,.0f}")
                elif tail_close and tail_decision:
                    self._tally("tail_profit_close", book)
                    _log(
                        f"[{book.name}] TAIL-CLOSE {addr[:10]} {coin} {ep['side']} "
                        f"remain={tail_decision.remaining_fraction:.1%} "
                        f"giveback={tail_decision.giveback_fraction:.0%} "
                        f"pnl=${ep['realized_pnl']:+,.0f}"
                    )
                elif smart_tail_close:
                    self._tally("smart_tp_tail_close", book)
                    _log(
                        f"[{book.name}] SMART-TP TAIL {addr[:10]} {coin} {ep['side']} "
                        f"target-cut={self.smart_tp_target_reduce_exit_pct:.0%} "
                        f"pnl=${ep['realized_pnl']:+,.0f}"
                    )
            elif smart_cut:
                self._tally("smart_tp_cut", book)
                _log(
                    f"[{book.name}] SMART-TP {addr[:10]} {coin} {ep['side']} "
                    f"stage={int(smart_tp_stage) + 1} remain={ep['rem_size'] / max(ep.get('smart_tp_base_size') or 1.0, 1e-12):.0%} "
                    f"pnl=${ep['realized_pnl']:+,.0f}"
                )
            if (self.execution_mode == "live" and requested_full_close and not closing
                    and ep["rem_size"] > config.FLAT):
                if _SOURCE_EVENT_ID.get():
                    raise RetryableSignalError("live_full_close_partial")
                self._schedule_live_full_close_retry(addr, coin, ep, book)

    def _schedule_live_full_close_retry(self, addr, coin, ep, book):
        """Schedule a bounded retry only when the last result is definitely non-ambiguous."""
        if self.execution_mode != "live" or ep.get("close_retry_scheduled"):
            return
        row = self.db.execute("SELECT state FROM execution_control WHERE id=1").fetchone()
        if row and row[0] == "reconcile_required":
            return
        if (addr, coin) not in book.open_ep or ep.get("rem_size", 0.0) <= config.FLAT:
            return
        ep["close_retry_scheduled"] = True
        self._spawn_background(
            self._retry_live_full_close(addr, coin, ep, book),
            f"full_close_retry:{addr[:8]}:{coin}", critical=False,
        )

    async def _retry_live_full_close(self, addr, coin, ep, book):
        """Bounded continuation for a target full-close whose IOC only partially filled."""
        try:
            for _attempt in range(2):
                await asyncio.sleep(1.0)
                if (addr, coin) not in book.open_ep or ep.get("rem_size", 0.0) <= config.FLAT:
                    return
                mark = self._mark_px(coin, ep.get("entry_px") or 0.0)
                await self._apply_reduce(
                    addr, coin, ep, now_ms(), mark, 0.0, 0.0,
                    closing=True, liq=False, forced_px=mark, book=book,
                )
        finally:
            ep["close_retry_scheduled"] = False

    async def _liquidate(self, addr, coin, ep, book=None):
        book = book or self.taker
        if ep.get("liquidating") or (addr, coin) not in book.open_ep:
            return
        ep["liquidating"] = True
        await self._apply_reduce(addr, coin, ep, now_ms(), ep["liq_px"], 0.0, 0.0,
                                 closing=True, liq=True, forced_px=ep["liq_px"], book=book)

    def _maybe_liquidate(self, coin, mark, book=None):
        """Schedule isolated liquidation only from a fresh exchange markPx supplied by the mark poller."""
        book = book or self.taker
        if book.name == "live":
            # Mainnet liquidation and its fills come from the exchange.  A local
            # mark crossing a projected liquidation price must never fabricate
            # a second reduce-only order.
            return
        if not mark or mark <= 0:
            return
        for (a, c), ep in list(book.open_ep.items()):
            if c == coin and ep.get("liq_px") and ep["rem_size"] > config.FLAT and not ep.get("liquidating"):
                hit = mark <= ep["liq_px"] if ep["side"] == "long" else mark >= ep["liq_px"]
                if hit:
                    self._spawn_background(
                        self._liquidate(a, coin, ep, book),
                        f"paper_liquidate:{a[:8]}:{coin}", critical=False,
                    )

    def _smart_take_profit_decision(self, coin, ep, mark_px):
        sigma = self._sigma(coin)
        return smart_take_profit_decision(
            enabled=self.smart_tp_enable,
            rem_size=ep.get("rem_size", 0.0),
            base_size=ep.get("smart_tp_base_size", 0.0),
            entry_px=ep.get("entry_px", 0.0),
            mark_px=mark_px,
            side=ep.get("side"),
            sigma=sigma,
            tier=self._tier(sigma, coin),
            armed=bool(ep.get("smart_tp_armed")),
            stage=int(ep.get("smart_tp_stage") or 0),
            peak_pnl=float(ep.get("smart_tp_peak_pnl") or 0.0),
            arm_sigma=self.smart_tp_arm_sigma,
            giveback_pcts=self.smart_tp_giveback_pcts,
            close_pcts=self.smart_tp_close_pcts,
            tail_remain_pct=self.smart_tp_tail_remain_pct,
            fee_rate=config.TAKER_FEE,
            min_fee_multiple=self.smart_tp_min_fee_mult,
        )

    def _queue_smart_take_profit(self, coin, mark_px, book=None):
        """Advance live high-water state and enqueue at most one cut per position."""
        book = book or self.taker
        if not self.smart_tp_enable or not mark_px or mark_px <= 0:
            return
        stamp = now_ms()
        dirty = []
        for (addr, c), ep in list(book.open_ep.items()):
            if c != coin or ep.get("entry_px") is None or ep.get("rem_size", 0.0) <= config.FLAT:
                continue
            decision = self._smart_take_profit_decision(coin, ep, mark_px)
            prior = (
                bool(ep.get("smart_tp_armed")),
                float(ep.get("smart_tp_peak_pnl") or 0.0),
                float(ep.get("smart_tp_base_size") or 0.0),
            )
            ep["smart_tp_armed"] = decision.armed
            ep["smart_tp_peak_pnl"] = decision.peak_pnl
            ep["smart_tp_base_size"] = decision.base_size
            changed = prior != (decision.armed, decision.peak_pnl, decision.base_size)
            last_write = int(ep.get("smart_tp_state_write_ms") or 0)
            if changed and (decision.trigger or decision.armed != prior[0]
                            or stamp - last_write >= MARK_WRITE_MIN_MS):
                dirty.append((
                    1 if decision.armed else 0,
                    decision.peak_pnl,
                    decision.base_size or None,
                    ep["pos_id"],
                ))
                ep["smart_tp_state_write_ms"] = stamp
            if decision.trigger and not ep.get("smart_tp_inflight"):
                ep["smart_tp_inflight"] = True
                self._spawn_background(
                    self._execute_smart_take_profit(addr, coin, ep, mark_px, decision.stage, book),
                    f"smart_tp:{addr[:8]}:{coin}:{decision.stage}", critical=False,
                )
        if dirty:
            self.db.executemany(
                f"UPDATE {book.pos_table} SET smart_tp_armed=?,smart_tp_peak_pnl=?,smart_tp_base_size=? "
                "WHERE pos_id=? AND status='open'",
                dirty,
            )
            self.db.commit()

    async def _execute_smart_take_profit(self, addr, coin, ep, mark_px, stage, book=None):
        book = book or self.taker
        try:
            await self._apply_reduce(
                addr,
                coin,
                ep,
                now_ms(),
                mark_px,
                0.0,
                float(ep.get("master_current") or 0.0),
                closing=False,
                liq=False,
                book=book,
                smart_tp_stage=stage,
            )
        finally:
            ep["smart_tp_inflight"] = False

    def _record_action(self, ep, addr, coin, t, action, oid, master_px, sz_delta, pos_after,
                       our_qty_delta, our_px, realized, slip, book=None):
        book = book or self.taker
        self._tally(f"act_{action}", book)   # copy activity by kind (open/add/reduce/stop/close) — taker only
        ep["num_actions"] += 1
        cur = self.db.execute(
            f"INSERT INTO {book.act_table} (pos_id,addr,coin,ts,recv_ms,action,master_oid,master_px,"
            "master_sz_delta,master_pos_after,our_qty_delta,our_px,realized_pnl,slippage_bps,"
            "strategy_revision_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ep["pos_id"], addr, coin, t, now_ms(), action, oid, master_px, sz_delta,
             pos_after, our_qty_delta, our_px, realized, slip, self.strategy_revision_id))
        if book.stats_loaded:
            traded = abs((our_qty_delta or 0.0) * (our_px or 0.0))
            book.gross_traded += traded
            # Preserve the dashboard's historical definition: taker-equivalent fee drag for the primary book.
            book.fees_cum += traded * config.TAKER_FEE
        self.db.execute(f"UPDATE {book.pos_table} SET num_actions=?, master_peak_sz=? WHERE pos_id=?",
                        (ep["num_actions"], ep["master_peak"], ep["pos_id"]))
        return cur.lastrowid

    def _persist_live_cursors(self, updates) -> None:
        """Persist one whole polling round with a single writer transaction.

        Empty target windows used to commit once per wallet, continuously starving the independent Live
        execution writer.  A stale durable cursor is intentionally safe: restart overlap plus tid/signal
        idempotency simply replays receipts that were already journalled.
        """
        if not updates or self.execution_mode != "live":
            return
        stamp = now_iso()
        self.db.executemany(
            "INSERT INTO observer_target_cursor "
            "(mode,session_id,addr,last_fill_ms,updated_at) VALUES ('live',?,?,?,?) "
            "ON CONFLICT(mode,session_id,addr) DO UPDATE SET "
            "last_fill_ms=MAX(last_fill_ms,excluded.last_fill_ms),updated_at=excluded.updated_at",
            [
                (self.execution_session_id, addr, int(cursor), stamp)
                for addr, cursor in updates
            ],
        )
        self.db.commit()

    async def _poll_fills(self, addr: str, since: int, *, persist_cursor: bool = True):
        """SIGNAL fetch: REST-pull the wallet's fills since `since` (a few seconds back — the live
        poll window, NOT history) and replay through the idempotent process_fill (dedup by tid).
        aggregateByTime MERGES an order's partial fills into one TRADE-level row, so (a) one sliced
        order = one record (not N), and (b) it isn't mis-counted as N scale-ins. Live persists the
        last successfully journalled API window and resumes it after a worker restart; the durable
        signal inbox then decides whether a recovered fill is executable, policy-skipped or retried."""
        page = await asyncio.to_thread(rest.post_soft, {
            "type": "userFillsByTime", "user": addr, "startTime": int(max(0, since)), "aggregateByTime": True})
        if not isinstance(page, list):
            # post_soft deliberately returns None after its bounded transport retries. Treat that as a real
            # polling failure: leave the cursor untouched and make degradation visible in Observer logs.
            raise RuntimeError("target_fills_unavailable")
        previous_cursor = self.last_fill_ms.get(addr)
        try:
            for x in sorted(page, key=lambda fl: fl["time"]):
                self.process_fill(addr, x)
            # The response is an authoritative view through this receive time.  Persisting it closes
            # the deploy/crash gap while the normal overlap still absorbs boundary races.
            next_cursor = max(
                int(previous_cursor or 0), now_ms(),
                max((int(x.get("time") or 0) for x in page), default=0),
            )
            self.last_fill_ms[addr] = next_cursor
            if self.execution_mode == "live" and persist_cursor:
                self._persist_live_cursors([(addr, next_cursor)])
            elif page:
                # The receipt and durable execution signal must commit before the ordered signal consumer can
                # act.  Empty pages have no writes and are batched by poll_loop instead.
                self.db.commit()
            return next_cursor
        except Exception:
            # A scanner write lock must never turn a fetched-but-uncommitted fill into a skipped fill.
            # Restore the exact prior cursor so the next round re-fetches the whole batch; tid dedup makes
            # this safe even if an inner path committed one row before a later row failed.
            self._rollback_db()
            if previous_cursor is None:
                self.last_fill_ms.pop(addr, None)
            else:
                self.last_fill_ms[addr] = previous_cursor
            raise


# ------------------------------------------------------------------------- loaders
def load_targets(db, n: int):
    """Load only the explicit published Core.

    Before the first successful Selection publication there are deliberately no new-entry targets. Wallets
    with existing copies are still re-added EXIT-ONLY by ``_reload_targets`` for safe position management.
    """
    explicit = selection.published_core_addrs(db, n)
    addrs = explicit or []
    seed = {a: {r[0] for r in db.execute("SELECT DISTINCT coin FROM episode WHERE addr=?", (a,)).fetchall()}
            for a in addrs}
    return addrs, seed


# -------------------------------------------------------------------------- report
def report(db) -> None:
    """On-demand snapshot. ONE table row per (followed-wallet, coin) — open + closed copies merged.
    The target's side (margin/entry/leverage) is read from what we PERSISTED AT OPEN (so it shows
    even after the position closes — no live re-fetch). OPEN rows mark-to-market the live book for
    unrealized PnL (tagged 浮); CLOSED rows show realized PnL (tagged 实)."""
    from collections import defaultdict
    acct = db.execute("SELECT initial_balance, balance FROM copy_account WHERE id=1").fetchone()
    init, bal = acct if acct else (config.INITIAL_BALANCE, config.INITIAL_BALANCE)
    rank_of = {a: r for r, a in db.execute("SELECT rank, addr FROM watchlist").fetchall()}

    groups = defaultdict(list)                       # (addr,coin) -> [position rows]
    for row in db.execute(
            "SELECT pos_id,addr,coin,side,leverage,margin,entry_px,size,rem_size,realized_pnl,status,"
            "master_open_px,master_leverage,master_margin FROM copy_position").fetchall():
        groups[(row[1], row[2])].append(row)

    open_keys = {(a, c) for (a, c), rs in groups.items() if any(x[10] == "open" for x in rs)}
    mark = {}                                        # coin -> live mid (open positions: unrealized PnL)
    for (_, coin) in open_keys:
        if coin not in mark:
            ba = rest.book_top(coin)
            mark[coin] = ((ba[0] + ba[1]) / 2) if ba else None

    def lag_of(pos_id):
        lr = db.execute("SELECT recv_ms-ts FROM copy_action WHERE pos_id=? AND action='open' "
                        "ORDER BY act_id LIMIT 1", (pos_id,)).fetchone()
        return lr[0] if lr else None

    table, open_margin, total_unreal = [], 0.0, 0.0
    for (addr, coin), rs in groups.items():
        realized = sum(x[9] for x in rs)
        opens = [x for x in rs if x[10] == "open"]
        num = rank_of.get(addr)
        num_s = f"#{num}" if num else addr[:6]
        ref = opens[0] if opens else rs[-1]          # target side persisted at open (survives close)
        m_entry, m_lev, m_mgn = ref[11], ref[12], ref[13]
        if opens:
            r = opens[0]
            our_lev, our_mgn, our_entry, rem, side = r[4], r[5], r[6], r[8], r[3]
            mk = mark.get(coin) or our_entry
            unreal = rem * (mk - our_entry) * (1 if side == "long" else -1)
            open_margin += our_mgn; total_unreal += unreal
            table.append((num_s, coin, side, m_mgn, m_entry, m_lev, lag_of(r[0]),
                          our_entry, our_mgn, our_lev, realized + unreal, "浮"))
        else:
            r = rs[-1]
            table.append((num_s, coin, r[3], m_mgn, m_entry, m_lev, lag_of(r[0]),
                          r[6], r[5], r[4], realized, "实"))
    equity = bal + total_unreal

    print(f"\n{'='*100}")
    print(f"PAPER COPY 报告    权益 ${equity:,.2f}    ROI {equity/init-1:+.2%}   (起始 ${init:,.0f})")
    print(f"  已实现余额 ${bal:,.2f}   浮动盈亏 ${total_unreal:+,.2f}   "
          f"持仓占用保证金 ${open_margin:,.2f}   可动用余额 ${bal-open_margin:,.2f}")
    n_open = sum(1 for t in table if t[11] == "浮")
    print(f"  在持 {n_open} 笔 / 已平 {len(table)-n_open} 笔   (按 钱包+币种 合并)")
    print("=" * 100)
    if not table:
        print("  (还没有跟单记录)\n"); return
    h = ("  {:>4} {:10} {:5}|{:>10} {:>11} {:>6}|{:>7}|{:>11} {:>10} {:>6}|{:>11}".format(
        "编号", "coin", "side", "tgt_mgn", "tgt_px", "tgt_lv", "lag", "our_px", "our_mgn", "our_lv", "pnl$"))
    print(h + "\n  " + "-" * (len(h) - 2))
    def s(v, spec, pre="", suf=""): return (pre + format(v, spec) + suf) if v is not None else "—"
    for t in sorted(table, key=lambda r: -r[10]):
        num_s, coin, side, m_mgn, m_entry, m_lev, lag_ms, o_entry, o_mgn, o_lev, pnl, lbl = t
        lag = f"{lag_ms/1000:.1f}s" if lag_ms is not None else "—"
        print("  {:>4} {:10} {:5}|{:>10} {:>11} {:>6}|{:>7}|{:>11} {:>10} {:>6}|{:>+10,.1f}{}".format(
            num_s, coin, side, s(m_mgn, ",.0f", "$"), s(m_entry, "g"), s(m_lev, ".0f", "", "x"),
            lag, format(o_entry, "g"), format(o_mgn, ",.0f"), format(o_lev, ".0f") + "x", pnl, lbl))
    print("\n  列: 编号=watchlist排名 · tgt_*=目标(保证金/均价/杠杆,在持为实时) · lag=跟单延迟 · "
          "our_*=我方(均价/保证金/杠杆) · pnl 浮=未平(mark) 实=已平(realized)")
    print(f"\n(sizing: σ-tiers margin/lev-cap [stable BTC-only: {config.STABLE_MARGIN_PCT*100:g}%/{config.STABLE_LEV_CAP:g}x · "
          f"mid: {config.MID_MARGIN_PCT*100:g}%/{config.MID_LEV_CAP:g}x · high σ≥{config.HIGH_SIGMA_MIN*100:g}%: {config.HIGH_MARGIN_PCT*100:g}%/{config.HIGH_LEV_CAP:g}x], "
          f"lev=tier cap clipped by market/master max, add={config.ADD_FRAC:g}×first "
          f"(max {config.STABLE_MAX_ADDS}/{config.MID_MAX_ADDS}/{config.HIGH_MAX_ADDS} by tier), isolated)")
