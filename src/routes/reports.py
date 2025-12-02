# src/routes/reports.py

import os
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse

from report_storage import (
    DAILY_DIR,
    WEEKLY_DIR,
    HTML_DIR,
)

router = APIRouter(prefix="/reports", tags=["reports"])


def list_files(directory: str):
    """Return files newest first."""
    if not os.path.exists(directory):
        return []
    return sorted(os.listdir(directory), reverse=True)


# ------------------------------------------------------------
# List all daily + weekly reports
# ------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def reports_index(request: Request):
    daily_files = list_files(DAILY_DIR)
    weekly_files = list_files(WEEKLY_DIR)

    return request.app.state.templates.get_template("reports/list_reports.html").render(
        request=request,
        daily_reports=daily_files,
        weekly_reports=weekly_files,
    )


# ------------------------------------------------------------
# View HTML-rendered report
# ------------------------------------------------------------
@router.get("/view/{name}", response_class=HTMLResponse)
async def view_report(request: Request, name: str):
    html_path = os.path.join(HTML_DIR, name)
    if not os.path.exists(html_path):
        raise HTTPException(404, "Report HTML not found")

    # Instead of embedding the file’s contents, we point iframe to static file:
    iframe_src = f"/reports/html/{name}"

    return request.app.state.templates.get_template("reports/view_report.html").render(
        request=request,
        html_file=iframe_src
    )


# ------------------------------------------------------------
# Download original JSON report
# ------------------------------------------------------------
@router.get("/download/{kind}/{name}")
async def download_report(kind: str, name: str):
    if kind == "daily":
        folder = DAILY_DIR
    elif kind == "weekly":
        folder = WEEKLY_DIR
    else:
        raise HTTPException(404, "Invalid report type")

    path = os.path.join(folder, name)
    if not os.path.exists(path):
        raise HTTPException(404, "Report not found")

    return FileResponse(path, filename=name)

