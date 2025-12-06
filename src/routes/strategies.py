from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER
from fastapi.templating import Jinja2Templates
import sqlite3
from typing import Optional

from utils import root_path, _open_db, get_supported_pairs, log_event

templates = Jinja2Templates(directory=str(root_path("src", "templates")))
templates.env.globals["abs"] = abs

router = APIRouter(prefix="/strategies", tags=["strategies"])


def get_db():
    conn = _open_db()
    conn.row_factory = sqlite3.Row
    return conn


# -----------------------------------------------------------
# LIST STRATEGIES (DEFAULT: BITVAVO)
# -----------------------------------------------------------
@router.get("/")
def list_strategies(request: Request, exchange: Optional[str] = None):
    db = get_db()
    rows = db.execute("SELECT * FROM strategies ORDER BY id DESC").fetchall()
    db.close()

    # Default exchange = Bitvavo unless ?exchange=kraken is provided
    current_exchange = (exchange or "bitvavo").lower()

    # Get symbols for the exchange (EUR pairs)
    symbols = get_supported_pairs(current_exchange)

    return templates.TemplateResponse("strategies.html", {
        "request": request,
        "strategies": rows,
        "symbols": symbols,
        "current_exchange": current_exchange
    })


# -----------------------------------------------------------
# AJAX: GET EUR SYMBOLS FOR SELECTED EXCHANGE
# -----------------------------------------------------------
@router.get("/symbols/{exchange}")
def ajax_symbols(exchange: str):
    exchange = exchange.lower()
    return get_supported_pairs(exchange)


# -----------------------------------------------------------
# ADD STRATEGY
# -----------------------------------------------------------
@router.post("/add")
def add_strategy(
    symbol: str = Form(...),
    timeframe: str = Form(...),
    exchange: str = Form(...),
    drop_pct: float = Form(...),
    rise_pct: float = Form(...),
    buy_amount_eur: float = Form(...),
    sell_amount_eur: float = Form(...),
    allocated_eur: float = Form(...),
    import_existing: Optional[str] = Form(None),
    existing_amount: Optional[str] = Form(""),
    existing_acb: Optional[str] = Form(""),
    return_to: str = Form("strategies")
):

    exchange = exchange.lower()

    drop_trigger = -abs(drop_pct)
    rise_trigger = abs(rise_pct)

    buy_amount_eur = max(buy_amount_eur, 5)
    sell_amount_eur = max(sell_amount_eur, 5)

    existing_amount_val = float(existing_amount or 0)
    existing_acb_val = float(existing_acb or 0)

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

    # Optional existing position import
    if import_existing and existing_amount_val > 0 and existing_acb_val > 0:
        from trade_manager import TradeManager
        from decimal import Decimal

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
                f"📥 Imported existing position {existing_amount_val} {symbol} @ {existing_acb_val}"
            )
        except Exception as e:
            log_event(f"⚠️ Import existing position failed: {e}")

    log_event(f"🧩 Added strategy {symbol} {timeframe} on {exchange}")
    
    # -------------------------
    # Decide RETURN LOCATION
    # -------------------------
    if return_to == "index":
        return RedirectResponse("/", status_code=303)
    else:
        return RedirectResponse("/strategies", status_code=303)


# -----------------------------------------------------------
# UPDATE STRATEGY
# -----------------------------------------------------------
@router.post("/update/{id}")
def update_strategy(
    id: int,
    symbol: str = Form(...),
    timeframe: str = Form(...),
    exchange: str = Form(...),
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

    log_event(f"📝 Updated strategy {id}: exchange={exchange}")

    return RedirectResponse("/strategies", status_code=HTTP_303_SEE_OTHER)


# -----------------------------------------------------------
# DELETE STRATEGY
# -----------------------------------------------------------
@router.post("/delete/{id}")
def delete_strategy(id: int):
    db = get_db()
    db.execute("DELETE FROM strategies WHERE id=?", (id,))
    db.commit()
    db.close()

    log_event(f"🗑 Deleted strategy {id}")

    return RedirectResponse("/strategies", status_code=HTTP_303_SEE_OTHER)


# -----------------------------------------------------------
# GET RECENT TRADES OF A STRATEGY (WITH FORMATTED PRICE)
# -----------------------------------------------------------
@router.get("/api/strategy/{symbol}/{timeframe}/{exchange}/trades")
def api_strategy_trades(symbol: str, timeframe: str, exchange: str):
    """
    Returns the last 10 trades for a specific strategy, including formatted prices.
    """

    from formatting import format_price
    from utils import to_decimal
    from exchange_factory import get_exchange

    ex = get_exchange(exchange)
    ticks = ex._market_ticks[symbol]["price_tick"]

    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT t.*
        FROM trades t
        JOIN strategies s ON t.strategy_id = s.id
        WHERE s.symbol = ?
          AND s.timeframe = ?
          AND s.exchange = ?
        ORDER BY t.timestamp DESC
        LIMIT 10
    """, (symbol, timeframe, exchange))

    rows = cur.fetchall()
    conn.close()

    result = []
    for r in rows:
        price = to_decimal(r["price"])
        amount = to_decimal(r["amount"])

        result.append({
            "timestamp": r["timestamp"],
            "symbol": r["symbol"],
            "side": r["side"],

            # raw numeric values
            "price": float(price),
            "amount": float(amount),

            # formatted string using tickSize (NEW!)
            "price_str": format_price(price, ticks),

            "strategy_id": r["strategy_id"],
        })

    return result
