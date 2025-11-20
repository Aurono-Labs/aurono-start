import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import time
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from utils import (
    log_event,
    current_config,
    to_decimal,
    get_db_path,
)
from trade_manager import TradeManager
from exchange_base import ExchangeBase
from exchange_factory import get_exchange


class TraderEngine:
    """
    Core strategy engine for Aurono.

    - Contains ALL generic strategy logic (buy/sell decisions)
    - Talks to an ExchangeBase instance (Kraken, Bitvavo, ...)
    - Completely exchange-agnostic
    """

    def __init__(self, exchange: ExchangeBase) -> None:
        self.exchange = exchange
        self.tm = TradeManager(get_db_path())

    def cfg(self):
        return current_config()

    # -------------------- One-shot execution --------------------

    def run_strategy_once(self, timeframe: Optional[str] = None):
        """
        Run one cycle of strategies with detailed decision logging.
        Exchange-agnostic; calls self.exchange.get_ticker / get_ohlc / place_limit_order.
        """
        cfg = self.cfg()
        mode = cfg.get("mode", "dev")
        tf_note = f" [{timeframe}]" if timeframe else ""
        log_event(f"🚀 Strategy cycle started{tf_note} (global engine={self.exchange.name}, mode={mode.upper()})")

        conn = sqlite3.connect(self.tm.db_path)
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
            log_event("⚠️ No active strategies found — skipping cycle.")
            return

        for s in strategies:
            sid = int(s["id"])
            symbol = s["symbol"].upper()
            s_timeframe = s["timeframe"]
            drop_trigger = to_decimal(s["drop_trigger"])
            rise_trigger = to_decimal(s["rise_trigger"])
            buy_eur = to_decimal(s["buy_amount_eur"])
            sell_eur = to_decimal(s["sell_amount_eur"])
            allocated = to_decimal(s["allocated_eur"] or 0)
            exchange_name = s["exchange"] if "exchange" in s.keys() else "bitvavo"
            exchange = get_exchange(exchange_name)

            log_event(
                f"📊 Running strategy {symbol} ({s_timeframe}) on {exchange.name}: "
                f"drop {drop_trigger}%, rise {rise_trigger}%"
            )

            # --- Live ticker ---
            ticker = exchange.get_ticker(symbol)
            if ticker <= 0:
                log_event(f"❌ No ticker for {symbol} on {exchange.name} — skipping.")
                continue

            log_event(f"📈 Current ticker {symbol} @ {exchange.name}: €{ticker}")

            # --- OHLC for timeframe ---
            ohlc = exchange.get_ohlc(symbol, s_timeframe)
            if len(ohlc) < 3:
                log_event(
                    f"⚠️ Not enough OHLC for {symbol} ({s_timeframe}) on {exchange.name} → skip."
                )
                continue

            # Use previous closed candle (index -2) to avoid current forming candle
            open_price = to_decimal(ohlc[-2][1])
            close_price = to_decimal(ohlc[-2][4])
            pct_change = (close_price - open_price) / open_price * to_decimal("100")
            log_event(
                f"{symbol} change {pct_change:+.3f}% (open={open_price}, close={close_price}) "
                f"on {exchange.name}"
            )

            acb = self.tm.get_average_cost_for_strategy(sid)
            balance = self.tm.get_balance_for_strategy(sid)

            # === 1️⃣ BUY logic ===
            if pct_change <= drop_trigger:
                if allocated < buy_eur:
                    log_event(
                        f"❌ No BUY: price dropped {pct_change:.2f}% ≤ {drop_trigger}%, "
                        f"but insufficient capital (€{allocated:.2f} < €{buy_eur:.2f})"
                    )
                else:
                    limit_price = (open_price * (Decimal("1") - drop_trigger / Decimal("100"))).quantize(Decimal("0.01"))
                    vol = (buy_eur / limit_price).quantize(Decimal("0.00000001"))
                    trade_id = self.tm.record_trade(symbol, "buy", limit_price, vol, sid)

                    # delegate order placement to exchange
                    exchange.place_limit_order(symbol, "buy", limit_price, vol, trade_id)

                    eur_spent = limit_price * vol
                    new_alloc = float(allocated - eur_spent)
                    with sqlite3.connect(self.tm.db_path) as c:
                        c.execute(
                            "UPDATE strategies SET allocated_eur=? WHERE id=?",
                            (new_alloc, sid),
                        )
                        c.commit()
                    log_event(
                        f"✅ BUY executed: {vol} {symbol} @ €{limit_price:.2f} on {exchange.name}"
                        f"(drop {pct_change:.2f}% ≤ {drop_trigger}%), "
                        f"spent €{eur_spent:.2f}, new alloc €{new_alloc:.2f}"
                    )
                # After BUY decision, don't evaluate SELL
                continue

            # === 2️⃣ SELL logic ===
            if pct_change >= rise_trigger:
                if acb is None:
                    log_event(
                        f"❌ No SELL: rise +{pct_change:.2f}% ≥ {rise_trigger}%, but no ACB found."
                    )
                    continue
                if close_price <= acb:
                    log_event(
                        f"❌ No SELL: rise +{pct_change:.2f}% ≥ {rise_trigger}%, "
                        f"but still below ACB €{acb:.2f}"
                    )
                    continue
                if balance <= to_decimal("0.00005"):
                    log_event(
                        f"❌ No SELL: balance {balance:.6f} {symbol} too low to sell."
                    )
                    continue

                limit_price = (open_price * (Decimal("1") + rise_trigger / Decimal("100"))).quantize(Decimal("0.01"))
                vol = min((sell_eur / limit_price).quantize(Decimal("0.00000001")), balance)
                trade_id = self.tm.record_trade(symbol, "sell", limit_price, vol, sid)

                # delegate order placement to exchange
                exchange.place_limit_order(symbol, "sell", limit_price, vol, trade_id)

                eur_gained = limit_price * vol
                new_alloc = float(allocated + eur_gained)
                with sqlite3.connect(self.tm.db_path) as c:
                    c.execute(
                        "UPDATE strategies SET allocated_eur=? WHERE id=?",
                        (new_alloc, sid),
                    )
                    c.commit()
                log_event(
                    f"💰 SELL executed: {vol} {symbol} @ €{limit_price:.2f} on {exchange.name}"
                    f"(rise +{pct_change:.2f}% ≥ {rise_trigger}%, above ACB €{acb:.2f}) → "
                    f"gained €{eur_gained:.2f}, new alloc €{new_alloc:.2f}"
                )
                continue

            # === 3️⃣ Otherwise: idle ===
            if pct_change < 0:
                log_event(
                    f"💤 No BUY: drop {pct_change:.2f}% smaller than threshold {drop_trigger}%"
                )
            else:
                log_event(
                    f"💤 No SELL: rise +{pct_change:.2f}% smaller than threshold {rise_trigger}%"
                )

        log_event("Cycle completed.\n")

    # -------------------- Loop Modes --------------------

    def run_live(self):
        """
        Live scheduler that triggers:
          - 1h strategies at xx:01 UTC every hour
          - 4h strategies at 00:03, 04:03, 08:03, 12:03, 16:03, 20:03 UTC
          - 1d strategies at 00:05 UTC daily
        """
        log_event(
            f"Aurono Trader started in LIVE mode on {self.exchange.name} "
            "(UTC scheduler: 1h@xx:01, 4h@xx:03 (0/4/8/12/16/20), 1d@00:05)."
        )

        def utcnow_floor_sec():
            return datetime.now(timezone.utc).replace(microsecond=0)

        def next_hourly(now: datetime) -> datetime:
            base = now.replace(second=0, microsecond=0)
            candidate = base.replace(minute=1)
            if base.minute >= 1:
                candidate = (base.replace(minute=0) + timedelta(hours=1)).replace(minute=1)
            return candidate

        def next_4h(now: datetime) -> datetime:
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

        def next_daily(now: datetime) -> datetime:
            today = now.date()
            candidate = datetime(
                today.year, today.month, today.day, 0, 5, 0, tzinfo=timezone.utc
            )
            if now >= candidate:
                candidate += timedelta(days=1)
            return candidate

        last_fired = {"1h": None, "4h": None, "1d": None}

        while True:
            now = utcnow_floor_sec()
            n1h = next_hourly(now)
            n4h = next_4h(now)
            n1d = next_daily(now)
            next_evt = min(n1h, n4h, n1d)

            sleep_s = max(1, int((next_evt - now).total_seconds()))
            log_event(
                f"🕒 Next event at {next_evt.strftime('%Y-%m-%d %H:%M:%S')} UTC "
                f"(sleep {sleep_s}s)"
            )
            time.sleep(sleep_s)

            fired_now = utcnow_floor_sec()

            # 1H @ xx:01
            if fired_now.minute == 1 and (
                last_fired["1h"] != fired_now.replace(minute=1, second=0)
            ):
                last_fired["1h"] = fired_now.replace(minute=1, second=0)
                log_event("⏱ Trigger 1H batch")
                self.run_strategy_once("1h")

            # 4H @ {0,4,8,12,16,20}:03
            if (
                fired_now.minute == 3
                and fired_now.hour % 4 == 0
                and last_fired["4h"] != fired_now.replace(minute=3, second=0)
            ):
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

    def run_dev(self):
        cfg = self.cfg()
        dev_sleep = int(cfg.get("dev_sleep_hours", 4))
        log_event(
            f"Aurono Trader started in DEV mode ({dev_sleep} h) on {self.exchange.name}."
        )
        while True:
            self.run_strategy_once()
            log_event(f"Sleeping {dev_sleep} hours…")
            time.sleep(max(1, dev_sleep * 3600))

