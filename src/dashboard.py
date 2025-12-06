import sys
from pathlib import Path

# --- ensure both src and root folders are visible ---
CURRENT = Path(__file__).resolve().parent        # /aurono-poc/src
ROOT = CURRENT.parent                            # /aurono-poc
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(CURRENT) not in sys.path:
    sys.path.insert(0, str(CURRENT))

print("DEBUG sys.path:", sys.path[:3])  # optional: shows top 3 entries

import importlib
settings = importlib.import_module("routes.settings")

from utils import current_config, save_config, log_event, root_path, to_decimal, load_api_keys, get_db_path
from utils import _open_db
from trade_manager import TradeManager
from kraken_exchange import KrakenExchange
from bitvavo_exchange import BitvavoExchange
from exchange_factory import get_exchange
from formatting import format_price

import os, signal, time, psutil
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from datetime import datetime
import sqlite3
from decimal import Decimal

# ---------------------------
# TICK LOOKUP (UNIFIED)
# ---------------------------

def get_price_tick(ex, symbol: str) -> Decimal:
    """
    Returns the correct price tick for Bitvavo or Kraken.
    Bitvavo uses market keys ("FLOKI-EUR"), Kraken uses altname ("FLOKIEUR").
    """
    symbol = symbol.upper()

    # ----- Bitvavo -----
    if hasattr(ex, "_market_ticks"):
        market = ex._market(symbol)  # FLOKI-EUR

        if market not in ex._market_ticks:
            try:
                ex._load_market_ticks(market)
            except Exception:
                return Decimal("0.01")

        try:
            return ex._market_ticks[market]["price"]
        except Exception:
            return Decimal("0.01")

    # ----- Kraken -----
    if hasattr(ex, "_tick_cache"):
        if symbol not in ex._tick_cache:
            try:
                ex._load_tick_size(symbol)
            except Exception:
                return Decimal("0.01")

        try:
            return ex._tick_cache[symbol]["price"]
        except Exception:
            return Decimal("0.01")

    return Decimal("0.01")

# ---------------------------
# EXCHANGE SELECTION
# ---------------------------

def get_exchange_backend():
    cfg = current_config()
    exchange = cfg.get("exchange", "kraken").lower()
    if exchange == "bitvavo":
        return BitvavoExchange()
    return KrakenExchange()

# ---------------------------
# FASTAPI APP + TEMPLATES
# ---------------------------

app = FastAPI(title="Aurono Dashboard", version="1.3")

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
app.state.templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

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

templates = app.state.templates
templates.env.globals["abs"] = abs

# ---------------------------
# UTILS
# ---------------------------

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

# ---------------------------
# PORTFOLIO SNAPSHOT
# ---------------------------

