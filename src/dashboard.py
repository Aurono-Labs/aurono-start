# ======================================================================
#  DASHBOARD.PY — FINAL PATCHED VERSION (Supports Option A tick lookup)
# ======================================================================

import sys
from pathlib import Path

# --- ensure both src and root folders are visible ---
CURRENT = Path(__file__).resolve().parent
ROOT = CURRENT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(CURRENT) not in sys.path:
    sys.path.insert(0, str(CURRENT))

import importlib
settings = importlib.import_module("routes.settings")

from utils import (
    current_config, save_config, log_event, root_path,
    to_decimal, get_db_path, load_api_keys
)
from utils import _open_db

from trade_manager import TradeManager
from kraken_exchange import KrakenExchange
from bitvavo_exchange import BitvavoExchange
from exchange_factory import get_exchange

# NEW: formatting helper
from formatting import format_price

import os, signal, time, psutil, sqlite3, re
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles


# ======================================================================
#  UTIL — dynamic tick accessor (Option A)
# ======================================================================
def get_price_tick(ex, symbol: str):
    """
    Exchange-agnostic tick lookup.
    Bitvavo   → ex._market_ticks[market]["price"]
    Kraken    → ex._tick_cache[symbol]["price"]
    """
    symbol = symbol.upper()

    # BITVAVO
    if hasattr(ex, "_market_ticks"):
        market = ex._market(symbol)  # e.g. "BTC-EUR"
        if market not in ex._market_ticks:
            ex._load_market_ticks(market)
        return ex._market_ticks[market]["price"]

    # KRAKEN
    if hasattr(ex, "_tick_cache"):
        if symbol not in ex._tick_cache:
            ex._load_tick_size(symbol)
        return ex._tick_cache[symbol]["price"]

    # Fallback
    return 0.01



# ======================================================================
#  FASTAPI APP
# ======================================================================

app = FastAPI(title="Aurono Dashboard", version="1.3")

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
app.state.templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates = app.state.templates
templates.env.globals["abs"] = abs


# Static HTML reports
html_reports_dir = Path(root_path("data", "reports", "html"))
html_reports_dir.mkdir(parents=True, exist_ok=True)

app.mount(
    "/reports/html",
    StaticFiles(directory=str(html_reports_dir)),
    name="reports_html"
)


# Routers
from routes import strategies
app.include_router(strategies.router)

from routes import settings
app.include_router(settings.router)

from routes.reports import router as reports_router
app.include_router(reports_router)


# ======================================================================
#  Process + Log Helpers
# ======================================================================

def log_path():
    cfg = current_config()
    return root_path("data", cfg["log_path"].split("/")[-1])


def get_running_processes():
    trader_running = False
    trader_pid = None
    for p in psutil.process_iter(attrs=["pid", "cmdline"]):
        try:
            c = p.info.get("cmdline") or []
            if any("trader_main.py" in s for s in c):
                trader_running = True
                trader_pid = p.info["pid"]
        except Exception:
            pass
    return trader_running, trader_pid


# ======================================================================
#  PORTFOLIO SNAPSHOT
# ======================================================================

def get_portfolio_snapshot():
    tm = TradeManager()
    portfolio = []
    total_crypto_value = 0.0

    try:
        conn = _open_db()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        strategies = cur.execute("""
            SELECT id, symbol, timeframe, allocated_eur, exchange
            FROM strategies
            WHERE enabled=1
            ORDER BY symbol, timeframe
        """).fetchall()

        for s in strategies:
            sid = s["id"]
            sym = s["symbol"].upper()
            tf = s["timeframe"]
            exch = s["exchange"]

            ex = get_exchange(exch)

            # ---- Price fetch ----
            raw_price = ex.get_ticker(sym)
            tick = get_price_tick(ex, sym)
            price = float(raw_price)
            price_str = format_price(raw_price, tick)

            # ---- ACB + balance ----
            balance = 0.0
            acb_f = None
            from decimal import Decimal

            total_cost = Decimal("0")
            total_qty = Decimal("0")

            trades = cur.execute(
                "SELECT side, price, amount FROM trades WHERE strategy_id=? ORDER BY id ASC",
                (sid,)
            ).fetchall()

            for side, price_t, amount_t in trades:
                price_t = to_decimal(price_t)
                amount_t = to_decimal(amount_t)

                if side == "buy":
                    total_cost += price_t * amount_t
                    total_qty += amount_t

                elif side == "sell" and total_qty > 0:
                    proportion = amount_t / total_qty
                    if proportion > 1:
                        proportion = 1
                    total_cost -= total_cost * proportion
                    total_qty -= amount_t
                    if total_qty < 0:
                        total_qty = Decimal("0")
                        total_cost = Decimal("0")

            balance = float(total_qty)
            acb_f = float(total_cost / total_qty) if total_qty > 0 else None
            acb_str = format_price(acb_f, tick) if acb_f else "-"

            value = price * balance
            total_crypto_value += value

            pnl_pct = ((price - acb_f) / acb_f * 100) if acb_f else 0
            alloc = float(s["allocated_eur"] or 0.0)

            portfolio.append({
                "strategy_id": sid,
                "symbol": sym,
                "timeframe": tf,
                "exchange": exch,

                "price": price,
                "price_str": price_str,

                "balance": balance,
                "value": value,

                "acb": acb_f,
                "acb_str": acb_str,

                "pnl_pct": pnl_pct,
                "allocated_eur": alloc,
            })

        conn.close()

    except Exception as e:
        log_event(f"⚠️ Portfolio snapshot error: {e}")

    # Sort by value desc
    portfolio.sort(key=lambda x: x["value"], reverse=True)

    # Compute totals
    available_cash = sum(s["allocated_eur"] for s in portfolio if s["allocated_eur"] > 0)
    total_value = total_crypto_value + available_cash

    return {
        "symbols": portfolio,
        "crypto_value": total_crypto_value,
        "available_cash": available_cash,
        "total_value": total_value,
    }


