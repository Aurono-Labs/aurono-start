from report_builder import generate_weekly_report
from report_storage import save_html_report, render_weekly_report_html, cleanup_old_reports
from emailer import send_email


report = generate_weekly_report()
html = render_weekly_report_html(report)
date = report["week_end"]

save_html_report(html, f"weekly_{date}")
cleanup_old_reports(90)

