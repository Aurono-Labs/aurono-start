# run_weekly_report.py

from report_builder import generate_weekly_report
from report_storage import (
    save_weekly_report_json,
    save_html_report,
    render_weekly_report_html,
    cleanup_old_reports,
)
from report_dispatcher import dispatch_report
from report_renderer_email import render_weekly_email


# --------------------------------------------------
# Generate report
# --------------------------------------------------
report = generate_weekly_report()
week_end = report["week_end"]


# --------------------------------------------------
# Persist report (browser version)
# --------------------------------------------------
save_weekly_report_json(report)

browser_html = render_weekly_report_html(report)
save_html_report(browser_html, f"weekly_{week_end}")


# --------------------------------------------------
# Dispatch report (EMAIL VERSION)
# --------------------------------------------------
email_html = render_weekly_email(report)

dispatch_report(
    report_type="weekly",
    subject=f"Aurono Weekly Report — week ending {week_end}",
    html_body=email_html,
)


# --------------------------------------------------
# Cleanup old weekly JSON and HTML reports
# --------------------------------------------------
cleanup_old_reports("data/reports/weekly", 90)
cleanup_old_reports("data/reports/html", 90)

