# run_daily_report.py

from report_builder import generate_daily_report
from report_storage import (
    save_daily_report_json,
    save_html_report,
    render_daily_report_html,
    cleanup_old_reports,
)
from report_dispatcher import dispatch_report
from report_renderer_email import render_daily_email

from utils import log_event

try:
    import psutil
except ImportError:
    log_event("⚠️ psutil missing — system metrics disabled")



# --------------------------------------------------
# Generate report
# --------------------------------------------------
report = generate_daily_report()
today = report["date"].split("T")[0]


# --------------------------------------------------
# Persist report (browser version)
# --------------------------------------------------
save_daily_report_json(report)

browser_html = render_daily_report_html(report)
save_html_report(browser_html, f"daily_{today}")


# --------------------------------------------------
# Dispatch report (EMAIL VERSION)
# --------------------------------------------------
email_html = render_daily_email(report)

dispatch_report(
    report_type="daily",
    subject=f"Aurono Daily Report — {today}",
    html_body=email_html,
)


# --------------------------------------------------
# Cleanup old reports
# --------------------------------------------------
cleanup_old_reports("data/reports/daily", 90)
cleanup_old_reports("data/reports/html", 90)

