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
    detect_missing_credentials,
    log_event,
)
from trade_manager import TradeManager
from exchange_factory import get_exchange
from report_validator import validate_daily_report, validate_weekly_report
from formatting import format_price_dynamic, format_amount_dynamic

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

    price_str = format_price_dynamic(price)
    acb_str = format_price_dynamic(acb) if acb is not None else "-"

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

        "price_str": price_str,
        "acb_str": acb_str,
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
# Helpers: timestamp parsing
# ============================================================

def _parse_trade_timestamp(ts_raw: str) -> datetime:
    if not ts_raw:
        return datetime.min.replace(tzinfo=timezone.utc)

    for fmt in ("%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(ts_raw, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass

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
# Helpers: load yesterday portfolio value
# ============================================================

def _load_yesterday_portfolio_value() -> Decimal | None:
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
# DAILY: get filled trades with formatted prices
# ============================================================

def _get_filled_trades_since(since: datetime) -> List[Dict[str, Any]]:
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    else:
        since = since.astimezone(timezone.utc)

    cfg = current_config()
    default_exchange = cfg.get("exchange", "bitvavo")

    conn = _open_db()
    conn.row_factory = sqlite3.Row

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
            s.name,
            s.symbol AS s_symbol,
            s.timeframe AS s_timeframe,
            s.exchange AS s_exchange
        FROM trades t
        LEFT JOIN strategies s ON t.strategy_id = s.id
        ORDER BY t.timestamp ASC
        """
    ).fetchall()
    conn.close()

    from collections import defaultdict

    VALID_TF = {"1h", "4h", "1d"}

    filled = []
    inv_qty = defaultdict(lambda: Decimal("0"))
    inv_cost = defaultdict(lambda: Decimal("0"))

    for r in rows:
        sid = r["strategy_id"]
        ts_dt = _parse_trade_timestamp(r["timestamp"])
        ts_iso = ts_dt.isoformat()

        s_tf = r["s_timeframe"]
        s_sym = r["s_symbol"] or r["symbol"]
        s_ex = r["s_exchange"] or default_exchange
        label = r["name"] or f"{s_sym} {s_tf} ({s_ex})"

        side = r["side"]
        price_dec = to_decimal(r["price"])
        amt_dec = to_decimal(r["amount"])

        pnl_dec = Decimal("0")

        if sid is not None:
            qty = inv_qty[sid]
            cost = inv_cost[sid]

            if side == "buy":
                cost += price_dec * amt_dec
                qty += amt_dec

            elif side == "sell":
                if qty > 0:
                    acb = cost / qty
                    pnl_dec = (price_dec - acb) * amt_dec

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

        if s_tf in VALID_TF and ts_dt >= since:
            filled.append({
                "symbol": r["symbol"],
                "exchange": s_ex,
                "timeframe": s_tf,
                "side": side,
                "amount": float(amt_dec),
                "amount_str": format_amount_dynamic(amt_dec),

                "price": float(price_dec),
                "price_str": format_price_dynamic(price_dec),

                "timestamp": ts_iso,
                "strategy_label": label,
                "pnl": float(round(pnl_dec, 2)),
            })

    return filled


# ============================================================
# DAILY: portfolio block
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
# DAILY: liquidity summary
# ============================================================
    
def get_liquidity_summary_for_report():
    """
    Returns a clean liquidity summary used in daily/weekly reports.
    Format:
    [
      { "exchange": "bitvavo", "allocated": 120.0, "available": 133.94, "free": 13.94, "has_creds": True },
      { "exchange": "kraken",  "allocated": 50.0,  "available": None,   "free": None,   "has_creds": False }
    ]
    """
    result = []

    # --- Allocated EUR from strategies table ---
    try:
        conn = _open_db()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        allocated_rows = cur.execute("""
            SELECT exchange, SUM(allocated_eur) AS allocated
            FROM strategies
            WHERE enabled = 1
            GROUP BY exchange
        """).fetchall()

        allocated = {row["exchange"]: float(row["allocated"] or 0.0) for row in allocated_rows}

        # Determine exchanges in use
        exchanges = list(allocated.keys())

        conn.close()
    except Exception as e:
        log_event(f"⚠️ Daily report liquidity read error: {e}")
        return []

    # --- Detect missing credentials ---
    missing = detect_missing_credentials() or []

    # --- Fetch available EUR per exchange ---
    for exch in exchanges:
        has_creds = exch not in missing
        alloc = allocated.get(exch, 0.0)

        if has_creds:
            try:
                backend = get_exchange(exch)
                avail = backend.get_available_eur()
                free = round(avail - alloc, 2)
            except Exception:
                avail = None
                free = None
        else:
            avail = None
            free = None

        result.append({
            "exchange": exch,
            "allocated": alloc,
            "available": avail,
            "free": free,
            "has_creds": has_creds
        })

    return result

# ============================================================
# DAILY: alerts block
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

    report = {
        "date": now.isoformat(),
        "system": system_block,
        "filled_orders": _get_filled_trades_since(since),
        "portfolio": _compute_portfolio_block(strategies),
        "alerts": _get_recent_alerts(hours=24),
        "liquidity": get_liquidity_summary_for_report(),
    }

    validate_daily_report(report)
    return report


# ============================================================
# WEEKLY REPORT
# ============================================================

def generate_weekly_report() -> Dict[str, Any]:
    now = datetime.now(timezone.utc)

    week_end_dt = now.astimezone(timezone.utc)
    week_start_dt = (now - timedelta(days=7)).astimezone(timezone.utc)

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

    weekly_trades = []
    sell_records = []

    buys = 0
    sells = 0

    pnl_by_strategy = defaultdict(lambda: Decimal("0"))
    sell_return_sum_by_strategy = defaultdict(lambda: Decimal("0"))
    sell_return_count_by_strategy = defaultdict(int)

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

        if sid is not None:
            qty = inv_qty[sid]
            cost = inv_cost[sid]

            if side == "buy":
                cost += price_dec * amt_dec
                qty += amt_dec

            elif side == "sell":
                if qty > 0:
                    acb = cost / qty
                    pnl_dec = (price_dec - acb) * amt_dec
                    ret_pct = (price_dec - acb) / acb * Decimal("100") if acb != 0 else None
                else:
                    pnl_dec = Decimal("0")
                    ret_pct = None

                if week_start_dt <= ts_dt <= week_end_dt:
                    sells += 1
                    pnl_by_strategy[sid] += pnl_dec

                    if ret_pct is not None:
                        sell_return_sum_by_strategy[sid] += ret_pct
                        sell_return_count_by_strategy[sid] += 1

                    sell_records.append({
                        "strategy_id": sid,
                        "symbol": sym,
                        "exchange": s_ex or "bitvavo",
                        "timeframe": s_tf or "",
                        "price": float(price_dec),
                        "price_str": format_price_dynamic(price_dec),
                        "amount": float(amt_dec),
                        "timestamp": r["timestamp"],
                        "pnl": float(pnl_dec),
                        "return_pct": float(ret_pct) if ret_pct is not None else None,
                    })

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

        if week_start_dt <= ts_dt <= week_end_dt:
            if side == "buy":
                buys += 1

            weekly_trades.append({
                "strategy_id": sid,
                "symbol": sym,
                "exchange": s_ex or "bitvavo",
                "timeframe": s_tf or "",
                "side": side,
                "price": float(price_dec),
                "price_str": format_price_dynamic(price_dec),
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
        "sell_win_rate": 0.0,
        "average_sell_return_pct": round(float(avg_sell_return), 2),
        "best_sell_return_pct": round(float(best_sell_return), 2),
        "worst_sell_return_pct": round(float(worst_sell_return), 2),
    }

    # --------------------------------------------------------
    # STRATEGIES BLOCK
    # --------------------------------------------------------

    strategies_block = []

    for s in strategies:
        sid = s["id"]

        s_trades = [t for t in weekly_trades if t["strategy_id"] == sid]

        buys_s = [t for t in s_trades if t["side"] == "buy"]
        sells_s = [t for t in s_trades if t["side"] == "sell"]

        avg_buy = sum(t["price"] for t in buys_s) / len(buys_s) if buys_s else 0.0
        avg_sell = sum(t["price"] for t in sells_s) / len(sells_s) if sells_s else 0.0

        pnl_s = float(pnl_by_strategy.get(sid, Decimal("0")))

        if sell_return_count_by_strategy.get(sid, 0) > 0:
            avg_ret_s = sell_return_sum_by_strategy[sid] / sell_return_count_by_strategy[sid]
            avg_ret_s_f = round(float(avg_ret_s), 2)
        else:
            avg_ret_s_f = 0.0

        strategies_block.append({
            "strategy_id": sid,
            "symbol": s["symbol"],
            "exchange": s["exchange"],
            "timeframe": s["timeframe"],
            "label": s["name"] or f"{s['symbol']} {s['timeframe']} ({s['exchange']})",

            "buys": len(buys_s),
            "sells": len(sells_s),

            "weekly_pnl_eur": round(pnl_s, 2),

            "avg_buy_price": round(avg_buy, 6),
            "avg_sell_price": round(avg_sell, 6),

            "avg_buy_price_str": format_price_dynamic(avg_buy),
            "avg_sell_price_str": format_price_dynamic(avg_sell),

            "avg_sell_return_pct": avg_ret_s_f,
        })

    # --------------------------------------------------------
    # HIGHLIGHTS BLOCK
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
            "price": best["price"],
            "price_str": best["price_str"],
        }

        worst_block = {
            "symbol": worst["symbol"],
            "exchange": worst["exchange"],
            "timeframe": worst["timeframe"],
            "pnl": round(worst["pnl"], 2),
            "return_pct": round(worst["return_pct"], 2) if worst["return_pct"] is not None else 0.0,
            "price": worst["price"],
            "price_str": worst["price_str"],
        }

    else:
        best_block = {
            "symbol": "",
            "exchange": "",
            "timeframe": "",
            "pnl": 0.0,
            "return_pct": 0.0,
            "price": 0.0,
            "price_str": "-",
        }
        worst_block = {
            "symbol": "",
            "exchange": "",
            "timeframe": "",
            "pnl": 0.0,
            "return_pct": 0.0,
            "price": 0.0,
            "price_str": "-",
        }

    highlights_block = {"best_trade": best_block, "worst_trade": worst_block}

    # --------------------------------------------------------
    # EXPOSURE BLOCK
    # --------------------------------------------------------

    snapshots = [compute_strategy_stats(s) for s in strategies]

    total_value = sum(s["value"] + s["allocated_eur"] for s in snapshots)
    crypto_value = sum(s["value"] for s in snapshots)
    cash_value = sum(s["allocated_eur"] for s in snapshots)

    exposure_block = {
        "cash_pct": round(cash_value / total_value * 100, 2) if total_value else 0.0,
        "crypto_pct": round(crypto_value / total_value * 100, 2) if total_value else 0.0,
        "by_coin": {},
        "by_exchange": {},
    }

    totals_by_coin = {}
    for s in snapshots:
        totals_by_coin.setdefault(s["symbol"], 0.0)
        totals_by_coin[s["symbol"]] += s["value"]

    for coin, val in totals_by_coin.items():
        exposure_block["by_coin"][coin] = round(val / total_value * 100, 2) if total_value else 0.0

    totals_by_ex_crypto = {}
    totals_by_ex_cash = {}

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
        "liquidity": get_liquidity_summary_for_report(),
        
        # Legacy blocks (kept for compatibility)
        "capital_efficiency": cap_eff,
        "system_reliability": reliability_block,
        "strategy_changes": changes_block,
    }

    validate_weekly_report(report)
    return report