def get_portfolio_snapshot():
    tm = TradeManager()
    portfolio = []
    total_crypto_value = 0.0
    allocated_total = 0.0
    available_cash = 0.0

    try:
        conn = _open_db()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        strategies = cur.execute(
            "SELECT id, symbol, timeframe, allocated_eur, exchange "
            "FROM strategies WHERE enabled=1 ORDER BY symbol, timeframe"
        ).fetchall()

        for s in strategies:
            sid = s["id"]
            sym = s["symbol"].upper()
            tf = s["timeframe"]
            ex_name = s["exchange"]

            ex = get_exchange(ex_name)

            raw_price = ex.get_ticker(sym)

            # unified tick lookup
            tick = get_price_tick(ex, sym)

            price = float(raw_price)
            price_str = format_price(raw_price, tick)

            # --- Calculate ACB ---
            balance = 0.0
            acb_f = None

            trades = cur.execute(
                "SELECT side, price, amount FROM trades WHERE strategy_id=? ORDER BY id ASC",
                (sid,)
            ).fetchall()

            from decimal import Decimal
            total_cost = Decimal("0")
            total_qty = Decimal("0")

            for side, p, amt in trades:
                p = to_decimal(p)
                amt = to_decimal(amt)

                if side == "buy":
                    total_cost += p * amt
                    total_qty += amt

                elif side == "sell" and total_qty > 0:
                    proportion = amt / total_qty
                    if proportion > 1:
                        proportion = 1
                    total_cost -= total_cost * proportion
                    total_qty -= amt

                    if total_qty < 0:
                        total_qty = Decimal("0")
                        total_cost = Decimal("0")

            balance = float(total_qty)
            acb_f = float(total_cost / total_qty) if total_qty > 0 else None
            value = price * balance
            total_crypto_value += value

            pnl_pct = ((price - acb_f) / acb_f * 100) if acb_f else 0

            alloc = float(s["allocated_eur"] or 0.0)
            acb_str = format_price(acb_f, tick) if acb_f else "-"

            portfolio.append({
                "strategy_id": sid,
                "symbol": sym,
                "timeframe": tf,
                "exchange": ex_name,

                "price": price,
                "price_str": price_str,

                "balance": balance,
                "value": value,

                "acb": acb_f,
                "acb_str": acb_str,

                "pnl_pct": pnl_pct,
                "allocated_eur": alloc,
            })

        cur.execute("SELECT SUM(allocated_eur) FROM strategies WHERE enabled=1")
        allocated_total = cur.fetchone()[0] or 0.0

        cur.execute("SELECT SUM(allocated_eur) FROM strategies WHERE enabled=1 AND allocated_eur > 0")
        available_cash = cur.fetchone()[0] or 0.0

        conn.close()

    except Exception as e:
        log_event(f"⚠️ Portfolio snapshot error: {e}")

    total_portfolio_value = total_crypto_value + available_cash

    portfolio.sort(key=lambda x: x["value"], reverse=True)

    return {
        "symbols": portfolio,
        "crypto_value": total_crypto_value,
        "allocated_total": allocated_total,
        "available_cash": available_cash,
        "total_value": total_portfolio_value
    }

# ---------------------------
# RECENT TRADES
# ---------------------------

def get_recent_trades(limit=15, days=7):
    trades = []
    try:
        conn = _open_db()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute(
            """
            SELECT t.timestamp, t.symbol, t.side, t.price, t.amount,
                   s.timeframe, s.symbol AS s_symbol, s.exchange
            FROM trades t
            LEFT JOIN strategies s ON t.strategy_id = s.id
            WHERE julianday('now') - julianday(t.timestamp) <= ?
            ORDER BY t.timestamp DESC
            LIMIT ?
            """,
            (days, limit)
        )

        rows = cur.fetchall()
        conn.close()

        for r in rows:
            exch = r["exchange"] or "bitvavo"
            sym = r["symbol"].upper()
            ex = get_exchange(exch)

            tick = get_price_tick(ex, sym)
            price_str = format_price(to_decimal(r["price"]), tick)

            if r["s_symbol"] and r["timeframe"]:
                strategy_name = f"{r['s_symbol']} {r['timeframe']} ({exch})"
            else:
                strategy_name = "-"

            trades.append({
                "timestamp": r["timestamp"],
                "symbol": sym,
                "side": r["side"],
                "price": r["price"],
                "price_str": price_str,
                "amount": r["amount"],
                "strategy": strategy_name,
            })

    except Exception as e:
        log_event(f"⚠️ Error loading recent trades: {e}")

    return trades

# ---------------------------
# DASHBOARD ROUTE
# ---------------------------

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

# ---------------------------
# START/STOP TRADER
# ---------------------------

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
        log_event("✅ started trader via dashboard (trader_main.py)")
        time.sleep(1)
    except Exception as e:
        log_event(f"❌ start trader failed: {e}")
    return RedirectResponse(url="/", status_code=303)

@app.get("/stop_trader")
async def stop_trader():
    running, pid = get_running_processes()
    if not running or not pid:
        return RedirectResponse(url="/", status_code=303)

    try:
        os.kill(pid, signal.SIGTERM)
        log_event(f"🛑 stopped trader {pid}")
        time.sleep(1)
    except Exception as e:
        log_event(f"❌ stop trader failed: {e}")

    return RedirectResponse(url="/", status_code=303)

# ---------------------------
# TOGGLE MODE
# ---------------------------

