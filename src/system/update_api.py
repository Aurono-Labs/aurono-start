from fastapi import APIRouter
from system.update_state import (
    get_update_status,
    snooze_update,
    apply_update,
)

router = APIRouter(prefix="/api/system/update", tags=["system"])

@router.get("")
def read_update_status():
    return get_update_status()

@router.post("/snooze")
def snooze(payload: dict):
    days = int(payload.get("days", 0))
    return {"ok": snooze_update(days)}

@router.post("/apply")
def apply():
    return {"ok": apply_update()}
