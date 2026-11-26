import hashlib
import hmac
import json
import re
import time
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlencode

import requests

BASE_URL = "https://api.gateio.ws"
API_PREFIX = "/api/v4"
_LEVERAGED_PATTERN = re.compile(r"(?:[2345][LS]|UP|DOWN|BULL|BEAR)$")


class GateioSpotAPI:
    """
    Pure-Python Gate.io Spot API v4 client (requests + HMAC-SHA512).
    100% compatible with Android / Buildozer without ccxt, pandas, or numpy.
    """

    def __init__(self, api_key: str = "", api_secret: str = "", timeout: int = 12):
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"Accept": "application/json", "Content-Type": "application/json"}
        )
        self._pair_meta: Dict[str, Dict[str, Any]] = {}

    def update_keys(self, api_key: str, api_secret: str) -> None:
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()

    @staticmethod
    def build_signature(
        method: str,
        full_path: str,
        query_str: str,
        body_str: str,
        timestamp: str,
        api_secret: str,
    ) -> str:
        hashed_payload = hashlib.sha512(body_str.encode("utf-8")).hexdigest()
        sign_str = f"{method.upper()}\n{full_path}\n{query_str}\n{hashed_payload}\n{timestamp}"
        return hmac.new(
            api_secret.encode("utf-8"),
            sign_str.encode("utf-8"),
            hashlib.sha512,
        ).hexdigest()

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        signed: bool = False,
    ) -> Any:
        full_path = f"{API_PREFIX}{endpoint}"
        query_str = urlencode(params) if params else ""
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        url = f"{BASE_URL}{full_path}" + (f"?{query_str}" if query_str else "")

        headers: Dict[str, str] = {}
        if signed:
            if not (self.api_key and self.api_secret):
                raise RuntimeError("Gate.io API Key and Secret are required for Live mode.")
            ts = str(int(time.time()))
            headers["KEY"] = self.api_key
            headers["Timestamp"] = ts
            headers["SIGN"] = self.build_signature(
                method, full_path, query_str, body_str, ts, self.api_secret
            )

        resp = self.session.request(
            method.upper(),
            url,
            data=body_str if body is not None else None,
            headers=headers,
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Gate.io API {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    @staticmethod
    def is_valid_spot_pair(pair: str, excluded_stables: Sequence[str]) -> bool:
        if not pair.endswith("_USDT"):
            return False
        base = pair.split("_")[0].upper()
        if base in {s.upper() for s in excluded_stables}:
            return False
        if _LEVERAGED_PATTERN.search(base):
            return False
        return True

    def load_pairs_meta(self) -> None:
        if self._pair_meta:
            return
        data = self._request("GET", "/spot/currency_pairs")
        for item in data:
            if item.get("trade_status") == "tradable":
                self._pair_meta[item["id"]] = item

    def fetch_top_usdt_pairs(
        self,
        num_coins: int = 150,
        min_quote_volume: float = 500000.0,
        excluded_stables: Sequence[str] = (),
    ) -> List[str]:
        self.load_pairs_meta()
        tickers = self._request("GET", "/spot/tickers")
        ranked = []
        for t in tickers:
            pair = t.get("currency_pair", "")
            if pair not in self._pair_meta:
                continue
            if not self.is_valid_spot_pair(pair, excluded_stables):
                continue
            try:
                qvol = float(t.get("quote_volume") or 0.0)
            except (TypeError, ValueError):
                qvol = 0.0
            if qvol < min_quote_volume:
                continue
            ranked.append((pair, qvol))

        ranked.sort(key=lambda x: x[1], reverse=True)
        return [p for p, _ in ranked[:num_coins]]

    @staticmethod
    def parse_candles(raw_rows: Any) -> List[Dict[str, float]]:
        """
        Parse Gate.io v4 /spot/candlesticks rows:
        [ts_sec, quote_vol, close, high, low, open, base_vol, window_closed]
        """
        candles: List[Dict[str, float]] = []
        if not isinstance(raw_rows, list):
            return candles
        for row in raw_rows:
            if len(row) < 6:
                continue
            try:
                quote_volume = float(row[1]) if len(row) > 1 and row[1] not in (None, "") else 0.0
                base_volume = float(row[6]) if len(row) > 6 and row[6] not in (None, "") else 0.0
                closed_value = str(row[7]).lower() if len(row) > 7 else ""
                candle = {
                    "ts": int(float(row[0])),
                    "open": float(row[5]),
                    "high": float(row[3]),
                    "low": float(row[4]),
                    "close": float(row[2]),
                    # Quote turnover is the more comparable volume measure for USDT pairs.
                    "volume": quote_volume if quote_volume > 0 else base_volume,
                    "quote_volume": quote_volume,
                    "base_volume": base_volume,
                }
                if closed_value in ("true", "false"):
                    candle["window_closed"] = closed_value == "true"
                candles.append(candle)
            except (TypeError, ValueError):
                continue
        candles.sort(key=lambda c: c["ts"])
        return candles

    def fetch_candles(
        self, pair: str, interval: str = "5m", limit: int = 260
    ) -> Optional[List[Dict[str, float]]]:
        try:
            raw = self._request(
                "GET",
                "/spot/candlesticks",
                params={"currency_pair": pair, "interval": interval, "limit": limit},
            )
            parsed = self.parse_candles(raw)
            return parsed if parsed else None
        except Exception:
            return None

    def fetch_last_price(self, pair: str) -> Optional[float]:
        try:
            data = self._request("GET", "/spot/tickers", params={"currency_pair": pair})
            if isinstance(data, list) and data:
                return float(data[0].get("last") or 0.0)
            return None
        except Exception:
            return None

    def fetch_usdt_balance(self) -> float:
        data = self._request("GET", "/spot/accounts", params={"currency": "USDT"}, signed=True)
        if isinstance(data, list) and data:
            return float(data[0].get("available") or 0.0)
        return 0.0

    def place_market_buy(self, pair: str, usdt_amount: float) -> Dict[str, Any]:
        """Gate.io Spot v4 Market Buy (IOC, amount specified in USDT quote)."""
        body = {
            "currency_pair": pair,
            "type": "market",
            "account": "spot",
            "side": "buy",
            "amount": f"{usdt_amount:.2f}",
            "time_in_force": "ioc",
        }
        res = self._request("POST", "/spot/orders", body=body, signed=True)
        filled_total = float(res.get("filled_total") or 0.0)
        filled_qty = float(res.get("filled_amount") or 0.0)
        avg_price = float(res.get("avg_deal_price") or 0.0)
        if avg_price <= 0 and filled_total > 0 and filled_qty > 0:
            avg_price = filled_total / filled_qty
        return {
            "order_id": str(res.get("id", "")),
            "filled_qty": filled_qty,
            "avg_price": avg_price,
            "cost": filled_total,
            "fee": float(res.get("fee") or 0.0),
            "fee_currency": str(res.get("fee_currency") or ""),
            "finish_as": str(res.get("finish_as") or ""),
        }

    def place_market_sell(self, pair: str, qty: float) -> Dict[str, Any]:
        """Gate.io Spot v4 Market Sell (IOC, amount specified in base asset)."""
        self.load_pairs_meta()
        prec = int(self._pair_meta.get(pair, {}).get("amount_precision", 6))
        qty_str = f"{qty:.{prec}f}"
        body = {
            "currency_pair": pair,
            "type": "market",
            "account": "spot",
            "side": "sell",
            "amount": qty_str,
            "time_in_force": "ioc",
        }
        res = self._request("POST", "/spot/orders", body=body, signed=True)
        avg_price = float(res.get("avg_deal_price") or 0.0)
        filled_qty = float(res.get("filled_amount") or 0.0)
        filled_total = float(res.get("filled_total") or 0.0)
        if avg_price <= 0 and filled_total > 0 and filled_qty > 0:
            avg_price = filled_total / filled_qty
        return {
            "order_id": str(res.get("id", "")),
            "filled_qty": filled_qty,
            "avg_price": avg_price,
            "cost": filled_total,
            "fee": float(res.get("fee") or 0.0),
            "fee_currency": str(res.get("fee_currency") or ""),
            "finish_as": str(res.get("finish_as") or ""),
        }
