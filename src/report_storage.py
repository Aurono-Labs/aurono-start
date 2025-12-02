# src/report_storage.py

import os
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Any
from fastapi.templating import Jinja2Templates
from utils import root_path


# ------------------------------------------------------------
# Directories (ALWAYS under data/reports)
# ------------------------------------------------------------
BASE_DIR = root_path("data", "reports")
DAILY_DIR = os.path.join(BASE_DIR, "daily")
WEEKLY_DIR = os.path.join(BASE_DIR, "weekly")
HTML_DIR = os.path.join(BASE_DIR, "html")

# Ensure directories exist
for d in (BASE_DIR, DAILY_DIR, WEEKLY_DIR, HTML_DIR):
    os.makedirs(d, exist_ok=True)


# ------------------------------------------------------------
# Save JSON report
# ------------------------------------------------------------

def save_daily_report_json(report: Dict[str, Any]) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"daily_{ts}.json"
    path = os.path.join(DAILY_DIR, filename)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    return path


def save_weekly_report_json(report: Dict[str, Any]) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"weekly_{ts}.json"
    path = os.path.join(WEEKLY_DIR, filename)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    return path


# ------------------------------------------------------------
# Save HTML (correct folder: data/reports/html)
# ------------------------------------------------------------

def save_html_report(html: str, name: str):
    """
    Save HTML reports into data/reports/html/.
    Ensures .html extension.
    """
    if not name.endswith(".html"):
        name += ".html"

    path = os.path.join(HTML_DIR, name)

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)

    return path


# ------------------------------------------------------------
# Load report files
# ------------------------------------------------------------

def load_daily_report(date_str: str) -> Dict[str, Any]:
    path = os.path.join(DAILY_DIR, f"daily_{date_str}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"No report found for {date_str}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_weekly_report(date_str: str) -> Dict[str, Any]:
    path = os.path.join(WEEKLY_DIR, f"weekly_{date_str}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"No report found for {date_str}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------
# Retention / Cleanup
# ------------------------------------------------------------

def cleanup_old_reports(days: int = 90):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    for folder in (DAILY_DIR, WEEKLY_DIR, HTML_DIR):
        for filename in os.listdir(folder):
            path = os.path.join(folder, filename)
            try:
                date_part = filename.split("_")[1].split(".")[0]
                file_date = datetime.fromisoformat(date_part)
            except Exception:
                continue

            if file_date < cutoff:
                os.remove(path)


# ------------------------------------------------------------
# HTML RENDERING
# ------------------------------------------------------------

templates = Jinja2Templates(directory="src/templates")


def render_daily_report_html(report):
    return templates.get_template("reports/daily_report.html").render(report=report)


def render_weekly_report_html(report):
    return templates.get_template("reports/weekly_report.html").render(report=report)

