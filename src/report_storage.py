import os
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Any
from fastapi.templating import Jinja2Templates
from utils import root_path

BASE_DIR = root_path("src", "reports")
DAILY_DIR = os.path.join(BASE_DIR, "daily")
WEEKLY_DIR = os.path.join(BASE_DIR, "weekly")
HTML_DIR = os.path.join(BASE_DIR, "html")

# Ensure directories exist
for d in (DAILY_DIR, WEEKLY_DIR, HTML_DIR):
    os.makedirs(d, exist_ok=True)

# Correct template directory
TEMPLATE_DIR = root_path("src", "templates", "reports")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

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

def save_html_report(html: str, name: str):
    if not name.endswith(".html"):
        name += ".html"
    path = os.path.join(HTML_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path

def render_daily_report_html(report):
    return templates.get_template("daily_report.html").render(report=report)

def render_weekly_report_html(report):
    return templates.get_template("weekly_report.html").render(report=report)

