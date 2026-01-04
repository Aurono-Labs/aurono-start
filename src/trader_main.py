# trader_main.py
from utils import current_config, log_event

# -----------------------------------------------------
# 🛡 Trader Single-Instance Lock (unchanged)
# -----------------------------------------------------
import fcntl
from pathlib import Path

LOCK_FILE = Path("/tmp/aurono_trader.lock")

def acquire_lock_or_exit():
    global lock_fh
    lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log_event("⚠️ Trader already running — second instance aborted.")
        print("Trader already running. Exiting.")
        exit(0)

acquire_lock_or_exit()

from trader_engine import TraderEngine

def get_engine() -> TraderEngine:
    # No global exchange selection here
    return TraderEngine()

if __name__ == "__main__":
    cfg = current_config()
    engine = get_engine()
    mode = cfg.get("mode", "dev").lower()

    if mode == "live":
        engine.run_live()
    elif mode == "dev":
        engine.run_dev()
    else:
        log_event(f"⚠️ Unknown mode {mode}, trader not started.")
