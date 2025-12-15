import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import time
import hmac
import hashlib
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
        api_key: str | None = None,
        api_secret: str | None = None,
        auth_method: str | None = None,
        api_key_name: str | None = None,
        ec_private_key_pem: str | None = None,
    ) -> None:

        self.fee_rate = Decimal("0.0025")
        self.tm = TradeManager(get_db_path())

        self.auth_method = auth_method or "hmac"
        self.hmac_api_key = None
        self.hmac_api_secret = None
        self.jwt_key_name = None
        self.jwt_private_pem = None

        # --- Settings test override ---
        if api_key or api_secret:
            self.auth_method = auth_method or "hmac"
            self.hmac_api_key = api_key
            self.hmac_api_secret = api_secret
        else:
            key, secret = get_credentials_for_exchange("coinbase")
            if secret:
                cfg = json.loads(secret)
                self.auth_method = cfg.get("auth_method", "hmac")
                self.hmac_api_key = cfg.get("hmac_key")
                self.hmac_api_secret = cfg.get("hmac_secret")
                self.jwt_key_name = cfg.get("jwt_key_name")
                self.jwt_private_pem = cfg.get("jwt_private_key")

        if api_key_name:
            self.jwt_key_name = api_key_name
        if ec_private_key_pem:
            self.jwt_private_pem = ec_private_key_pem

        log_event(
            f"ℹ️ CoinbaseExchange initialized "
            f"(auth_method={self.auth_method}, "
            f"HMAC={bool(self.hmac_api_key)}, "
            f"JWT={bool(self.jwt_key_name and self.jwt_private_pem)})"
        )

    # ============================================================
    # Helpers
    # ============================================================

    def _product_id(self, symbol: str) -> str:
        s = symbol.replace("/", "").upper()
        return f"{s[:-3]}-EUR" if s.endswith("EUR") else s

    @staticmethod
    def _to_iso8601(ts: int) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ============================================================
    # Authentication
    # ============================================================

    def _build_hmac_headers(self, method: str, request_path: str, body: str) -> Dict[str, str]:
        ts = str(int(time.time()))
        prehash = ts + method.upper() + request_path + body

        sig = hmac.new(
            self.hmac_api_secret.encode(),
            prehash.encode(),
            hashlib.sha256,
        ).hexdigest()

        return {
            "CB-ACCESS-KEY": self.hmac_api_key,
            "CB-ACCESS-SIGN": sig,
            "CB-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    def _build_jwt_headers(self, method: str, request_path: str) -> Dict[str, str]:
        now = int(time.time())

        uri = f"{method.upper()} api.coinbase.com{request_path}"

        payload = {
            "iss": "cdp",
            "sub": self.jwt_key_name,
            "nbf": now,
            "exp": now + 120,
            "uri": uri,
        }

        private_key = serialization.load_pem_private_key(
            self.jwt_private_pem.encode(),
            password=None,
        )

        token = jwt.encode(
            payload,
            private_key,
            algorithm="ES256",
            headers={
                "kid": self.jwt_key_name,
                "nonce": secrets.token_hex(16),
            },
        )

        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ============================================================
    # HTTP core
    # ============================================================

    def _private_request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:

        body_json = "" if body is None else json.dumps(body, separators=(",", ":"))
        request_path = COINBASE_API_PREFIX + path

        if params:
            request_path += "?" + urlencode(params)

        try:
            if self.auth_method == "jwt":
                headers = self._build_jwt_headers(method, request_path)
            else:
                headers = self._build_hmac_headers(method, request_path, body_json)
        except Exception as e:
            log_event(f"❌ Coinbase auth error: {e}")
            return {"error": str(e)}

        url = COINBASE_API_BASE + request_path.split("?")[0]

        resp = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            data=body_json if body else None,
            timeout=15,
        )

        try:
            return resp.json()
        except Exception:
            log_event(f"⚠️ Coinbase non-JSON ({resp.status_code}): {resp.text}")
            return {"error": "non-json", "status": resp.status_code}

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
        tf_map = {
            "1h": 3600,
            "6h": 21600,
            "1d": 86400,
        }
        gran = tf_map.get(timeframe, 3600)

        now = int(time.time())
        start = now - gran * limit

        r = self._private_request(
            "GET",
            f"/products/{pid}/candles",
            params={
                "start": self._to_iso8601(start),
                "end": self._to_iso8601(now),
                "granularity": gran,
            },
        )

        candles = []
        for c in r.get("candles", []):
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

