from pathlib import Path

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.status import HTTP_303_SEE_OTHER

import yaml

from utils import (
    log_event,
    list_credentials_for_ui,
    upsert_credentials,
    get_credentials_plain_by_id,
    delete_credentials_by_id,
    current_config,
    get_config_path,
)
from kraken_exchange import KrakenExchange
from bitvavo_exchange import BitvavoExchange

router = APIRouter(prefix="/settings", tags=["settings"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


# --------------------------------------------------------------
# Settings Page
# --------------------------------------------------------------
@router.get("/")
def show_settings(
    request: Request,
    message: str | None = None,
    message_type: str | None = None,
):
    creds = list_credentials_for_ui()
    email_cfg = current_config().get("email", {})

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "credentials": creds,
            "message": message,
            "message_type": message_type,
            "email_enabled": email_cfg.get("enabled", False),
            "email_username": email_cfg.get("username", ""),
            "email_to": email_cfg.get("to", ""),
            "email_password": "********" if email_cfg.get("password") else "",
        },
    )


# --------------------------------------------------------------
# Add new exchange credentials
# --------------------------------------------------------------
@router.post("/add")
def add_credentials(
    exchange: str = Form(...),
    api_key: str = Form(...),
    api_secret: str = Form(...),
):
    exchange = exchange.lower().strip()
    upsert_credentials(exchange, api_key, api_secret)

    log_event(f"🔐 Added or updated API credentials for {exchange}.")
    return RedirectResponse(url="/settings", status_code=HTTP_303_SEE_OTHER)


# --------------------------------------------------------------
# Update credentials by ID
# --------------------------------------------------------------
@router.post("/update/{cred_id}")
def update_credentials(
    cred_id: int,
    api_key: str = Form(""),
    api_secret: str = Form(""),
    request: Request = None,
):
    record = get_credentials_plain_by_id(cred_id)
    if not record:
        return show_settings(request, "Credentials not found.", "error")

    exchange = record["exchange"]

    # If empty, keep existing value
    new_key = api_key.strip() or record["api_key"]
    new_secret = api_secret.strip() or record["api_secret"]

    upsert_credentials(exchange, new_key, new_secret)

    log_event(f"📝 Updated credentials for {exchange} (id={cred_id})")
    return RedirectResponse(url="/settings", status_code=HTTP_303_SEE_OTHER)


# --------------------------------------------------------------
# Delete credentials
# --------------------------------------------------------------
@router.post("/delete/{cred_id}")
def delete_credentials(cred_id: int):
    record = get_credentials_plain_by_id(cred_id)
    delete_credentials_by_id(cred_id)

    if record:
        log_event(f"🗑 Deleted API credentials for {record['exchange']} (id={cred_id})")
    else:
        log_event(f"🗑 Deleted unknown API credentials id={cred_id}")

    return RedirectResponse(url="/settings", status_code=HTTP_303_SEE_OTHER)


# --------------------------------------------------------------
# Update Email Reporting Settings
# --------------------------------------------------------------
@router.post("/email")
def update_email_settings(
    request: Request,
    email_enabled: str = Form(None),
    email_username: str = Form(""),
    email_to: str = Form(""),
    email_password: str = Form(""),
):
    """
    Update email reporting settings in config.yaml.
    """
    config = current_config()

    # Ensure email section exists
    if "email" not in config:
        config["email"] = {}

    # Toggle enabled
    config["email"]["enabled"] = (email_enabled == "on")

    # Username / sender
    config["email"]["username"] = email_username.strip()

    # Receiver (default = sender)
    config["email"]["to"] = (email_to.strip() or email_username.strip())

    # Update password ONLY if user typed something new
    if email_password not in ("", None, "********"):
        config["email"]["password"] = email_password.strip()

    # Save back to config.yaml
    with open(get_config_path(), "w", encoding="utf-8") as f:
        yaml.dump(config, f)

    return RedirectResponse(url="/settings", status_code=HTTP_303_SEE_OTHER)

