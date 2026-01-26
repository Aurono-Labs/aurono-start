from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
import subprocess
import time
from pathlib import Path

STATE_FILE = "/var/lib/aurono/update_state.json"


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None

_UPDATE_STALE_AFTER = timedelta(minutes=30)

def _updater_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "/usr/local/bin/aurono-update apply"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except Exception:
        return False


APPLY_LOG = "/var/lib/aurono/update_apply.log"

def apply_update() -> bool:
    raw = load_update_state()
    if not raw:
        return False

    # 🔒 HARD GUARD — single entry point
    if raw.get("status") == "updating":
        return False

    if raw.get("status") != "pending":
        return False

    # Spawn updater first (prove it started), then mark "updating"
    try:
        Path("/var/lib/aurono").mkdir(parents=True, exist_ok=True)
        logf = open(APPLY_LOG, "ab", buffering=0)

        p = subprocess.Popen(
            [
                "/usr/bin/sudo",
                "-n",
                "/usr/bin/python3",
                "-u",
                "/usr/local/bin/aurono-update",
                "apply",
            ],
            stdout=logf,
            stderr=logf,
            start_new_session=True,
            close_fds=True,
        )

        # If sudo fails, it usually exits immediately. Detect that.
        time.sleep(0.2)
        rc = p.poll()
        if rc is not None and rc != 0:
            return False

    except Exception:
        return False

    # 🔒 Persist intent AFTER spawn succeeded
    raw["status"] = "updating"
    raw["update_started_at"] = _now_utc().isoformat()
    return _write_state(raw)


# ------------------------------------------------------------
# Core reader
# ------------------------------------------------------------

def load_update_state() -> Dict[str, Any]:
    """
    Low-level state loader.
    Never raises.
    """
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ------------------------------------------------------------
# Public API (used by dashboard / API)
# ------------------------------------------------------------

def get_update_status() -> Dict[str, Any]:
    """
    Returns a normalized, UI-friendly update status.
    Always returns a dict with stable keys.
    """

    raw = load_update_state()

    # --------------------------------------------------
    # 🛟 Safety net: recover stale "updating" state
    # --------------------------------------------------
    if raw.get("status") == "updating":
        started_at = _parse_iso(raw.get("update_started_at"))

        if started_at:
            now = _now_utc()
            age = now - started_at

            if age > _UPDATE_STALE_AFTER and not _updater_running():
                raw["status"] = "pending"
                _write_state(raw)

    current_version = raw.get("current_version")
    available_version = raw.get("available_version")
    status = raw.get("status")  # pending | applied | up_to_date | None

    snoozed_until = _parse_iso(raw.get("snoozed_until"))
    last_checked = _parse_iso(raw.get("last_checked"))

    now = _now_utc()
    is_snoozed = snoozed_until is not None and snoozed_until > now

    update_available = (
        status == "pending"
        and bool(available_version)
        and not is_snoozed
    )

    return {
        "current_version": current_version,
        "available_version": available_version,
        "update_available": update_available,
        "status": status or "unknown",
        "release_notes": raw.get("release_notes"),
        "release_date": raw.get("release_date"),
        "last_checked": last_checked.isoformat() if last_checked else None,
        "snoozed_until": snoozed_until.isoformat() if snoozed_until else None,
        "is_snoozed": is_snoozed,
    }


# ------------------------------------------------------------
# Snooze handling
# ------------------------------------------------------------

def snooze_update(days: int) -> bool:
    """
    Snooze update for N days.
    Returns True if state was updated.
    """
    if days <= 0:
        return False

    raw = load_update_state()
    if not raw:
        return False

    until = _now_utc() + timedelta(days=days)
    raw["snoozed_until"] = until.isoformat()

    return _write_state(raw)


def clear_snooze() -> bool:
    raw = load_update_state()
    if not raw:
        return False

    raw["snoozed_until"] = None
    return _write_state(raw)


def _write_state(state: Dict[str, Any]) -> bool:
    """
    Atomic best-effort write.
    Backend is allowed to write only snooze-related fields.
    """
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
        return True
    except Exception:
        return False
