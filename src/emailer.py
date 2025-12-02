# src/emailer.py

import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

from utils import current_config


def send_email(subject: str, html_body: str, attachments=None):
    """
    Sends a UTF-8 encoded HTML email using SMTP.
    """
    cfg = current_config().get("email", {})
    username = cfg.get("username")
    password = cfg.get("password")
    to_addr = cfg.get("to", username)

    if not username or not password:
        raise ValueError("Email username or password missing in config.yaml")

    # Create message
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = username
    msg["To"] = to_addr

    # Attach UTF-8 HTML body
    part = MIMEText(html_body, "html", "utf-8")
    msg.attach(part)

    # Attachments (future support)
    if attachments:
        for filename, data in attachments:
            from email.mime.application import MIMEApplication
            attachment = MIMEApplication(data, Name=filename)
            attachment["Content-Disposition"] = f'attachment; filename="{filename}"'
            msg.attach(attachment)

    # SMTP settings (Gmail example)
    smtp_server = "smtp.gmail.com"
    port = 587

    context = ssl.create_default_context()

    with smtplib.SMTP(smtp_server, port) as server:
        server.ehlo()
        server.starttls(context=context)
        server.login(username, password)
        server.sendmail(username, to_addr, msg.as_string())

