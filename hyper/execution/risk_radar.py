"""15-minute, shadow-only market risk radar.

The model provides a directional prior; deterministic local indicators shrink low-confidence output and
own the confirmation state machine.  Nothing in this module can prevent an Observer order from executing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import urllib.error
import urllib.request
from statistics import mean, pstdev

from hyper import config
from hyper.market import rest
from hyper.ops.credentials import CredentialStore, decrypt_envelope, ensure_instance_keypair
from hyper.util import f, now_iso, now_ms


def _ema(values, period):
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1)
    out = float(values[0])
    for value in values[1:]:
        out += alpha * (float(value) - out)
    return out


def _rsi(values, period=14):
    if len(values) < 2:
        return 50.0
    moves = [values[i] - values[i - 1] for i in range(1, len(values))][-period:]
    gains = mean([max(x, 0.0) for x in moves])
    losses = mean([max(-x, 0.0) for x in moves])
    if losses <= 1e-12:
        return 100.0 if gains > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def _atr(rows, period=14):
    trs = []
    for i, row in enumerate(rows[-(period + 1):]):
        high, low = f(row.get("h")), f(row.get("l"))
        prev = f(rows[-(period + 1) + i - 1].get("c")) if i else f(row.get("o"))
        trs.append(max(high - low, abs(high - prev), abs(low - prev)))
    return mean(trs[-period:]) if trs else 0.0


def candle_features(rows):
    rows = [r for r in (rows or []) if f(r.get("c")) > 0][-220:]
    closes = [f(r.get("c")) for r in rows]
    volumes = [f(r.get("v")) for r in rows]
    if len(closes) < 30:
        return {"available": False, "samples": len(closes)}
    ema9, ema21, ema55 = _ema(closes, 9), _ema(closes, 21), _ema(closes, 55)
    macd = _ema(closes, 12) - _ema(closes, 26)
    signal_source = []
    for end in range(max(26, len(closes) - 20), len(closes) + 1):
        signal_source.append(_ema(closes[:end], 12) - _ema(closes[:end], 26))
    macd_signal = _ema(signal_source, 9)
    win = closes[-20:]
    std20 = pstdev(win) if len(win) > 1 else 0.0
    mid = mean(win)
    vol_win = volumes[-30:]
    vol_std = pstdev(vol_win) if len(vol_win) > 1 else 0.0
    last = closes[-1]
    return {
        "available": True, "samples": len(closes), "close": last,
        "return_1": last / closes[-2] - 1 if closes[-2] else 0.0,
        "return_4": last / closes[-5] - 1 if len(closes) >= 5 and closes[-5] else 0.0,
        "ema9GapPct": ema9 / last - 1, "ema21GapPct": ema21 / last - 1,
        "ema55GapPct": ema55 / last - 1, "emaStack": 1 if ema9 > ema21 > ema55 else -1 if ema9 < ema21 < ema55 else 0,
        "macdPct": macd / last, "macdHistogramPct": (macd - macd_signal) / last,
        "rsi14": _rsi(closes), "atrPct": _atr(rows) / last,
        "bollingerZ": (last - mid) / std20 if std20 else 0.0,
        "volumeZ": (volumes[-1] - mean(vol_win)) / vol_std if vol_std else 0.0,
    }


def orderbook_features(book):
    levels = (book or {}).get("levels") if isinstance(book, dict) else None
    if not levels or len(levels) != 2 or not levels[0] or not levels[1]:
        return {"available": False}
    bids = [(f(x.get("px")), f(x.get("sz"))) for x in levels[0] if f(x.get("px")) > 0]
    asks = [(f(x.get("px")), f(x.get("sz"))) for x in levels[1] if f(x.get("px")) > 0]
    if not bids or not asks or asks[0][0] < bids[0][0]:
        return {"available": False}
    bid, ask = bids[0][0], asks[0][0]
    mid = (bid + ask) / 2

    def depth(rows, bps, is_bid):
        return sum(px * sz for px, sz in rows if ((mid - px) if is_bid else (px - mid)) / mid * 10_000 <= bps)

    bid10, ask10 = depth(bids, 10, True), depth(asks, 10, False)
    bid25, ask25 = depth(bids, 25, True), depth(asks, 25, False)
    best_bid_sz, best_ask_sz = bids[0][1], asks[0][1]
    micro = ((ask * best_bid_sz + bid * best_ask_sz) / (best_bid_sz + best_ask_sz)
             if best_bid_sz + best_ask_sz else mid)
    return {"available": True, "spreadBps": (ask - bid) / mid * 10_000,
            "bidDepth10bps": bid10, "askDepth10bps": ask10,
            "imbalance10bps": (bid10 - ask10) / (bid10 + ask10) if bid10 + ask10 else 0.0,
            "bidDepth25bps": bid25, "askDepth25bps": ask25,
            "imbalance25bps": (bid25 - ask25) / (bid25 + ask25) if bid25 + ask25 else 0.0,
            "micropriceOffsetBps": (micro - mid) / mid * 10_000}


def local_evidence(features):
    """Return directional score and independent evidence groups used to guard model confirmation."""
    votes = []
    groups = set()
    for coin in ("BTC", "ETH"):
        for interval in ("15m", "1h", "4h"):
            row = features.get("markets", {}).get(coin, {}).get(interval, {})
            if not row.get("available"):
                continue
            weight = {"15m": 1.0, "1h": 1.25, "4h": 1.5}[interval]
            momentum = row.get("emaStack", 0) + (1 if row.get("macdHistogramPct", 0) > 0 else -1)
            oscillator = 1 if row.get("rsi14", 50) >= 55 else -1 if row.get("rsi14", 50) <= 45 else 0
            votes.append(weight * (momentum + oscillator * 0.5))
            if abs(momentum) >= 2:
                groups.add(f"trend:{interval}:{'up' if momentum > 0 else 'down'}")
            if oscillator:
                groups.add(f"oscillator:{interval}:{'up' if oscillator > 0 else 'down'}")
        micro = features.get("microstructure", {}).get(coin, {})
        imbalance = f(micro.get("imbalance10bps"))
        if micro.get("available") and abs(imbalance) >= 0.12:
            votes.append(imbalance * 3.0)
            groups.add(f"book:{'up' if imbalance > 0 else 'down'}")
    total = sum(votes)
    score = max(0.0, min(100.0, 50.0 + total * 3.0))
    return score, sorted(groups)


def validate_model_output(value):
    if not isinstance(value, dict):
        raise ValueError("DeepSeek risk response is not an object")
    required = {"bullish_score", "confidence", "regime", "reason", "evidence", "invalidating_conditions"}
    if not required.issubset(value):
        raise ValueError("DeepSeek risk response is missing required fields")
    try:
        bullish, confidence = float(value["bullish_score"]), float(value["confidence"])
    except (TypeError, ValueError):
        raise ValueError("DeepSeek risk scores are not numeric") from None
    if not math.isfinite(bullish) or not math.isfinite(confidence) or not 0 <= bullish <= 100 or not 0 <= confidence <= 100:
        raise ValueError("DeepSeek risk scores are outside 0..100")
    if not isinstance(value["evidence"], list) or not isinstance(value["invalidating_conditions"], list):
        raise ValueError("DeepSeek risk evidence is not a list")
    if not isinstance(value["reason"], str) or not isinstance(value["regime"], str):
        raise ValueError("DeepSeek risk labels are not strings")
    return value


class DeepSeekClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _json_request(self, url, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
        with urllib.request.urlopen(req, timeout=config.RISK_RADAR_REQUEST_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))

    def balance(self):
        return self._json_request(config.DEEPSEEK_BALANCE_URL)

    def assess(self, features):
        system = (
            "You are a crypto market risk classifier. Return JSON only. bullish_score is 0-100: 0 means "
            "strong downside risk and 100 means strong upside pressure. Do not invent missing data."
        )
        prompt = {
            "task": "Assess BTC/ETH broad-market direction for the next 15-60 minutes",
            "required": {"bullish_score": "0..100", "confidence": "0..100", "regime": "string",
                         "reason": "concise Chinese", "evidence": ["string"], "invalidating_conditions": ["string"]},
            "features": features,
        }
        started = time.monotonic()
        response = self._json_request(config.DEEPSEEK_API_URL, {
            "model": config.RISK_RADAR_MODEL,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(prompt, separators=(",", ":"))}],
            "thinking": {"type": "disabled"}, "response_format": {"type": "json_object"},
            "temperature": 0.1, "max_tokens": 800,
        })
        content = response["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        usage = response.get("usage") or {}
        return parsed, response, int((time.monotonic() - started) * 1000), usage


def validate_balance_response(value):
    if (not isinstance(value, dict) or not isinstance(value.get("is_available"), bool)
            or not isinstance(value.get("balance_infos"), list) or not value["balance_infos"]):
        raise ValueError("DeepSeek balance response is invalid")
    info = value["balance_infos"][0]
    if not isinstance(info, dict) or not info.get("currency") or info.get("total_balance") is None:
        raise ValueError("DeepSeek balance response is incomplete")
    return value


class RiskRadar:
    def __init__(self, db):
        self.db = db
        ensure_instance_keypair(db)
        self.credentials = CredentialStore(db)
        self.mode = self._load_mode()
        self.stop = False
        self._next_assessment_attempt_ms = 0
        self._assessment_inflight = False
        self._ensure_state()

    def _load_mode(self):
        row = self.db.execute("SELECT mode FROM market_risk_state WHERE id=1").fetchone()
        return row[0] if row and row[0] in ("off", "shadow") else "off"

    def _ensure_state(self):
        now = now_iso()
        self.db.execute(
            "INSERT OR IGNORE INTO market_risk_state (id,mode,status,updated_at) VALUES (1,?,?,?)",
            (self.mode, "running" if self.mode == "shadow" else "stopped", now),
        )
        self.db.commit()

    def _set_state(self, **values):
        if not values:
            return
        values["updated_at"] = now_iso()
        sql = ",".join(f"{key}=?" for key in values)
        self.db.execute(f"UPDATE market_risk_state SET {sql} WHERE id=1", tuple(values.values()))
        self.db.commit()

    async def set_mode(self, enabled: bool):
        self.mode = "shadow" if enabled else "off"
        self._set_state(mode=self.mode, status="running" if enabled else "stopped",
                        last_error=None, **({} if enabled else {"current_assessment_id": None,
                        "block_side": None, "risk_score": None, "confirmation_mode": None, "valid_until_ms": None}))
        if enabled:
            if not self.credentials.secret("deepseek"):
                self._set_state(status="needs_credential", connection_status="not_configured")
                return {"mode": self.mode, "status": "needs_credential"}
            if not self._funds_available():
                self._set_state(status="insufficient_balance", connection_status="insufficient_balance")
                return {"mode": self.mode, "status": "insufficient_balance"}
            asyncio.create_task(self.assess_once())
        return {"mode": self.mode, "status": "running" if enabled else "stopped"}

    async def install_credential(self, envelope):
        secret = decrypt_envelope(self.db, envelope)
        balance = validate_balance_response(
            await asyncio.to_thread(DeepSeekClient(secret).balance)
        )  # validate before replacing the working credential
        self.credentials.save_envelope("deepseek", envelope)
        self._write_balance(balance)
        available = balance.get("is_available") is not False
        state_values = {"connection_status": "connected" if available else "insufficient_balance",
                        "status": ("running" if self.mode == "shadow" else "stopped") if available else "insufficient_balance",
                        "last_error": None}
        if not available:
            state_values.update(current_assessment_id=None, block_side=None, risk_score=None,
                                confirmation_mode=None, valid_until_ms=None)
        self._set_state(**state_values)
        if self.mode == "shadow" and available:
            asyncio.create_task(self.assess_once())
        return {"provider": "deepseek", "status": "connected" if available else "insufficient_balance",
                "balance": self._safe_balance(balance)}

    def delete_credential(self):
        changed = self.credentials.delete("deepseek")
        self._set_state(connection_status="not_configured", status="needs_credential" if self.mode == "shadow" else "stopped")
        return {"provider": "deepseek", "deleted": changed}

    async def test_connection(self):
        secret = self.credentials.secret("deepseek")
        if not secret:
            raise ValueError("DeepSeek API key is not configured")
        balance = validate_balance_response(await asyncio.to_thread(DeepSeekClient(secret).balance))
        self._write_balance(balance)
        available = balance.get("is_available") is not False
        self._set_state(connection_status="connected" if available else "insufficient_balance",
                        status=("running" if self.mode == "shadow" else "stopped") if available else "insufficient_balance",
                        **({} if available else {"current_assessment_id": None, "block_side": None,
                                                "risk_score": None, "confirmation_mode": None,
                                                "valid_until_ms": None}), last_error=None)
        if available and self.mode == "shadow":
            asyncio.create_task(self.assess_once())
        return {"provider": "deepseek", "status": "connected" if available else "insufficient_balance",
                "balance": self._safe_balance(balance)}

    def _funds_available(self):
        row = self.db.execute(
            "SELECT is_available FROM provider_balance_snapshot WHERE provider='deepseek' "
            "ORDER BY balance_id DESC LIMIT 1"
        ).fetchone()
        return row is None or bool(row[0])

    @staticmethod
    def _safe_balance(balance):
        info = (balance.get("balance_infos") or [{}])[0]
        return {"isAvailable": bool(balance.get("is_available")), "currency": info.get("currency"),
                "totalBalance": f(info.get("total_balance"))}

    def _write_balance(self, balance, error=None):
        info = (balance.get("balance_infos") or [{}])[0] if isinstance(balance, dict) else {}
        total = f(info.get("total_balance"))
        currency = str(info.get("currency") or "").upper()
        row = self.db.execute(
            "SELECT AVG(estimated_cost) FROM market_risk_assessment WHERE status='ok' AND estimated_cost>0 AND cost_currency=?",
            (currency,),
        ).fetchone()
        average_cost = f(row[0]) if row else 0.0
        requests = int(total / average_cost) if average_cost > 0 else None
        days = requests * config.RISK_RADAR_INTERVAL_S / 86400 if requests is not None else None
        self.db.execute(
            "INSERT INTO provider_balance_snapshot (provider,checked_at,currency,total_balance,granted_balance,"
            "topped_up_balance,is_available,estimated_days,estimated_requests,error) VALUES ('deepseek',?,?,?,?,?,?,?,?,?)",
            (now_iso(), info.get("currency"), total, f(info.get("granted_balance")), f(info.get("topped_up_balance")),
             1 if balance.get("is_available") else 0, days, requests, str(error)[:500] if error else None),
        )
        self.db.commit()

    def build_features(self):
        markets, microstructure, present, expected = {}, {}, 0, 8
        for coin in ("BTC", "ETH"):
            markets[coin] = {}
            for interval, days in (("15m", 3), ("1h", 10), ("4h", 40)):
                feat = candle_features(rest.candle_snapshot(coin, interval, days))
                markets[coin][interval] = feat
                present += 1 if feat.get("available") else 0
            microstructure[coin] = orderbook_features(rest.book_snapshot(coin))
            present += 1 if microstructure[coin].get("available") else 0
        ctxs = rest.asset_contexts()
        context = {}
        for coin in ("BTC", "ETH"):
            c = ctxs.get(coin) or {}
            mark = f(c.get("markPx"))
            oracle = f(c.get("oraclePx"))
            context[coin] = {"funding": f(c.get("funding")), "openInterest": f(c.get("openInterest")),
                             "markOracleBasisPct": (mark / oracle - 1) if mark and oracle else None,
                             "dayNotionalVolume": f(c.get("dayNtlVlm"))}
        features = {"asOfMs": now_ms(), "markets": markets, "microstructure": microstructure, "context": context}
        local_score, groups = local_evidence(features)
        features["local"] = {"bullishScore": round(local_score, 2), "evidenceGroups": groups}
        return features, {"present": present, "expected": expected, "ratio": present / expected}

    async def assess_once(self):
        # set_mode/install/test and the 15-second loop may all request an immediate assessment.  Serialize
        # them before the first await so one 15-minute slot can never spend provider balance twice.
        if self._assessment_inflight:
            return None
        self._assessment_inflight = True
        try:
            return await self._assess_once_serialized()
        finally:
            self._assessment_inflight = False

    async def _assess_once_serialized(self):
        if self.mode != "shadow":
            return None
        if now_ms() < self._next_assessment_attempt_ms:
            return None
        try:
            secret = self.credentials.secret("deepseek")
        except Exception as exc:
            self._set_state(status="degraded", connection_status="error", current_assessment_id=None,
                            block_side=None, risk_score=None, confirmation_mode=None, valid_until_ms=None,
                            last_error=str(exc)[:500])
            self._next_assessment_attempt_ms = now_ms() + 5 * 60_000
            return None
        if not secret:
            self._set_state(status="needs_credential", connection_status="not_configured")
            return None
        if not self._funds_available():
            self._set_state(status="insufficient_balance", connection_status="insufficient_balance",
                            current_assessment_id=None, block_side=None, risk_score=None,
                            confirmation_mode=None, valid_until_ms=None)
            return None
        assessed_for = (now_ms() // (15 * 60_000)) * (15 * 60_000)
        if self.db.execute("SELECT 1 FROM market_risk_assessment WHERE assessed_for_ms=? AND status='ok'", (assessed_for,)).fetchone():
            return None
        try:
            features, coverage = await asyncio.to_thread(self.build_features)
            encoded = json.dumps(features, sort_keys=True, separators=(",", ":"))
            cur = self.db.execute(
                "INSERT OR IGNORE INTO market_risk_snapshot (assessed_for_ms,features_json,coverage_json,input_hash,created_at) VALUES (?,?,?,?,?)",
                (assessed_for, encoded, json.dumps(coverage), hashlib.sha256(encoded.encode()).hexdigest(), now_iso()),
            )
            snapshot_id = cur.lastrowid or self.db.execute(
                "SELECT snapshot_id FROM market_risk_snapshot WHERE assessed_for_ms=?", (assessed_for,)).fetchone()[0]
            self.db.commit()  # never hold the SQLite writer transaction across the external model request
            parsed, response, latency, usage = await asyncio.to_thread(DeepSeekClient(secret).assess, features)
            parsed = validate_model_output(parsed)
            raw = max(0.0, min(100.0, f(parsed.get("bullish_score"))))
            confidence = max(0.0, min(100.0, f(parsed.get("confidence"))))
            effective = 50.0 + (raw - 50.0) * confidence / 100.0
            bullish, bearish = effective, 100.0 - effective
            risk = max(bullish, bearish)
            block_side = "short" if bullish >= bearish else "long"
            risky_direction = "bullish" if block_side == "short" else "bearish"
            prev = self.db.execute(
                "SELECT assessment_id,assessed_for_ms,block_side,MAX(bullish_score,bearish_score),active_block "
                "FROM market_risk_assessment WHERE status='ok' ORDER BY assessed_for_ms DESC,assessment_id DESC LIMIT 1"
            ).fetchone()
            local_score = f(features["local"]["bullishScore"])
            local_same = local_score >= 65 if block_side == "short" else local_score <= 35
            local_suffix = ":up" if block_side == "short" else ":down"
            local_group_count = sum(
                1 for group in features["local"].get("evidenceGroups", []) if str(group).endswith(local_suffix)
            )
            four_h = [features["markets"][c]["4h"].get("emaStack", 0) for c in ("BTC", "ETH")]
            four_h_against = all(v < 0 for v in four_h) if block_side == "short" else all(v > 0 for v in four_h)
            mode = None
            if risk >= config.RISK_RADAR_EXTREME_SCORE and confidence >= 85 and local_same and not four_h_against:
                mode = "extreme"
            elif prev and prev[2] == block_side and assessed_for - int(prev[1]) <= 20 * 60_000:
                if prev[3] >= config.RISK_RADAR_BLOCK_SCORE and risk >= config.RISK_RADAR_BLOCK_SCORE:
                    mode = "steady"
                elif (prev[3] >= 60 and risk >= config.RISK_RADAR_BLOCK_SCORE and risk - prev[3] >= 10
                      and local_same and local_group_count >= 2):
                    mode = "accelerating"
            active = int(bool(mode))
            valid_until = assessed_for + config.RISK_RADAR_VALID_FOR_S * 1000
            usage_prompt = int(usage.get("prompt_tokens") or 0)
            usage_completion = int(usage.get("completion_tokens") or 0)
            details = usage.get("prompt_tokens_details") or {}
            cache_hit = int(details.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0)
            cache_miss = int(usage.get("prompt_cache_miss_tokens") or max(0, usage_prompt - cache_hit))
            estimated_cost = (
                cache_hit * config.DEEPSEEK_V4_PRO_INPUT_CACHE_HIT_CNY_PER_M
                + cache_miss * config.DEEPSEEK_V4_PRO_INPUT_CACHE_MISS_CNY_PER_M
                + usage_completion * config.DEEPSEEK_V4_PRO_OUTPUT_CNY_PER_M
            ) / 1_000_000
            cur = self.db.execute(
                "INSERT INTO market_risk_assessment (snapshot_id,assessed_for_ms,model,prompt_version,status,"
                "raw_bullish_score,bullish_score,bearish_score,confidence,regime,risky_direction,block_side,"
                "confirmation_mode,active_block,previous_assessment_id,valid_until_ms,reason,evidence_json,"
                "invalidation_json,response_json,latency_ms,prompt_tokens,completion_tokens,estimated_cost,cost_currency,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (snapshot_id, assessed_for, config.RISK_RADAR_MODEL, config.RISK_RADAR_PROMPT_VERSION, "ok", raw,
                 bullish, bearish, confidence, str(parsed.get("regime") or "unknown")[:80], risky_direction,
                 block_side, mode, active, prev[0] if prev else None, valid_until, str(parsed.get("reason") or "")[:1000],
                 json.dumps(parsed.get("evidence") or [], ensure_ascii=False),
                 json.dumps(parsed.get("invalidating_conditions") or [], ensure_ascii=False),
                 json.dumps(response, ensure_ascii=False), latency, usage_prompt, usage_completion,
                 estimated_cost, "CNY", now_iso()),
            )
            assessment_id = cur.lastrowid
            self.db.commit()
            credential_still_present = bool(self.db.execute(
                "SELECT 1 FROM provider_credential WHERE provider='deepseek'"
            ).fetchone())
            if self.mode == "shadow" and credential_still_present:
                self._set_state(status="running", current_assessment_id=assessment_id,
                                block_side=block_side if active else None, risk_score=risk,
                                confirmation_mode=mode, valid_until_ms=valid_until,
                                connection_status="connected", last_assessed_at=now_iso(), last_error=None)
            else:
                self._set_state(status="needs_credential" if self.mode == "shadow" else "stopped",
                                current_assessment_id=None, block_side=None, risk_score=None,
                                confirmation_mode=None, valid_until_ms=None)
            self._next_assessment_attempt_ms = 0
            return assessment_id
        except Exception as exc:  # fail open: stale/error assessment can never become an active block
            self.db.rollback()
            auth_error = isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403)
            if auth_error:
                self.credentials.mark_error("deepseek", "provider authentication failed")
            self.db.execute(
                "INSERT INTO market_risk_assessment (assessed_for_ms,model,prompt_version,status,error,created_at) VALUES (?,?,?,'error',?,?)",
                (assessed_for, config.RISK_RADAR_MODEL, config.RISK_RADAR_PROMPT_VERSION, str(exc)[:1000], now_iso()),
            )
            self.db.commit()
            self._set_state(status="degraded", current_assessment_id=None, block_side=None, risk_score=None,
                            confirmation_mode=None, valid_until_ms=None,
                            **({"connection_status": "error"} if auth_error else {}), last_error=str(exc)[:500])
            self._next_assessment_attempt_ms = now_ms() + 5 * 60_000
            return None

    async def assessment_loop(self):
        while not self.stop:
            try:
                if self.mode == "shadow":
                    await self.assess_once()
            except Exception as exc:
                self._set_state(status="degraded", current_assessment_id=None, block_side=None,
                                risk_score=None, confirmation_mode=None, valid_until_ms=None,
                                last_error=str(exc)[:500])
                self._next_assessment_attempt_ms = now_ms() + 5 * 60_000
            await asyncio.sleep(15)

    async def balance_loop(self):
        while not self.stop:
            try:
                if self.credentials.secret("deepseek"):
                    await self.test_connection()
            except Exception as exc:
                self._set_state(connection_status="error", last_error=str(exc)[:500])
            await asyncio.sleep(config.RISK_RADAR_BALANCE_INTERVAL_S)

    def _entry_decision(self, side):
        row = self.db.execute(
            "SELECT current_assessment_id,block_side,risk_score,confirmation_mode,valid_until_ms,status "
            "FROM market_risk_state WHERE id=1"
        ).fetchone()
        fresh = bool(row and row[0] and row[4] and int(row[4]) >= now_ms() and row[5] == "running")
        would_block = int(fresh and row[1] == side)
        reason = "confirmed directional conflict" if would_block else ("no confirmed conflict" if fresh else "radar unavailable or stale")
        return {"assessment_id": row[0] if fresh else None, "risk_score": row[2] if fresh else None,
                "would_block": would_block, "confirmation_mode": row[3] if fresh else None,
                "reason": reason, "fresh": fresh}

    def record_intent(self, pos_id, addr, coin, side, source_oid=None):
        """Freeze the first-open verdict and create the V2 latent episode.

        The normal Paper open still executes.  A blocked entry starts its AI counterfactual with zero exposure,
        but the episode remains alive so a later add can independently become a delayed entry.
        """
        if self.mode != "shadow":
            return None
        decision = self._entry_decision(side)
        opened_at = now_iso()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO market_risk_intent (pos_id,addr,coin,side,source_oid,assessment_id,risk_score,"
            "would_block,confirmation_mode,decision_reason,opened_at,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,'open')",
            (pos_id, addr, coin, side, source_oid, decision["assessment_id"], decision["risk_score"],
             decision["would_block"], decision["confirmation_mode"], decision["reason"], opened_at),
        )
        self.db.execute(
            "INSERT OR IGNORE INTO market_risk_episode (pos_id,addr,coin,side,status,entry_blocked,opened_at) "
            "VALUES (?,?,?,?, 'open',?,?)",
            (pos_id, addr, coin, side, decision["would_block"], opened_at),
        )
        return cur.lastrowid or None

    def record_exposure_action(self, pos_id, action, side, qty_delta, px, copy_act_id=None, source_oid=None):
        """Apply one baseline open/add to the AI-filtered ledger using only its entry-time verdict.

        Exchange fills sharing an order id reuse the first verdict.  Allowed exposure copies only the baseline
        action's incremental quantity; it never catches up quantities rejected earlier in the episode.
        """
        if action not in ("open", "add"):
            raise ValueError("risk exposure action must be open or add")
        if copy_act_id is not None and self.db.execute(
                "SELECT 1 FROM market_risk_action WHERE copy_act_id=?", (copy_act_id,)).fetchone():
            return None
        episode = self.db.execute(
            "SELECT episode_id,shadow_qty,shadow_entry_px,shadow_fee FROM market_risk_episode "
            "WHERE pos_id=? AND status='open'", (pos_id,)
        ).fetchone()
        if not episode:
            return None  # V1/pre-radar position: do not fabricate a partial counterfactual mid-episode.
        decision_group = f"{action}:oid:{source_oid}" if source_oid is not None else f"{action}:act:{copy_act_id or now_ms()}"
        prior = self.db.execute(
            "SELECT assessment_id,risk_score,would_block,confirmation_mode,decision_reason "
            "FROM market_risk_action WHERE pos_id=? AND decision_group=? ORDER BY risk_action_id LIMIT 1",
            (pos_id, decision_group),
        ).fetchone()
        new_decision_group = not bool(prior)
        if prior:
            decision = {"assessment_id": prior[0], "risk_score": prior[1], "would_block": int(prior[2]),
                        "confirmation_mode": prior[3], "reason": prior[4], "fresh": prior[0] is not None}
        elif action == "open":
            intent = self.db.execute(
                "SELECT assessment_id,risk_score,would_block,confirmation_mode,decision_reason "
                "FROM market_risk_intent WHERE pos_id=?", (pos_id,)
            ).fetchone()
            if not intent:
                return None
            decision = {"assessment_id": intent[0], "risk_score": intent[1], "would_block": int(intent[2]),
                        "confirmation_mode": intent[3], "reason": intent[4], "fresh": intent[0] is not None}
        else:
            decision = self._entry_decision(side)

        amount = abs(f(qty_delta))
        execution_px = f(px)
        sign = 1.0 if side == "long" else -1.0
        old_qty, old_entry = f(episode[1]), f(episode[2])
        shadow_delta = 0.0
        delayed = False
        if not decision["would_block"] and amount > 0 and execution_px > 0:
            new_qty = old_qty + amount
            new_entry = ((old_qty * old_entry + amount * execution_px) / new_qty) if new_qty else execution_px
            shadow_delta = amount * sign
            delayed = action == "add" and old_qty <= config.FLAT
            entry_fee = amount * execution_px * config.TAKER_FEE
            self.db.execute(
                "UPDATE market_risk_episode SET shadow_qty=?,shadow_entry_px=?,shadow_realized_pnl=shadow_realized_pnl-?,"
                "shadow_fee=shadow_fee+?,allowed_entries=allowed_entries+?,delayed_entry=MAX(delayed_entry,?) "
                "WHERE episode_id=?",
                (new_qty, new_entry, entry_fee, entry_fee, 1 if new_decision_group else 0,
                 1 if delayed else 0, episode[0]),
            )
        else:
            self.db.execute(
                "UPDATE market_risk_episode SET blocked_entries=blocked_entries+? WHERE episode_id=?",
                (1 if new_decision_group else 0, episode[0]),
            )
        if decision["would_block"]:
            verdict = "blocked_open" if action == "open" else "blocked_add"
        elif delayed:
            verdict = "delayed_entry"
        elif not decision["fresh"]:
            verdict = "radar_unavailable_allow"
        else:
            verdict = "allowed_open" if action == "open" else "allowed_add"
        self.db.execute(
            "INSERT INTO market_risk_action (episode_id,pos_id,copy_act_id,decision_group,source_oid,action,side,"
            "assessment_id,risk_score,would_block,confirmation_mode,decision,decision_reason,baseline_qty_delta,"
            "baseline_px,shadow_qty_delta,shadow_px,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (episode[0], pos_id, copy_act_id, decision_group, source_oid, action, side,
             decision["assessment_id"], decision["risk_score"], decision["would_block"],
             decision["confirmation_mode"], verdict, decision["reason"], f(qty_delta), execution_px,
             shadow_delta, execution_px if shadow_delta else None, now_iso()),
        )
        return verdict

    def record_exit_action(self, pos_id, action, side, baseline_qty_delta, px, close_fraction,
                           copy_act_id=None, source_oid=None):
        """Mirror an exit proportionally into the AI ledger.  Risk reduction is never vetoed by AI."""
        if action not in ("reduce", "close"):
            raise ValueError("risk exit action must be reduce or close")
        if copy_act_id is not None and self.db.execute(
                "SELECT 1 FROM market_risk_action WHERE copy_act_id=?", (copy_act_id,)).fetchone():
            return None
        episode = self.db.execute(
            "SELECT episode_id,shadow_qty,shadow_entry_px FROM market_risk_episode "
            "WHERE pos_id=? AND status='open'", (pos_id,)
        ).fetchone()
        if not episode:
            return None
        shadow_qty = f(episode[1])
        fraction = max(0.0, min(1.0, f(close_fraction)))
        close_qty = shadow_qty if action == "close" else shadow_qty * fraction
        execution_px, entry_px = f(px), f(episode[2])
        sign = 1.0 if side == "long" else -1.0
        exit_fee = abs(close_qty * execution_px) * config.TAKER_FEE
        realized = close_qty * (execution_px - entry_px) * sign - exit_fee if close_qty else 0.0
        remaining = max(0.0, shadow_qty - close_qty)
        self.db.execute(
            "UPDATE market_risk_episode SET shadow_qty=?,shadow_entry_px=?,"
            "shadow_realized_pnl=shadow_realized_pnl+?,shadow_fee=shadow_fee+? WHERE episode_id=?",
            (remaining, entry_px if remaining > config.FLAT else None, realized, exit_fee, episode[0]),
        )
        self.db.execute(
            "INSERT INTO market_risk_action (episode_id,pos_id,copy_act_id,decision_group,source_oid,action,side,"
            "would_block,decision,decision_reason,baseline_qty_delta,baseline_px,shadow_qty_delta,shadow_px,"
            "shadow_realized_pnl,close_fraction,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (episode[0], pos_id, copy_act_id, f"{action}:act:{copy_act_id or now_ms()}", source_oid, action, side,
             0, "mandatory_exit", "risk reduction is never blocked", f(baseline_qty_delta), execution_px,
             -close_qty * sign, execution_px if close_qty else None, realized, fraction, now_iso()),
        )
        return realized

    def resolve_intent(self, pos_id):
        row = self.db.execute(
            "SELECT realized_pnl FROM copy_position WHERE pos_id=? AND status!='open'", (pos_id,)
        ).fetchone()
        if not row:
            return False
        realized = f(row[0])  # includes reduce/close fees, while open/add fees were deducted from the account only
        fee_row = self.db.execute(
            "SELECT COALESCE(SUM(ABS(our_qty_delta*our_px)*?),0) FROM copy_action WHERE pos_id=?",
            (config.TAKER_FEE, pos_id),
        ).fetchone()
        fee = f(fee_row[0]) if fee_row else 0.0
        entry_fee_row = self.db.execute(
            "SELECT COALESCE(SUM(ABS(our_qty_delta*our_px)*?),0) FROM copy_action "
            "WHERE pos_id=? AND action IN ('open','add')",
            (config.TAKER_FEE, pos_id),
        ).fetchone()
        net_pnl = realized - (f(entry_fee_row[0]) if entry_fee_row else 0.0)
        intent = self.db.execute("SELECT would_block FROM market_risk_intent WHERE pos_id=?", (pos_id,)).fetchone()
        if not intent:
            return False
        episode = self.db.execute(
            "SELECT episode_id,shadow_realized_pnl FROM market_risk_episode WHERE pos_id=?", (pos_id,)
        ).fetchone()
        if episode:
            shadow_net = f(episode[1])
            benefit = shadow_net - net_pnl
            outcome = "improved" if benefit > 1e-9 else "harmed" if benefit < -1e-9 else "flat"
            self.db.execute(
                "UPDATE market_risk_episode SET status='resolved',shadow_qty=0,shadow_entry_px=NULL,"
                "baseline_net_pnl=?,shadow_net_pnl=?,net_benefit=?,outcome=?,resolved_at=? WHERE episode_id=?",
                (net_pnl, shadow_net, benefit, outcome, now_iso(), episode[0]),
            )
            legacy_outcome = "avoided_loss" if benefit > 1e-9 else "missed_profit" if benefit < -1e-9 else "flat"
        else:
            legacy_outcome = (("avoided_loss" if net_pnl < 0 else "missed_profit" if net_pnl > 0 else "flat")
                              if intent[0] else "allowed")
        self.db.execute(
            "UPDATE market_risk_intent SET status='resolved',realized_pnl=?,fee=?,net_pnl=?,outcome=?,resolved_at=? "
            "WHERE pos_id=? AND status='open'",
            (realized, fee, net_pnl, legacy_outcome, now_iso(), pos_id),
        )
        self.db.commit()
        return True

    def prune(self):
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - config.RISK_RADAR_RETENTION_DAYS * 86400))
        self.db.execute("DELETE FROM provider_balance_snapshot WHERE checked_at < ?", (cutoff,))
        self.db.execute("DELETE FROM market_risk_assessment WHERE created_at < ? AND assessment_id NOT IN "
                        "(SELECT assessment_id FROM market_risk_intent WHERE assessment_id IS NOT NULL UNION "
                        " SELECT assessment_id FROM market_risk_action WHERE assessment_id IS NOT NULL) "
                        "AND assessment_id NOT IN (SELECT current_assessment_id FROM market_risk_state "
                        "WHERE current_assessment_id IS NOT NULL)", (cutoff,))
        self.db.execute("DELETE FROM market_risk_snapshot WHERE created_at < ? AND snapshot_id NOT IN "
                        "(SELECT snapshot_id FROM market_risk_assessment WHERE snapshot_id IS NOT NULL)", (cutoff,))
        # Older builds could launch set_mode/test/loop assessments concurrently.  Keep every order-referenced
        # verdict (plus the current state), otherwise retain only the newest row for each 15-minute slot.
        protected = {
            int(row[0]) for row in self.db.execute(
                "SELECT assessment_id FROM market_risk_intent WHERE assessment_id IS NOT NULL UNION "
                "SELECT assessment_id FROM market_risk_action WHERE assessment_id IS NOT NULL UNION "
                "SELECT current_assessment_id FROM market_risk_state WHERE current_assessment_id IS NOT NULL"
            ).fetchall()
        }
        duplicate_ids = []
        slot_rows = self.db.execute(
            "SELECT assessment_id,assessed_for_ms FROM market_risk_assessment "
            "ORDER BY assessed_for_ms DESC,assessment_id DESC"
        ).fetchall()
        by_slot = {}
        for assessment_id, assessed_for_ms in slot_rows:
            by_slot.setdefault(assessed_for_ms, []).append(int(assessment_id))
        for ids in by_slot.values():
            if len(ids) <= 1:
                continue
            keep = {assessment_id for assessment_id in ids if assessment_id in protected}
            if not keep:
                keep.add(ids[0])
            duplicate_ids.extend(assessment_id for assessment_id in ids if assessment_id not in keep)
        if duplicate_ids:
            marks = ",".join("?" for _ in duplicate_ids)
            self.db.execute(
                f"DELETE FROM market_risk_assessment WHERE assessment_id IN ({marks})",
                tuple(duplicate_ids),
            )
        # Bound the high-frequency judgement trail by row count as well as age.  Open/resolvable Shadow
        # bookkeeping keeps its referenced assessment; all other rows outside the newest budget are removed.
        self.db.execute(
            "DELETE FROM market_risk_assessment WHERE assessment_id NOT IN ("
            " SELECT assessment_id FROM market_risk_assessment "
            " ORDER BY assessed_for_ms DESC,assessment_id DESC LIMIT ?"
            ") AND assessment_id NOT IN ("
            " SELECT assessment_id FROM market_risk_intent WHERE assessment_id IS NOT NULL"
            " UNION SELECT assessment_id FROM market_risk_action WHERE assessment_id IS NOT NULL"
            ") AND assessment_id NOT IN ("
            " SELECT current_assessment_id FROM market_risk_state WHERE current_assessment_id IS NOT NULL"
            ")",
            (config.RISK_RADAR_MAX_ASSESSMENTS,),
        )
        self.db.execute("DELETE FROM market_risk_snapshot WHERE snapshot_id NOT IN "
                        "(SELECT snapshot_id FROM market_risk_assessment WHERE snapshot_id IS NOT NULL)")
        self.db.commit()
