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
from trade_manager import TradeManager
from kraken_exchange import KrakenExchange
from bitvavo_exchange import BitvavoExchange
from exchange_factory import get_exchange

def get_exchange_backend():
    cfg = current_config()
    exchange = cfg.get("exchange", "kraken").lower()
    if exchange == "bitvavo":
        return BitvavoExchange()
    return KrakenExchange()

import os, signal, time, psutil
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime
import sqlite3

app = FastAPI(title="Aurono Dashboard", version="1.3")
from routes import strategies
app.include_router(strategies.router)
from routes import settings
app.include_router(settings.router)

TEMPLATE_DIR = root_path("src","templates")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals["abs"] = abs  # ✅ make abs() available in templates

def log_path():
    cfg = current_config()
    return root_path("data", cfg["log_path"].split("/")[-1])

def get_running_processes():
    trader_running = False
    trader_pid = None
    for p in psutil.process_iter(attrs=["pid", "cmdline"]):
        try:
            c = p.info.get("cmdline") or []
            # Only detect the real trader script
            if any("trader_main.py" in s for s in c):
                trader_running = True
                trader_pid = p.info["pid"]
        except Exception:
            pass
    return trader_running, trader_pid

def get_portfolio_snapshot():
    """Return per-strategy portfolio snapshot, so each strategy shows separately (no merging by symbol)."""
    tm = TradeManager()
    ex = get_exchange_backend()
    portfolio = []
    total_crypto_value = 0.0
    allocated_total = 0.0
    available_cash = 0.0

    try:
        conn = sqlite3.connect(get_db_path())
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # --- Fetch all active strategies ---
        strategies = cur.execute(
            "SELECT id, symbol, timeframe, allocated_eur, exchange FROM strategies WHERE enabled=1 ORDER BY symbol, timeframe"
        ).fetchall()

        for s in strategies:
            sid = s["id"]
            sym = s["symbol"].upper()
            tf = s["timeframe"]
            exchange_name = s["exchange"]
            ex = get_exchange(exchange_name)

            # fetch live price
            price = float(ex.get_ticker(sym))
            # compute only trades linked to this strategy
            balance = 0.0
            acb_f = None
            try:
                trades = cur.execute(
                    "SELECT side, price, amount FROM trades WHERE strategy_id=? ORDER BY id ASC",
                    (sid,)
                ).fetchall()

                from decimal import Decimal
                total_cost = Decimal("0")
                total_qty = Decimal("0")

                for side, price_t, amount_t in trades:
                    from utils import to_decimal
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
            except Exception:
                pass

            value = price * balance
            total_crypto_value += value
            pnl_pct = ((price - acb_f) / acb_f * 100) if acb_f else 0
            alloc = float(s["allocated_eur"] or 0.0)

            portfolio.append({
                "strategy_id": sid,
                "symbol": sym,
                "timeframe": tf,
                "exchange": exchange_name,
                "price": price,
                "balance": balance,
                "value": value,
                "acb": acb_f,
                "pnl_pct": pnl_pct,
                "allocated_eur": alloc,
            })

        # totals
        cur.execute("SELECT SUM(allocated_eur) FROM strategies WHERE enabled=1")
        allocated_total = cur.fetchone()[0] or 0.0
        cur.execute("SELECT SUM(allocated_eur) FROM strategies WHERE enabled=1 AND allocated_eur>0")
        available_cash = cur.fetchone()[0] or 0.0
        conn.close()

    except Exception as e:
        log_event(f"⚠️ Portfolio snapshot error: {e}")

    total_portfolio_value = total_crypto_value + available_cash
    
    # ✅ Sort strategies by value (descending)
    portfolio.sort(key=lambda x: x["value"], reverse=True)

    return {
        "symbols": portfolio,
        "crypto_value": total_crypto_value,
        "allocated_total": allocated_total,
        "available_cash": available_cash,
        "total_value": total_portfolio_value
    }

