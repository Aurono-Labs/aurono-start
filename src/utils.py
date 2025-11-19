import sys
from pathlib import Path
# Add project root and src to sys.path for service compatibility
ROOT = Path(__file__).resolve().parent.parent
SRC  = ROOT / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))

import os, yaml, json
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import List, Dict, Optional

from cryptography.fernet import Fernet


def root_path(*parts):
    return ROOT.joinpath(*parts)

def load_config():
    with open(root_path("config","config.yaml"), "r") as f:
        return yaml.safe_load(f)

def save_config(cfg: dict):
    p = root_path("config","config.yaml")
    with open(p, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)

def current_config():
    return load_config()

def log_event(msg:str):
    cfg = current_config()
    logp = root_path("data", Path(cfg["log_path"]).name)
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    os.makedirs(logp.parent, exist_ok=True)
    with open(logp, "a") as f:
        f.write(line + "\\n")

def to_decimal(v):
    try: return Decimal(str(v))
    except Exception: return Decimal("0.0")

def get_db_path():
    cfg = current_config()
    return root_path("data", Path(cfg["db_path"]).name)

    
# ============================================================
# 🗃 API credentials table helpers (encrypted at rest)
# ============================================================

def _ensure_credentials_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange TEXT NOT NULL UNIQUE,
            api_key_enc TEXT NOT NULL,
            api_secret_enc TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _encrypt_secret(plain: str) -> str:
    if not plain:
        return ""
    f = _get_fernet()
    token = f.encrypt(plain.encode("utf-8"))
    return token.decode("utf-8")

def _decrypt_secret(token: str) -> str:
    if not token:
        return ""
    f = _get_fernet()
    try:
        return f.decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""

def _get_fernet() -> Fernet:
    return Fernet(_load_or_create_device_key())


def upsert_credentials(exchange: str, api_key: str, api_secret: str) -> None:
    """
    Insert or update encrypted credentials for an exchange.
    """
    exchange = (exchange or "").lower().strip()
    if not exchange:
        return

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    try:
        _ensure_credentials_table(conn)
        cur = conn.cursor()

        api_key_enc = _encrypt_secret(api_key.strip())
        api_secret_enc = _encrypt_secret(api_secret.strip())

        cur.execute(
            "SELECT id FROM api_credentials WHERE exchange = ?",
            (exchange,),
        )
        row = cur.fetchone()

        if row:
            cur.execute(
                """
                UPDATE api_credentials
                SET api_key_enc = ?, api_secret_enc = ?, updated_at = CURRENT_TIMESTAMP
                WHERE exchange = ?
                """,
                (api_key_enc, api_secret_enc, exchange),
            )
        else:
            cur.execute(
                """
                INSERT INTO api_credentials (exchange, api_key_enc, api_secret_enc)
                VALUES (?, ?, ?)
                """,
                (exchange, api_key_enc, api_secret_enc),
            )

        conn.commit()
    finally:
        conn.close()


def get_credentials_for_exchange(exchange: str) -> tuple[Optional[str], Optional[str]]:
    """
    Decrypt and return (api_key, api_secret) for the given exchange.
    Returns (None, None) if not found.
    """
    exchange = (exchange or "").lower().strip()
    if not exchange:
        return None, None

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    try:
        _ensure_credentials_table(conn)
        cur = conn.cursor()
        cur.execute(
            "SELECT api_key_enc, api_secret_enc FROM api_credentials WHERE exchange = ?",
            (exchange,),
        )
        row = cur.fetchone()
        if not row:
            return None, None

        api_key_enc, api_secret_enc = row
        return _decrypt_secret(api_key_enc), _decrypt_secret(api_secret_enc)
    finally:
        conn.close()


def get_credentials_plain_by_id(cred_id: int) -> Optional[Dict]:
    """
    Returns a dict with decrypted creds for internal use (e.g., testing),
    never sent directly to templates.
    """
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_credentials_table(conn)
        cur = conn.cursor()
        cur.execute("SELECT * FROM api_credentials WHERE id = ?", (cred_id,))
        row = cur.fetchone()
        if not row:
            return None

        api_key = _decrypt_secret(row["api_key_enc"])
        api_secret = _decrypt_secret(row["api_secret_enc"])
        return {
            "id": row["id"],
            "exchange": row["exchange"],
            "api_key": api_key,
            "api_secret": api_secret,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    finally:
        conn.close()


def list_credentials_for_ui() -> List[Dict]:
    """
    Returns a list of credentials with MASKED keys/secrets for safe display in UI.
    """
    def mask_tail(s: str, visible: int = 4) -> str:
        s = s or ""
        if len(s) <= visible:
            return "•" * len(s)
        return "•" * (len(s) - visible) + s[-visible:]

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    creds: List[Dict] = []
    try:
        _ensure_credentials_table(conn)
        cur = conn.cursor()
        cur.execute(
            "SELECT id, exchange, api_key_enc, api_secret_enc, created_at, updated_at "
            "FROM api_credentials ORDER BY exchange ASC"
        )
        rows = cur.fetchall()
        for r in rows:
            api_key = _decrypt_secret(r["api_key_enc"])
            api_secret = _decrypt_secret(r["api_secret_enc"])
            creds.append({
                "id": r["id"],
                "exchange": r["exchange"],
                "api_key_masked": mask_tail(api_key),
                "api_secret_masked": mask_tail(api_secret),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            })
    finally:
        conn.close()

    return creds


def delete_credentials_by_id(cred_id: int) -> None:
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    try:
        _ensure_credentials_table(conn)
        conn.execute("DELETE FROM api_credentials WHERE id = ?", (cred_id,))
        conn.commit()
    finally:
        conn.close()


# ============================================================
# 🔑 Backwards-compatible API for exchange classes
# ============================================================

def load_api_keys() -> tuple[Optional[str], Optional[str]]:
    """
    Backwards-compatible function used by KrakenExchange/BitvavoExchange.

    Now reads encrypted API keys from the api_credentials table,
    selecting the currently active exchange from config["exchange"].
    """
    cfg = current_config()
    exchange = cfg.get("exchange", "kraken")
    api_key, api_secret = get_credentials_for_exchange(exchange)
    return api_key, api_secret
