# src/report_builder.py

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any

from trade_manager import TradeManager
from utils import current_config, get_db_path
from exchange_factory import get_exchange
from strategy_loader import load_strategies  # if you have this helper
from portfolio import compute_portfolio_snapshot  # if you have this helper
from log_manager import get_recent_alerts  # optional helper
from report_validator import validate_weekly_report
from report_validator import validate_daily_report

from report_storage import (
    save_daily_report_json,
    save_weekly_report_json,
    save_html_report,
    cleanup_old_reports
)



def generate_daily_report() -> Dict[str, Any]:
    """
    Generates the structured Daily Report according to aurono.daily.report.schema.v1.
    Returns a Python dict that can be JSON-serialized or rendered in UI/email.
    """

    now = datetime.now(timezone.utc)
    tm = TradeManager(get_db_path())
    cfg = current_config()
    strategies = load_strategies()

    # ------------------------------------------------------------
    # 1. SYSTEM STATUS
    # ------------------------------------------------------------
    exchanges_status = []
    for ex_name in cfg["exchanges"].keys():
        ex = get_exchange(ex_name)

        exchanges_status.append({
            "name": ex_name,
            "connected": ex.health_ok(),
            "last_ohlc_update_ok": ex.last_ohlc_ok(),
            "last_ticker_ok": ex.last_ticker_ok(),
            "errors": ex.recent_errors()
        })

    system_block = {
        "trader_running": True,            # replace with actual system check
        "dashboard_running": True,         # replace with actual system check
        "exchanges": exchanges_status
    }

    # ------------------------------------------------------------
    # 2. FILLED ORDERS (24h)
    # ------------------------------------------------------------
    since = now - timedelta(hours=24)
    filled_24h = tm.get_filled_orders_since(since)

    filled_orders_block = []
    for o in filled_24h:
        filled_orders_block.append({
            "symbol": o.symbol,
            "exchange": o.exchange,
            "timeframe": o.timeframe,
            "side": o.side,
            "amount": float(o.amount),
            "price": float(o.price),
            "timestamp": o.timestamp.isoformat(),
            "strategy_label": o.strategy_label,
            "pnl": float(o.pnl or 0)
        })

    # ------------------------------------------------------------
    # 3. CAPITAL (reserved + available)
    # ------------------------------------------------------------
    reserved_list = []
    for s in strategies:
        reserved_list.append({
            "strategy_id": s["id"],
            "symbol": s["symbol"],
            "exchange": s["exchange"],
            "amount_eur": float(tm.get_reserved_eur_for_strategy(s["id"]))
        })

    available_dict = {}
    for ex_name in cfg["exchanges"].keys():
        ex = get_exchange(ex_name)
        eur_balance = ex.get_balance_eur()
        available_dict[ex_name] = float(eur_balance)

    capital_block = {
        "reserved": reserved_list,
        "available": available_dict
    }

    # ------------------------------------------------------------
    # 4. PORTFOLIO SNAPSHOT
    # ------------------------------------------------------------
    portfolio = compute_portfolio_snapshot()

    portfolio_block = {
        "total_value_eur": float(portfolio["total"]),
        "crypto_value_eur": float(portfolio["crypto"]),
        "cash_value_eur": float(portfolio["cash"]),
        "unrealized_pnl_eur": float(portfolio["unrealized_pnl"]),
        "change_since_yesterday_pct": float(portfolio["change_pct"])
    }

    # ------------------------------------------------------------
    # 5. ALERTS
    # ------------------------------------------------------------
    alerts_block = get_recent_alerts(hours=24)
    
    report = {
        "date": now.isoformat(),
        "system": system_block,
        "filled_orders": filled_orders_block,
        "capital": capital_block,
        "portfolio": portfolio_block,
        "alerts": alerts_block
    }

    validate_daily_report(report)
    save_daily_report_json(report)
    cleanup_old_reports(days=90)

    return report




