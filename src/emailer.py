import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from utils import current_config


def send_email(subject: str, html_body: str, attachments=None):
    """
    Sends an email using the SMTP settings stored in config.yaml.
    Uses Gmail App Passwords (recommended).
    """

    cfg = current_config().get("email", {})
    if not cfg.get("enabled", False):
        raise RuntimeError("Email reporting is disabled in config.yaml")

    username = cfg.get("username")
    password = cfg.get("password")
    to_addr = cfg.get("to", username)

    if not username or not password:
        raise RuntimeError("Email credentials not configured")

    msg = MIMEMultipart()
    msg["From"] = username
    msg["To"] = to_addr
    msg["Subject"] = subject

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # Attach files
    attachments = attachments or []
    for file_path in attachments:
        try:
            part = MIMEBase("application", "octet-stream")
            with open(file_path, "rb") as f:
                part.set_payload(f.read())
            encoders.encode_base64(part)
            filename = file_path.split("/")[-1]
            part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
            msg.attach(part)
        except Exception:
            pass  # Never block email send for attachment error

    # Gmail SMTP
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(username, password)
        server.sendmail(username, to_addr, msg.as_string())

    return True

