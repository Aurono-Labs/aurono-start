# src/kraken_exchange.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import time
import base64
import hashlib
import hmac
import urllib.parse
from decimal import Decimal
from typing import Any, Dict, Optional, List

import requests
import sqlite3

from utils import (
    log_event,
    current_config,
    load_api_keys,
    to_decimal,
    get_db_path,
)
from trade_manager import TradeManager
from exchange_base import ExchangeBase

KRAKEN_API_PUBLIC = "https://api.kraken.com/0/public"
KRAKEN_API_PRIVATE = "https://api.kraken.com/0/private"


class KrakenExchange(ExchangeBase):
    """
    Kraken implementation for Aurono.

    - Symbols are Aurono-style like 'BTCEUR', 'NEAREUR'
    - Internally we pass these directly as 'pair' to Kraken (same as before)
    """
    name = "kraken"

    def __init__(self) -> None:
        self.api_key, self.api_secret = load_api_keys()
        self.tm = TradeManager(get_db_path())

    # -------------------- Public API --------------------

    def get_ticker(self, symbol: str) -> Decimal:
        """
        symbol: 'BTCEUR' (config pair)
        """
        pair = symbol.upper()
        try:
            r = requests.get(
                f"{KRAKEN_API_PUBLIC}/Ticker?pair={pair}",
                timeout=10
            ).json()
            k = list(r["result"].keys())[0]
            return to_decimal(r["result"][k]["c"][0])
        except Exception as e:
            log_event(f"⚠️ Kraken ticker error for {pair}: {e}")
            return Decimal("0")

    def get_ohlc(self, symbol: str, timeframe: str) -> List[list]:
        """
        symbol: 'BTCEUR'
        timeframe: '1h', '4h', '1d'
        """
        pair = symbol.upper()
        interval_map = {"1h": 60, "4h": 240, "1d": 1440}
        interval = interval_map.get(timeframe, 1440)

        try:
            r = requests.get(
                f"{KRAKEN_API_PUBLIC}/OHLC?pair={pair}&interval={interval}",
                timeout=10
            ).json()
            result = list(r["result"].values())[0]
            return result[-730:]
        except Exception as e:
            log_event(f"⚠️ Kraken OHLC error for {pair} ({timeframe}): {e}")
            return []

    # -------------------- Private API helper --------------------

    def _private_request(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Send a signed request to Kraken's private API (same signing logic as v2.2).
        endpoint: '/AddOrder', '/QueryOrders', etc.
        """
        data = data or {}
        data["nonce"] = str(int(time.time() * 1000))
        postdata = urllib.parse.urlencode(data)

        encoded = (data["nonce"] + postdata).encode()
        message = f"/0/private{endpoint}".encode() + hashlib.sha256(encoded).digest()

        signature = hmac.new(
            base64.b64decode(self.api_secret),
            message,
            hashlib.sha512
        )
        headers = {
            "API-Key": self.api_key,
            "API-Sign": base64.b64encode(signature.digest()),
        }

        try:
            r = requests.post(
                f"{KRAKEN_API_PRIVATE}{endpoint}",
                headers=headers,
                data=data,
                timeout=10,
            )
            res = r.json()
            if res.get("error"):
                log_event(f"⚠️ Kraken API error: {res['error']}")
            return res
        except Exception as e:
            log_event(f"❌ Kraken private API request failed: {e}")
            return {"error": [str(e)]}

    def _get_order_details(self, txid: str) -> Optional[Dict[str, Any]]:
        """
        Query Kraken for order details and return fill info.
        """
        res = self._private_request("/QueryOrders", {"txid": txid})
        if res.get("result"):
            info = list(res["result"].values())[0]
            price = Decimal(info.get("price", "0"))
            vol_exec = Decimal(info.get("vol_exec", "0"))
            status = info.get("status")
            descr = info.get("descr", {}).get("order", "")
            return {
                "price": price,
                "vol_exec": vol_exec,
                "status": status,
                "descr": descr,
            }
        return None

    # -------------------- Place limit order --------------------

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        volume: Decimal,
        trade_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Place a live Kraken order or simulate if live_trading=False.
        Symbol: 'BTCEUR', 'NEAREUR', etc.
        """
        cfg = current_config()
        live = cfg.get("live_trading", False)
        pair = symbol.upper()

        if not live:
            log_event(f"🧪 Simulated {side.upper()} {volume} {pair} @ €{price} (Kraken)")
            return {"result": "simulated"}

        data = {
            "pair": pair,
            "type": side,
            "ordertype": "limit",
            "price": str(price),
            "volume": str(volume),
        }

        res = self._private_request("/AddOrder", data)

        if "txid" not in res.get("result", {}):
            log_event(f"⚠️ Kraken order failed → {res}")
            return res

        txid = res["result"]["txid"][0]
        log_event(f"📤 Sent {side.upper()} order → Kraken TXID: {txid}")

        # Attach TXID to DB record
        if trade_id:
            try:
                conn = sqlite3.connect(self.tm.db_path)
                conn.execute(
                    "UPDATE trades SET txid=? WHERE id=?",
                    (txid, trade_id),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                log_event(f"⚠️ Could not store Kraken TXID in DB: {e}")

        # Wait for execution, then try to update trade with final fill
        time.sleep(6)
        detail = self._get_order_details(txid)
        if not detail:
            return res

        log_event(f"📊 Order status: {detail['status']} ({detail['descr']})")

        if (
            detail["status"] == "closed"
            and detail["price"] > 0
            and detail["vol_exec"] > 0
            and trade_id is not None
        ):
            try:
                conn = sqlite3.connect(self.tm.db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE trades
                    SET price = ?, amount = ?
                    WHERE id = ?
                    """,
                    (float(detail["price"]), float(detail["vol_exec"]), trade_id),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                log_event(f"⚠️ Could not update executed Kraken trade in DB: {e}")

            log_event(
                f"💾 Updated executed Kraken trade → "
                f"€{detail['price']} × {detail['vol_exec']} "
                f"(was {price} × {volume}, trade_id={trade_id})"
            )

        return res

