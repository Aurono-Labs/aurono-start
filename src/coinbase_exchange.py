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

    _product_tick_cache: Dict[str, Dict[str, Decimal]] = {}
    
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

        log_event("ℹ️ CoinbaseExchange initialized (JWT)")


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
            
    def _aggregate_candles(self, candles: List[list], factor: int) -> List[list]:
        """
        Aggregate candles into higher timeframe.
        factor = number of base candles to combine
        """
        out = []
        for i in range(0, len(candles), factor):
            chunk = candles[i:i + factor]
            if len(chunk) < factor:
                continue

            ts = chunk[0][0]
            open_ = chunk[0][1]
            high = max(c[2] for c in chunk)
            low = min(c[3] for c in chunk)
            close = chunk[-1][4]
            volume = sum(c[5] for c in chunk)

            out.append([ts, open_, high, low, close, volume])

        return out

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
        
        # DEBUG: Print what we're signing
        log_event(f"🔐 Coinbase JWT uri: {uri}")

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
        
    def get_ohlc(self, symbol: str, timeframe: str, limit: int = 200) -> List[list]:
        pid = self._product_id(symbol)

        # --------------------------------------------------
        # Synthetic timeframes (Coinbase does not support)
        # --------------------------------------------------

        if timeframe == "4h":
            base = self.get_ohlc(symbol, "1h", limit * 4)
            return self._aggregate_candles(base, 4)

        if timeframe == "1w":
            base = self.get_ohlc(symbol, "1d", limit * 7)
            return self._aggregate_candles(base, 7)

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

        data = resp.json()

        candles = []
        for c in data:
            # Coinbase Exchange format:
            # [ time, low, high, open, close, volume ]
            candles.append([
                int(c[0]) * 1000,
                float(c[3]),
                float(c[2]),
                float(c[1]),
                float(c[4]),
                float(c[5]),
            ])

        return sorted(candles, key=lambda x: x[0])

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

        body = {
            "product_id": self._product_id(symbol),
            "side": side.upper(),
            "order_configuration": {
                "limit_limit_gtc": {
                    "base_size": format(volume, "f"),
                    "limit_price": format(price, "f"),
                }
            },
        }

        res = self._private_request("POST", "/orders", body=body)

        if trade_id and res.get("order_id"):
            conn = _open_db()
            conn.execute("UPDATE trades SET txid=? WHERE id=?", (res["order_id"], trade_id))
            conn.commit()
            conn.close()

        return res