def generate_weekly_report() -> Dict[str, Any]:
    """
    Generates the Weekly Report according to aurono.weekly.report.schema.v1.
    """

    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=7)
    tm = TradeManager(get_db_path())
    cfg = current_config()
    strategies = load_strategies()

    # ------------------------------------------------------------
    # 1. PERFORMANCE
    # ------------------------------------------------------------
    filled_week = tm.get_filled_orders_since(week_start)

    buys = sum(1 for o in filled_week if o.side == "buy")
    sells = sum(1 for o in filled_week if o.side == "sell")
    sell_wins = sum(1 for o in filled_week if o.side == "sell" and (o.pnl or 0) > 0)
    weekly_pnl = sum(float(o.pnl or 0) for o in filled_week)

    performance_block = {
        "weekly_pnl_eur": weekly_pnl,
        "buys": buys,
        "sells": sells,
        "sell_win_rate": (sell_wins / sells * 100) if sells > 0 else 0
    }

    # ------------------------------------------------------------
    # 2. PER-STRATEGY BREAKDOWN
    # ------------------------------------------------------------
    strategies_block = []

    for s in strategies:
        s_fills = [o for o in filled_week if o.strategy_id == s["id"]]

        buys_s = [o for o in s_fills if o.side == "buy"]
        sells_s = [o for o in s_fills if o.side == "sell"]

        pnl_s = sum(float(o.pnl or 0) for o in s_fills)

        if buys_s:
            avg_buy = sum(float(o.price) for o in buys_s) / len(buys_s)
        else:
            avg_buy = None

        if sells_s:
            avg_sell = sum(float(o.price) for o in sells_s) / len(sells_s)
        else:
            avg_sell = None

        strategies_block.append({
            "strategy_id": s["id"],
            "symbol": s["symbol"],
            "exchange": s["exchange"],
            "timeframe": s["timeframe"],
            "label": s["label"],
            "buys": len(buys_s),
            "sells": len(sells_s),
            "weekly_pnl_eur": pnl_s,
            "avg_buy_price": avg_buy,
            "avg_sell_price": avg_sell
        })

    # ------------------------------------------------------------
    # 3. HIGHLIGHTS
    # ------------------------------------------------------------
    if filled_week:
        best = max(filled_week, key=lambda o: o.pnl or 0)
        worst = min(filled_week, key=lambda o: o.pnl or 0)
        best_block = {
            "symbol": best.symbol,
            "exchange": best.exchange,
            "timeframe": best.timeframe,
            "pnl": float(best.pnl or 0)
        }
        worst_block = {
            "symbol": worst.symbol,
            "exchange": worst.exchange,
            "timeframe": worst.timeframe,
            "pnl": float(worst.pnl or 0)
        }
    else:
        best_block = worst_block = None

    highlights_block = {
        "best_trade": best_block,
        "worst_trade": worst_block
    }

    # ------------------------------------------------------------
    # 4. EXPOSURE
    # ------------------------------------------------------------
    portfolio = compute_portfolio_snapshot()

    exposure_block = {
        "cash_pct": float(portfolio["cash_pct"]),
        "crypto_pct": float(portfolio["crypto_pct"]),
        "by_coin": portfolio["by_coin"],
        "by_exchange": portfolio["by_exchange"]
    }

    # ------------------------------------------------------------
    # 5. CAPITAL EFFICIENCY
    # ------------------------------------------------------------
    cap_eff = {
        "eur_deployed": float(portfolio["deployed_eur"]),
        "eur_reserved": float(portfolio["reserved_eur"]),
        "eur_free": float(portfolio["free_eur"])
    }

    # ------------------------------------------------------------
    # 6. SYSTEM RELIABILITY
    # ------------------------------------------------------------
    reliability_block = {
        "api_uptime_pct": 100,      # replace with metrics
        "ohlc_failures": 0,
        "order_retries": 0,
        "credential_issues": 0,
        "trader_uptime_pct": 100
    }

    # ------------------------------------------------------------
    # 7. STRATEGY CHANGES
    # ------------------------------------------------------------
    changes_block = {
        "added": [],      # fill if you log strategy changes
        "updated": [],
        "deleted": []
    }
    
    report = {
        "week_start": week_start.date().isoformat(),
        "week_end": now.date().isoformat(),
        "performance": performance_block,
        "strategies": strategies_block,
        "highlights": highlights_block,
        "exposure": exposure_block,
        "capital_efficiency": cap_eff,
        "system_reliability": reliability_block,
        "strategy_changes": changes_block
    }

    validate_weekly_report(report)
    save_weekly_report_json(report)
    cleanup_old_reports(days=90)
    return report



