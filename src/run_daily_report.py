from datetime import datetime
from report_builder import generate_daily_report
from report_storage import save_html_report, render_daily_report_html, cleanup_old_reports

report = generate_daily_report()
html = render_daily_report_html(report)
today = report["date"].split("T")[0]

save_html_report(html, f"daily_{today}")
cleanup_old_reports(90)

