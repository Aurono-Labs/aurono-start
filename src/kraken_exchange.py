import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import time
import base64
import hashlib
import hmac
import urllib.parse
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
    get_credentials_for_exchange,
)
from utils import _open_db
from trade_manager import TradeManager
from exchange_base import ExchangeBase

KRAKEN_API_PUBLIC = "https://api.kraken.com/0/public"
KRAKEN_API_PRIVATE = "https://api.kraken.com/0/private"

class KrakenExchange(ExchangeBase):
    """
    Kraken implementation for Aurono.

    - Symbols are Aurono-style like 'BTCEUR', 'NEAREUR'
    - Internally we pass these directly as 'pair' to Kraken
    """
    name = "kraken"

    def __init__(self, api_key: str | None = None, api_secret: str | None = None) -> None:
        # Allow overriding keys (used by Settings "Test" button)
        if api_key and api_secret:
            self.api_key = api_key
            self.api_secret = api_secret
        else:
            # Trader mode → ALWAYS load encrypted Kraken keys from the credentials DB
            self.api_key, self.api_secret = get_credentials_for_exchange("kraken")

        self.tm = TradeManager(get_db_path())

        # --- DEFAULT FEE RATE (new) ---
        self.fee_rate = Decimal("0.0025")

    # Cache for Kraken tick sizes: {"BTCEUR": Decimal("0.1"), ... }
    _tick_cache: Dict[str, Dict[str, Decimal]] = {}
    
    def _to_kraken_pair(self, symbol: str) -> str:
        symbol = symbol.upper()

        # BTC
        if symbol == "BTCEUR":
            return "XBTEUR"

        # DOGE
        if symbol == "DOGEEUR":
            return "XDGEUR"

        return symbol

    def _load_tick_size(self, symbol: str) -> Dict[str, Decimal]:
        symbol = self._to_kraken_pair(symbol)

        if symbol in self._tick_cache:
            return self._tick_cache[symbol]

        fallback = {
            "price": Decimal("0.01"),
            "amount": Decimal("0.0001")
        }

        try:
            r = requests.get(f"{KRAKEN_API_PUBLIC}/AssetPairs").json()
            pairs = r.get("result", {})
        except Exception as e:
            log_event(f"⚠️ Kraken tick-size fetch failed: {e}")
            self._tick_cache[symbol] = fallback
            return fallback

        for pair_name, info in pairs.items():
            altname = info.get("altname", "").upper()
            if altname == symbol:
                price_dec = info.get("pair_decimals", 2)
                price_tick = Decimal("1") / (Decimal("10") ** Decimal(price_dec))

                lot_dec = info.get("lot_decimals", 4)
                amount_tick = Decimal("1") / (Decimal("10") ** Decimal(lot_dec))

                result = {
                    "price": price_tick,
                    "amount": amount_tick
                }
                self._tick_cache[symbol] = result
                return result

        log_event(f"⚠️ Kraken: no tick size found for {symbol}, using defaults")
        self._tick_cache[symbol] = fallback
        return fallback

    def _normalize_amount(self, symbol: str, amount: Decimal) -> Decimal:
        ticks = self._load_tick_size(symbol)
        tick = ticks["amount"]

        try:
            return amount.quantize(tick, rounding=ROUND_HALF_UP)
        except Exception:
            log_event(f"⚠️ Kraken amount rounding failed for {symbol}, tick={tick}, amount={amount}")
            return amount

    # -------------------- Public API --------------------

    def get_ticker(self, symbol: str) -> Decimal:
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
        pair = symbol.upper()
        interval_map = {"1h": 60, "4h": 240, "6h": 360, "1d": 1440, "1w": 10080}
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
        res = self._private_request("/QueryOrders", {"txid": txid})
        if res.get("result"):
            info = list(res["result"].values())[0]
            price = Decimal(info.get("price", "0"))
            vol_exec = Decimal(info.get("vol_exec", "0"))
            status = info.get("status")
            descr = info.get("descr", {}).get("order", "")

            try:
                cost = Decimal(info.get("cost", "0"))
                fee = Decimal(info.get("fee", "0"))

                if cost > 0 and fee >= 0:
                    fee_rate = (fee / cost).quantize(Decimal("0.00001"))
                    self.fee_rate = fee_rate
                    log_event(
                        f"ℹ️ Kraken effective fee updated → cost={cost}, fee={fee}, rate={fee_rate}"
                    )
            except Exception as e:
                log_event(f"⚠️ Kraken fee calculation failed: {e}")

            return {
                "price": price,
                "vol_exec": vol_exec,
                "status": status,
                "descr": descr,
            }
        return None
        
    # ----------------------------------------
    # Get available EUR balance
    # ----------------------------------------
    
    def get_available_eur(self) -> float:
        """
        Return the available EUR balance from Kraken.
        Uses private endpoint /Balance
        """
        try:
            # MUST START WITH A SLASH
            data = self._private_request("/Balance", {})

            if not isinstance(data, dict) or "result" not in data:
                log_event(f"⚠️ Kraken get_available_eur: unexpected response {data}")
                return 0.0

            result = data["result"]

            if "ZEUR" in result:
                return float(result["ZEUR"])

            # Rare alternative
            if "EUR" in result:
                return float(result["EUR"])

            return 0.0

        except Exception as e:
            log_event(f"⚠️ Kraken EUR balance error: {e}")
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
        pair = self._to_kraken_pair(symbol)

        ticks = self._load_tick_size(symbol)
        tick = ticks["price"]

        if side.lower() == "buy":
            price = price.quantize(tick, rounding=ROUND_UP)
        else:
            price = price.quantize(tick, rounding=ROUND_DOWN)

        volume = self._normalize_amount(symbol, volume)

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

        if trade_id:
            try:
                conn = _open_db()
                conn.execute(
                    "UPDATE trades SET txid=? WHERE id=?",
                    (txid, trade_id),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                log_event(f"⚠️ Could not store Kraken TXID in DB: {e}")

        time.sleep(12)
        detail = self._get_order_details(txid)
        if not detail:
            log_event(f"⚠️ Kraken returned no order details for TXID={txid}")
            return res

        log_event(f"📊 Order status: {detail['status']} ({detail['descr']})")

        if (
            detail["status"] == "closed"
            and detail["price"] > 0
            and detail["vol_exec"] > 0
            and trade_id is not None
        ):
            actual_price = detail["price"]
            actual_amount = detail["vol_exec"]

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
                log_event(f"⚠️ Kraken: could not load original reserved values: {e}")
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
                        f"🔁 Adjusted allocated_eur for strategy {strategy_id} on Kraken "
                        f"by €{delta:.2f} (reserved €{reserved_eur:.2f}, actual €{actual_eur:.2f})"
                    )

                conn.commit()
                conn.close()

            except Exception as e:
                log_event(f"⚠️ Could not update executed Kraken trade / allocation in DB: {e}")

            log_event(
                f"💾 Updated executed Kraken trade → "
                f"€{actual_price} × {actual_amount} "
                f"(was {price} × {volume}, trade_id={trade_id})"
            )

            final_alloc_str = f", new alloc €{new_alloc:.2f}" if new_alloc is not None else ""

            log_event(
                f"✅ {side.upper()} executed: {actual_amount} {symbol} @ €{actual_price} "
                f"on kraken (actual €{actual_eur:.2f}, reserved €{reserved_eur:.2f}"
                f"{final_alloc_str})"
            )

        return res

