from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from system.update_state import (
    load_update_state,
    get_update_status,
    snooze_update,
    clear_snooze,
    apply_update,
)

router = APIRouter(prefix="/api/system/update", tags=["system"])


# -------------------------
# Models
# -------------------------

class SnoozeRequest(BaseModel):
    days: int


# -------------------------
# Routes
# -------------------------

@router.get("")
def read_update_status():
    return get_update_status()


@router.post("/snooze")
def snooze(req: SnoozeRequest):
    if req.days <= 0:
        raise HTTPException(status_code=400, detail="Invalid snooze duration")

    if not snooze_update(req.days):
        raise HTTPException(status_code=409, detail="No update to snooze")

    return {"status": "ok"}


@router.post("/clear-snooze")
def clear():
    clear_snooze()
    return {"status": "ok"}


@router.post("/apply")
def apply():
    state = load_update_state()

    if state.get("status") != "pending":
        raise HTTPException(
            status_code=409,
            detail="Update already running or not applicable",
        )

    if not apply_update():
        raise HTTPException(
            status_code=500,
            detail="Failed to start updater",
        )

    return {"status": "started"}
