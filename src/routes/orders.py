from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER

import sqlite3
from decimal import Decimal
from datetime import datetime

from utils import get_db_path, log_event, current_config
from kraken_trader import KrakenTrader

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("/cancel/{order_id}")
def cancel_order(order_id: int):
    """Cancel an open order (Kraken + local) and release reserved cash for BUY orders."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    o = cur.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o:
        conn.close()
        return RedirectResponse(url="/", status_code=HTTP_303_SEE_OTHER)

    cfg = current_config()
    live = cfg.get("live_trading", False)
    txid = o["txid"]
    side = o["side"]
    strategy_id = o["strategy_id"]

    # If live and we have a txid, send cancel to Kraken
    if live and txid:
        kt = KrakenTrader()
        res = kt.cancel_order(txid)
        log_event(f"📨 Sent cancel for order {order_id} (txid={txid}) → {res}")
        # Let the trader do a settlement pass against Kraken's state
        kt.settle_open_orders()
        conn.close()
        return RedirectResponse(url="/", status_code=HTTP_303_SEE_OTHER)

    # Otherwise: purely local cancel (e.g., DEV mode or no txid yet)
    now = datetime.utcnow().isoformat()
    price = Decimal(str(o["price"]))
    volume = Decimal(str(o["volume"]))
    res_amt = price * volume

    if side == "buy" and strategy_id is not None:
        cur.execute(
            """
            UPDATE strategies
            SET reserved_eur = reserved_eur - ?,
                allocated_eur = allocated_eur + ?
            WHERE id=?
            """,
            (float(res_amt), float(res_amt), strategy_id)
        )

    cur.execute(
        "UPDATE orders SET status='canceled', updated_at=? WHERE id=?",
        (now, order_id)
    )
    conn.commit()
    conn.close()

    log_event(f"🛑 Locally canceled order {order_id} (side={side})")
    return RedirectResponse(url="/", status_code=HTTP_303_SEE_OTHER)
