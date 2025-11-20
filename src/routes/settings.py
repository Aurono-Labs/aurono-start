from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER
from fastapi.templating import Jinja2Templates
from pathlib import Path

from utils import (
    log_event,
    list_credentials_for_ui,
    upsert_credentials,
    get_credentials_plain_by_id,
    delete_credentials_by_id,
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

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "credentials": creds,
            "message": message,
            "message_type": message_type,
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

