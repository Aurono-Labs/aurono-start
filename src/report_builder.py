# src/report_builder.py

import os
import re
import sqlite3
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List

import psutil

from utils import (
    _open_db,
    current_config,
    to_decimal,
    get_db_path,
    root_path,
)
from trade_manager import TradeManager
from exchange_factory import get_exchange
from report_validator import validate_daily_report, validate_weekly_report


# ============================================================
# Helpers: load strategies from DB
# ============================================================

def load_strategies_from_db() -> List[sqlite3.Row]:
    """
    Returns enabled strategies from DB as sqlite3.Row objects.

    Expected columns in 'strategies':
    - id, name, symbol, timeframe, exchange, allocated_eur, enabled
    """
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
    """
    Uses existing trades table + live ticker to compute:
    - price, balance (qty), ACB, value, pnl_% for a single strategy.

    trades schema:
      id, symbol, side, price, amount, timestamp, txid, strategy_id
    """
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

    return {
        "symbol": sym,
        "timeframe": tf,
        "exchange": exch,
        "price": price,
        "balance": balance,
        "value": value,
        "acb": acb,
        "pnl_pct": pnl_pct,
        "allocated_eur": float(strategy["allocated_eur"] or 0.0),
    }


# ============================================================
# Helpers: process / system status for daily report
# ============================================================

def _is_process_running(match: str) -> bool:
    """
    Return True if any process command line contains the given substring.
    """
    try:
        for p in psutil.process_iter(attrs=["cmdline"]):
            cmd = " ".join(p.info.get("cmdline") or [])
            if match in cmd:
                return True
    except Exception:
        pass
    return False


def _collect_exchanges_from_strategies(strategies: List[sqlite3.Row]) -> List[str]:
    """
    Return unique exchange names from strategies.
    """
    ex_set = {s["exchange"] for s in strategies if s["exchange"]}
    return sorted(ex_set)


# ============================================================
# Helpers: filled trades (as 'filled_orders' block)
# ============================================================

