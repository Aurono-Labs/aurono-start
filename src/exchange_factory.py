from decimal import Decimal
from kraken_exchange import KrakenExchange
from bitvavo_exchange import BitvavoExchange
from coinbase_exchange import CoinbaseExchange
from utils import log_event
from utils import get_credentials_for_exchange


def get_exchange(name: str):
    name = (name or "bitvavo").lower()

    # -------------------
    # Kraken
    # -------------------
    if name == "kraken":
        ex = KrakenExchange()
        return ex

    # -------------------
    # Bitvavo
    # -------------------
    if name == "bitvavo":
        ex = BitvavoExchange()
        return ex

    # ---------------------------------------------------------
    # Coinbase – SAFE REGISTRATION
    # ---------------------------------------------------------
    if name == "coinbase":
        try:
            creds = get_credentials_for_exchange("coinbase")

            # Disable Coinbase until credentials exist
            if not creds:
                log_event("⚠️ Coinbase requested but no credentials found. Disabled.")
                return None

            return CoinbaseExchange()

        except Exception as e:
            log_event(f"❌ Failed loading CoinbaseExchange: {e}")
            return None

    raise ValueError(f"Unsupported exchange: {name}")

