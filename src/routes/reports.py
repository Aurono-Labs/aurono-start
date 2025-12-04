# src/routes/reports.py

import os
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse

from utils import root_path
from report_storage import _ensure_report_dirs  # internal helper to create folders

router = APIRouter(prefix="/reports", tags=["reports"])

# Persistent directories
BASE = Path(root_path("data", "reports"))
DAILY_DIR = BASE / "daily"
WEEKLY_DIR = BASE / "weekly"
HTML_DIR = BASE / "html"


def list_files(directory: Path):
    """Return files newest first."""
    if not directory.exists():
        return []
    return sorted(os.listdir(directory), reverse=True)


# ------------------------------------------------------------
# List all daily + weekly reports
# ------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def reports_index(request: Request):

    # Ensure directories exist (first use)
    _ensure_report_dirs()

    daily_files = list_files(DAILY_DIR)
    weekly_files = list_files(WEEKLY_DIR)

    return request.app.state.templates.get_template("reports/list_reports.html").render(
        request=request,
        daily_reports=daily_files,
        weekly_reports=weekly_files,
    )


# ------------------------------------------------------------
# View a single HTML report using iframe
# ------------------------------------------------------------
@router.get("/view/{name}", response_class=HTMLResponse)
async def view_report(request: Request, name: str):
    """
    Confirms file exists in data/reports/html.
    Shows view_report.html containing an iframe
    that loads `/reports/html/<name>`.
    """

    path = HTML_DIR / name
    if not path.exists():
        raise HTTPException(404, "Report HTML not found")

    iframe_src = f"/reports/html/{name}"

    return request.app.state.templates.get_template("reports/view_report.html").render(
        request=request,
        html_file=iframe_src,
    )


# ------------------------------------------------------------
# Download original daily/weekly JSON report
# ------------------------------------------------------------
@router.get("/download/{kind}/{name}")
async def download_report(kind: str, name: str):

    if kind == "daily":
        folder = DAILY_DIR
    elif kind == "weekly":
        folder = WEEKLY_DIR
    else:
        raise HTTPException(404, "Invalid report type")

    path = folder / name
    if not path.exists():
        raise HTTPException(404, "Report not found")

    return FileResponse(path, filename=name)