def _get_filled_trades_since(since: datetime) -> List[Dict[str, Any]]:
    """
    Aurono does not have a separate orders table; each row in 'trades'
    is effectively a filled order. We approximate 'filled orders'
    from the trades table.

    Returns list of dicts ready for the JSON 'filled_orders' block.

    trades columns:
      id, symbol, side, price, amount, timestamp, txid, strategy_id

    We JOIN strategies to get exchange + timeframe.
    """
    cfg = current_config()
    default_exchange = cfg.get("exchange", "bitvavo")

    conn = _open_db()
    conn.row_factory = sqlite3.Row

    since_str = since.strftime("%Y-%m-%d %H:%M:%S")
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
        WHERE t.timestamp >= ?
        ORDER BY t.timestamp DESC
        """,
        (since_str,),
    ).fetchall()
    conn.close()

    filled: List[Dict[str, Any]] = []
    VALID_TF = {"1h", "4h", "1d"}

    for r in rows:
        # Timestamp is stored as "YYYY-MM-DD HH:MM:SS" (naive). Treat as UTC for schema.
        try:
            ts_raw = r["timestamp"]
            dt = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            ts_iso = dt.isoformat()
        except Exception:
            ts_iso = datetime.now(timezone.utc).isoformat()

        # Strategy context
        s_sym = r["s_symbol"] or r["symbol"]
        s_tf = r["s_timeframe"]
        s_ex = r["s_exchange"] or default_exchange

        # DAILY JSON SCHEMA: timeframe is required and must be one of ["1h","4h","1d"]
        if not s_tf or s_tf not in VALID_TF:
            # Skip trades that cannot be mapped to a valid timeframe
            continue

        strategy_label = r["s_name"] or f"{s_sym} {s_tf} ({s_ex})"

        filled.append({
            "symbol": r["symbol"],
            "exchange": s_ex,
            "timeframe": s_tf,
            "side": r["side"],
            "amount": float(r["amount"]),
            "price": float(r["price"]),
            "timestamp": ts_iso,
            "strategy_label": strategy_label,
            # DB has no pnl column; schema does not require it → set 0.0 placeholder
            "pnl": 0.0,
        })

    return filled


# ============================================================
# Helpers: portfolio snapshot (no extra backend)
# ============================================================

def _compute_portfolio_block(strategies: List[sqlite3.Row]) -> Dict[str, float]:
    """
    Build 'portfolio' block for daily report from strategy snapshots.

    Uses existing trades + live tickers; does NOT require extra tables.
    """
    snapshots = [compute_strategy_stats(s) for s in strategies]

    crypto_value = sum(s["value"] for s in snapshots)
    cash_value = sum(s["allocated_eur"] for s in snapshots)
    total_value = crypto_value + cash_value

    unrealized_pnl = 0.0
    for s in snapshots:
        if s["acb"] is not None and s["balance"] > 0:
            unrealized_pnl += (s["price"] - s["acb"]) * s["balance"]

    # We do not have a historical snapshot for "yesterday", so we set 0 for now.
    change_since_yesterday_pct = 0.0

    return {
        "total_value_eur": float(total_value),
        "crypto_value_eur": float(crypto_value),
        "cash_value_eur": float(cash_value),
        "unrealized_pnl_eur": float(unrealized_pnl),
        "change_since_yesterday_pct": float(change_since_yesterday_pct),
    }


# ============================================================
# Helpers: capital (reserved & available)
# ============================================================

def _compute_capital_block(strategies: List[sqlite3.Row]) -> Dict[str, Any]:
    """
    CAPITAL block for the Daily Report.

    - reserved: directly from strategies.allocated_eur  
    - available: currently 0.0 per exchange (Aurono does not track free EUR yet)
    """

    # RESERVED: per strategy (allocated_eur)
    reserved_list = []
    for s in strategies:
        reserved_list.append({
            "strategy_id": s["id"],
            "symbol": s["symbol"],
            "exchange": s["exchange"],
            "amount_eur": float(s["allocated_eur"] or 0.0),
        })

    # AVAILABLE: 0 for now
    exchanges = _collect_exchanges_from_strategies(strategies)
    available = {ex: 0.0 for ex in exchanges}

    return {
        "reserved": reserved_list,
        "available": available,
    }


# ============================================================
# Helpers: alerts from log file
# ============================================================

def _get_recent_alerts(hours: int = 24) -> List[str]:
    """
    Very light-weight alert collector from aurono_log.txt:
    - returns lines containing '⚠️' or '❌' or 'ERROR'
    - within the last N hours (based on timestamp prefix).
    """
    cfg = current_config()
    log_rel = cfg.get("log_path", "../data/aurono_log.txt")
    log_filename = log_rel.split("/")[-1]
    log_file = root_path("data", log_filename)

    if not os.path.exists(log_file):
        return []

    cutoff = datetime.now() - timedelta(hours=hours)
    alerts: List[str] = []

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
                # If no timestamp, include as generic alert
                alerts.append(text)
                continue

            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except Exception:
                alerts.append(text)
                continue

            if ts >= cutoff:
                alerts.append(text)

    return alerts


# ============================================================
# DAILY REPORT  — matches aurono.daily.report.schema.v1
# ============================================================

def generate_daily_report() -> Dict[str, Any]:
    """
    Generate the full Daily Report using only existing Aurono data.

    - system: processes + placeholder exchange health
    - filled_orders: from trades in last 24h (JOIN strategies for exchange/timeframe)
    - capital: reserved via TradeManager; available=0.0 per exchange
    - portfolio: based on per-strategy snapshots
    - alerts: from log file
    """

    now = datetime.now(timezone.utc)
    strategies = load_strategies_from_db()

    # SYSTEM
    ex_names = _collect_exchanges_from_strategies(strategies)

    exchanges_status = []
    for ex_name in ex_names:
        # We don't yet expose fine-grained health metrics in the exchange classes.
        # Use simple placeholders here (schema-compatible).
        exchanges_status.append({
            "name": ex_name,
            "connected": True,
            "last_ohlc_update_ok": True,
            "last_ticker_ok": True,
            "errors": [],
        })

    system_block = {
        "trader_running": _is_process_running("trader_main.py"),
        "dashboard_running": _is_process_running("dashboard.py"),
        "exchanges": exchanges_status,
    }

    # FILLED ORDERS (24h)
    since = now - timedelta(hours=24)
    filled_orders_block = _get_filled_trades_since(since)

    # CAPITAL (reserved + available)
    capital_block = _compute_capital_block(strategies)

    # PORTFOLIO SNAPSHOT
    portfolio_block = _compute_portfolio_block(strategies)

    # ALERTS (from log)
    alerts_block = _get_recent_alerts(hours=24)

    report: Dict[str, Any] = {
        "date": now.isoformat(),
        "system": system_block,
        "filled_orders": filled_orders_block,
        "capital": capital_block,
        "portfolio": portfolio_block,
        "alerts": alerts_block,
    }

    # Validate against aurono.daily.report.schema.v1
    validate_daily_report(report)
    return report


# ============================================================
# WEEKLY REPORT  — matches aurono.weekly.report.schema.v1
# ============================================================

def generate_weekly_report() -> Dict[str, Any]:
    """
    Full weekly report aligned with aurono.weekly.report.schema.v1,
    using existing trades + strategies + live tickers.

    Since the trades table has no pnl column, all pnl-related fields
    are set to 0.0 (placeholders) but keep the structure schema-valid.
    """

    now = datetime.utcnow()
    week_end = now.strftime("%Y-%m-%d")
    week_start_dt = now - timedelta(days=7)
    week_start = week_start_dt.strftime("%Y-%m-%d")

    conn = _open_db()
    conn.row_factory = sqlite3.Row

    # Load strategies
    strategies = load_strategies_from_db()

    # Create lookup by strategy_id for exchange/timeframe
    strat_by_id = {s["id"]: s for s in strategies}

    # Load trades for the past week, joined with strategies to get exchange/timeframe
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
        WHERE t.timestamp >= ?
        ORDER BY t.timestamp ASC
        """,
        (week_start_dt.strftime("%Y-%m-%d %H:%M:%S"),),
    ).fetchall()
    conn.close()

    trades: List[Dict[str, Any]] = []
    for r in rows:
        sid = r["strategy_id"]
        s_tf = r["s_timeframe"]
        s_ex = r["s_exchange"]

        # Fallback: use strategy lookup if join was None
        if sid in strat_by_id:
            if not s_tf:
                s_tf = strat_by_id[sid]["timeframe"]
            if not s_ex:
                s_ex = strat_by_id[sid]["exchange"]

        trades.append({
            "strategy_id": sid,
            "symbol": r["symbol"],
            "exchange": s_ex or "bitvavo",
            "timeframe": s_tf or "",
            "side": r["side"],
            "price": float(r["price"]),
            "amount": float(r["amount"]),
            # No pnl column in DB → use 0.0 placeholder
            "pnl": 0.0,
        })

    # --------------------------------------------------------
    # PERFORMANCE
    # --------------------------------------------------------
    buys = sum(1 for t in trades if t["side"] == "buy")
    sells = sum(1 for t in trades if t["side"] == "sell")
    sell_wins = 0  # no pnl data → win-rate not meaningful
    weekly_pnl = 0.0

    performance_block = {
        "weekly_pnl_eur": round(weekly_pnl, 2),
        "buys": buys,
        "sells": sells,
        "sell_win_rate": 0.0 if sells == 0 else 0.0,
    }

    # --------------------------------------------------------
    # PER-STRATEGY BREAKDOWN
    # --------------------------------------------------------
    strategies_block: List[Dict[str, Any]] = []

    for s in strategies:
        sid = s["id"]
        s_trades = [t for t in trades if t["strategy_id"] == sid]

        buys_s = [t for t in s_trades if t["side"] == "buy"]
        sells_s = [t for t in s_trades if t["side"] == "sell"]

        # No real pnl in DB; keep 0.0 for now
        pnl_s = 0.0

        avg_buy = sum(t["price"] for t in buys_s) / len(buys_s) if buys_s else None
        avg_sell = sum(t["price"] for t in sells_s) / len(sells_s) if sells_s else None

        strategies_block.append({
            "strategy_id": sid,
            "symbol": s["symbol"],
            "exchange": s["exchange"],
            "timeframe": s["timeframe"],
            "label": s["name"] or f"{s['symbol']} {s['timeframe']} ({s['exchange']})",
            "buys": len(buys_s),
            "sells": len(sells_s),
            "weekly_pnl_eur": round(pnl_s, 2),
            "avg_buy_price": round(avg_buy, 4) if avg_buy is not None else 0.0,
            "avg_sell_price": round(avg_sell, 4) if avg_sell is not None else 0.0,
        })

    # --------------------------------------------------------
    # HIGHLIGHTS (best/worst trade)
    # pnl is always 0.0, so we only fill structure, not meaningful ranking
    # --------------------------------------------------------
    if trades:
        # just pick the first trade as "best" and last as "worst" structurally
        best = trades[0]
        worst = trades[-1]
        best_block = {
            "symbol": best["symbol"],
            "exchange": best["exchange"],
            "timeframe": best["timeframe"],
            "pnl": 0.0,
        }
        worst_block = {
            "symbol": worst["symbol"],
            "exchange": worst["exchange"],
            "timeframe": worst["timeframe"],
            "pnl": 0.0,
        }
    else:
        best_block = None
        worst_block = None

    highlights_block = {
        "best_trade": best_block,
        "worst_trade": worst_block,
    }

    # --------------------------------------------------------
    # EXPOSURE (re-use compute_strategy_stats)
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

    # by_coin
    totals_by_coin: Dict[str, float] = {}
    for s in snapshots:
        totals_by_coin.setdefault(s["symbol"], 0.0)
        totals_by_coin[s["symbol"]] += s["value"]
    for coin, val in totals_by_coin.items():
        exposure_block["by_coin"][coin] = round(val / total_value * 100, 2) if total_value else 0.0

    # by_exchange
    totals_by_ex: Dict[str, float] = {}
    for s in snapshots:
        totals_by_ex.setdefault(s["exchange"], 0.0)
        totals_by_ex[s["exchange"]] += s["value"]
    for ex, val in totals_by_ex.items():
        exposure_block["by_exchange"][ex] = round(val / total_value * 100, 2) if total_value else 0.0

    # --------------------------------------------------------
    # CAPITAL EFFICIENCY (approximation)
    # --------------------------------------------------------
    cap_eff = {
        "eur_deployed": round(crypto_value, 2),
        "eur_reserved": round(cash_value, 2),
        "eur_free": 0.0,  # free EUR not yet tracked separately
    }

    # --------------------------------------------------------
    # SYSTEM RELIABILITY (placeholders)
    # --------------------------------------------------------
    reliability_block = {
        "api_uptime_pct": 100.0,
        "ohlc_failures": 0,
        "order_retries": 0,
        "credential_issues": 0,
        "trader_uptime_pct": 100.0,
    }

    # --------------------------------------------------------
    # STRATEGY CHANGES (future extension)
    # --------------------------------------------------------
    changes_block = {
        "added": [],
        "updated": [],
        "deleted": [],
    }

    report: Dict[str, Any] = {
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

