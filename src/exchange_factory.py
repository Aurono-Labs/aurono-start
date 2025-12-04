from decimal import Decimal
from utils import current_config

from kraken_exchange import KrakenExchange
from bitvavo_exchange import BitvavoExchange

def get_exchange(name: str):
    name = (name or "bitvavo").lower()

    cfg = current_config()

    if name == "kraken":
        ex = KrakenExchange()
        ex.fee_rate = Decimal(str(cfg["exchanges"]["kraken"].get("fee_rate", 0)))
        return ex

    # default: Bitvavo
    ex = BitvavoExchange()
    ex.fee_rate = Decimal(str(cfg["exchanges"]["bitvavo"].get("fee_rate", 0)))
    return ex

