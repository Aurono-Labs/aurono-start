# run_daily_report.py

from report_builder import generate_daily_report
from report_storage import (
    save_daily_report_json,
    save_html_report,
    render_daily_report_html,
    cleanup_old_reports,
)
from report_dispatcher import dispatch_report


# --------------------------------------------------
# Generate report
# --------------------------------------------------
report = generate_daily_report()
today = report["date"].split("T")[0]


# --------------------------------------------------
# Persist report
# --------------------------------------------------
save_daily_report_json(report)

html = render_daily_report_html(report)
save_html_report(html, f"daily_{today}")


# --------------------------------------------------
# Dispatch report (email, future channels)
# --------------------------------------------------
dispatch_report(
    report_type="daily",
    subject=f"Aurono Daily Report — {today}",
    html_body=html,
)


# --------------------------------------------------
# Cleanup old reports
# --------------------------------------------------
cleanup_old_reports("data/reports/daily", 90)
cleanup_old_reports("data/reports/html", 90)

