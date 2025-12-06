# src/report_builder.py

import os
import re
import json
import sqlite3
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, List

import psutil

from utils import (
    _open_db,
    current_config,
    to_decimal,
    root_path,
)
from trade_manager import TradeManager
from exchange_factory import get_exchange
from formatting import format_price
from report_validator import validate_daily_report, validate_weekly_report


# ============================================================
# Helpers: load strategies from DB
# ============================================================

def load_strategies_from_db() -> List[sqlite3.Row]:
    conn = _open_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, name, symbol, timeframe, exchange, allocated_eur
        FROM strategies
        WHERE enabled = 1
        """
    ).fetchall()
    conn.close()
    return rows


# ============================================================
# Helpers: compute ACB, balance, value per strategy
# ============================================================

def compute_strategy_stats(strategy: sqlite3.Row) -> Dict[str, Any]:
    sid = strategy["id"]
    sym = strategy["symbol"]
    tf = strategy["timeframe"]
    exch = strategy["exchange"]

    exchange = get_exchange(exch)
    price = float(exchange.get_ticker(sym))

    conn = _open_db()
    conn.row_factory = sqlite3.Row
    trades = conn.execute(
        """
        SELECT side, price, amount
        FROM trades
        WHERE strategy_id = ?
        ORDER BY id ASC
        """,
        (sid,),
    ).fetchall()
    conn.close()

    total_cost = Decimal("0")
    total_qty = Decimal("0")

    for t in trades:
        p = to_decimal(t["price"])
        a = to_decimal(t["amount"])

        if t["side"] == "buy":
            total_cost += p * a
            total_qty += a

        elif t["side"] == "sell" and total_qty > 0:
            proportion = a / total_qty
            if proportion > 1:
                proportion = 1

            total_cost -= total_cost * proportion
            total_qty -= a

            if total_qty < 0:
                total_qty = Decimal("0")
                total_cost = Decimal("0")

    balance = float(total_qty)
    acb = float(total_cost / total_qty) if total_qty > 0 else None
    value = balance * price
    pnl_pct = ((price - acb) / acb * 100) if acb else 0.0
    
    ticks = exchange._market_ticks[sym]["price_tick"]
    price_str = format_price(Decimal(str(price)), ticks)
    acb_str = format_price(Decimal(str(acb)), ticks) if acb else None

    return {
        "symbol": sym,
        "timeframe": tf,
        "exchange": exch,
        "price": price,
        "price_str": price_str,
        "balance": balance,
        "value": value,
        "acb": acb,
        "acb_str": acb_str,
        "pnl_pct": pnl_pct,
        "allocated_eur": float(strategy["allocated_eur"] or 0.0),
    }

# ============================================================
# Helpers: process & system status
# ============================================================

def _is_process_running(match: str) -> bool:
    try:
        for p in psutil.process_iter(attrs=["cmdline"]):
            cmd = " ".join(p.info.get("cmdline") or [])
            if match in cmd:
                return True
    except Exception:
        pass
    return False


def _collect_exchanges_from_strategies(strategies: List[sqlite3.Row]) -> List[str]:
    ex_set = {s["exchange"] for s in strategies if s["exchange"]}
    return sorted(ex_set)


# ============================================================
# Helpers: timestamp parsing for trades
# ============================================================

def _parse_trade_timestamp(ts_raw: str) -> datetime:
    """
    Parse trade timestamp from DB into a timezone-aware UTC datetime.

    Supports:
    - "YYYY-MM-DDTHH:MM:SS.ssssss"
    - "YYYY-MM-DDTHH:MM:SS"
    - "YYYY-MM-DD HH:MM:SS"
    - Generic ISO via datetime.fromisoformat
    """
    if not ts_raw:
        return datetime.min.replace(tzinfo=timezone.utc)

    # Try explicit patterns first
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(ts_raw, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass

    # Fallback: generic ISO
    try:
        dt = datetime.fromisoformat(ts_raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


# ============================================================
# Helpers: load yesterday portfolio value (persistent)
# ============================================================

def _load_yesterday_portfolio_value() -> Decimal | None:
    """
    Loads yesterday's daily report from:
        data/reports/daily/
    """
    base = Path(root_path("data", "reports", "daily"))
    if not base.exists():
        return None

    files = sorted(base.glob("daily_*.json"))
    if not files:
        return None

    yesterday_file = files[-1]

    try:
        with open(yesterday_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        val = data.get("portfolio", {}).get("total_value_eur")
        if val is not None:
            return Decimal(str(val))
    except Exception:
        return None

    return None


# ============================================================
# Helpers: filled trades since (correct timestamps + realized P/L)
# ============================================================

def _get_filled_trades_since(since: datetime) -> List[Dict[str, Any]]:
    """
    Returns all trades executed since `since` (UTC, aware), with:
    - Correct ISO timestamps
    - Realized P/L for SELLs based on ACB at time of trade
    - Inventory updated per strategy (consistent with weekly logic)
    """

    # Ensure since is UTC-aware
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    else:
        since = since.astimezone(timezone.utc)

    cfg = current_config()
    default_exchange = cfg.get("exchange", "bitvavo")

    conn = _open_db()
    conn.row_factory = sqlite3.Row

    # We fetch full history and filter by timestamp in Python
    rows = conn.execute(
        """
        SELECT
            t.id,
            t.strategy_id,
            t.symbol,
            t.side,
            t.price,
            t.amount,
            t.timestamp,
            s.name      AS s_name,
            s.symbol    AS s_symbol,
            s.timeframe AS s_timeframe,
            s.exchange  AS s_exchange
        FROM trades t
        LEFT JOIN strategies s ON t.strategy_id = s.id
        ORDER BY t.timestamp ASC
        """
    ).fetchall()
    conn.close()

    from collections import defaultdict

    VALID_TF = {"1h", "4h", "1d"}
    filled: List[Dict[str, Any]] = []

    # Inventory state per strategy for ACB calculation
    inv_qty: Dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    inv_cost: Dict[int, Decimal] = defaultdict(lambda: Decimal("0"))

    for r in rows:
        sid = r["strategy_id"]
        raw_ts = r["timestamp"]
        ts_dt = _parse_trade_timestamp(raw_ts)
        ts_iso = ts_dt.isoformat()

        s_tf = r["s_timeframe"]
        s_sym = r["s_symbol"] or r["symbol"]
        s_ex = r["s_exchange"] or default_exchange
        label = r["s_name"] or f"{s_sym} {s_tf} ({s_ex})"

        side = r["side"]
        price_dec = to_decimal(r["price"])
        amt_dec = to_decimal(r["amount"])

        # Default P/L is 0
        pnl_dec = Decimal("0")

        # ----- Inventory / ACB update (per strategy) -----
        if sid is not None:
            qty = inv_qty[sid]
            cost = inv_cost[sid]

            if side == "buy":
                # Buys increase inventory and cost basis, but no realized P/L
                cost += price_dec * amt_dec
                qty += amt_dec

            elif side == "sell":
                # Realized P/L based on ACB before the sell
                if qty > 0:
                    acb = cost / qty
                    pnl_dec = (price_dec - acb) * amt_dec
                else:
                    pnl_dec = Decimal("0")

                # After computing P/L, update inventory
                if qty > 0:
                    proportion = amt_dec / qty
                    if proportion > 1:
                        proportion = Decimal("1")
                    cost -= cost * proportion
                    qty -= amt_dec
                    if qty < 0:
                        qty = Decimal("0")
                        cost = Decimal("0")

            inv_qty[sid] = qty
            inv_cost[sid] = cost
            
        # ----- Filter for daily report window + valid timeframes -----
        if s_tf in VALID_TF and ts_dt >= since:
            # Format price using market tickSize
            ex_backend = get_exchange(s_ex)
            ticks = ex_backend._market_ticks[s_sym]["price_tick"]
            price_str = format_price(price_dec, ticks)

            filled.append({
                "symbol": r["symbol"],
                "exchange": s_ex,
                "timeframe": s_tf,
                "side": side,
                "amount": float(amt_dec),

                # raw value
                "price": float(price_dec),

                # formatted value (NEW)
                "price_str": price_str,

                "timestamp": ts_iso,
                "strategy_label": label,
                "pnl": float(round(pnl_dec, 2)),
            })

    return filled


# ============================================================
# Helpers: portfolio block with daily % change
# ============================================================

def _compute_portfolio_block(strategies: List[sqlite3.Row]) -> Dict[str, float]:
    snapshots = [compute_strategy_stats(s) for s in strategies]

    crypto_value = sum(s["value"] for s in snapshots)
    cash_value = sum(s["allocated_eur"] for s in snapshots)
    total_value = crypto_value + cash_value

    unrealized_pnl = 0.0
    for s in snapshots:
        if s["acb"] is not None and s["balance"] > 0:
            unrealized_pnl += (s["price"] - s["acb"]) * s["balance"]

    yesterday_val = _load_yesterday_portfolio_value()
    if yesterday_val and yesterday_val > 0:
        change_pct = float((Decimal(str(total_value)) - yesterday_val) / yesterday_val * 100)
    else:
        change_pct = 0.0

    return {
        "total_value_eur": float(total_value),
        "crypto_value_eur": float(crypto_value),
        "cash_value_eur": float(cash_value),
        "unrealized_pnl_eur": float(unrealized_pnl),
        "change_since_yesterday_pct": change_pct,
    }


# ============================================================
# Helpers: capital block
# ============================================================

def _compute_capital_block(strategies: List[sqlite3.Row]) -> Dict[str, Any]:
    reserved_list = [{
        "strategy_id": s["id"],
        "symbol": s["symbol"],
        "exchange": s["exchange"],
        "amount_eur": float(s["allocated_eur"] or 0.0),
    } for s in strategies]

    exchanges = _collect_exchanges_from_strategies(strategies)
    available = {ex: 0.0 for ex in exchanges}

    return {
        "reserved": reserved_list,
        "available": available,
    }


# ============================================================
# Helpers: alerts from log (correct 24h filter)
# ============================================================

def _get_recent_alerts(hours: int = 24) -> List[str]:
    cfg = current_config()
    log_rel = cfg.get("log_path", "../data/aurono_log.txt")
    log_filename = log_rel.split("/")[-1]
    log_file = root_path("data", log_filename)

    if not os.path.exists(log_file):
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    alerts = []

    ts_re = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})]")

    with open(log_file, "r", encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue

            if "⚠️" not in text and "❌" not in text and "ERROR" not in text.upper():
                continue

            m = ts_re.match(text)
            if not m:
                continue

            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except Exception:
                continue

            if ts >= cutoff:
                alerts.append(text)

    return alerts


# ============================================================
# DAILY REPORT
# ============================================================

def generate_daily_report() -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    strategies = load_strategies_from_db()

    ex_names = _collect_exchanges_from_strategies(strategies)
    exchanges_status = [{
        "name": ex_name,
        "connected": True,
        "last_ohlc_update_ok": True,
        "last_ticker_ok": True,
        "errors": [],
    } for ex_name in ex_names]

    system_block = {
        "trader_running": _is_process_running("trader_main.py"),
        "dashboard_running": _is_process_running("dashboard.py"),
        "exchanges": exchanges_status,
    }

    since = now - timedelta(hours=24)
    filled_orders_block = _get_filled_trades_since(since)
    capital_block = _compute_capital_block(strategies)
    portfolio_block = _compute_portfolio_block(strategies)
    alerts_block = _get_recent_alerts(hours=24)

    report = {
        "date": now.isoformat(),
        "system": system_block,
        "filled_orders": filled_orders_block,
        "capital": capital_block,
        "portfolio": portfolio_block,
        "alerts": alerts_block,
    }

    validate_daily_report(report)
    return report


# ============================================================
# WEEKLY REPORT
# ============================================================

def generate_weekly_report() -> Dict[str, Any]:
    """
    Full weekly report aligned with aurono.weekly.report.schema.v1.
    Uses full trade history to reconstruct ACB at the moment of each SELL.
    """
    now = datetime.now(timezone.utc)

    # Ensure week boundaries are timezone-aware UTC
    week_end_dt = now
    week_start_dt = (now - timedelta(days=7))

    # Make absolutely sure both are timezone-aware UTC
    week_end_dt = week_end_dt.astimezone(timezone.utc)
    week_start_dt = week_start_dt.astimezone(timezone.utc)

    week_end = week_end_dt.strftime("%Y-%m-%d")
    week_start = week_start_dt.strftime("%Y-%m-%d")

    conn = _open_db()
    conn.row_factory = sqlite3.Row

    strategies = load_strategies_from_db()
    strat_by_id = {s["id"]: s for s in strategies}

    rows = conn.execute(
        """
        SELECT
            t.id,
            t.strategy_id,
            t.symbol,
            t.side,
            t.price,
            t.amount,
            t.timestamp,
            s.timeframe AS s_timeframe,
            s.exchange  AS s_exchange
        FROM trades t
        LEFT JOIN strategies s ON t.strategy_id = s.id
        ORDER BY t.timestamp ASC
        """
    ).fetchall()
    conn.close()

    from collections import defaultdict

    inv_qty = defaultdict(lambda: Decimal("0"))
    inv_cost = defaultdict(lambda: Decimal("0"))

    weekly_trades: List[Dict[str, Any]] = []
    sell_records: List[Dict[str, Any]] = []

    buys = 0
    sells = 0

    pnl_by_strategy = defaultdict(lambda: Decimal("0"))
    sell_return_sum_by_strategy = defaultdict(lambda: Decimal("0"))
    sell_return_count_by_strategy = defaultdict(int)

    # --------------------------------------------------------
    # RECONSTRUCT ACB HISTORY
    # --------------------------------------------------------

    for r in rows:
        sid = r["strategy_id"]
        sym = r["symbol"]
        side = r["side"]
        price_dec = to_decimal(r["price"])
        amt_dec = to_decimal(r["amount"])

        ts_dt = _parse_trade_timestamp(r["timestamp"])

        s_tf = r["s_timeframe"]
        s_ex = r["s_exchange"]

        if sid in strat_by_id:
            if not s_tf:
                s_tf = strat_by_id[sid]["timeframe"]
            if not s_ex:
                s_ex = strat_by_id[sid]["exchange"]

        # Inventory update
        if sid is not None:
            qty = inv_qty[sid]
            cost = inv_cost[sid]

            if side == "buy":
                cost += price_dec * amt_dec
                qty += amt_dec

            elif side == "sell":
                # ACB BEFORE the sell
                if qty > 0:
                    acb = cost / qty
                    pnl_dec = (price_dec - acb) * amt_dec
                    ret_pct = (price_dec - acb) / acb * Decimal("100") if acb != 0 else None
                else:
                    pnl_dec = Decimal("0")
                    ret_pct = None

                # Inside weekly window → count PNL + stats
                if week_start_dt <= ts_dt <= week_end_dt:
                    sells += 1
                    pnl_by_strategy[sid] += pnl_dec

                    if ret_pct is not None:
                        sell_return_sum_by_strategy[sid] += ret_pct
                        sell_return_count_by_strategy[sid] += 1
                        
                    # Get tickSize for formatting
                    ex_backend = get_exchange(s_ex or "bitvavo")
                    ticks = ex_backend._market_ticks[sym]["price_tick"]
                    price_str = format_price(price_dec, ticks)

                    sell_records.append({
                        "strategy_id": sid,
                        "symbol": sym,
                        "exchange": s_ex or "bitvavo",
                        "timeframe": s_tf or "",

                        # raw
                        "price": float(price_dec),

                        # formatted
                        "price_str": price_str,

                        "amount": float(amt_dec),
                        "timestamp": r["timestamp"],
                        "pnl": float(pnl_dec),
                        "return_pct": float(ret_pct) if ret_pct is not None else None,
                    })

                # Apply sell to inventory
                if qty > 0:
                    proportion = amt_dec / qty
                    if proportion > 1:
                        proportion = Decimal("1")
                    cost -= cost * proportion
                    qty -= amt_dec
                    if qty < 0:
                        qty = Decimal("0")
                        cost = Decimal("0")

            inv_qty[sid] = qty
            inv_cost[sid] = cost

        # Weekly trade counts
        if week_start_dt <= ts_dt <= week_end_dt:
            if side == "buy":
                buys += 1
            elif side == "sell" and sid is None:
                sells += 1
                
            # Get tickSize for formatting
            ex_backend = get_exchange(s_ex or "bitvavo")
            ticks = ex_backend._market_ticks[sym]["price_tick"]
            price_str = format_price(price_dec, ticks)

            weekly_trades.append({
                "strategy_id": sid,
                "symbol": sym,
                "exchange": s_ex or "bitvavo",
                "timeframe": s_tf or "",
                "side": side,

                # raw
                "price": float(price_dec),

                # formatted
                "price_str": price_str,

                "amount": float(amt_dec),
                "timestamp": r["timestamp"],
            })

    # --------------------------------------------------------
    # PERFORMANCE BLOCK
    # --------------------------------------------------------

    weekly_pnl_dec = sum(Decimal(str(sr["pnl"])) for sr in sell_records)
    weekly_pnl = float(weekly_pnl_dec)

    all_returns = [sr["return_pct"] for sr in sell_records if sr["return_pct"] is not None]

    if all_returns:
        avg_sell_return = sum(all_returns) / len(all_returns)
        best_sell_return = max(all_returns)
        worst_sell_return = min(all_returns)
    else:
        avg_sell_return = 0.0
        best_sell_return = 0.0
        worst_sell_return = 0.0

    performance_block = {
        "weekly_pnl_eur": round(weekly_pnl, 2),
        "buys": buys,
        "sells": sells,
        # kept for schema compatibility; not meaningful because we only sell above ACB
        "sell_win_rate": 0.0,
        "average_sell_return_pct": round(float(avg_sell_return), 2),
        "best_sell_return_pct": round(float(best_sell_return), 2),
        "worst_sell_return_pct": round(float(worst_sell_return), 2),
    }

    # --------------------------------------------------------
    # PER-STRATEGY BREAKDOWN
    # --------------------------------------------------------

    strategies_block: List[Dict[str, Any]] = []

    for s in strategies:
        sid = s["id"]
        s_trades = [t for t in weekly_trades if t["strategy_id"] == sid]

        buys_s = [t for t in s_trades if t["side"] == "buy"]
        sells_s = [t for t in s_trades if t["side"] == "sell"]

        avg_buy = sum(t["price"] for t in buys_s) / len(buys_s) if buys_s else None
        avg_sell = sum(t["price"] for t in sells_s) / len(sells_s) if sells_s else None

        pnl_s = float(pnl_by_strategy.get(sid, Decimal("0")))

        if sell_return_count_by_strategy.get(sid, 0) > 0:
            avg_ret_s = sell_return_sum_by_strategy[sid] / sell_return_count_by_strategy[sid]
            avg_ret_s_f = round(float(avg_ret_s), 2)
        else:
            avg_ret_s_f = 0.0
            
        # tickSize formatting for averages
        ex_backend = get_exchange(s["exchange"])
        ticks = ex_backend._market_ticks[s["symbol"]]["price_tick"]

        avg_buy_str = format_price(Decimal(str(avg_buy)), ticks) if avg_buy is not None else None
        avg_sell_str = format_price(Decimal(str(avg_sell)), ticks) if avg_sell is not None else None

        strategies_block.append({
            "strategy_id": sid,
            "symbol": s["symbol"],
            "exchange": s["exchange"],
            "timeframe": s["timeframe"],
            "label": s["name"] or f"{s['symbol']} {s['timeframe']} ({s['exchange']})",
            "buys": len(buys_s),
            "sells": len(sells_s),

            "weekly_pnl_eur": round(pnl_s, 2),

            # raw values
            "avg_buy_price": float(avg_buy) if avg_buy is not None else None,
            "avg_sell_price": float(avg_sell) if avg_sell is not None else None,

            # formatted values
            "avg_buy_price_str": avg_buy_str,
            "avg_sell_price_str": avg_sell_str,

            "avg_sell_return_pct": avg_ret_s_f,
        })

    # --------------------------------------------------------
    # HIGHLIGHTS
    # --------------------------------------------------------

    if sell_records:
        best = max(sell_records, key=lambda x: x["pnl"])
        worst = min(sell_records, key=lambda x: x["pnl"])

        best_block = {
            "symbol": best["symbol"],
            "exchange": best["exchange"],
            "timeframe": best["timeframe"],
            "pnl": round(best["pnl"], 2),
            "return_pct": round(best["return_pct"], 2) if best["return_pct"] is not None else 0.0,
        }

        worst_block = {
            "symbol": worst["symbol"],
            "exchange": worst["exchange"],
            "timeframe": worst["timeframe"],
            "pnl": round(worst["pnl"], 2),
            "return_pct": round(worst["return_pct"], 2) if worst["return_pct"] is not None else 0.0,
        }

    else:
        # Schema requires objects, not null
        best_block = {
            "symbol": "",
            "exchange": "",
            "timeframe": "",
            "pnl": 0.0,
            "return_pct": 0.0,
        }
        worst_block = {
            "symbol": "",
            "exchange": "",
            "timeframe": "",
            "pnl": 0.0,
            "return_pct": 0.0,
        }

    highlights_block = {
        "best_trade": best_block,
        "worst_trade": worst_block,
    }

    # --------------------------------------------------------
    # EXPOSURE
    # --------------------------------------------------------

    snapshots = [compute_strategy_stats(s) for s in strategies]

    total_value = sum(s["value"] + s["allocated_eur"] for s in snapshots)
    crypto_value = sum(s["value"] for s in snapshots)
    cash_value = sum(s["allocated_eur"] for s in snapshots)

    exposure_block: Dict[str, Any] = {
        "cash_pct": round(cash_value / total_value * 100, 2) if total_value else 0.0,
        "crypto_pct": round(crypto_value / total_value * 100, 2) if total_value else 0.0,
        "by_coin": {},
        "by_exchange": {},
    }

    totals_by_coin: Dict[str, float] = {}
    for s in snapshots:
        totals_by_coin.setdefault(s["symbol"], 0.0)
        totals_by_coin[s["symbol"]] += s["value"]

    for coin, val in totals_by_coin.items():
        exposure_block["by_coin"][coin] = round(val / total_value * 100, 2) if total_value else 0.0

    totals_by_ex_crypto: Dict[str, float] = {}
    totals_by_ex_cash: Dict[str, float] = {}

    for s in snapshots:
        ex = s["exchange"]
        totals_by_ex_crypto.setdefault(ex, 0.0)
        totals_by_ex_cash.setdefault(ex, 0.0)
        totals_by_ex_crypto[ex] += s["value"]
        totals_by_ex_cash[ex] += s["allocated_eur"]

    for ex, crypto_val_ex in totals_by_ex_crypto.items():
        cash_val_ex = totals_by_ex_cash.get(ex, 0.0)
        ex_total = crypto_val_ex + cash_val_ex
        exposure_block["by_exchange"][ex] = round(ex_total / total_value * 100, 2) if total_value else 0.0

    # --------------------------------------------------------
    # CAPITAL EFFICIENCY
    # --------------------------------------------------------

    cap_eff = {
        "eur_deployed": round(crypto_value, 2),
        "eur_reserved": round(cash_value, 2),
        "eur_free": 0.0,
    }

    # --------------------------------------------------------
    # SYSTEM RELIABILITY
    # --------------------------------------------------------

    reliability_block = {
        "api_uptime_pct": 100.0,
        "ohlc_failures": 0,
        "order_retries": 0,
        "credential_issues": 0,
        "trader_uptime_pct": 100.0,
    }

    changes_block = {"added": [], "updated": [], "deleted": []}

    report = {
        "week_start": week_start,
        "week_end": week_end,
        "performance": performance_block,
        "strategies": strategies_block,
        "highlights": highlights_block,
        "exposure": exposure_block,
        "capital_efficiency": cap_eff,
        "system_reliability": reliability_block,
        "strategy_changes": changes_block,
    }

    validate_weekly_report(report)
    return report