# --------------------------------------------------------------
# Generate Test Daily Report (manual trigger)
# --------------------------------------------------------------
@router.post("/test-report")
def test_generate_daily_report(request: Request):
    from report_builder import generate_daily_report
    from report_storage import (
        save_daily_report_json,
        save_html_report,
        render_daily_report_html,
    )

    try:
        # Build the full schema-aligned daily report
        report = generate_daily_report()

        # Convert to HTML using the daily_report.html template
        html = render_daily_report_html(report)

        # Filename
        date_str = report["date"].split("T")[0]

        # Save JSON + HTML in /reports/daily + /reports/html
        save_daily_report_json(report)
        save_html_report(html, f"daily_test_{date_str}")

        return show_settings(
            request,
            "Test Daily Report generated successfully.",
            "success",
        )

    except Exception as e:
        return show_settings(
            request,
            f"Test Report failed: {e}",
            "error",
        )

# --------------------------------------------------------------
# Generate Test Weekly Report (manual trigger)
# --------------------------------------------------------------
@router.post("/test-weekly-report")
def test_generate_weekly_report(request: Request):
    from report_builder import generate_weekly_report
    from report_storage import (
        save_weekly_report_json,
        save_html_report,
        render_weekly_report_html
    )

    try:
        report = generate_weekly_report()
        html = render_weekly_report_html(report)

        date_str = report["week_end"]

        save_weekly_report_json(report)
        save_html_report(html, f"weekly_test_{date_str}")

        return show_settings(
            request,
            "Test Weekly Report generated successfully.",
            "success"
        )
    except Exception as e:
        return show_settings(
            request,
            f"Weekly Test Report failed: {e}",
            "error"
        )


# --------------------------------------------------------------
# Send Test Email
# --------------------------------------------------------------
@router.post("/test-email")
def test_email(request: Request):
    import asyncio
    from emailer import send_email

    email_cfg = current_config().get("email", {})

    if not email_cfg.get("enabled", False):
        return show_settings(request, "Email is disabled.", "error")

    try:
        html = """
        <h2>Aurono Test Email</h2>
        <p>This is a test email to confirm your SMTP settings are correct.</p>
        """

        asyncio.run(
            send_email(
                subject="Aurono Test Email",
                html_body=html,
                attachments=[],
            )
        )

        return show_settings(request, "Test email sent successfully.", "success")

    except Exception as e:
        return show_settings(request, f"Email failed: {e}", "error")


# --------------------------------------------------------------
# Mask credentials
# --------------------------------------------------------------
def mask_key(k: str) -> str:
    """
    Mask API keys so UI shows: ••••••••••ABCD
    Always 10 dots + last 4 chars.
    """
    if not k:
        return ""
    k = k.strip()
    if len(k) <= 4:
        return "••••"
    return "••••••••••" + k[-4:]


# --------------------------------------------------------------
# Test credentials
# --------------------------------------------------------------
@router.post("/test/{cred_id}")
def test_credentials(cred_id: int, request: Request):
    """
    Test credentials by calling a simple authenticated endpoint.
    """
    record = get_credentials_plain_by_id(cred_id)
    if not record:
        return show_settings(request, "Credentials not found.", "error")

    exch = record["exchange"].lower()
    api_key = record["api_key"]
    api_secret = record["api_secret"]

    if not api_key or not api_secret:
        return show_settings(request, f"{exch.capitalize()} keys are empty.", "error")

    try:
        if exch == "kraken":
            client = KrakenExchange(api_key=api_key, api_secret=api_secret)
            res = client._private_request("/Balance", {})
            if res.get("error"):
                return show_settings(request, f"❌ Kraken test failed: {res['error']}", "error")
            return show_settings(request, "✅ Kraken credentials are valid.", "success")

        elif exch == "bitvavo":
            client = BitvavoExchange(api_key=api_key, api_secret=api_secret)
            res = client._private_request("GET", "balance", None)
            if isinstance(res, dict) and ("error" in res or "errorCode" in res):
                err = res.get("error") or res.get("errorCode")
                return show_settings(request, f"❌ Bitvavo test failed: {err}", "error")
            return show_settings(request, "✅ Bitvavo credentials are valid.", "success")

        else:
            return show_settings(request, f"❌ Testing not supported for {exch}.", "error")

    except Exception as e:
        log_event(f"❌ Test failed: {e}")
        return show_settings(request, f"❌ Connection error: {e}", "error")

