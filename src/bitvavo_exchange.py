import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import time
import hmac
import hashlib
import json
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
    get_credentials_for_exchange
)
from trade_manager import TradeManager
from exchange_base import ExchangeBase

BITVAVO_BASE = "https://api.bitvavo.com/v2"

class BitvavoExchange(ExchangeBase):
    """
    Bitvavo implementation for Aurono.

    - Aurono symbol: 'BTCEUR' → Bitvavo market: 'BTC-EUR'
    - Uses REST API authentication per official Bitvavo docs
    """
    name = "bitvavo"

    def __init__(self, api_key: str | None = None, api_secret: str | None = None) -> None:
        # Allow overriding keys (used by Settings "Test" button)
        if api_key and api_secret:
            self.api_key = api_key
            self.api_secret = api_secret
        else:
            self.api_key, self.api_secret = get_credentials_for_exchange("bitvavo")

        self.tm = TradeManager(get_db_path())

    # ----------------------------------------
    # Helpers
    # ----------------------------------------

    def _market(self, symbol: str) -> str:
        """
        Convert 'BTCEUR' → 'BTC-EUR', 'SOLEUR' → 'SOL-EUR', etc.
        """
        p = symbol.replace("/", "").upper()
        if p.endswith("EUR"):
            return f"{p[:-3]}-EUR"
        return p

    # ----------------------------------------
    # Public API
    # ----------------------------------------
    
    def get_ticker(self, symbol: str) -> Decimal:
        market = self._market(symbol)
        try:
            r = requests.get(
                f"{BITVAVO_BASE}/ticker/price?market={market}",
                timeout=10,
            ).json()
            return to_decimal(r["price"])
        except Exception as e:
            log_event(f"⚠️ Bitvavo ticker error for {market}: {e}")
            return Decimal("0")

    def get_ohlc(self, symbol: str, timeframe: str, limit: int = 730) -> List[list]:
        """
        Fetch OHLC candlestick data from Bitvavo.
        
        Bitvavo returns: [timestamp, open, high, low, close, volume]
        Data is returned newest → oldest, so we reverse it to oldest → newest.
        
        Args:
            symbol: Trading pair (e.g., 'BTCEUR')
            timeframe: Timeframe ('1h', '4h', '1d')
            limit: Number of candles to return (default: 730, max: 1440)
        
        Returns:
            List of OHLC data in chronological order (oldest → newest)
        """
        market = self._market(symbol)
        interval_map = {"1h": "1h", "4h": "4h", "1d": "1d"}
        interval = interval_map.get(timeframe, "1d")
        
        # Get current time to ensure we fetch the most recent candles
        # Use 'end' parameter to get candles up to now, otherwise Bitvavo
        # may return very old historical data
        import time
        end_timestamp = int(time.time() * 1000)  # Current time in milliseconds
        
        # Bitvavo endpoint: GET /v2/{market}/candles
        # IMPORTANT: Use 'end' parameter to get recent data, not ancient history
        url = (
            f"{BITVAVO_BASE}/{market}/candles"
            f"?market={market}&interval={interval}&limit={min(limit, 1440)}&end={end_timestamp}"
        )
        
        try:
            resp = requests.get(
                url,
                timeout=10,
                headers={"Content-Type": "application/json"},
            )
            
            try:
                data = resp.json()
            except ValueError:
                raw = resp.text.strip()
                log_event(f"⚠️ Bitvavo OHLC non-JSON for {market} ({timeframe}): {raw[:200]}...")
                return []
            
            if not isinstance(data, list):
                log_event(f"⚠️ Bitvavo OHLC unexpected JSON format for {market}: {data}")
                return []
            
            if len(data) == 0:
                log_event(f"⚠️ Bitvavo OHLC returned empty list for {market}")
                return []
            
            # Bitvavo returns newest → oldest, reverse to get oldest → newest
            # This ensures data[0] is the oldest candle and data[-1] is most recent
            data.reverse()
            
            # === DEBUG: Print last candles ===
            try:
                import datetime

                def fmt(ts):
                    return datetime.datetime.utcfromtimestamp(ts/1000).strftime("%Y-%m-%d %H:%M:%S")

                last = data[-1]
                prev = data[-2]

                print("📌 Active candle (ignored):")
                print(f"  timestamp = {fmt(last[0])}")
                print(f"  open={last[1]}, close={last[4]}")

                print("📌 Previous closed candle (USED):")
                print(f"  timestamp = {fmt(prev[0])}")
                print(f"  open={prev[1]}, close={prev[4]}")

            except Exception as e:
                print("DEBUG ERROR (cannot print candles):", e)

            
            return data
        
        except Exception as e:
            log_event(f"⚠️ Bitvavo OHLC request failed for {market}: {e}")
            return []

    # ----------------------------------------
    # Private / signed request (REST API)
    # ----------------------------------------

    def _private_request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Generate Bitvavo REST API signature.

        REST signature = HMAC_SHA256(secret, timestamp + METHOD + "/v2/" + endpoint + body)

        Where:
        - endpoint = "order", "balance", "market", etc. (NO leading /v2/)
        - body = canonical JSON or "" for GET
        - secret = raw bytes from the API secret
        - query parameters are removed from the signature but included in URL
        """
       
        # Canonical JSON body (no spaces) - empty string for GET
        if body is None:
            body_json = ""
        else:
            body_json = json.dumps(body, separators=(",", ":"))

        timestamp = str(int(time.time() * 1000))

        signature_path = path

        # Build the signature string: timestamp + METHOD + /v2/path + body
        # Note: path MUST include /v2/ prefix as per official Bitvavo docs
        prehash = timestamp + method.upper() + "/v2/" + signature_path + body_json

        # Use raw secret bytes (REST API, not WebSocket)
        secret_bytes = self.api_secret.encode("utf-8")

        signature = hmac.new(
            secret_bytes,
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        headers = {
            "Bitvavo-Access-Key": self.api_key,
            "Bitvavo-Access-Signature": signature,
            "Bitvavo-Access-Timestamp": timestamp,
            "Bitvavo-Access-Window": "60000",
            "Content-Type": "application/json",
        }

        url = BITVAVO_BASE + "/" + path

        try:
            if method.upper() == "POST":
                resp = requests.post(url, data=body_json, headers=headers, timeout=10)
            else:
                resp = requests.get(url, headers=headers, timeout=10)

            result = resp.json()
            
            # Log errors for debugging
            if "errorCode" in result:
                log_event(
                    f"⚠️ Bitvavo API error {result.get('errorCode')}: {result.get('error')} "
                    f"(method={method}, path=/v2/{path})"
                )
            
            return result
        except Exception as e:
            log_event(f"❌ Bitvavo private request failed: {e}")
            return {"error": str(e)}

    def _get_order_details(self, order_id: str, market: str) -> Optional[Dict[str, Any]]:
        """
        GET /order?orderId=...&market=BTC-EUR
        """
        path = f"order?orderId={order_id}&market={market}"
        res = self._private_request("GET", path)

        if "errorCode" in res:
            return None

        try:
            status = res.get("status")
            vol_exec = Decimal(res.get("filledAmount", "0"))
            price = Decimal(res.get("filledPrice", "0"))
            return {"status": status, "vol_exec": vol_exec, "price": price}
        except Exception:
            return None

    # ----------------------------------------
    # Place limit order
    # ----------------------------------------

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        volume: Decimal,
        trade_id: Optional[int] = None
    ) -> Dict[str, Any]:
        cfg = current_config()
        live = cfg.get("live_trading", False)
        market = self._market(symbol)

        if not live:
            log_event(f"🧪 Simulated {side.upper()} {volume} {market} @ €{price} (Bitvavo)")
            return {"result": "simulated"}

        body = {
            "market": market,
            "side": side,
            "orderType": "limit",
            "price": str(price),
            "amount": str(volume),
            "operatorId": 1    # 🔥 REQUIRED FIX
        }

        res = self._private_request("POST", "order", body)

        if "orderId" not in res:
            log_event(f"⚠️ Bitvavo order failed → {res}")
            return res

        order_id = res["orderId"]
        log_event(f"📤 Sent {side.upper()} order → Bitvavo orderId: {order_id}")

        # Attach orderId to DB record
        if trade_id:
            try:
                conn = sqlite3.connect(self.tm.db_path)
                conn.execute(
                    "UPDATE trades SET txid=? WHERE id=?",
                    (order_id, trade_id),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                log_event(f"⚠️ Could not store Bitvavo orderId in DB: {e}")

        # Wait for execution, then try to update trade with final fill
        time.sleep(6)
        detail = self._get_order_details(order_id, market)
        if not detail:
            return res

        log_event(f"📊 Bitvavo order status: {detail}")

        if (
            detail["status"] in ("filled", "partiallyFilled")
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
                log_event(f"⚠️ Could not update executed Bitvavo trade in DB: {e}")

            log_event(
                f"💾 Updated executed Bitvavo trade → "
                f"€{detail['price']} × {detail['vol_exec']} "
                f"(was {price} × {volume}, trade_id={trade_id})"
            )

        return res
