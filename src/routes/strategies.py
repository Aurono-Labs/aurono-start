from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER
from fastapi.templating import Jinja2Templates
import sqlite3
from pathlib import Path
from typing import Optional

from utils import get_db_path, log_event, root_path

# DB & templates
DB_PATH = root_path("data", "trades.db")
templates = Jinja2Templates(directory=str(root_path("src", "templates")))
templates.env.globals["abs"] = abs

router = APIRouter(prefix="/strategies", tags=["strategies"])


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# -----------------------------------------------------------
# LIST STRATEGIES
# -----------------------------------------------------------
@router.get("/")
def list_strategies(request: Request):
    db = get_db()
    rows = db.execute("SELECT * FROM strategies ORDER BY id DESC").fetchall()
    db.close()
    return templates.TemplateResponse("strategies.html", {
        "request": request,
        "strategies": rows
    })


# -----------------------------------------------------------
# ADD STRATEGY
# -----------------------------------------------------------
@router.post("/add")
def add_strategy(
    symbol: str = Form(...),
    timeframe: str = Form(...),
    exchange: str = Form("bitvavo"),
    drop_pct: float = Form(...),
    rise_pct: float = Form(...),
    buy_amount_eur: float = Form(...),
    sell_amount_eur: float = Form(...),
    allocated_eur: float = Form(...),
    import_existing: Optional[str] = Form(None),
    existing_amount: Optional[str] = Form(""),
    existing_acb: Optional[str] = Form("")
):

    # -----------------------------
    # Normalize numeric values
    # -----------------------------
    exchange = (exchange or "bitvavo").lower()

    drop_trigger = -abs(min(max(drop_pct, 0.1), 100.0))
    rise_trigger = abs(min(max(rise_pct, 0.1), 100.0))

    buy_amount_eur = max(5.0, buy_amount_eur)
    sell_amount_eur = max(5.0, sell_amount_eur)
    allocated_eur = max(0.0, allocated_eur)

    # Parse optional existing values
    existing_amount_val = 0.0
    existing_acb_val = 0.0

    try:
        if existing_amount and existing_amount.strip():
            existing_amount_val = float(existing_amount)
    except ValueError:
        existing_amount_val = 0.0

    try:
        if existing_acb and existing_acb.strip():
            existing_acb_val = float(existing_acb)
    except ValueError:
        existing_acb_val = 0.0

    # -----------------------------
    # Insert strategy
    # -----------------------------
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO strategies 
        (name, symbol, timeframe, drop_trigger, rise_trigger,
         buy_amount_eur, sell_amount_eur, allocated_eur, exchange)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        f"{symbol}_{timeframe}",
        symbol,
        timeframe,
        drop_trigger,
        rise_trigger,
        buy_amount_eur,
        sell_amount_eur,
        allocated_eur,
        exchange
    ))
    strategy_id = cur.lastrowid
    conn.commit()
    conn.close()

    # -----------------------------
    # Import existing position?
    # -----------------------------
    if import_existing and existing_amount_val > 0 and existing_acb_val > 0:
        from decimal import Decimal
        from trade_manager import TradeManager

        try:
            tm = TradeManager()
            tm.record_trade(
                symbol=symbol,
                side="buy",
                price=Decimal(str(existing_acb_val)),
                amount=Decimal(str(existing_amount_val)),
                strategy_id=strategy_id
            )
            log_event(
                f"📥 Imported existing position: {existing_amount_val} {symbol} @ €{existing_acb_val} "
                f"(synthetic trade, strategy {strategy_id})"
            )
        except Exception as e:
            log_event(f"⚠️ Import existing position failed: {e}")

    log_event(
        f"🧩 Added strategy {symbol} {timeframe}: "
        f"drop {drop_trigger}%, rise {rise_trigger}%, "
        f"buy €{buy_amount_eur}, sell €{sell_amount_eur}, "
        f"allocated €{allocated_eur}, exchange={exchange}"
    )

    return RedirectResponse(url="/strategies", status_code=303)


# -----------------------------------------------------------
# UPDATE STRATEGY
# -----------------------------------------------------------
@router.post("/update/{id}")
def update_strategy(
    id: int,
    symbol: str = Form(...),
    timeframe: str = Form(...),
    exchange: str = Form("bitvavo"),
    drop_trigger: float = Form(...),
    rise_trigger: float = Form(...),
    buy_amount_eur: float = Form(...),
    sell_amount_eur: float = Form(...),
    allocated_eur: float = Form(...),
    enabled: str = Form("0")
):
    exchange = exchange.lower()
    is_enabled = 1 if enabled == "1" else 0

    db = get_db()
    db.execute("""
        UPDATE strategies
        SET symbol=?, timeframe=?, exchange=?, 
            drop_trigger=?, rise_trigger=?, 
            buy_amount_eur=?, sell_amount_eur=?, 
            allocated_eur=?, enabled=?
        WHERE id=?
    """, (
        symbol,
        timeframe,
        exchange,
        -abs(drop_trigger),
        abs(rise_trigger),
        buy_amount_eur,
        sell_amount_eur,
        allocated_eur,
        is_enabled,
        id
    ))
    db.commit()
    db.close()

    log_event(
        f"📝 Updated strategy {symbol} {timeframe}: "
        f"enabled={is_enabled}, drop {drop_trigger}%, rise {rise_trigger}%, "
        f"buy €{buy_amount_eur}, sell €{sell_amount_eur}, allocated €{allocated_eur}"
    )

    return RedirectResponse(url="/strategies", status_code=HTTP_303_SEE_OTHER)


# -----------------------------------------------------------
# DELETE STRATEGY
# -----------------------------------------------------------
@router.post("/delete/{id}")
def delete_strategy(id: int):
    db = get_db()
    cur = db.cursor()

    cur.execute("SELECT symbol, timeframe FROM strategies WHERE id=?", (id,))
    row = cur.fetchone()

    cur.execute("DELETE FROM strategies WHERE id=?", (id,))
    db.commit()
    db.close()

    if row:
        log_event(f"🗑 Deleted strategy {row['symbol']} {row['timeframe']}")
    else:
        log_event(f"🗑 Deleted unknown strategy ID {id}")

    return RedirectResponse(url="/strategies", status_code=HTTP_303_SEE_OTHER)

