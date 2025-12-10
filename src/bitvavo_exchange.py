import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import time
import hmac
import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP, ROUND_DOWN
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

        # --- DEFAULT FEE RATE (new) ---
        self.fee_rate = Decimal("0.0025")

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
        Load correct price & amount ticks based on Bitvavo /markets.

        Bitvavo fields (as of v2.8+ in 2025):
          - tickSize (string)          → actual price increment (NEW, replaces pricePrecision)
          - quantityDecimals (int)     → number of decimals for amount
          - pricePrecision (int)       → DEPRECATED (often null now)
        
        Examples:
          - BTC-EUR: tickSize="1" (only whole euros)
          - SOL-EUR: tickSize="0.01" (cent precision)
          - VELO-EUR: tickSize="0.00001" (5 decimals)
        """
        market = market.upper()

        if market in self._market_tick_cache:
            return self._market_tick_cache[market]

        # Fetch markets list
        try:
            resp = requests.get(f"{BITVAVO_BASE}/markets", timeout=10)
            data = resp.json()
        except Exception as e:
            log_event(f"⚠️ Bitvavo tick fetch failed: {e}")
            ticks = {"price": Decimal("0.01"), "amount": Decimal("0.0001")}
            self._market_tick_cache[market] = ticks
            return ticks

        if not isinstance(data, list):
            log_event(f"⚠️ Bitvavo /markets unexpected JSON: {data}")
            ticks = {"price": Decimal("0.01"), "amount": Decimal("0.0001")}
            self._market_tick_cache[market] = ticks
            return ticks

        for info in data:
            if not isinstance(info, dict):
                continue

            if info.get("market", "").upper() != market:
                continue

            # =====================================================
            # PRICE TICK: Use new tickSize field (preferred)
            # =====================================================
            price_tick = None
            tick_size_str = info.get("tickSize")
            
            if tick_size_str:
                try:
                    price_tick = Decimal(str(tick_size_str))
                    log_event(f"ℹ️ Bitvavo {market}: using tickSize={price_tick}")
                except Exception as e:
                    log_event(f"⚠️ Bitvavo {market}: invalid tickSize '{tick_size_str}': {e}")
                    price_tick = None

            # Fallback to old pricePrecision (if tickSize missing/invalid)
            if price_tick is None:
                price_prec = info.get("pricePrecision")
                try:
                    price_prec = int(price_prec) if price_prec is not None else 2
                except Exception:
                    price_prec = 2
                price_tick = Decimal("1") / (Decimal("10") ** price_prec)
                log_event(f"ℹ️ Bitvavo {market}: fallback pricePrecision={price_prec} → tick={price_tick}")
                
            # =====================================================
            # AMOUNT TICK: Use quantityDecimals (0 must be respected!)
            # =====================================================
            qty_prec = info.get("quantityDecimals")
            if qty_prec is None:
                qty_prec = info.get("quantityPrecision")

            try:
                qty_prec = int(qty_prec) if qty_prec is not None else 4
            except Exception:
                qty_prec = 4

            amount_tick = Decimal("1") / (Decimal("10") ** qty_prec)

            ticks = {
                "price": price_tick,
                "amount": amount_tick
            }

            self._market_tick_cache[market] = ticks
            log_event(f"ℹ️ Bitvavo ticks for {market}: {ticks}")
            return ticks

        # Fallback if market not found
        log_event(f"⚠️ Bitvavo: no tick info found for {market}, using defaults")
        ticks = {"price": Decimal("0.01"), "amount": Decimal("0.0001")}
        self._market_tick_cache[market] = ticks
        return ticks
        
    def _normalize_amount(self, market: str, amount: Decimal) -> Decimal:
        """
        Robust amount normalization for Bitvavo.
        - Floors to the correct tickSize (quantityDecimals)
        - Prevents scientific notation
        - Ensures exact decimal output
        """
        ticks = self._load_market_ticks(market)
        tick = ticks["amount"]

        try:
            if tick <= 0:
                return amount

            # Floor to tick units
            units = (amount / tick).quantize(Decimal("0"), rounding=ROUND_DOWN)
            normalized = units * tick

            # Prevent scientific notation and hidden precision
            normalized_str = format(normalized, "f")

            return Decimal(normalized_str)

        except Exception as e:
            log_event(
                f"⚠️ Bitvavo amount rounding failed for {market} "
                f"amount={amount}, tick={tick}, error={e}"
            )
            return amount

    def _normalize_price(self, market: str, side: str, price: Decimal) -> Decimal:
        """
        Snap price to the nearest valid tick:
        - BUY  → round UP (so price is not too low)
        - SELL → round DOWN (so price is not too high)
        """
        ticks = self._load_market_ticks(market)
        tick = ticks["price"]

        try:
            if tick <= 0:
                return price

            if side.lower() == "buy":
                units = (price / tick).quantize(Decimal("0"), rounding=ROUND_UP)
            else:
                units = (price / tick).quantize(Decimal("0"), rounding=ROUND_DOWN)

            norm = units * tick
            return norm
        except Exception:
            log_event(f"⚠️ Bitvavo price rounding failed for {market} price={price}, tick={tick}")
            return price

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
        """
        market = self._market(symbol)
        interval_map = {"1h": "1h", "4h": "4h", "1d": "1d", "1w": "1w"}
        interval = interval_map.get(timeframe, "1d")

        end_timestamp = int(time.time() * 1000)

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

            data.reverse()

            return data

        except Exception as e:
            log_event(f"⚠️ Bitvavo OHLC request failed for {market}: {e}")
            return []

    # ----------------------------------------
    # Private / signed request
    # ----------------------------------------

    def _private_request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if body is None:
            body_json = ""
        else:
            body_json = json.dumps(body, separators=(",", ":"))

        timestamp = str(int(time.time() * 1000))
        signature_path = path

        prehash = timestamp + method.upper() + "/v2/" + signature_path + body_json

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

        price = Decimal("0")

        filled_quote = res.get("filledAmountQuote")
        if filled_quote and vol_exec > 0:
            price = Decimal(filled_quote) / vol_exec

        fills = res.get("fills")
        if fills and isinstance(fills, list) and len(fills) > 0:
            total = Decimal("0")
            cost = Decimal("0")
            total_fee = Decimal("0")
            for f in fills:
                amt = Decimal(f.get("amount", "0"))
                p = Decimal(f.get("price", "0"))
                fee = Decimal(f.get("fee", "0"))
                total += amt
                cost += amt * p
                total_fee += fee
            if total > 0:
                price = cost / total

                try:
                    self.fee_rate = (total_fee / cost).quantize(Decimal("0.00001"))
                    log_event(f"ℹ️ Bitvavo effective fee updated to {self.fee_rate}")
                except Exception:
                    pass

        return {"status": status, "vol_exec": vol_exec, "price": price}
        
    # ----------------------------------------
    # Get available EUR balance
    # ----------------------------------------
        
    def get_available_eur(self) -> float:
        """
        Return the available EUR balance from Bitvavo.
        Uses the private /balance endpoint.
        """
        try:
            result = self._private_request("GET", "balance")

            if not isinstance(result, list):
                log_event(f"⚠️ Bitvavo get_available_eur: unexpected response {result}")
                return 0.0

            for item in result:
                if item.get("symbol") == "EUR":
                    # Bitvavo returns strings → convert to float
                    return float(item.get("available", "0"))

            return 0.0

        except Exception as e:
            log_event(f"⚠️ Bitvavo EUR balance error: {e}")
            return 0.0


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

        price = self._normalize_price(market, side, to_decimal(price))
        volume = self._normalize_amount(market, to_decimal(volume))

        log_event(f"📏 Bitvavo normalized {side.upper()} for {market}: price={price}, amount={volume}")

        if not live:
            log_event(f"🧪 Simulated {side.upper()} {volume} {market} @ €{price} (Bitvavo)")
            return {"result": "simulated"}

        body = {
            "market": market,
            "side": side,
            "orderType": "limit",
            "price": format(price, "f"),
            "amount": format(volume, "f"),
            "operatorId": 1,
        }

        res = self._private_request("POST", "order", body)

        if "orderId" not in res:
            log_event(f"⚠️ Bitvavo order failed → {res}")
            return res

        order_id = res["orderId"]
        log_event(f"📤 Sent {side.upper()} order → Bitvavo orderId: {order_id}")

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
            try:
                conn = _open_db()
                cur = conn.cursor()
                cur.execute(
                    "SELECT price, amount FROM trades WHERE id=?",
                    (trade_id,),
                )
                row = cur.fetchone()
                conn.close()

                orig_price = Decimal(str(row[0]))
                orig_amount = Decimal(str(row[1]))
                reserved_eur = (orig_price * orig_amount).quantize(Decimal("0.01"))

            except Exception as e:
                log_event(f"⚠️ Bitvavo: could not load original reserved values: {e}")
                reserved_eur = (price * volume).quantize(Decimal("0.01"))

            actual_eur = (actual_price * actual_amount).quantize(Decimal("0.01"))

            if side.lower() == "buy":
                delta = reserved_eur - actual_eur
            else:
                delta = actual_eur - reserved_eur

            try:
                conn = _open_db()
                cur = conn.cursor()

                cur.execute(
                    """
                    UPDATE trades
                    SET price = ?, amount = ?
                    WHERE id = ?
                    """,
                    (float(actual_price), float(actual_amount), trade_id),
                )

                cur.execute(
                    "SELECT strategy_id FROM trades WHERE id = ?",
                    (trade_id,),
                )
                row = cur.fetchone()
                strategy_id = row[0] if row and row[0] is not None else None

                new_alloc = None
                if strategy_id is not None and delta != 0:
                    cur.execute(
                        """
                        UPDATE strategies
                        SET allocated_eur = allocated_eur + ?
                        WHERE id = ?
                        """,
                        (float(delta), strategy_id),
                    )

                    cur.execute(
                        "SELECT allocated_eur FROM strategies WHERE id = ?",
                        (strategy_id,),
                    )
                    row2 = cur.fetchone()
                    if row2:
                        new_alloc = float(row2[0])

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

            final_alloc_str = f", new alloc €{new_alloc:.2f}" if new_alloc is not None else ""

            log_event(
                f"✅ {side.upper()} executed: {actual_amount} {symbol} @ €{actual_price} "
                f"on bitvavo (actual €{actual_eur:.2f}, reserved €{reserved_eur:.2f}"
                f"{final_alloc_str})"
            )

        return res

