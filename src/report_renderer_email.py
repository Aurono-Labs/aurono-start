# src/report_renderer_email.py

from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = (
    Path(__file__).resolve()
    .parent            # src/
    / "templates"      # src/templates/
)

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)

def render_daily_email(report: dict) -> str:
    return _env.get_template(
        "email/daily_report_email.html"
    ).render(report=report)

def render_weekly_email(report: dict) -> str:
    return _env.get_template(
        "email/weekly_report_email.html"
    ).render(report=report)

