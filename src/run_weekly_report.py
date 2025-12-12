from report_builder import generate_weekly_report
from report_storage import (
    save_html_report,
    render_weekly_report_html,
    cleanup_old_reports,
)
from emailer import send_email

# Generate the report
report = generate_weekly_report()
html = render_weekly_report_html(report)
date = report["week_end"]

# Save HTML output
save_html_report(html, f"weekly_{date}")

# Cleanup old weekly JSON and HTML reports (retention 90 days)
cleanup_old_reports("data/reports/weekly", 90)
cleanup_old_reports("data/reports/html", 90)