# ======================================================================
#  RECENT TRADES
# ======================================================================

def get_recent_trades(limit=15, days=7):
    trades = []
    try:
        conn = _open_db()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        rows = cur.execute("""
            SELECT t.timestamp, t.symbol, t.side, t.price, t.amount,
                   s.timeframe, s.symbol AS s_symbol, s.exchange
            FROM trades t
            LEFT JOIN strategies s ON t.strategy_id = s.id
            WHERE julianday('now') - julianday(t.timestamp) <= ?
            ORDER BY t.timestamp DESC
            LIMIT ?
        """, (days, limit)).fetchall()

        conn.close()

        for r in rows:
            sym = r["symbol"].upper()
            exch = r["exchange"] or "bitvavo"
            label = f"{r['s_symbol']} {r['timeframe']} ({exch})" if r["s_symbol"] else "-"

            ex = get_exchange(exch)
            tick = get_price_tick(ex, sym)
            price_str = format_price(to_decimal(r["price"]), tick)

            trades.append({
                "timestamp": r["timestamp"],
                "symbol": sym,
                "side": r["side"],
                "price_str": price_str,
                "amount": r["amount"],
                "strategy": label,
            })

    except Exception as e:
        log_event(f"⚠️ Error loading recent trades: {e}")

    return trades


# ======================================================================
#  ROOT DASHBOARD PAGE
# ======================================================================

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    cfg = current_config()
    running, _ = get_running_processes()

    snapshot = get_portfolio_snapshot()
    recent = get_recent_trades()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "mode": cfg["mode"].capitalize(),
        "trader_running": running,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot": snapshot,
        "recent_trades": recent,
    })


# ======================================================================
#  TRADER CONTROL
# ======================================================================

@app.get("/start_trader")
async def start_trader():
    running, pid = get_running_processes()
    if running:
        log_event(f"⚠️ Trader already running (PID={pid}) — start ignored.")
        return RedirectResponse(url="/", status_code=303)

    py = os.popen("which python3").read().strip() or "python3"
    try:
        trader_script = root_path("src", "trader_main.py")
        os.spawnlp(os.P_NOWAIT, py, py, str(trader_script))
        log_event("✅ started trader via dashboard")
        time.sleep(1)
    except Exception as e:
        log_event(f"❌ start trader failed: {e}")

    return RedirectResponse(url="/", status_code=303)


@app.get("/stop_trader")
async def stop_trader():
    running, pid = get_running_processes()
    if running and pid:
        try:
            os.kill(pid, signal.SIGTERM)
            log_event(f"🛑 stopped trader {pid}")
            time.sleep(1)
        except Exception as e:
            log_event(f"❌ stop trader failed: {e}")

    return RedirectResponse(url="/", status_code=303)


# ======================================================================
#  DOWNLOAD FULL LOG
# ======================================================================

@app.get("/activity/download")
async def download_log():
    log_p = log_path()
    if not log_p.exists():
        return {"error": "Log file not found"}
    return FileResponse(path=log_p, filename="aurono_log.txt", media_type="text/plain")


# ======================================================================
#  RUN DIRECTLY
# ======================================================================

if __name__ == "__main__":
    import uvicorn
    cfg = current_config()
    uvicorn.run(
        "dashboard:app",
        host=cfg.get("dashboard_host", "0.0.0.0"),
        port=cfg.get("dashboard_port", 8000),
        reload=False,
    )

