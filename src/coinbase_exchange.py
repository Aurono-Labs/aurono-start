import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import time
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP, ROUND_DOWN
from typing import Any, Dict, Optional, List
from urllib.parse import urlencode

import requests
import jwt
import secrets
from cryptography.hazmat.primitives import serialization
import uuid

from utils import (
    log_event,
    current_config,
    to_decimal,
    get_db_path,
    get_credentials_for_exchange,
)
from utils import _open_db
from trade_manager import TradeManager
from exchange_base import ExchangeBase


COINBASE_API_BASE = "https://api.coinbase.com"
COINBASE_API_PREFIX = "/api/v3/brokerage"


class CoinbaseExchange(ExchangeBase):
    name = "coinbase"
    # Coinbase: keep candle requests minimal & deterministic
    COINBASE_REQUIRED_CANDLES = 3


    _product_tick_cache: Dict[str, Dict[str, Any]] = {}
    
    def __init__(
        self,
        api_key_name: str | None = None,
        ec_private_key_pem: str | None = None,
    ) -> None:
        self.fee_rate = Decimal("0.0025")
        self.tm = TradeManager(get_db_path())

        if api_key_name and ec_private_key_pem:
            self.jwt_key_name = api_key_name
            self.jwt_private_pem = ec_private_key_pem
        else:
            _, secret = get_credentials_for_exchange("coinbase")
            if not secret:
                raise RuntimeError("No Coinbase JWT credentials found.")

            cfg = json.loads(secret)
            if cfg.get("auth_method") != "jwt":
                raise RuntimeError("Coinbase credentials are not JWT-based.")

            self.jwt_key_name = cfg["jwt_key_name"]
            self.jwt_private_pem = cfg["jwt_private_key"]

    # ============================================================
    # Helpers
    # ============================================================

    def _product_id(self, symbol: str) -> str:
        s = symbol.replace("/", "").upper()
        return f"{s[:-3]}-EUR" if s.endswith("EUR") else s
        
    def _public_request(self, path: str, params: Optional[dict] = None) -> Any:
        url = COINBASE_API_BASE + COINBASE_API_PREFIX + path

        if params:
            url += "?" + urlencode(params, doseq=True)

        resp = requests.get(url, timeout=15)

        if resp.status_code >= 400:
            log_event(f"⚠️ Coinbase PUBLIC HTTP {resp.status_code}: {resp.text}")
            return {}

        try:
            return resp.json()
        except Exception:
            return {}
            
    def _last_closed_index(self, candles: List[list], tf_seconds: int) -> int:
        """
        Returns index of last fully closed candle.
        """
        now_ms = int(time.time() * 1000)

        for i in range(len(candles) - 1, -1, -1):
            candle_close = candles[i][0] + tf_seconds * 1000
            if candle_close <= now_ms:
                return i

        return -1
        
    def _aggregate_from_anchor(self, candles: List[list], anchor_idx: int, count: int) -> Optional[list]:
        """
        Aggregate `count` candles ending at anchor_idx.
        """
        start = anchor_idx - (count - 1)
        if start < 0:
            return None

        chunk = candles[start:anchor_idx + 1]
        if len(chunk) != count:
            return None

        ts = chunk[0][0]
        open_ = chunk[0][1]
        high = max(c[2] for c in chunk)
        low = min(c[3] for c in chunk)
        close = chunk[-1][4]
        volume = sum(c[5] for c in chunk)

        return [ts, open_, high, low, close, volume]
        
    def _get_product_increments(self, symbol: str) -> Dict[str, Decimal | bool]:
        pid = self._product_id(symbol)

        if pid in self._product_tick_cache:
            return self._product_tick_cache[pid]

        r = self._private_request("GET", f"/products/{pid}")

        try:
            price_inc = Decimal(r["price_increment"])
            base_inc = Decimal(r["base_increment"])
            min_funds = Decimal(r.get("min_market_funds", "0"))
            trading_disabled = bool(r.get("trading_disabled", False))
        except Exception:
            raise RuntimeError(f"Missing increment data for {pid}: {r}")

        self._product_tick_cache[pid] = {
            "price_increment": price_inc,
            "base_increment": base_inc,
            "min_market_funds": min_funds,
            "trading_disabled": trading_disabled,
        }

        return self._product_tick_cache[pid]
        
    def _quantize_down(self, value: Decimal, step: Decimal) -> Decimal:
        """
        Snap value DOWN to nearest valid step.
        """
        return (value // step) * step



    # ============================================================
    # Authentication
    # ============================================================

    def _build_jwt_headers(self, method: str, full_request_path: str) -> Dict[str, str]:
        """
        Coinbase App API key auth (CDP Secret API Key, ES256).
        full_request_path must be the exact path used on the request line,
        including /api/v3/brokerage prefix and query string if present.
        Example: /api/v3/brokerage/accounts?limit=10
        """
        if not self.jwt_key_name or not self.jwt_private_pem:
            raise RuntimeError("Missing Coinbase JWT credentials (jwt_key_name / jwt_private_pem).")

        now = int(time.time())

        # IMPORTANT: docs require host included in 'uri' claim
        uri = f"{method.upper()} api.coinbase.com{full_request_path}"

        payload = {
            "sub": self.jwt_key_name,
            "iss": "cdp",
            "nbf": now,
            "exp": now + 120,
            "uri": uri,
        }

        private_key = serialization.load_pem_private_key(
            self.jwt_private_pem.encode("utf-8"),
            password=None,
        )

        token = jwt.encode(
            payload,
            private_key,
            algorithm="ES256",
            headers={
                "kid": self.jwt_key_name,
                "nonce": secrets.token_hex(16)
            },
        )

        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "aurono",
        }

    def _private_request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        """
        Always builds the exact request path + query string, so the JWT 'uri'
        claim matches the real request.
        """
        body_json = "" if body is None else json.dumps(body, separators=(",", ":"))

        # Build full path including Coinbase prefix
        full_path = COINBASE_API_PREFIX + path  # e.g. /api/v3/brokerage/accounts

        # Build query string deterministically with sorted keys (required for JWT)
        query = ""
        if params:
            # Sort parameters alphabetically by key for consistent JWT signing
            sorted_params = sorted(params.items())
            query = "?" + urlencode(sorted_params, doseq=True)
        full_request_path = full_path + query
        
        try:
            headers = self._build_jwt_headers(method, full_request_path)
        except Exception as e:
            log_event(f"❌ Coinbase auth error: {e}")
            return {"error": str(e)}

        url = COINBASE_API_BASE + full_request_path

        resp = requests.request(
            method.upper(),
            url,
            headers=headers,
            data=body_json if body is not None else None,
            timeout=15,
        )

        # Always log raw body on non-2xx to speed up debugging
        if resp.status_code >= 400:
            log_event(f"⚠️ Coinbase HTTP {resp.status_code}: {resp.text}")
            return {"error": "http_error", "status": resp.status_code, "body": resp.text}

        try:
            return resp.json()
        except Exception:
            log_event(f"⚠️ Coinbase non-JSON ({resp.status_code}): {resp.text}")
            return {"error": "non-json", "status": resp.status_code, "body": resp.text}

    # ============================================================
    # Required ExchangeBase methods
    # ============================================================

    def get_supported_pairs(self) -> List[str]:
        r = self._private_request("GET", "/products")
        out = []
        for p in r.get("products", []):
            if p.get("quote_currency_id") == "EUR":
                out.append(p["product_id"].replace("-", ""))
        return sorted(set(out))

    def get_ticker(self, symbol: str) -> Decimal:
        pid = self._product_id(symbol)
        r = self._private_request("GET", f"/products/{pid}")
        return to_decimal(r.get("price", "0"))
        
        
    def _get_ohlc_raw(self, symbol: str, timeframe: str, limit: int) -> List[list]:
        """
        Fetch raw OHLC candles without trimming to COINBASE_REQUIRED_CANDLES.
        ONLY for internal synthetic timeframe construction.
        """
        pid = self._product_id(symbol)

        TF_SECONDS = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "6h": 21600,
            "1d": 86400,
        }

        gran = TF_SECONDS.get(timeframe)
        if not gran:
            return []

        limit = min(limit, 300)

        end = int(time.time())
        start = end - (limit * gran)

        url = f"https://api.exchange.coinbase.com/products/{pid}/candles"
        params = {
            "start": datetime.utcfromtimestamp(start).isoformat(),
            "end": datetime.utcfromtimestamp(end).isoformat(),
            "granularity": gran,
        }

        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            log_event(f"⚠️ Coinbase EXCHANGE candles HTTP {resp.status_code}: {resp.text}")
            return []

        candles = []
        for c in resp.json():
            candles.append([
                int(c[0]) * 1000,
                float(c[3]),
                float(c[2]),
                float(c[1]),
                float(c[4]),
                float(c[5]),
            ])

        return sorted(candles, key=lambda x: x[0])

    def get_ohlc(self, symbol: str, timeframe: str, limit: int = 200) -> List[list]:
        pid = self._product_id(symbol)

        # --------------------------------------------------
        # Coinbase synthetic timeframes
        # Anchored to last UTC-closed base candle
        # --------------------------------------------------
        
        if timeframe == "4h":
            # Need 3x 4h candles => 3 * 4 = 12 hourly candles minimum,
            # plus extra buffer so we can snap to boundary safely.
            base = self._get_ohlc_raw(symbol, "1h", limit=24)

            if len(base) < 16:
                log_event(f"⚠️ Not enough base OHLC for {symbol} (4h) on coinbase → skip.")
                return []

            idx = self._last_closed_index(base, 3600)
            if idx < 0:
                log_event(f"⚠️ No closed base candle for {symbol} (4h) on coinbase → skip.")
                return []
                
            # Snap to END of 4h block (UTC): last 1h candle in the 4h window
            # 4h windows are [00-04), [04-08), ... so the last 1h starts at UTC 03,07,11,15,19,23
            while idx >= 0:
                hour = (base[idx][0] // 1000) // 3600
                if (hour + 1) % 4 == 0:
                    break
                idx -= 1

            # We want 3 consecutive 4h candles:
            # anchors at idx, idx-4, idx-8 (in 1h steps)
            anchors = [idx - 8, idx - 4, idx]  # chronological order
            out = []

            for a in anchors:
                c = self._aggregate_from_anchor(base, a, 4)
                if not c:
                    log_event(f"⚠️ Not enough OHLC for {symbol} (4h) on coinbase → skip.")
                    return []
                out.append(c)

            return out

            
        if timeframe == "1w":
            base = self._get_ohlc_raw(symbol, "1d", limit=10)

            if len(base) < 7:
                log_event(f"⚠️ Not enough base OHLC for {symbol} (1w) on coinbase → skip.")
                return []

            idx = self._last_closed_index(base, 86400)
            if idx < 0:
                log_event(f"⚠️ No closed base candle for {symbol} (1w) on coinbase → skip.")
                return []

            candle = self._aggregate_from_anchor(base, idx, 7)
            return [candle] if candle else []

        # --------------------------------------------------
        # Native Coinbase timeframes
        # --------------------------------------------------

        TF_SECONDS = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "6h": 21600,
            "1d": 86400,
        }

        gran = TF_SECONDS.get(timeframe)
        if not gran:
            return []

        # Request a bit more than we need because Coinbase may omit the forming candle
        request_candles = max(self.COINBASE_REQUIRED_CANDLES + 2, limit)
        request_candles = min(request_candles, 300)  # Coinbase Exchange API practical cap

        end = int(time.time())
        start = end - (request_candles * gran)

        url = f"https://api.exchange.coinbase.com/products/{pid}/candles"
        params = {
            "start": datetime.utcfromtimestamp(start).isoformat(),
            "end": datetime.utcfromtimestamp(end).isoformat(),
            "granularity": gran,
        }

        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            log_event(f"⚠️ Coinbase EXCHANGE candles HTTP {resp.status_code}: {resp.text}")
            return []

        data = resp.json()

        candles = []
        for c in data:
            candles.append([
                int(c[0]) * 1000,   # time
                float(c[3]),        # open
                float(c[2]),        # high
                float(c[1]),        # low
                float(c[4]),        # close
                float(c[5]),        # volume
            ])

        candles = sorted(candles, key=lambda x: x[0])

        # Keep only what the strategy engine expects (minimal deterministic window)
        if len(candles) < self.COINBASE_REQUIRED_CANDLES:
            log_event(f"⚠️ Not enough OHLC for {symbol} ({timeframe}) on coinbase → skip.")
            return []

        return candles[-self.COINBASE_REQUIRED_CANDLES:]

 
    def get_available_eur(self) -> float:
        r = self._private_request("GET", "/accounts")
        for a in r.get("accounts", []):
            if a.get("currency") == "EUR":
                return float(a["available_balance"]["value"])
        return 0.0
        
    def place_limit_order(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        volume: Decimal,
        trade_id: Optional[int] = None,
    ) -> Dict[str, Any]:

        if not current_config().get("live_trading", False):
            log_event(f"🧪 Simulated {side} {symbol}")
            return {"result": "simulated"}

        side_u = side.upper()
        if side_u not in ("BUY", "SELL"):
            return {"success": False, "error": f"invalid_side:{side}"}

        inc = self._get_product_increments(symbol)

        if inc.get("trading_disabled", False):
            log_event(f"⛔ Coinbase trading disabled for {symbol} → skip order")
            return {"success": False, "error": "trading_disabled"}


        # Quantize + normalize to Coinbase increments
        q_price = self._quantize_down(price, inc["price_increment"]).quantize(
            inc["price_increment"], rounding=ROUND_DOWN
        )
        q_volume = self._quantize_down(volume, inc["base_increment"]).quantize(
            inc["base_increment"], rounding=ROUND_DOWN
        )

        if q_price <= 0 or q_volume <= 0:
            log_event(
                f"❌ Coinbase quantization failed {symbol}: "
                f"price={price}->{q_price}, volume={volume}->{q_volume}"
            )
            return {"success": False, "error": "invalid_quantized_values"}

        if q_price != price or q_volume != volume:
            log_event(
                f"ℹ️ Coinbase snap {symbol}: "
                f"price {price} → {q_price}, "
                f"size {volume} → {q_volume}"
            )

        # EUR notional computed from quantized values
        notional_eur = (q_price * q_volume).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # Enforce Coinbase minimum notional (if provided by product metadata)
        if side_u == "BUY" and inc.get("min_market_funds", Decimal("0")) > 0:
            if notional_eur < inc["min_market_funds"]:
                log_event(
                    f"🛑 Coinbase min notional not met for {symbol}: "
                    f"{notional_eur} < {inc['min_market_funds']} EUR"
                )
                return {"success": False, "error": "min_notional"}

        # Required by Coinbase; keep <=128 chars
        client_order_id = f"aurono-{trade_id}" if trade_id is not None else str(uuid.uuid4())

        # BUY -> quote_size, SELL -> base_size
        if side_u == "BUY":
            order_cfg: Dict[str, Any] = {
                "limit_limit_gtc": {
                    "quote_size": format(notional_eur, "f"),
                    "limit_price": format(q_price, "f"),
                }
            }
        else:
            order_cfg = {
                "limit_limit_gtc": {
                    "base_size": format(q_volume, "f"),
                    "limit_price": format(q_price, "f"),
                }
            }

        body = {
            "client_order_id": client_order_id,
            "product_id": self._product_id(symbol),
            "side": side_u,
            "order_configuration": order_cfg,
        }

        res = self._private_request("POST", "/orders", body=body)

        if trade_id and res.get("order_id"):
            conn = _open_db()
            conn.execute("UPDATE trades SET txid=? WHERE id=?", (res["order_id"], trade_id))
            conn.commit()
            conn.close()

        return res
