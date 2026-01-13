# src/report_storage.py

import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
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
# Jinja2 environment (FastAPI removed)
# ============================================================

TEMPLATE_DIR = root_path("src", "templates", "reports")

env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


# ============================================================
# Save Daily JSON
# ============================================================

def save_daily_report_json(report: Dict[str, Any]) -> str:
    base = _ensure_report_dirs()

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
    base = _ensure_report_dirs()

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
    base = _ensure_report_dirs()

    if not name.endswith(".html"):
        name += ".html"

    path = base / "html" / name
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)

    return str(path)


# ============================================================
# Render HTML using templates (FastAPI-free)
# ============================================================

def render_daily_report_html(report):
    return env.get_template("daily_report.html").render(report=report)


def render_weekly_report_html(report):
    return env.get_template("weekly_report.html").render(report=report)


# ============================================================
# Cleanup old reports
# ============================================================

def cleanup_old_reports(base_dir: str, days: int = 90):
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

