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
        """Return latest trade price for given symbol (e.g. 'BTCEUR') as Decimal."""
        raise NotImplementedError

    @abstractmethod
    def get_ohlc(self, symbol: str, timeframe: str) -> List[list]:
        """Return OHLC candles where [_, open, _, _, close, ...]."""
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
        Must handle simulation mode and log events.
        """
        raise NotImplementedError

    # ─────────────────────────────────────────────────────────
    # NEW: required method for EUR balance (Dashboard feature)
    # ─────────────────────────────────────────────────────────
    @abstractmethod
    def get_available_eur(self) -> float:
        """
        Return the available EUR balance on the exchange.

        Must return the EUR amount that can actually be used for new trades.
        """
        raise NotImplementedError
