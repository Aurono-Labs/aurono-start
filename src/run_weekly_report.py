# run_weekly_report.py

from report_builder import generate_weekly_report
from report_storage import (
    save_weekly_report_json,
    save_html_report,
    render_weekly_report_html,
    cleanup_old_reports,
)
from report_dispatcher import dispatch_report


# --------------------------------------------------
# Generate report
# --------------------------------------------------
report = generate_weekly_report()
week_end = report["week_end"]


# --------------------------------------------------
# Persist report
# --------------------------------------------------
save_weekly_report_json(report)

html = render_weekly_report_html(report)
save_html_report(html, f"weekly_{week_end}")


# --------------------------------------------------
# Dispatch report (email, future channels)
# --------------------------------------------------
dispatch_report(
    report_type="weekly",
    subject=f"Aurono Weekly Report — week ending {week_end}",
    html_body=html,
)


# --------------------------------------------------
# Cleanup old weekly JSON and HTML reports
# --------------------------------------------------
cleanup_old_reports("data/reports/weekly", 90)
cleanup_old_reports("data/reports/html", 90)

