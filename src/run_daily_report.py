from datetime import datetime
from report_builder import generate_daily_report
from report_storage import (
    save_html_report,
    render_daily_report_html,
    cleanup_old_reports,
)
from emailer import send_email

# Generate report
report = generate_daily_report()
html = render_daily_report_html(report)
today = report["date"].split("T")[0]

# Save HTML output
save_html_report(html, f"daily_{today}")

# Cleanup old reports (90 days retention)
cleanup_old_reports("data/reports/daily", 90)
cleanup_old_reports("data/reports/html", 90)