def get_recent_trades(limit=15, days=7):
    """Return last N trades (default 7 days) including strategy names."""
    trades = []
    try:
        conn = sqlite3.connect(get_db_path())
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
            # Fallback: if strategy is gone, show "-"
            if r["s_symbol"] and r["timeframe"]:
                exch = r["exchange"] or "bitvavo"
                strategy_name = f"{r['s_symbol']} {r['timeframe']} ({exch})"
            else:
                strategy_name = "-"

            trades.append({
                "timestamp": r["timestamp"],
                "symbol": r["symbol"],
                "side": r["side"],
                "price": r["price"],
                "amount": r["amount"],
                "strategy": strategy_name,
            })

    except Exception as e:
        log_event(f"⚠️ Error loading recent trades: {e}")

    return trades

@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    symbol: str = "",
    timeframe: str = "",
    exchange: str = ""
):

    cfg = current_config()
    running, _ = get_running_processes()
    snapshot = get_portfolio_snapshot()
    
    # --- apply filters to portfolio snapshot ---
    filtered = []
    for s in snapshot["symbols"]:
        if symbol and s["symbol"] != symbol:
            continue
        if timeframe and s["timeframe"] != timeframe:
            continue
        if exchange and s["exchange"] != exchange:
            continue
        filtered.append(s)

    # Replace the original list with the filtered one
    snapshot["symbols"] = filtered

    # Populate dropdown values from all strategies (unfiltered)
    symbols_list = sorted({x["symbol"] for x in get_portfolio_snapshot()["symbols"]})
    timeframes_list = sorted({x["timeframe"] for x in get_portfolio_snapshot()["symbols"]})
    exchanges_list = sorted({x["exchange"] for x in get_portfolio_snapshot()["symbols"]})


    import re
    from datetime import datetime
    timestamp_re = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})]")

    def group_log_blocks(lines):
        blocks = []
        current = []
        current_type = None

        def flush():
            nonlocal current, current_type
            if current:
                blocks.append(current)
                current = []
                current_type = None

        for line in lines:
            # --- Strategy management group ---
            if "🧩 Added strategy" in line or "🗑 Deleted strategy" in line:
                flush()
                current = [line]
                current_type = "strategy_mgmt"
                flush()
                continue

            # --- Import sessions (start → complete) ---
            if "📥 Starting import" in line:
                flush()
                current = [line]
                current_type = "import"
                continue
            if "✅ Import complete" in line and current_type == "import":
                current.append(line)
                flush()
                continue

            # --- Trader lifecycle (start/stop/mode) ---
            if any(tag in line for tag in ["started trader", "stopped trader", "Mode switched", "Aurono Trader started"]):
                flush()
                current = [line]
                current_type = "trader"
                flush()
                continue

            # --- Strategy cycle / batch ---
            if "⏱ Trigger" in line:
                flush()
                current = [line]
                current_type = "cycle"
                continue
            if "🚀 Strategy cycle started" in line and current_type == "cycle":
                current.append(line)
                continue
            if "Cycle completed." in line and current_type == "cycle":
                current.append(line)
                flush()
                continue

            # --- API warnings/errors ---
            if any(tag in line for tag in ["⚠️", "❌"]):
                flush()
                blocks.append([line])
                continue

            # --- Default handling: append to current if active, else standalone ---
            if current_type:
                current.append(line)
            else:
                blocks.append([line])

        if current:
            blocks.append(current)

        return blocks

    def parse_dt(line):
        m = timestamp_re.match(line)
        if not m:
            return datetime.min
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return datetime.min
    
    def summarize_block(block):
        text = "\n".join(block).lower()

        # More precise logic: match order → only one per block
        if "💰 sell" in text:
            return "💰 SELL executed"
        if "✅ buy" in text or "🧪 simulated buy" in text or "recorded buy" in text:
            return "✅ BUY executed"
        if "no buy" in text or "no sell" in text or "idle" in text:
            return "⏸ No trade (Idle)"
        if "error" in text or "⚠️" in text:
            return "⚠️ Error / API issue"
        if "strategy cycle started" in text:
            return "🚀 Strategy Cycle"
        if "added strategy" in text:
            return "🧩 Strategy Added"
        if "started trader" in text:
            return "▶️ Trader Started"
        if "stopped trader" in text:
            return "⏹ Trader Stopped"
        if "mode switched" in text:
            return "🔁 Mode Switch"
        return "ℹ️ Other"

    try:
        with open(log_path(), "r") as f:
            lines = [line.strip() for line in f if line.strip()]
            blocks = group_log_blocks(lines)
            blocks.sort(key=lambda b: parse_dt(b[0]), reverse=True)
            latest_blocks = blocks[:30]

            summaries = [
                {"summary": summarize_block(b), "details": "\n".join(b).replace("\\n", "\n")}
                for b in latest_blocks
            ]

    except FileNotFoundError:
        summaries = []
        
    return templates.TemplateResponse("index.html", {
        "request": request,
        "mode": cfg["mode"].capitalize(),
        "trader_running": running,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot": snapshot,
        "recent_trades": get_recent_trades(),
        "symbols_list": symbols_list,
        "timeframes_list": timeframes_list,
        "exchanges_list": exchanges_list
    })

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

