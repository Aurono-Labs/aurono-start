import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import time
import hmac
import hashlib
import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_UP, ROUND_DOWN
from typing import Any, Dict, Optional, List, Tuple

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

# Coinbase Advanced Trade API base
COINBASE_API_BASE = "https://api.coinbase.com"
COINBASE_API_PREFIX = "/api/v3/brokerage"


class CoinbaseExchange(ExchangeBase):
    """
    Coinbase implementation for Aurono (Advanced Trade API).

    - Aurono symbol: 'BTCEUR' → Coinbase product: 'BTC-EUR'
    - Supports 2 auth methods:
        * HMAC legacy keys      (auth_method = 'hmac')
        * CDP JWT (ECDSA P-256) (auth_method = 'jwt')
    - Fully compatible with Aurono TraderEngine interface:
        * get_ticker
        * get_ohlc (1h, 4h, 6h, 1d, 1w)
        * get_available_eur
        * place_limit_order
    """
    name = "coinbase"

    # Cache: stores price & amount ticks per product
    _product_tick_cache: Dict[str, Dict[str, Decimal]] = {}
    
    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        auth_method: str | None = None,
        api_key_name: str | None = None,
        ec_private_key_pem: str | None = None,
    ) -> None:
        """
        Coinbase credentials are stored as JSON inside api_secret for maximum flexibility:

        {
          "auth_method": "hmac" | "jwt",
          "hmac_key": "...",
          "hmac_secret": "...",
          "jwt_key_name": "organizations/{org}/apiKeys/{key}",
          "jwt_private_key": "-----BEGIN EC PRIVATE KEY----- ... -----END EC PRIVATE KEY-----"
        }

        Direct override (api_key, api_secret) is allowed for Settings → Test button.
        """
        # ---- Default fee rate (auto-adjusted by fills later) ----
        self.fee_rate = Decimal("0.0025")

        # ---- TradingManager ----
        self.tm = TradeManager(get_db_path())

        # ------------------------------------------------------
        # INITIALIZE EMPTY CREDENTIAL FIELDS
        # ------------------------------------------------------
        self.auth_method = auth_method or "hmac"
        self.hmac_api_key: str | None = None
        self.hmac_api_secret: str | None = None
        self.jwt_key_name: str | None = None
        self.jwt_private_pem: str | None = None

        # ------------------------------------------------------
        # 1. DIRECT OVERRIDES (used by Settings → Test Coinbase)
        # ------------------------------------------------------
        if api_key is not None or api_secret is not None:
            # Treat overrides as simple HMAC credentials
            self.auth_method = auth_method or "hmac"
            self.hmac_api_key = api_key
            self.hmac_api_secret = api_secret
            log_event("ℹ️ CoinbaseExchange: using direct override credentials (likely from Test button).")

        else:
            # --------------------------------------------------
            # 2. LOAD CREDENTIALS FROM DATABASE (JSON for Coinbase)
            # --------------------------------------------------
            key, secret = get_credentials_for_exchange("coinbase")

            if key is None and secret is None:
                log_event("⚠️ CoinbaseExchange: no stored credentials found.")
            else:
                # secret contains JSON
                try:
                    cfg = json.loads(secret or "{}")
                except Exception as e:
                    cfg = {}
                    log_event(f"⚠️ CoinbaseExchange: credentials JSON malformed: {e}")

                # Extract authentication method
                self.auth_method = cfg.get("auth_method", "hmac")

                # HMAC
                self.hmac_api_key = cfg.get("hmac_key")
                self.hmac_api_secret = cfg.get("hmac_secret")

                # JWT
                self.jwt_key_name = cfg.get("jwt_key_name")
                self.jwt_private_pem = cfg.get("jwt_private_key")

        # ------------------------------------------------------
        # 3. CONSTRUCTOR OVERRIDES ALWAYS WIN
        # ------------------------------------------------------
        if api_key_name:
            self.jwt_key_name = api_key_name
        if ec_private_key_pem:
            self.jwt_private_pem = ec_private_key_pem

        # ------------------------------------------------------
        # 4. Final log summary
        # ------------------------------------------------------
        log_event(
            f"ℹ️ CoinbaseExchange initialized "
            f"(auth_method={self.auth_method}, "
            f"HMAC={bool(self.hmac_api_key and self.hmac_api_secret)}, "
            f"JWT={bool(self.jwt_key_name and self.jwt_private_pem)})"
        )

    # ============================================================
    # Helpers: symbol mapping, time, aggregation
    # ============================================================

    def _product_id(self, symbol: str) -> str:
        """
        Convert Aurono symbol 'BTCEUR' → Coinbase product 'BTC-EUR'.
        """
        p = symbol.replace("/", "").upper()
        if p.endswith("EUR"):
            return f"{p[:-3]}-EUR"
        return p

    @staticmethod
    def _to_iso8601(ts: int) -> str:
        """
        Convert UNIX seconds to RFC3339 / ISO8601 string.
        Coinbase expects timestamps like '2024-01-01T00:00:00Z'.
        """
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _group_start(timestamp_s: int, group_seconds: int) -> int:
        """
        Align timestamp (seconds) to the start of the timeframe group.
        Example:
            group_seconds = 4 * 3600  (4h)
            returns timestamp aligned to 00:00, 04:00, 08:00, ...
        """
        return timestamp_s - (timestamp_s % group_seconds)

    def _aggregate_candles(self, candles: List[List[Any]], group_seconds: int) -> List[List[Any]]:
        """
        Generic aggregation:
            input:  [ [ts_ms, open, high, low, close, volume], ... ]
            output: [ [ts_ms_group_start, open, high, low, close, volume], ... ]
        Assumes input is sorted oldest → newest.
        """
        if not candles:
            return []

        groups: Dict[int, Dict[str, Any]] = {}
        for c in candles:
            try:
                ts_ms = int(c[0])
                ts_s = ts_ms // 1000
                group_start_s = self._group_start(ts_s, group_seconds)
                group_ts_ms = group_start_s * 1000

                o = Decimal(str(c[1]))
                h = Decimal(str(c[2]))
                l = Decimal(str(c[3]))
                cl = Decimal(str(c[4]))
                v = Decimal(str(c[5]))

                g = groups.get(group_ts_ms)
                if g is None:
                    groups[group_ts_ms] = {
                        "open": o,
                        "high": h,
                        "low": l,
                        "close": cl,
                        "volume": v,
                    }
                else:
                    g["high"] = max(g["high"], h)
                    g["low"] = min(g["low"], l)
                    g["close"] = cl
                    g["volume"] += v
            except Exception as e:
                log_event(f"⚠️ Coinbase aggregate_candles: failed on {c}: {e}")
                continue

        result: List[List[Any]] = []
        for ts in sorted(groups.keys()):
            g = groups[ts]
            result.append([
                ts,
                float(g["open"]),
                float(g["high"]),
                float(g["low"]),
                float(g["close"]),
                float(g["volume"]),
            ])

        return result

    # ============================================================
    # Ticks: price & amount increments
    # ============================================================

    def _load_product_ticks(self, product_id: str) -> Dict[str, Decimal]:
        """
        Load correct price & amount ticks based on Coinbase /products.

        Coinbase fields:
          - base_increment  → size/amount increment
          - quote_increment → price increment
        """
        product_id = product_id.upper()
        if product_id in self._product_tick_cache:
            return self._product_tick_cache[product_id]

        try:
            # /products endpoint lists all, but for simplicity we fetch the single product
            url = f"{COINBASE_API_BASE}{COINBASE_API_PREFIX}/products/{product_id}"
            resp = requests.get(url, timeout=10)
            data = resp.json()
        except Exception as e:
            log_event(f"⚠️ Coinbase tick fetch failed for {product_id}: {e}")
            ticks = {"price": Decimal("0.01"), "amount": Decimal("0.0001")}
            self._product_tick_cache[product_id] = ticks
            return ticks

        if not isinstance(data, dict):
            log_event(f"⚠️ Coinbase /products/{product_id} unexpected JSON: {data}")
            ticks = {"price": Decimal("0.01"), "amount": Decimal("0.0001")}
            self._product_tick_cache[product_id] = ticks
            return ticks

        try:
            base_inc = data.get("base_increment", "0.0001")
            quote_inc = data.get("quote_increment", "0.01")

            amount_tick = Decimal(str(base_inc))
            price_tick = Decimal(str(quote_inc))

            ticks = {"price": price_tick, "amount": amount_tick}
            self._product_tick_cache[product_id] = ticks
            log_event(f"ℹ️ Coinbase ticks for {product_id}: {ticks}")
            return ticks
        except Exception as e:
            log_event(f"⚠️ Coinbase: invalid tick info for {product_id}: {e}")
            ticks = {"price": Decimal("0.01"), "amount": Decimal("0.0001")}
            self._product_tick_cache[product_id] = ticks
            return ticks

    def _normalize_amount(self, product_id: str, amount: Decimal) -> Decimal:
        """
        Robust amount normalization for Coinbase.
        - Floors to base_increment
        """
        ticks = self._load_product_ticks(product_id)
        tick = ticks["amount"]

        try:
            if tick <= 0:
                return amount

            units = (amount / tick).quantize(Decimal("0"), rounding=ROUND_DOWN)
            normalized = units * tick
            normalized_str = format(normalized, "f")
            return Decimal(normalized_str)

        except Exception as e:
            log_event(
                f"⚠️ Coinbase amount rounding failed for {product_id} "
                f"amount={amount}, tick={tick}, error={e}"
            )
            return amount

    def _normalize_price(self, product_id: str, side: str, price: Decimal) -> Decimal:
        """
        Snap price to the nearest valid tick:
        - BUY  → round UP (so price is not too low)
        - SELL → round DOWN (so price is not too high)
        """
        ticks = self._load_product_ticks(product_id)
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
            log_event(f"⚠️ Coinbase price rounding failed for {product_id} price={price}, tick={tick}")
            return price

    # ============================================================
    # Authentication helpers (HMAC + JWT)
    # ============================================================

    def _build_hmac_headers(self, method: str, path: str, body_json: str) -> Dict[str, str]:
        """
        Legacy HMAC authentication for Coinbase Advanced Trade.
        prehash = timestamp + method + requestPath + body
        headers:
          CB-ACCESS-KEY
          CB-ACCESS-SIGN
          CB-ACCESS-TIMESTAMP
        """
        if not self.hmac_api_key or not self.hmac_api_secret:
            raise RuntimeError("Coinbase HMAC keys not configured")

        timestamp = str(int(time.time()))
        request_path = COINBASE_API_PREFIX + path  # path like "/orders"
        prehash = timestamp + method.upper() + request_path + body_json

        signature = hmac.new(
            self.hmac_api_secret.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return {
            "CB-ACCESS-KEY": self.hmac_api_key,
            "CB-ACCESS-SIGN": signature,
            "CB-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }

    def _build_jwt_headers(self, method: str, path: str) -> Dict[str, str]:
        """
        CDP JWT authentication using EC private key (P-256), via Coinbase jwt_generator.

        Requires:
          self.jwt_key_name
          self.jwt_private_pem
        """
        if not self.jwt_key_name or not self.jwt_private_pem:
            raise RuntimeError("Coinbase JWT credentials not configured")

        try:
            from coinbase import jwt_generator
        except ImportError:
            raise RuntimeError(
                "coinbase.jwt_generator not available. "
                "Install official Coinbase Python SDK or switch auth_method to 'hmac'."
            )

        request_path = COINBASE_API_PREFIX + path  # e.g. "/orders"
        jwt_uri = jwt_generator.format_jwt_uri(method.upper(), request_path)
        jwt_token = jwt_generator.build_rest_jwt(
            jwt_uri,
            self.jwt_key_name,
            self.jwt_private_pem,
        )

        return {
            "Authorization": f"Bearer {jwt_token}",
            "Content-Type": "application/json",
        }

    def _private_request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Unified private request using either HMAC or JWT auth.
        path: without API prefix, e.g. "/orders"
        """
        if body is None:
            body_json = ""
        else:
            body_json = json.dumps(body, separators=(",", ":"))

        try:
            if self.auth_method == "jwt":
                headers = self._build_jwt_headers(method, path)
            else:
                headers = self._build_hmac_headers(method, path, body_json)
        except Exception as e:
            log_event(f"❌ Coinbase auth error: {e}")
            return {"error": str(e)}

        url = COINBASE_API_BASE + COINBASE_API_PREFIX + path

        try:
            if method.upper() == "POST":
                resp = requests.post(url, data=body_json, headers=headers, timeout=15)
            elif method.upper() == "DELETE":
                resp = requests.delete(url, data=body_json, headers=headers, timeout=15)
            else:
                # For GET with query params in path, keep body empty
                resp = requests.get(url, headers=headers, timeout=15)

            try:
                result = resp.json()
            except ValueError:
                raw = resp.text.strip()
                log_event(f"⚠️ Coinbase private non-JSON response ({path}): {raw[:200]}...")
                return {"error": "non-json-response", "raw": raw}

            if resp.status_code >= 400:
                log_event(f"⚠️ Coinbase API error {resp.status_code} on {path}: {result}")
            return result

        except Exception as e:
            log_event(f"❌ Coinbase private request failed ({method} {path}): {e}")
            return {"error": str(e)}

    # ============================================================
    # Public API: ticker, OHLC (1h, 4h, 6h, 1d, 1w)
    # ============================================================

    def get_ticker(self, symbol: str) -> Decimal:
        product_id = self._product_id(symbol)
        url = f"{COINBASE_API_BASE}{COINBASE_API_PREFIX}/products/{product_id}/ticker"
        try:
            r = requests.get(url, timeout=10).json()
            # Coinbase ticker typically returns: {"price": "12345.67", ...}
            price = r.get("price")
            if price is None:
                log_event(f"⚠️ Coinbase ticker missing price for {product_id}: {r}")
                return Decimal("0")
            return to_decimal(price)
        except Exception as e:
            log_event(f"⚠️ Coinbase ticker error for {product_id}: {e}")
            return Decimal("0")

    def _fetch_raw_candles(
        self,
        product_id: str,
        granularity: int,
        limit: int,
    ) -> List[List[Any]]:
        """
        Fetch raw Coinbase candles for a given granularity (in seconds).
        Coinbase returns oldest → newest or newest → oldest depending on impl;
        we normalize to oldest → newest and to:
            [ts_ms, open, high, low, close, volume]
        """
        now_s = int(time.time())
        # Rough window: limit * granularity seconds
        duration_s = limit * granularity
        start_s = now_s - duration_s

        params = {
            "start": self._to_iso8601(start_s),
            "end": self._to_iso8601(now_s),
            "granularity": granularity,
        }

        url = f"{COINBASE_API_BASE}{COINBASE_API_PREFIX}/products/{product_id}/candles"
        try:
            resp = requests.get(url, params=params, timeout=15)
            try:
                data = resp.json()
            except ValueError:
                raw = resp.text.strip()
                log_event(f"⚠️ Coinbase OHLC non-JSON for {product_id}: {raw[:200]}...")
                return []

            if not isinstance(data, list):
                log_event(f"⚠️ Coinbase OHLC unexpected JSON for {product_id}: {data}")
                return []

            if len(data) == 0:
                log_event(f"⚠️ Coinbase OHLC returned empty list for {product_id}")
                return []

            normalized: List[List[Any]] = []

            # Coinbase candles may be list or dict; common list format:
            # [ start, low, high, open, close, volume ]
            for c in data:
                try:
                    if isinstance(c, list) and len(c) >= 6:
                        start_s = int(c[0])
                        low = Decimal(str(c[1]))
                        high = Decimal(str(c[2]))
                        open_ = Decimal(str(c[3]))
                        close = Decimal(str(c[4]))
                        volume = Decimal(str(c[5]))
                    elif isinstance(c, dict):
                        start_s = int(c.get("start", 0))
                        open_ = Decimal(str(c.get("open", "0")))
                        high = Decimal(str(c.get("high", "0")))
                        low = Decimal(str(c.get("low", "0")))
                        close = Decimal(str(c.get("close", "0")))
                        volume = Decimal(str(c.get("volume", "0")))
                    else:
                        continue

                    ts_ms = start_s * 1000
                    normalized.append([
                        ts_ms,
                        float(open_),
                        float(high),
                        float(low),
                        float(close),
                        float(volume),
                    ])
                except Exception as e:
                    log_event(f"⚠️ Coinbase normalize candle failed for {product_id}: {c}, err={e}")
                    continue

            # Ensure oldest → newest
            normalized.sort(key=lambda x: x[0])
            return normalized

        except Exception as e:
            log_event(f"⚠️ Coinbase OHLC request failed for {product_id}: {e}")
            return []

    def get_ohlc(self, symbol: str, timeframe: str, limit: int = 730) -> List[list]:
        """
        Fetch OHLC candlestick data from Coinbase.

        Standardized output:
          [timestamp_ms, open, high, low, close, volume]

        Timeframes (Aurono):
          1h → native (3600)
          4h → synthetic (4 × 1h)
          6h → native (21600)
          1d → native (86400)
          1w → synthetic (7 × 1d)
        """
        product_id = self._product_id(symbol)

        # Map Aurono TF → Coinbase granularity
        if timeframe == "1h":
            granularity = 3600
            raw = self._fetch_raw_candles(product_id, granularity, limit)
            return raw

        elif timeframe == "4h":
            # Aggregate 4 × 1h
            granularity = 3600
            raw = self._fetch_raw_candles(product_id, granularity, limit * 4)
            return self._aggregate_candles(raw, 4 * 3600)

        elif timeframe == "6h":
            granularity = 21600
            raw = self._fetch_raw_candles(product_id, granularity, limit)
            return raw

        elif timeframe == "1d":
            granularity = 86400
            raw = self._fetch_raw_candles(product_id, granularity, limit)
            return raw

        elif timeframe == "1w":
            # Synthetic weekly from daily candles
            granularity = 86400
            raw = self._fetch_raw_candles(product_id, granularity, limit * 7)
            # 7 * 1d = 1 week (604800s)
            return self._aggregate_candles(raw, 7 * 86400)

        else:
            # Default: daily
            granularity = 86400
            raw = self._fetch_raw_candles(product_id, granularity, limit)
            return raw

    # ============================================================
    # EUR Balance
    # ============================================================

    def get_available_eur(self) -> float:
        """
        Return the available EUR balance from Coinbase.

        Coinbase endpoint:
            /api/v3/brokerage/accounts
        """
        try:
            result = self._private_request("GET", "/accounts")

            if not isinstance(result, dict) or "accounts" not in result:
                log_event(f"⚠️ Coinbase get_available_eur: unexpected response {result}")
                return 0.0

            for acct in result.get("accounts", []):
                try:
                    if acct.get("currency") == "EUR":
                        return float(acct.get("available_balance", {}).get("value", "0"))
                except Exception:
                    continue

            return 0.0

        except Exception as e:
            log_event(f"⚠️ Coinbase EUR balance error: {e}")
            return 0.0

    # ============================================================
    # Fetch order details (status, executed amount, executed price)
    # ============================================================

    def _get_order_details(self, order_id: str) -> Optional[Dict[str, Any]]:
        """
        Poll Coinbase order status.

        Endpoint:
            GET /orders/historical/{order_id}

        Coinbase response includes:
            - status: FILLED, OPEN, CANCELLED, etc.
            - filled_size
            - average_filled_price
            - fees
        """
        path = f"/orders/historical/{order_id}"
        res = self._private_request("GET", path)

        if not isinstance(res, dict):
            log_event(f"⚠️ Coinbase _get_order_details invalid JSON for {order_id}: {res}")
            return None

        try:
            if "order" not in res:
                log_event(f"⚠️ Coinbase no 'order' field for {order_id}: {res}")
                return None

            o = res["order"]

            status = o.get("status", "")
            filled = Decimal(str(o.get("filled_size", "0")))
            avg_price = Decimal(str(o.get("average_filled_price", "0")))

            # ---- Fee handling (similar to Bitvavo effective-fee update) ----
            fees_list = o.get("fees", [])
            total_fee = Decimal("0")
            if isinstance(fees_list, list):
                for f in fees_list:
                    try:
                        total_fee += Decimal(str(f.get("amount", "0")))
                    except Exception:
                        pass

            executed_value = filled * avg_price
            if executed_value > 0 and total_fee > 0:
                try:
                    eff_fee_rate = (total_fee / executed_value).quantize(Decimal("0.000001"))
                    self.fee_rate = eff_fee_rate
                    log_event(f"ℹ️ Coinbase effective fee updated to {eff_fee_rate}")
                except Exception:
                    pass

            return {
                "status": status,
                "vol_exec": filled,
                "price": avg_price,
                "fee": total_fee,
            }

        except Exception as e:
            log_event(f"⚠️ Coinbase failed parsing order details for {order_id}: {e}")
            return None
            
    # ============================================================
    # Place Limit Order (FULL VERSION, replaces previous stub)
    # ============================================================

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        volume: Decimal,
        trade_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Fully aligns with BitvavoExchange.place_limit_order:

        - Normalizes price & volume to correct tick sizes
        - Simulated if live_trading=False
        - Sends limit order to Coinbase
        - Stores orderId in trades.txid
        - Polls for final fill
        - Adjusts allocated_eur based on actual vs reserved EUR
        - Updates trades table with executed price/amount
        - Logs final execution summary
        """
        cfg = current_config()
        live = cfg.get("live_trading", False)

        product_id = self._product_id(symbol)

        # ----------------------------
        # Normalize price/amount
        # ----------------------------
        raw_price = to_decimal(price)
        raw_volume = to_decimal(volume)

        norm_price = self._normalize_price(product_id, side, raw_price)
        norm_amount = self._normalize_amount(product_id, raw_volume)

        log_event(
            f"📏 Coinbase normalized {side.upper()} for {product_id}: "
            f"price={norm_price}, amount={norm_amount}"
        )

        # ----------------------------
        # Simulation mode
        # ----------------------------
        if not live:
            log_event(
                f"🧪 Simulated {side.upper()} {norm_amount} {product_id} @ €{norm_price} (Coinbase)"
            )
            return {"result": "simulated"}

        # ----------------------------
        # Build order payload
        # Coinbase expects:
        #   side: "BUY" or "SELL"
        #   order_configuration.limit_limit_gtc
        # ----------------------------
        body = {
            "product_id": product_id,
            "side": side.upper(),
            "order_configuration": {
                "limit_limit_gtc": {
                    "base_size": format(norm_amount, "f"),
                    "limit_price": format(norm_price, "f"),
                    "post_only": False,
                }
            },
        }

        # Coinbase endpoint for posting orders
        res = self._private_request("POST", "/orders", body)

        # Handle error
        order_id = None
        if isinstance(res, dict):
            # Typical success:
            # { "success": True, "order_id": "...", "success_response": {...} }
            order_id = res.get("order_id") or res.get("success_response", {}).get("order_id")

        if not order_id:
            log_event(f"⚠️ Coinbase order failed → {res}")
            return res

        log_event(f"📤 Sent {side.upper()} order → Coinbase orderId: {order_id}")

        # ----------------------------
        # Store orderId in our DB
        # ----------------------------
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
                log_event(f"⚠️ Could not store Coinbase orderId in DB: {e}")

        # ----------------------------
        # Poll for fill (similar to Bitvavo)
        # ----------------------------
        time.sleep(12)  # Let Coinbase process

        detail = self._get_order_details(order_id)
        if not detail:
            log_event(f"⚠️ Coinbase returned no order details for orderId={order_id}")
            return res

        log_event(f"📊 Coinbase order status: {detail}")

        status = detail.get("status")
        actual_price = detail.get("price", Decimal("0"))
        actual_amount = detail.get("vol_exec", Decimal("0"))

        # ----------------------------
        # Update DB with actual exec & correct allocation
        # ----------------------------
        if (
            status
            and status.upper() in ("FILLED", "PARTIALLY_FILLED")
            and actual_price > 0
            and actual_amount > 0
            and trade_id is not None
        ):
            # 1) Determine reserved EUR from the original trade
            try:
                conn = _open_db()
                cur = conn.cursor()
                cur.execute(
                    "SELECT price, amount FROM trades WHERE id=?",
                    (trade_id,),
                )
                row = cur.fetchone()
                conn.close()

                if row is None:
                    raise RuntimeError("Trade row not found")

                orig_price = Decimal(str(row[0]))
                orig_amount = Decimal(str(row[1]))
                reserved_eur = (orig_price * orig_amount).quantize(Decimal("0.01"))

            except Exception as e:
                log_event(f"⚠️ Coinbase: could not load original reserved values: {e}")
                reserved_eur = (norm_price * norm_amount).quantize(Decimal("0.01"))

            # 2) Actual EUR based on executed price * executed amount
            actual_eur = (actual_price * actual_amount).quantize(Decimal("0.01"))

            # 3) Determine delta
            if side.lower() == "buy":
                # If we buy cheaper, we get some EUR back
                delta = reserved_eur - actual_eur
            else:
                # If we sell higher, we earn extra EUR (delta positive)
                delta = actual_eur - reserved_eur

            # 4) Write back executed price/amount + adjust strategies.allocated_eur
            try:
                conn = _open_db()
                cur = conn.cursor()

                # Update executed trade values
                cur.execute(
                    """
                    UPDATE trades
                    SET price = ?, amount = ?
                    WHERE id = ?
                    """,
                    (float(actual_price), float(actual_amount), trade_id),
                )

                # Get strategy_id for allocation adjustment
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
                        f"🔁 Adjusted allocated_eur for strategy {strategy_id} on Coinbase "
                        f"by €{delta:.2f} (reserved €{reserved_eur:.2f}, actual €{actual_eur:.2f})"
                    )

                conn.commit()
                conn.close()

            except Exception as e:
                log_event(f"⚠️ Could not update executed Coinbase trade / allocation in DB: {e}")

            # 5) Detailed log of execution
            log_event(
                f"💾 Updated executed Coinbase trade → "
                f"€{actual_price} × {actual_amount} "
                f"(was {norm_price} × {norm_amount}, trade_id={trade_id})"
            )

            final_alloc_str = f", new alloc €{new_alloc:.2f}" if 'new_alloc' in locals() and new_alloc is not None else ""

            log_event(
                f"✅ {side.upper()} executed: {actual_amount} {symbol} @ €{actual_price} "
                f"on coinbase (actual €{actual_eur:.2f}, reserved €{reserved_eur:.2f}"
                f"{final_alloc_str})"
            )

        # For interface-compatibility with BitvavoExchange, return the original API response.
        return res


