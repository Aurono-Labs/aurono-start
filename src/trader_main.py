from utils import current_config, log_event
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