@app.get("/toggle_mode")
async def toggle_mode():
    """Toggle between dev and live mode (manual trader restart may be needed)."""
    cfg = current_config()
    new_mode = "dev" if cfg["mode"].lower() == "live" else "live"
    cfg["mode"] = new_mode
    save_config(cfg)
    log_event(f"🔁 Mode switched to {new_mode.upper()} via dashboard")
    
    # Try to detect if running as service
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
        # Try to restart if running manually
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
     
@app.get("/api/snapshot")
async def api_snapshot():
    """Return live price, balance, ACB, and P/L as JSON."""
    return get_portfolio_snapshot()

from fastapi.responses import FileResponse

# ---------- Activity helpers (improved splitter + categorizer) ----------

import re

TS_PATTERN = re.compile(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}]")

def split_physical_line_into_events(line: str):
    """
    Many lines contain multiple timestamps. Example:
    [ts1] ... [ts2] ... [ts3] ...
    This splits them into individual physical log entries.
    """
    parts = TS_PATTERN.split(line)
    timestamps = TS_PATTERN.findall(line)

    events = []
    for idx, ts in enumerate(timestamps):
        msg = parts[idx + 1].strip()
        events.append(f"{ts} {msg}")
    return events


def _categorize_event(text: str):
    """
    Return one of: strategy, trade, error, system.
    """
    if any(tag in text for tag in [
        "🧩 Added strategy", "📝 Updated strategy", "🗑 Deleted strategy"
    ]):
        return "strategy"

    if any(tag in text for tag in [
        "💰 SELL", "✅ BUY",
        "🧪 Simulated BUY", "🧪 Simulated SELL",
        "💾 Recorded BUY", "💾 Recorded SELL"
    ]):
        return "trade"

    if any(tag in text for tag in ["⚠️", "❌"]):
        return "error"

    return "system"


def load_activity_events(limit_per_group: int = 50):
    """
    Full robust parser:
    - Split physical lines by timestamps
    - Extract individual events
    - Categorize
    - Return newest->oldest
    """
    log_p = log_path()
    groups = {"strategy": [], "trade": [], "error": []}

    if not log_p.exists():
        return groups

    with open(log_p, encoding="utf-8") as f:
        raw_lines = [l.rstrip("\n") for l in f if l.strip()]

    events = []

    # Split lines containing multiple timestamps
    for line in raw_lines:
        if TS_PATTERN.search(line):
            events.extend(split_physical_line_into_events(line))

    # Reverse to newest-first
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

    # Apply filter type
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
    """Download full aurono_log.txt for debugging."""
    log_p = log_path()
    if not log_p.exists():
        return {"error": "Log file not found"}
    return FileResponse(path=log_p, filename="aurono_log.txt", media_type="text/plain")


if __name__=="__main__":
    import uvicorn
    cfg = current_config()
    uvicorn.run("dashboard:app", host=cfg.get("dashboard_host","0.0.0.0"), port=cfg.get("dashboard_port",8000), reload=False)

