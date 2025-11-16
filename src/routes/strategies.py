from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER
from fastapi.templating import Jinja2Templates
import sqlite3
from pathlib import Path
from typing import Optional

# Lightweight DB helper
from utils import get_db_path, log_event, root_path

DB_PATH = root_path("data", "trades.db")  # ✅ always absolute
templates = Jinja2Templates(directory=str(root_path("src", "templates")))  # ✅ absolute template path
templates.env.globals["abs"] = abs  # ✅ allow abs() in templates
router = APIRouter(prefix="/strategies", tags=["strategies"])

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

@router.get("/")
def list_strategies(request: Request):
    db = get_db()
    rows = db.execute("SELECT * FROM strategies ORDER BY id DESC").fetchall()
    db.close()
    return templates.TemplateResponse("strategies.html", {"request": request, "strategies": rows})

@router.post("/add")
def add_strategy(
    symbol: str = Form(...),
    timeframe: str = Form(...),
    exchange: str = Form("bitvavo"),
    drop_pct: float = Form(...),
    rise_pct: float = Form(...),
    invest_eur: float = Form(...),
    allocated_eur: float = Form(...),
    import_existing: Optional[str] = Form(None),
    existing_amount: Optional[str] = Form(""),      # ✅ accept as string
    existing_acb: Optional[str] = Form("")          # ✅ accept as string
):

    # --- Validate & normalize numeric input ---
    exchange = (exchange or "bitvavo").lower()
    drop_trigger = -abs(min(max(drop_pct, 0.1), 100.0))   # between 0.1–100
    rise_trigger = abs(min(max(rise_pct, 0.1), 100.0))    # between 0.1–100
    trade_amount_eur = max(5.0, invest_eur)               # at least 5 euros as trade amount
    allocated_eur = max(0.0, allocated_eur)              # needs to be positive. 0 is possible if user already has a position and wants to sell it.

    # --- Safe parse optional fields (handle empty strings) ---
    existing_amount_val = 0.0
    existing_acb_val = 0.0
    
    if existing_amount and existing_amount.strip():
        try:
            existing_amount_val = float(existing_amount)
        except ValueError:
            existing_amount_val = 0.0
    
    if existing_acb and existing_acb.strip():
        try:
            existing_acb_val = float(existing_acb)
        except ValueError:
            existing_acb_val = 0.0

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO strategies (name, symbol, timeframe, drop_trigger, rise_trigger, trade_amount_eur, allocated_eur, exchange)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        f"{symbol}_{timeframe}",
        symbol,
        timeframe,
        drop_trigger,
        rise_trigger,
        trade_amount_eur,
        allocated_eur,
        exchange
    ))
    strategy_id = cur.lastrowid
    conn.commit()
    conn.close()

    # 🧩 Import existing position if checked AND values provided
    if import_existing and existing_amount_val > 0 and existing_acb_val > 0:
        try:
            from decimal import Decimal
            from trade_manager import TradeManager
            tm = TradeManager()
            tm.record_trade(
                symbol=symbol,
                side="buy",
                price=Decimal(str(existing_acb_val)),
                amount=Decimal(str(existing_amount_val)),
                strategy_id=strategy_id
            )
            log_event(f"📥 Imported existing position {existing_amount_val} {symbol} @ €{existing_acb_val} (synthetic trade)")
        except Exception as e:
            log_event(f"⚠️ Import existing position failed: {e}")

    log_event(
        f"🧩 Added strategy {symbol} {timeframe}: drop {drop_trigger}%, rise {rise_trigger}%, "
        f"amount €{trade_amount_eur}, allocated €{allocated_eur}"
    )
    return RedirectResponse(url="/strategies", status_code=303)

@router.post("/update/{id}")
def update_strategy(id: int,
                    symbol: str = Form(...),
                    timeframe: str = Form(...),
                    exchange: str = Form("bitvavo"),
                    drop_trigger: float = Form(...),
                    rise_trigger: float = Form(...),
                    trade_amount_eur: float = Form(...),
                    allocated_eur: float = Form(...),
                    enabled: str = Form("0")):
                    
    exchange = exchange.lower()
    db = get_db()
    is_enabled = 1 if enabled == "1" else 0
    db.execute("""
        UPDATE strategies
        SET symbol=?, timeframe=?, exchange=?, drop_trigger=?, rise_trigger=?, trade_amount_eur=?, allocated_eur=?, enabled=?
        WHERE id=?
    """, (symbol, timeframe, exchange, -abs(drop_trigger), abs(rise_trigger), trade_amount_eur, allocated_eur, is_enabled, id))
    db.commit()
    db.close()

    log_event(f"📝 Updated strategy {symbol} {timeframe}: enabled={is_enabled}, drop -{abs(drop_trigger)}%, rise {abs(rise_trigger)}%, amount €{trade_amount_eur}, EUR allocated €{allocated_eur}")
    return RedirectResponse(url="/strategies", status_code=HTTP_303_SEE_OTHER)

@router.post("/delete/{id}")
def delete_strategy(id: int):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT symbol, timeframe FROM strategies WHERE id=?", (id,))
    row = cur.fetchone()
    cur.execute("DELETE FROM strategies WHERE id=?", (id,))
    db.commit()
    db.close()

    # 🗑 Log deletion
    if row:
        log_event(f"🗑 Deleted strategy {row['symbol']} {row['timeframe']}")
    else:
        log_event(f"🗑 Deleted unknown strategy ID {id}")

    return RedirectResponse(url="/strategies", status_code=HTTP_303_SEE_OTHER)
