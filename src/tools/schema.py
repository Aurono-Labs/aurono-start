from __future__ import annotations
import sqlite3

# ============================================================
# Unified Database Schema (no legacy migrations)
# ============================================================

def ensure_schema(conn: sqlite3.Connection) -> None:
    # --- Trades table ---
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('buy','sell')),
            price REAL NOT NULL,
            amount REAL NOT NULL,
            timestamp TEXT NOT NULL,
            txid TEXT,
            strategy_id INTEGER
        );
        """
    )

    # --- Strategies table ---
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS strategies (
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
            sell_amount_eur REAL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0
        );
        """
    )

    # --- API Credentials table ---
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange TEXT NOT NULL UNIQUE,
            api_key_enc TEXT NOT NULL,
            api_secret_enc TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    conn.commit()

