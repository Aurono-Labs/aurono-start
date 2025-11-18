import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import log_event, to_decimal, get_db_path

import sqlite3
from decimal import Decimal
from datetime import datetime


class TradeManager:
    def __init__(self, db_path: Path = None):
        self.db_path = str(db_path or get_db_path())

        # Run schema only once per process
        if not hasattr(TradeManager, "_schema_initialized"):
            self._ensure_schema()
            TradeManager._schema_initialized = True

    def _ensure_schema(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        # --- Trades table ---
        cur.execute('''CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('buy','sell')),
            price REAL NOT NULL,
            amount REAL NOT NULL,
            timestamp TEXT NOT NULL,
            txid TEXT,
            strategy_id INTEGER
        )''')

        # --- Strategies table ---
        cur.execute('''CREATE TABLE IF NOT EXISTS strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            drop_trigger REAL NOT NULL,
            rise_trigger REAL NOT NULL,
            allocated_eur REAL NOT NULL DEFAULT 0,
            enabled BOOLEAN DEFAULT 1,
            last_run TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        cur.execute("PRAGMA table_info(strategies)")
        cols = [r[1] for r in cur.fetchall()]
        if "exchange" not in cols:
            cur.execute("ALTER TABLE strategies ADD COLUMN exchange TEXT DEFAULT 'bitvavo'")
            
        cur.execute("PRAGMA table_info(strategies)")
        cols = [r[1] for r in cur.fetchall()]

        # --- Add new columns if missing ---
        if "buy_amount_eur" not in cols:
            cur.execute("ALTER TABLE strategies ADD COLUMN buy_amount_eur REAL DEFAULT 0")

        if "sell_amount_eur" not in cols:
            cur.execute("ALTER TABLE strategies ADD COLUMN sell_amount_eur REAL DEFAULT 0")

        # --- Soft-migrate existing trade_amount_eur values ---
        if "trade_amount_eur" in cols:
            cur.execute("""
                UPDATE strategies
                SET buy_amount_eur = COALESCE(buy_amount_eur, trade_amount_eur),
                    sell_amount_eur = COALESCE(sell_amount_eur, trade_amount_eur)
                WHERE (buy_amount_eur = 0 OR buy_amount_eur IS NULL)
                  AND (sell_amount_eur = 0 OR sell_amount_eur IS NULL)
            """)
            
            
        # After retrieving cols:
        if "trade_amount_eur" in cols:
            cur.execute("ALTER TABLE strategies RENAME TO strategies_old")

            # Recreate table WITHOUT trade_amount_eur
            cur.execute("""
                CREATE TABLE strategies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    drop_trigger REAL NOT NULL,
                    rise_trigger REAL NOT NULL,
                    allocated_eur REAL NOT NULL DEFAULT 0,
                    enabled BOOLEAN DEFAULT 1,
                    last_run TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    exchange TEXT DEFAULT 'bitvavo',
                    buy_amount_eur REAL DEFAULT 0,
                    sell_amount_eur REAL DEFAULT 0
                )
            """)

            # Copy data from old → new
            cur.execute("""
                INSERT INTO strategies (
                    id, name, symbol, timeframe, drop_trigger, rise_trigger, 
                    allocated_eur, enabled, last_run, created_at, exchange,
                    buy_amount_eur, sell_amount_eur
                )
                SELECT 
                    id, name, symbol, timeframe, drop_trigger, rise_trigger,
                    allocated_eur, enabled, last_run, created_at, 
                    COALESCE(exchange,'bitvavo'),
                    COALESCE(buy_amount_eur, trade_amount_eur, 0),
                    COALESCE(sell_amount_eur, trade_amount_eur, 0)
                FROM strategies_old
            """)

            cur.execute("DROP TABLE strategies_old")


        conn.commit()
        conn.close()

    # ---------------------------------------------------
    # 🔹 Generic multi-asset helpers
    # ---------------------------------------------------

    def record_trade(self, symbol, side, price: Decimal, amount: Decimal, strategy_id: int | None = None):
        """Insert a buy/sell trade for the given symbol and link to a strategy. Returns new trade ID."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO trades (symbol, side, price, amount, timestamp, strategy_id) VALUES (?,?,?,?,?,?)',
            (symbol.upper(), side, float(price), float(amount), datetime.utcnow().isoformat(), strategy_id)
        )
        trade_id = cur.lastrowid
        conn.commit()
        conn.close()
        log_event(f"💾 Recorded {side.upper()} {amount} {symbol.upper()} @ €{price} (strategy_id={strategy_id})")
        return trade_id

    def get_trades(self, symbol):
        """Return all trades for a specific symbol."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT side, price, amount FROM trades WHERE symbol=? ORDER BY id ASC", (symbol.upper(),))
        rows = cur.fetchall()
        conn.close()
        return rows

    def get_trades_for_strategy(self, strategy_id):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT side, price, amount FROM trades WHERE strategy_id=? ORDER BY id ASC",
            (strategy_id,)
        )
        rows = cur.fetchall()
        conn.close()
        return rows

    def get_balance_for_strategy(self, strategy_id):
        trades = self.get_trades_for_strategy(strategy_id)
        buy = sum(to_decimal(a) for s, _, a in trades if s == 'buy')
        sell = sum(to_decimal(a) for s, _, a in trades if s == 'sell')
        return buy - sell

    def get_average_cost_for_strategy(self, strategy_id):
        trades = self.get_trades_for_strategy(strategy_id)
        total_cost = Decimal('0')
        total_qty = Decimal('0')

        for side, price, amount in trades:
            price = to_decimal(price)
            amount = to_decimal(amount)
            if side == 'buy':
                total_cost += price * amount
                total_qty += amount
            elif side == 'sell' and total_qty > 0:
                proportion = amount / total_qty
                if proportion > 1:
                    proportion = 1
                total_cost -= total_cost * proportion
                total_qty -= amount

                if total_qty < 0:
                    total_qty = Decimal('0')
                    total_cost = Decimal('0')

        if total_qty <= 0:
            return None
        return total_cost / total_qty

    def get_balance(self, symbol):
        """Return net quantity held for a given symbol."""
        trades = self.get_trades(symbol)
        buy = sum(to_decimal(a) for s, _, a in trades if s == 'buy')
        sell = sum(to_decimal(a) for s, _, a in trades if s == 'sell')
        return buy - sell

    def get_average_cost(self, symbol):
        """
        Return average cost basis for the currently held position of 'symbol'.
        Uses weighted average and adjusts proportionally on sells.
        """
        trades = self.get_trades(symbol)
        total_cost = Decimal('0')
        total_qty = Decimal('0')

        for side, price, amount in trades:
            price = to_decimal(price)
            amount = to_decimal(amount)
            if side == 'buy':
                total_cost += price * amount
                total_qty += amount
            elif side == 'sell' and total_qty > 0:
                # Reduce cost basis in proportion to the amount sold
                proportion = amount / total_qty
                if proportion > 1:
                    proportion = 1
                total_cost -= total_cost * proportion
                total_qty -= amount
                if total_qty < 0:
                    total_qty = Decimal('0')
                    total_cost = Decimal('0')

        if total_qty <= 0:
            return None
        return total_cost / total_qty

