# src/report_storage.py

import os
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, Any

from fastapi.templating import Jinja2Templates
from utils import root_path

# ============================================================
# Directory structure for persistent reports
# ============================================================

def _ensure_report_dirs():
    """
    Ensures the following directory structure exists:

        data/reports/
            daily/
            weekly/
            html/
    """
    base = Path(root_path("data", "reports"))
    (base / "daily").mkdir(parents=True, exist_ok=True)
    (base / "weekly").mkdir(parents=True, exist_ok=True)
    (base / "html").mkdir(parents=True, exist_ok=True)
    return base


# ============================================================
# Template directory (unchanged)
# ============================================================

TEMPLATE_DIR = root_path("src", "templates", "reports")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


# ============================================================
# Save Daily JSON
# ============================================================

def save_daily_report_json(report: Dict[str, Any]) -> str:
    """
    Saves daily report JSON into:
        data/reports/daily/daily_YYYY-MM-DD.json
    """
    base = _ensure_report_dirs()

    # "date" is ISO timestamp → take YYYY-MM-DD
    ts = report["date"].split("T")[0]
    filename = f"daily_{ts}.json"

    path = base / "daily" / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return str(path)


# ============================================================
# Save Weekly JSON
# ============================================================

def save_weekly_report_json(report: Dict[str, Any]) -> str:
    """
    Saves weekly report JSON into:
        data/reports/weekly/weekly_YYYY-MM-DD.json
    """
    base = _ensure_report_dirs()

    # weekly reports provide a clean "week_end" field
    ts = report["week_end"]
    filename = f"weekly_{ts}.json"

    path = base / "weekly" / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return str(path)


# ============================================================
# Save HTML report
# ============================================================

def save_html_report(html: str, name: str):
    """
    Saves HTML to:
        data/reports/html/<name>.html
    """
    base = _ensure_report_dirs()

    if not name.endswith(".html"):
        name += ".html"

    path = base / "html" / name
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return str(path)


# ============================================================
# Render HTML using templates (unchanged)
# ============================================================

def render_daily_report_html(report):
    return templates.get_template("daily_report.html").render(report=report)


def render_weekly_report_html(report):
    return templates.get_template("weekly_report.html").render(report=report)

# ============================================================
# Cleanup old reports of more than 90 days old
# ============================================================

def cleanup_old_reports(base_dir: str, days: int = 90):
    """
    Remove report files older than <days> from the given directory.
    """
    cutoff = datetime.now() - timedelta(days=days)
    base = Path(base_dir)

    if not base.exists():
        return

    for f in base.iterdir():
        if f.is_file():
            ts = datetime.fromtimestamp(f.stat().st_mtime)
            if ts < cutoff:
                try:
                    f.unlink()
                except Exception:
                    pass
