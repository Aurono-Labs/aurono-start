from datetime import datetime
from report_builder import generate_daily_report
from report_storage import (
    save_daily_report_json,
    save_html_report,
    render_daily_report_html,
    cleanup_old_reports,
)
from emailer import send_email

# Generate the report
report = generate_daily_report()
today = report["date"].split("T")[0]

# Save JSON
save_daily_report_json(report)

# Render and save HTML
html = render_daily_report_html(report)
save_html_report(html, f"daily_{today}")

# Cleanup old reports
cleanup_old_reports("data/reports/daily", 90)
cleanup_old_reports("data/reports/html", 90)