@app.get("/toggle_mode")
async def toggle_mode():
    cfg = current_config()
    new_mode = "dev" if cfg["mode"].lower() == "live" else "live"
    cfg["mode"] = new_mode
    save_config(cfg)
    log_event(f"🔁 Mode switched to {new_mode.upper()} via dashboard")

    import subprocess
    is_service = False
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "aurono-trader.service"],
            capture_output=True,
            text=True,
            timeout=5
        )
        is_service = result.returncode == 0
    except Exception:
        pass

    if is_service:
        log_event("ℹ️ Running as service - please manually restart: sudo systemctl restart aurono-trader.service")
    else:
        running, pid = get_running_processes()
        if running and pid:
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(2)
            except Exception:
                pass

        try:
            py = os.popen("which python3").read().strip() or "python3"
            trader_script = root_path("src", "trader_main.py")
            os.spawnlp(os.P_NOWAIT, py, py, str(trader_script))
            log_event(f"♻️ Restarted trader in {new_mode.upper()} mode via trader_main.py")
        except Exception as e:
            log_event(f"⚠️ Could not auto-restart trader: {e}")

    return RedirectResponse(url="/", status_code=303)

# ---------------------------
# ACTIVITY + LOG DOWNLOAD
# ---------------------------

from fastapi.responses import FileResponse
import re

TS_PATTERN = re.compile(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}]")

def split_physical_line_into_events(line: str):
    parts = TS_PATTERN.split(line)
    timestamps = TS_PATTERN.findall(line)

    events = []
    for idx, ts in enumerate(timestamps):
        msg = parts[idx + 1].strip()
        events.append(f"{ts} {msg}")
    return events

def _categorize_event(text: str):
    if any(tag in text for tag in ["🧩 Added strategy", "📝 Updated strategy", "🗑 Deleted strategy"]):
        return "strategy"
    if any(tag in text for tag in ["💰 SELL", "✅ BUY", "🧪 Simulated BUY", "🧪 Simulated SELL", "💾 Recorded BUY", "💾 Recorded SELL"]):
        return "trade"
    if any(tag in text for tag in ["⚠️", "❌"]):
        return "error"
    return "system"

def load_activity_events(limit_per_group: int = 50):
    log_p = log_path()
    groups = {"strategy": [], "trade": [], "error": []}

    if not log_p.exists():
        return groups

    with open(log_p, encoding="utf-8") as f:
        raw_lines = [l.rstrip("\n") for l in f if l.strip()]

    events = []
    for line in raw_lines:
        if TS_PATTERN.search(line):
            events.extend(split_physical_line_into_events(line))

    events = list(reversed(events))

    final = {"strategy": [], "trade": [], "error": []}

    for ev in events:
        cat = _categorize_event(ev)
        if cat in final and len(final[cat]) < limit_per_group:
            ts = ev.split("]")[0].strip("[")
            final[cat].append({
                "timestamp": ts,
                "text": ev,
                "category": cat,
            })

        if all(len(final[k]) >= limit_per_group for k in final):
            break

    return final

@app.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request,
                        show: str = "all",
                        limit: int = 50):

    try:
        limit = max(10, min(int(limit), 200))
    except ValueError:
        limit = 50

    groups_raw = load_activity_events(limit)

    groups = {}
    for key, events in groups_raw.items():
        groups[key] = events if show in ("all", key) else []

    return templates.TemplateResponse("activity.html", {
        "request": request,
        "groups": groups,
        "show": show,
        "limit": limit,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

@app.get("/activity/download")
async def download_log():
    log_p = log_path()
    if not log_p.exists():
        return {"error": "Log file not found"}
    return FileResponse(path=log_p, filename="aurono_log.txt", media_type="text/plain")

# ---------------------------
# MAIN
# ---------------------------

if __name__ == "__main__":
    import uvicorn
    cfg = current_config()
    uvicorn.run(
        "dashboard:app",
        host=cfg.get("dashboard_host", "0.0.0.0"),
        port=cfg.get("dashboard_port", 8000),
        reload=False
    )

