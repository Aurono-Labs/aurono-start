from utils import current_config, log_event

# -----------------------------------------------------
# 🛡 Trader Single-Instance Lock
# -----------------------------------------------------
import fcntl
import os
from pathlib import Path

LOCK_FILE = Path("/tmp/aurono_trader.lock")

def acquire_lock_or_exit():
    """Prevent multiple trader instances from running."""
    global lock_fh
    
    lock_fh = open(LOCK_FILE, "w")

    try:
        # Try to acquire exclusive non-blocking lock
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Lock already held → another trader is running
        log_event("⚠️ Trader already running — second instance aborted.")
        print("Trader already running. Exiting.")
        exit(0)

# acquire lock at startup
acquire_lock_or_exit()

from trader_engine import TraderEngine
from kraken_exchange import KrakenExchange
from bitvavo_exchange import BitvavoExchange


def get_engine() -> TraderEngine:
    cfg = current_config()
    exchange_name = cfg.get("exchange", "kraken").lower()

    if exchange_name == "bitvavo":
        log_event("🔄 Using Bitvavo exchange backend (exchange=bitvavo)")
        ex = BitvavoExchange()
    else:
        log_event("🔄 Using Kraken exchange backend (exchange=kraken)")
        ex = KrakenExchange()

    return TraderEngine(ex)


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

