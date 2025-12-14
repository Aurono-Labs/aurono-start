# src/report_dispatcher.py

from typing import Literal
from utils import current_config, log_event
from emailer import send_email


ReportType = Literal["daily", "weekly"]


def dispatch_report(
    report_type: ReportType,
    subject: str,
    html_body: str,
    attachments: list[tuple[str, bytes]] | None = None,
):
    """
    Central dispatch layer for Aurono reports.

    Handles:
    - delivery channel enablement
    - email sending
    - future extensions (Slack, webhook, push, etc.)
    """

    cfg = current_config()
    attachments = attachments or []

    # --------------------------------------------------
    # EMAIL CHANNEL
    # --------------------------------------------------
    email_cfg = cfg.get("email", {})
    if email_cfg.get("enabled"):
        try:
            send_email(
                subject=subject,
                html_body=html_body,
                attachments=attachments,
            )
            log_event(f"📧 {report_type.capitalize()} report emailed successfully.")
        except Exception as e:
            log_event(f"❌ Email dispatch failed ({report_type}): {e}")
    else:
        log_event(f"📭 Email disabled — {report_type} report not sent.")

