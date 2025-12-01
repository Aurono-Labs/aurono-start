import sqlite3
from decimal import Decimal
from datetime import datetime
from utils import _open_db, current_config, to_decimal
from exchange_factory import get_exchange


# ------------------------------------------------------------
# Load enabled strategies from DB
# ------------------------------------------------------------
def load_strategies_from_db():
    conn = _open_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, symbol, timeframe, exchange, allocated_eur FROM strategies WHERE enabled=1"
    ).fetchall()
    conn.close()
    return rows


# ------------------------------------------------------------
# Compute ACB, balance, and value for a single strategy
# ------------------------------------------------------------
def compute_strategy_stats(strategy):
    sid = strategy["id"]
    sym = strategy["symbol"]
    tf = strategy["timeframe"]
    exch = strategy["exchange"]

    exchange = get_exchange(exch)
    price = float(exchange.get_ticker(sym))

    conn = _open_db()
    conn.row_factory = sqlite3.Row
    trades = conn.execute(
        "SELECT side, price, amount FROM trades WHERE strategy_id=? ORDER BY id ASC",
        (sid,)
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

    pnl_pct = ((price - acb) / acb * 100) if acb else 0

    return {
        "symbol": sym,
        "timeframe": tf,
        "exchange": exch,
        "price": price,
        "balance": balance,
        "value": value,
        "acb": acb,
        "pnl_pct": pnl_pct,
        "allocated_eur": float(strategy["allocated_eur"] or 0),
    }


# ------------------------------------------------------------
# Daily report
# ------------------------------------------------------------
def generate_daily_report():
    strategies = load_strategies_from_db()
    entries = [compute_strategy_stats(s) for s in strategies]

    total_value = sum(e["value"] + e["allocated_eur"] for e in entries)
    crypto_value = sum(e["value"] for e in entries)
    cash_value = sum(e["allocated_eur"] for e in entries)

    return {
        "date": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        "entries": entries,
        "totals": {
            "total_value": total_value,
            "crypto_value": crypto_value,
            "cash_value": cash_value,
        }
    }


# ------------------------------------------------------------
# Weekly report (end of week summary)
# ------------------------------------------------------------
def generate_weekly_report():
    report = generate_daily_report()
    report["week_end"] = datetime.utcnow().strftime("%Y-%m-%d")
    return report

