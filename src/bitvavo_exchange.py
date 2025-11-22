import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import time
import hmac
import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP  # ⬅ add ROUND_HALF_UP
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
from utils import _open_db
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
        
    # Cache: stores price & amount ticks per market
    _market_tick_cache: Dict[str, Dict[str, Decimal]] = {}

    def _load_market_ticks(self, market: str) -> Dict[str, Decimal]:
        """
        Load price tick and amount tick from Bitvavo /markets endpoint.
        Example return: {"price": Decimal("0.01"), "amount": Decimal("0.0001")}
        """
        market = market.upper()

        # Already cached
        if market in self._market_tick_cache:
            return self._market_tick_cache[market]

        try:
            r = requests.get(f"{BITVAVO_BASE}/markets", timeout=10).json()
        except Exception as e:
            log_event(f"⚠️ Bitvavo tick fetch failed: {e}")
            # Safe fallback
            ticks = {"price": Decimal("0.01"), "amount": Decimal("0.0001")}
            self._market_tick_cache[market] = ticks
            return ticks

        for info in r:
            if info.get("market", "").upper() == market:
                price_dec = info.get("priceDecimals", 2)
                amount_dec = info.get("amountDecimals", 4)

                ticks = {
                    "price": Decimal("1") / (Decimal("10")**Decimal(price_dec)),
                    "amount": Decimal("1") / (Decimal("10")**Decimal(amount_dec)),
                }

                self._market_tick_cache[market] = ticks
                return ticks

        # Fallback if market not found
        log_event(f"⚠️ Bitvavo: no tick info found for {market}, using defaults")
        ticks = {"price": Decimal("0.01"), "amount": Decimal("0.0001")}
        self._market_tick_cache[market] = ticks
        return ticks

    def _normalize_price(self, market: str, price: Decimal) -> Decimal:
        ticks = self._load_market_ticks(market)
        tick = ticks["price"]

        try:
            return price.quantize(tick, rounding=ROUND_HALF_UP)
        except Exception:
            log_event(f"⚠️ Bitvavo price rounding failed for {market} price={price}, tick={tick}")
            return price

    def _normalize_amount(self, market: str, amount: Decimal) -> Decimal:
        ticks = self._load_market_ticks(market)
        tick = ticks["amount"]

        try:
            return amount.quantize(tick, rounding=ROUND_HALF_UP)
        except Exception:
            log_event(f"⚠️ Bitvavo amount rounding failed for {market} amount={amount}, tick={tick}")
            return amount


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
        interval_map = {"1h": "1h", "4h": "4h", "6h": "6h", "1d": "1d", "1w": "1w"}
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
        path = f"order?orderId={order_id}&market={market}"
        res = self._private_request("GET", path)

        if not isinstance(res, dict) or "errorCode" in res:
            return None

        status = res.get("status", "")
        vol_exec = Decimal(res.get("filledAmount", "0"))

        # ---- Calculate actual filled price ----
        price = Decimal("0")

        # 1) If Bitvavo provides filledAmountQuote, use it
        filled_quote = res.get("filledAmountQuote")
        if filled_quote and vol_exec > 0:
            price = Decimal(filled_quote) / vol_exec

        # 2) Else, check if fills are provided
        fills = res.get("fills")
        if fills and isinstance(fills, list) and len(fills) > 0:
            total = Decimal("0")
            cost = Decimal("0")
            for f in fills:
                amt = Decimal(f.get("amount", "0"))
                p = Decimal(f.get("price", "0"))
                total += amt
                cost += amt * p
            if total > 0:
                price = cost / total

        return {"status": status, "vol_exec": vol_exec, "price": price}

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
        """
        Place a Bitvavo limit order.

        Behaviour:
        - TraderEngine already updates strategies.allocated_eur based on the *limit* price
          (as a reservation).
        - Here we:
            1) send the order to Bitvavo
            2) store the Bitvavo orderId into trades.txid
            3) fetch the actual fill (status, vol_exec, price)
            4) update trades.price / trades.amount to the actuals
            5) adjust strategies.allocated_eur by the difference between:
               reserved EUR vs. actual EUR.
        """
        cfg = current_config()
        live = cfg.get("live_trading", False)
        market = self._market(symbol)

        # Normalize price and amount
        price = self._normalize_price(market, price)
        volume = self._normalize_amount(market, volume)

        if not live:
            log_event(f"🧪 Simulated {side.upper()} {volume} {market} @ €{price} (Bitvavo)")
            return {"result": "simulated"}

        body = {
            "market": market,
            "side": side,
            "orderType": "limit",
            "price": str(price),
            "amount": str(volume),
            "operatorId": 1,
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
                conn = _open_db()
                conn.execute(
                    "UPDATE trades SET txid=? WHERE id=?",
                    (order_id, trade_id),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                log_event(f"⚠️ Could not store Bitvavo orderId in DB: {e}")

        # Wait for execution, then try to update trade with final fill
        time.sleep(12)
        detail = self._get_order_details(order_id, market)
        if not detail:
            log_event(f"⚠️ Bitvavo returned no order details for orderId={order_id}")
            return res

        log_event(f"📊 Bitvavo order status: {detail}")

        status = detail.get("status")
        actual_price = detail.get("price", Decimal("0"))
        actual_amount = detail.get("vol_exec", Decimal("0"))

        if (
            status in ("filled", "partiallyFilled")
            and actual_price > 0
            and actual_amount > 0
            and trade_id is not None
        ):
            # Reserved EUR that TraderEngine already used
            reserved_eur = price * volume

            # Actual EUR based on fill
            actual_eur = actual_price * actual_amount

            if side.lower() == "buy":
                delta = reserved_eur - actual_eur
            else:  # "sell"
                delta = actual_eur - reserved_eur

            try:
                conn = _open_db()
                cur = conn.cursor()

                # 1) Update the trade with actual fill price/amount
                cur.execute(
                    """
                    UPDATE trades
                    SET price = ?, amount = ?
                    WHERE id = ?
                    """,
                    (float(actual_price), float(actual_amount), trade_id),
                )

                # 2) Fetch strategy_id linked to this trade
                cur.execute(
                    "SELECT strategy_id FROM trades WHERE id = ?",
                    (trade_id,),
                )
                row = cur.fetchone()
                strategy_id = row[0] if row and row[0] is not None else None

                # 3) Adjust allocated_eur for that strategy by delta (if we know the strategy)
                if strategy_id is not None and delta != 0:
                    cur.execute(
                        """
                        UPDATE strategies
                        SET allocated_eur = allocated_eur + ?
                        WHERE id = ?
                        """,
                        (float(delta), strategy_id),
                    )
                    log_event(
                        f"🔁 Adjusted allocated_eur for strategy {strategy_id} on Bitvavo "
                        f"by €{delta:.2f} (reserved €{reserved_eur:.2f}, actual €{actual_eur:.2f})"
                    )

                conn.commit()
                conn.close()
            except Exception as e:
                log_event(f"⚠️ Could not update executed Bitvavo trade / allocation in DB: {e}")

            log_event(
                f"💾 Updated executed Bitvavo trade → "
                f"€{actual_price} × {actual_amount} "
                f"(was {price} × {volume}, trade_id={trade_id})"
            )

        return res

