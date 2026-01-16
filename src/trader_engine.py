import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import time
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Dict, Any

from utils import (
    log_event,
    current_config,
    to_decimal,
    get_db_path,
    _open_db,
)

from trade_manager import TradeManager
from exchange_factory import get_exchange

# ------------------------------------------------------------
# Cache exchange objects so:
# - fee_rate persists (auto-learning from fills)
# - tick-size caches persist
# - API usage is lower
# - improved performance + stability
# ------------------------------------------------------------
EXCHANGE_CACHE: Dict[str, Any] = {}


class TraderEngine:
    """
    Core strategy engine for Aurono.

    - Contains ALL generic strategy logic (buy/sell decisions)
    - Exchange-agnostic
    - Resolves the exchange PER STRATEGY using exchange_factory.get_exchange()
    """

    def __init__(self) -> None:
        self.tm = TradeManager(get_db_path())

    def cfg(self) -> dict:
        return current_config()

    # ------------------------------------------------------------
    # Exchange resolution (per strategy)
    # ------------------------------------------------------------
    def _get_exchange_for_strategy(self, exchange_name: str):
        name = (exchange_name or "").lower().strip()

        if not name:
            # TEMP backward-compat fallback; prefer migrating DB so every strategy has exchange
            fallback = current_config().get("exchange", "bitvavo").lower()
            log_event(f"⚠️ Strategy has no exchange; falling back to config exchange={fallback}")
            name = fallback

        if name in EXCHANGE_CACHE and EXCHANGE_CACHE[name] is not None:
            return EXCHANGE_CACHE[name]

        ex = get_exchange(name)  # may return None (e.g., coinbase creds missing)
        EXCHANGE_CACHE[name] = ex

        return ex

    # ------------------------------------------------------------
    # Candle selection
    # ------------------------------------------------------------
    def _select_closed_candle(self, ohlc: list) -> list:
        """
        Return the most recent *closed* candle from a chronological OHLC list.

        Logic:
          - Use the last two candles.
          - Infer the interval from their timestamps.
          - If the last candle started less than ~half an interval ago, treat it as
            'still forming' and use the previous one.
          - Otherwise, use the last one.
        """
        import time as _t

        if not ohlc:
            raise ValueError("Empty OHLC data")
        if len(ohlc) == 1:
            return ohlc[-1]

        last = ohlc[-1]
        prev = ohlc[-2]

        def ts_seconds(candle) -> float:
            # Bitvavo: ms → seconds, Kraken/Coinbase: usually seconds
            try:
                ts = int(candle[0])
            except Exception:
                return 0.0
            if ts > 10**12:  # detect ms timestamps
                ts = ts / 1000.0
            return float(ts)

        last_ts = ts_seconds(last)
        prev_ts = ts_seconds(prev)
        now_ts = _t.time()

        interval = max(1.0, last_ts - prev_ts)

        if interval > 0 and (now_ts - last_ts) < interval * 0.5:
            chosen = prev
            idx = -2
        else:
            chosen = last
            idx = -1

        try:
            ct = datetime.fromtimestamp(ts_seconds(chosen), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            log_event(
                f"Using OHLC candle index {idx} (start={ct} UTC)",
                level="DEBUG"
            )

        except Exception:
            pass

        return chosen
        
    # ------------------------------------------------------------
    # Check if order is accepted by exchange
    # ------------------------------------------------------------
    def _order_accepted(self, res: dict) -> bool:
        if not isinstance(res, dict):
            return False
        if res.get("error"):
            return False

        r = res.get("result")

        # Kraken simulated mode
        if r == "simulated":
            return True

        if isinstance(r, dict) and "txid" in r:
            return True

        return False

    # ------------------------------------------------------------
    # One-shot execution
    # ------------------------------------------------------------
    def run_strategy_once(self, timeframe: Optional[str] = None):
        """
        Run one cycle of strategies with detailed decision logging.
        Resolves exchange per strategy using exchange_factory.get_exchange().
        """
        cfg = self.cfg()
        mode = cfg.get("mode", "dev").lower()
        tf_note = f" [{timeframe}]" if timeframe else ""
        log_event(f"🚀 Strategy cycle started{tf_note} (mode={mode.upper()})")

        conn = _open_db()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        if timeframe:
            strategies = cur.execute(
                "SELECT * FROM strategies WHERE enabled=1 AND timeframe=?",
                (timeframe,),
            ).fetchall()
        else:
            strategies = cur.execute(
                "SELECT * FROM strategies WHERE enabled=1"
            ).fetchall()

        conn.close()

        if not strategies:
            log_event("💤 No active strategies found — skipping cycle.")
            return

        for s in strategies:
            try:
                sid = int(s["id"])
                symbol = (s["symbol"] or "").upper().strip()
                s_timeframe = (s["timeframe"] or "").strip()

                drop_trigger = to_decimal(s["drop_trigger"])
                rise_trigger = to_decimal(s["rise_trigger"])
                buy_eur = to_decimal(s["buy_amount_eur"])
                sell_eur = to_decimal(s["sell_amount_eur"])
                allocated = to_decimal(s["allocated_eur"] or 0)

                exchange_name = (s["exchange"] or "").lower().strip()
                exchange = self._get_exchange_for_strategy(exchange_name)

                if exchange is None:
                    log_event(
                        f"Skipping strategy id={sid} {symbol} {s_timeframe}: "
                        f"exchange '{exchange_name}' unavailable (no creds or failed init).",
                        level="WARN"
                    )

                    continue

                log_event(f"▶ Strategy id={sid} {symbol} {s_timeframe} (exchange={exchange.name})")

                # --- Live ticker ---
                try:
                    ticker = exchange.get_ticker(symbol)
                except Exception as e:
                    log_event(f"❌ Ticker error for {symbol} on {exchange.name}: {e}")
                    continue

                if ticker <= 0:
                    log_event(f"❌ No ticker for {symbol} on {exchange.name} — skipping.")
                    continue

                log_event(
                    f"Ticker {symbol} @ {exchange.name}: €{ticker}",
                    level="DEBUG"
                )


                # --- OHLC for timeframe ---
                try:
                    ohlc = exchange.get_ohlc(symbol, s_timeframe)
                except Exception as e:
                    log_event(f"❌ OHLC error for {symbol} ({s_timeframe}) on {exchange.name}: {e}")
                    continue

                if not ohlc or len(ohlc) < 3:
                    log_event(
                        f"⚠️ Not enough OHLC for {symbol} ({s_timeframe}) on {exchange.name} → skip.",
                        level="WARN"
                    )
                    continue

                candle = self._select_closed_candle(ohlc)

                open_price = to_decimal(candle[1])
                close_price = to_decimal(candle[4])

                if open_price <= 0:
                    log_event(f"❌ Invalid open price for {symbol} on {exchange.name} — skip.")
                    continue

                pct_change = (close_price - open_price) / open_price * to_decimal("100")

                acb = self.tm.get_average_cost_for_strategy(sid)
                balance = self.tm.get_balance_for_strategy(sid)

                # =======================
                # BUY LOGIC
                # =======================
                if pct_change <= drop_trigger:
                    log_event(
                        f"{symbol} {s_timeframe} change {pct_change:+.2f}% → BUY trigger hit",
                        level="INFO"
                    )

                    if allocated < buy_eur:
                        log_event(
                            f"❌ No BUY for {symbol} ({s_timeframe}) on {exchange.name}: "
                            f"drop {pct_change:.2f}% ≤ {drop_trigger}%, "
                            f"but insufficient capital (€{allocated:.2f} < €{buy_eur:.2f})"
                        )
                        continue

                    fee = to_decimal(getattr(exchange, "fee_rate", 0) or 0)
                    limit_price = open_price * (Decimal("1") + drop_trigger / Decimal("100"))

                    # Conservative quantize; exchange may later re-quantize to its lot-size rules
                    vol = (buy_eur / (to_decimal(ticker) * (Decimal("1") + fee))).quantize(Decimal("0.00000001"))

                    if vol <= 0:
                        log_event(f"❌ No BUY: computed volume is 0 for {symbol} on {exchange.name}.")
                        continue

                    log_event(
                        f"BUY calc → spend={buy_eur}, price={ticker}, fee={fee}, limit={limit_price}, volume={vol}",
                        level="DEBUG"
                    )


                    # Record-then-place can create phantom trades if placement fails.
                    # Mitigation: place order and if placement fails, do NOT keep DB changes (no alloc update, delete trade row).
                    trade_id = None
                    try:
                        trade_id = self.tm.record_trade(symbol, "buy", limit_price, vol, sid)
                        res = exchange.place_limit_order(symbol, "buy", limit_price, vol, trade_id)

                        if not self._order_accepted(res):
                            raise RuntimeError(f"Order rejected: {res}")

                        # Only update allocated after exchange accepted order
                        eur_spent = limit_price * vol
                        new_alloc = float(allocated - eur_spent)
                        with _open_db() as c:
                            c.execute("UPDATE strategies SET allocated_eur=? WHERE id=?", (new_alloc, sid))
                            c.commit()

                        log_event(
                            f"📤 BUY accepted by exchange: {symbol} {s_timeframe} "
                            f"{vol} @ {limit_price} on {exchange.name} (trade_id={trade_id})",
                            level="INFO"
                        )

                    except Exception as e:
                        log_event(f"BUY failed: {symbol} on {exchange.name}: {e}", level="ERROR")

                        if trade_id is not None:
                            try:
                                with _open_db() as c:
                                    c.execute("DELETE FROM trades WHERE id=?", (trade_id,))
                                    c.commit()
                                log_event(f"Removed phantom BUY trade_id={trade_id}", level="DEBUG")
                            except Exception as e2:
                                log_event(f"⚠️ Failed to delete phantom trade_id={trade_id}: {e2}")

                    continue  # Skip SELL evaluation

                # =======================
                # SELL LOGIC
                # =======================
                if pct_change >= rise_trigger:
                    log_event(
                        f"{symbol} {s_timeframe} change {pct_change:+.2f}% → SELL trigger hit",
                        level="INFO"
                    )

                    if acb is None:
                        log_event(
                            f"❌ No SELL for {symbol} ({s_timeframe}) on {exchange.name}: "
                            f"rise +{pct_change:.2f}% ≥ {rise_trigger}%, but no ACB."
                        )
                        continue

                    if close_price <= acb:
                        log_event(
                            f"❌ No SELL for {symbol} ({s_timeframe}) on {exchange.name}: "
                            f"rise +{pct_change:.2f}% ≥ {rise_trigger}%, but below ACB €{acb:.2f}"
                        )
                        continue

                    notional_eur = balance * close_price
                    if notional_eur < to_decimal("6.00"):
                        log_event(
                            f"❌ No SELL: balance {balance:.6f} → notional €{notional_eur:.2f} for {symbol} too low."
                        )
                        continue

                    fee = to_decimal(getattr(exchange, "fee_rate", 0) or 0)
                    limit_price = open_price * (Decimal("1") + rise_trigger / Decimal("100"))
                    vol = (sell_eur / (to_decimal(ticker) * (Decimal("1") - fee))).quantize(Decimal("0.00000001"))

                    # cap at available coin balance
                    vol = min(vol, balance)

                    if vol <= 0:
                        log_event(f"❌ No SELL: computed volume is 0 for {symbol} on {exchange.name}.")
                        continue

                    log_event(
                        f"SELL calc → target={sell_eur}, price={ticker}, fee={fee}, limit={limit_price}, volume={vol}, balance={balance}",level="DEBUG"
                    )
                    
                    trade_id = None
                    try:
                        trade_id = self.tm.record_trade(symbol, "sell", limit_price, vol, sid)
                        res = exchange.place_limit_order(symbol, "sell", limit_price, vol, trade_id)

                        if not self._order_accepted(res):
                            raise RuntimeError(f"Order rejected: {res}")

                        # Only update allocated after exchange accepted order
                        eur_gained = limit_price * vol
                        new_alloc = float(allocated + eur_gained)
                        with _open_db() as c:
                            c.execute(
                                "UPDATE strategies SET allocated_eur=? WHERE id=?",
                                (new_alloc, sid),
                            )
                            c.commit()

                        log_event(
                            f"📤 SELL accepted by exchange: {symbol} {s_timeframe} "
                            f"{vol} @ {limit_price} on {exchange.name} (trade_id={trade_id})",
                            level="INFO"
                        )

                    except Exception as e:
                        log_event(f"SELL failed: {symbol} on {exchange.name}: {e}", level="ERROR")

                        if trade_id is not None:
                            try:
                                with _open_db() as c:
                                    c.execute("DELETE FROM trades WHERE id=?", (trade_id,))
                                    c.commit()
                                log_event(f"Removed phantom SELL trade_id={trade_id}", level="DEBUG")
                            except Exception as e2:
                                log_event(f"⚠️ Failed to delete phantom trade_id={trade_id}: {e2}")

                    continue

                # =======================
                # IDLE CASE
                # =======================
                else:
                    log_event(
                        f"{symbol} {s_timeframe} change {pct_change:+.3f}% (no trigger)",
                        level="DEBUG"
                    )


            except Exception as e:
                sid_safe = s["id"] if "id" in s.keys() else "unknown"
                log_event(f"🔥 Strategy id={sid_safe} crashed: {e}")
                continue

        log_event("Cycle completed.")

    # ------------------------------------------------------------
    # Loop modes
    # ------------------------------------------------------------
    def run_live(self):
        """
        Schedules:
          - 1h @ xx:01 UTC
          - 4h @ 00:03, 04:03, 08:03, 12:03, 16:03, 20:03 UTC
          - 1d @ 00:05 UTC
          - 1w @ 00:08 UTC on Mondays
        """
        log_event(
            "Aurono Trader started in LIVE mode "
            "(UTC scheduler: 1h@xx:01, 4h@xx:03 (0/4/8/12/16/20), 1d@00:05, 1w@00:08 Mondays)."
        )

        def utcnow_floor_sec():
            return datetime.now(timezone.utc).replace(microsecond=0)

        def next_hourly(now):
            base = now.replace(second=0, microsecond=0)
            candidate = base.replace(minute=1)
            if base.minute >= 1:
                candidate = (base.replace(minute=0) + timedelta(hours=1)).replace(minute=1)
            return candidate

        def next_4h(now):
            base = now.replace(minute=3, second=0, microsecond=0)
            four_hours = [0, 4, 8, 12, 16, 20]
            start_hour = (
                now.hour
                if now.minute < 3 or (now.minute == 3 and now.second == 0)
                else (now.hour + 1) % 24
            )
            days_add = 0
            h = start_hour
            while True:
                if h in four_hours:
                    candidate = base.replace(hour=h) + timedelta(days=days_add)
                    if candidate > now:
                        return candidate
                h += 1
                if h >= 24:
                    h = 0
                    days_add += 1

        def next_daily(now):
            today = now.date()
            candidate = datetime(today.year, today.month, today.day, 0, 5, 0, tzinfo=timezone.utc)
            if now >= candidate:
                candidate += timedelta(days=1)
            return candidate

        def next_weekly(now):
            # Weekly run at Monday 00:08 UTC
            days_until_monday = (7 - now.weekday()) % 7
            next_monday = now + timedelta(days=days_until_monday)
            candidate = datetime(next_monday.year, next_monday.month, next_monday.day, 0, 8, 0, tzinfo=timezone.utc)
            if candidate <= now:
                candidate += timedelta(days=7)
            return candidate

        last_fired = {"1h": None, "4h": None, "1d": None, "1w": None}

        while True:
            now = utcnow_floor_sec()
            n1h = next_hourly(now)
            n4h = next_4h(now)
            n1d = next_daily(now)
            n1w = next_weekly(now)
            next_evt = min(n1h, n4h, n1d, n1w)

            sleep_s = max(1, int((next_evt - now).total_seconds()))

            log_event(
                f"Next scheduler event at {next_evt} UTC (sleep {sleep_s}s)",
                level="DEBUG"
            )

            time.sleep(sleep_s)

            fired_now = utcnow_floor_sec()

            # 1H @ xx:01
            if fired_now.minute == 1 and (last_fired["1h"] != fired_now.replace(minute=1, second=0)):
                last_fired["1h"] = fired_now.replace(minute=1, second=0)
                log_event("⏱ Trigger 1H batch")
                self.run_strategy_once("1h")

            # 4H at 0/4/8/12/16/20 :03
            if (fired_now.minute == 3 and fired_now.hour % 4 == 0 and
                    last_fired["4h"] != fired_now.replace(minute=3, second=0)):
                last_fired["4h"] = fired_now.replace(minute=3, second=0)
                log_event("⏱ Trigger 4H batch")
                self.run_strategy_once("4h")

            # 1D @ 00:05
            if fired_now.minute == 5 and fired_now.hour == 0:
                key_time = fired_now.replace(minute=5, second=0)
                if last_fired["1d"] != key_time:
                    last_fired["1d"] = key_time
                    log_event("⏱ Trigger 1D batch")
                    self.run_strategy_once("1d")

            # 1W @ Monday 00:08
            if fired_now.minute == 8 and fired_now.hour == 0 and fired_now.weekday() == 0:
                key_time = fired_now.replace(minute=8, second=0)
                if last_fired["1w"] != key_time:
                    last_fired["1w"] = key_time
                    log_event("⏱ Trigger 1W batch")
                    self.run_strategy_once("1w")

    def run_dev(self):
        cfg = self.cfg()
        dev_sleep = int(cfg.get("dev_sleep_hours", 4))
        log_event(f"Aurono Trader started in DEV mode ({dev_sleep} h).")

        while True:
            self.run_strategy_once()
            log_event(f"Sleeping {dev_sleep} hours…")
            time.sleep(max(1, dev_sleep * 3600))

