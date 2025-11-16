import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, Dict, List, Optional


class ExchangeBase(ABC):
    """
    Abstract base class for all exchanges.
    Engine talks only to this interface, not to concrete Kraken/Bitvavo code.
    Symbols are always Aurono-style like 'BTCEUR', 'NEAREUR', 'SOLEUR'.
    """

    name: str = "base"

    @abstractmethod
    def get_ticker(self, symbol: str) -> Decimal:
        """
        Return latest trade price for given symbol (e.g. 'BTCEUR') as Decimal.
        """
        raise NotImplementedError

    @abstractmethod
    def get_ohlc(self, symbol: str, timeframe: str) -> List[list]:
        """
        Return OHLC candles for given symbol & timeframe.
        Each candle must be a sequence where:
          [_, open, _, _, close, ...]
        """
        raise NotImplementedError

    @abstractmethod
    def place_limit_order(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        volume: Decimal,
        trade_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Place a limit order on the exchange.

        Responsibilities:
        - respect config['live_trading'] (simulate vs real)
        - send order to the exchange
        - store order id / txid in 'trades.txid' when trade_id is given
        - optionally poll final fill and update price/amount in DB
        - write descriptive log_event entries

        Returns a raw dict response.
        """
        raise NotImplementedError

