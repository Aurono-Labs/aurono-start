from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER
from fastapi.templating import Jinja2Templates
from pathlib import Path
import json
from utils import root_path, log_event, current_config, save_config

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/")
def show_settings(request: Request):
    cfg = current_config()
    exchange = cfg.get("exchange", "kraken")

    if exchange == "bitvavo":
        key_file = root_path("bitvavo_api_key.json")
    else:
        key_file = root_path("kraken_api_key.json")

    if key_file.exists():
        with open(key_file) as f:
            creds = json.load(f)
    else:
        creds = {"api_key": "", "api_secret": ""}

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "creds": creds,
        "cfg": cfg
    })


@router.post("/save")
def save_settings(
    exchange: str = Form(...),
    api_key: str = Form(...),
    api_secret: str = Form(...)
):
    cfg = current_config()
    exchange = exchange.lower().strip()

    if exchange not in ("kraken", "bitvavo"):
        exchange = "kraken"

    cfg["exchange"] = exchange

    if exchange == "bitvavo":
        cfg["api_credentials"] = "bitvavo_api_key.json"
        key_file = root_path("bitvavo_api_key.json")
    else:
        cfg["api_credentials"] = "kraken_api_key.json"
        key_file = root_path("kraken_api_key.json")

    with open(key_file, "w") as f:
        json.dump({
            "api_key": api_key.strip(),
            "api_secret": api_secret.strip()
        }, f, indent=2)

    save_config(cfg)
    log_event(f"🔐 {exchange.capitalize()} API credentials updated via settings page")
    return RedirectResponse(url="/settings", status_code=HTTP_303_SEE_OTHER)
