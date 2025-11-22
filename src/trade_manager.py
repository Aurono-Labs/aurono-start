import sqlite3
from decimal import Decimal
from datetime import datetime
from pathlib import Path

from utils import log_event, to_decimal, get_db_path, _open_db
from tools.schema import ensure_schema


class TradeManager:
    def __init__(self, db_path: Path = None):
        self.db_path = str(db_path or get_db_path())

        # Initialize schema once per runtime
        if not hasattr(TradeManager, "_schema_initialized"):
            conn = _open_db()
            ensure_schema(conn)
            conn.close()
            TradeManager._schema_initialized = True

    # ---------------------------------------------------
    # --- Trade recording and retrieval
    # ---------------------------------------------------

    def record_trade(self, symbol, side, price: Decimal, amount: Decimal, strategy_id=None):
        conn = _open_db()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO trades (symbol, side, price, amount, timestamp, strategy_id)
               VALUES (?,?,?,?,?,?)""",
            (symbol.upper(), side, float(price), float(amount), datetime.utcnow().isoformat(), strategy_id)
        )
        trade_id = cur.lastrowid
        conn.commit()
        conn.close()

        log_event(f"Recorded {side.upper()} {amount} {symbol.upper()} @ €{price} (strategy_id={strategy_id})")
        return trade_id

    def get_trades(self, symbol):
        conn = _open_db()
        cur = conn.cursor()
        cur.execute("SELECT side, price, amount FROM trades WHERE symbol=? ORDER BY id ASC", (symbol.upper(),))
        rows = cur.fetchall()
        conn.close()
        return rows

    def get_trades_for_strategy(self, strategy_id):
        conn = _open_db()
        cur = conn.cursor()
        cur.execute("SELECT side, price, amount FROM trades WHERE strategy_id=? ORDER BY id ASC", (strategy_id,))
        rows = cur.fetchall()
        conn.close()
        return rows

    # ---------------------------------------------------
    # --- Balance & ACB
    # ---------------------------------------------------

    def get_balance_for_strategy(self, strategy_id):
        trades = self.get_trades_for_strategy(strategy_id)
        buy = sum(to_decimal(a) for s, _, a in trades if s == "buy")
        sell = sum(to_decimal(a) for s, _, a in trades if s == "sell")
        return buy - sell

    def get_average_cost_for_strategy(self, strategy_id):
        trades = self.get_trades_for_strategy(strategy_id)
        return _calculate_acb(trades)

    def get_balance(self, symbol):
        trades = self.get_trades(symbol)
        buy = sum(to_decimal(a) for s, _, a in trades if s == "buy")
        sell = sum(to_decimal(a) for s, _, a in trades if s == "sell")
        return buy - sell

    def get_average_cost(self, symbol):
        trades = self.get_trades(symbol)
        return _calculate_acb(trades)


# ---------------------------------------------------
# Shared ACB logic
# ---------------------------------------------------

def _calculate_acb(trades):
    total_cost = Decimal("0")
    total_qty = Decimal("0")

    for side, price, amount in trades:
        price = to_decimal(price)
        amount = to_decimal(amount)

        if side == "buy":
            total_cost += price * amount
            total_qty += amount

        elif side == "sell" and total_qty > 0:
            proportion = amount / total_qty
            if proportion > 1:
                proportion = 1
            total_cost -= total_cost * proportion
            total_qty -= amount

            if total_qty < 0:
                total_qty = Decimal("0")
                total_cost = Decimal("0")

    if total_qty <= 0:
        return None

    return total_cost / total_qty

