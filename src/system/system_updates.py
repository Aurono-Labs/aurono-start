from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from system.update_state import (
    get_update_status,
    snooze_update,
    clear_snooze,
    apply_update,
)

router = APIRouter(prefix="/api/system/update", tags=["system"])


class SnoozeRequest(BaseModel):
    days: int


@router.get("")
def read_update_status():
    return get_update_status()


@router.post("/snooze")
def snooze(req: SnoozeRequest):
    if req.days <= 0:
        raise HTTPException(status_code=400, detail="Invalid snooze duration")
    ok = snooze_update(req.days)
    if not ok:
        raise HTTPException(status_code=409, detail="No update to snooze")
    return {"status": "ok"}


@router.post("/clear-snooze")
def clear():
    clear_snooze()
    return {"status": "ok"}


@router.post("/apply")
def apply():
    ok = apply_update()
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to start updater")
    return {"status": "updating"}
