from decimal import Decimal
from kraken_exchange import KrakenExchange
from bitvavo_exchange import BitvavoExchange

def get_exchange(name: str):
    name = (name or "bitvavo").lower()

    # Kraken
    if name == "kraken":
        ex = KrakenExchange()
        # leave ex.fee_rate untouched → KrakenExchange has internal default (0.0025)
        return ex

    # Bitvavo
    ex = BitvavoExchange()
    # leave ex.fee_rate untouched → BitvavoExchange has internal default (0.0025)
    return ex

