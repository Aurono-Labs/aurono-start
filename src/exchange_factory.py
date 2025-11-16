from kraken_exchange import KrakenExchange
from bitvavo_exchange import BitvavoExchange

def get_exchange(name: str):
    name = (name or "bitvavo").lower()
    if name == "kraken":
        return KrakenExchange()
    return BitvavoExchange()
