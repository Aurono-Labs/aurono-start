from report_builder import generate_weekly_report
from report_storage import (
    save_weekly_report_json,
    save_html_report,
    render_weekly_report_html,
    cleanup_old_reports,
)
from emailer import send_email

# Generate the report
report = generate_weekly_report()
date = report["week_end"]

# Save JSON output
save_weekly_report_json(report)

# Render and save HTML
html = render_weekly_report_html(report)
save_html_report(html, f"weekly_{date}")

# Cleanup old weekly JSON and HTML reports (retention 90 days)
cleanup_old_reports("data/reports/weekly", 90)
cleanup_old_reports("data/reports/html", 90)

