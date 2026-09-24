"""Ondo Perps venue adapter (api.ondoperps.xyz, USD-settled perpetuals).

Market metadata and the book come from public REST + the official websocket
(see feeds.OndoBookFeed) — no credentials or signing library needed for
--record-only. Trading authenticates each request with an HMAC-SHA256 API-key
signature carried in three headers (ONDO-KEY-ID / ONDO-TIMESTAMP / ONDO-SIGN);
there is no SDK and no EIP-712 — the signing scheme is plain stdlib `hmac`.

The canonical string is the concatenation (no separators) of, in order:
    timestamp + METHOD + pathWithQuery + body
where `timestamp` is the same millisecond value as the ONDO-TIMESTAMP header,
`pathWithQuery` is the full path + query (no hostname), and `body` is the raw
JSON string (empty for GET). The timestamp's ±30s window is the anti-replay
mechanism — there is no nonce to manage.

IOC limit orders settle synchronously in the order response (its `status`,
`filledSize` and `filledCost` give the fill; avg_px = filledCost / filledSize),
so there is no separate fills query. Unknown outcomes (5xx / timeout) fall back
to order-status polling by `client:{clientOrderId}` inside send_taker(), so the
engine sees the same unified result shape as the other venues: {status,
filled_base, avg_px, err, unresolved}.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import time
import urllib.parse
from typing import Optional

import aiohttp

from .book import OrderBook, floor_step
from .config import ONDO_API_URL, ONDO_WS_URL, VenueConf
from .feeds import OndoBookFeed

log = logging.getLogger("ondo")

REST_TIMEOUT = 10.0
POLL_INTERVAL = 0.5        # unresolved-order polling cadence (same as HL/Aster)

# Terminal order statuses per the REST/WS docs (an IOC never rests).
TERMINAL = {"fullyfilled", "canceled"}

# Margin-class rejections — the engine pauses the venue on status "margin".
MARGIN_ERRORS = ("insufficient_margin", "insufficient_balance",
                 "account_liquidating", "margin_below_required_initial")


class OndoAccount:
    """API-key signer: HMAC-SHA256 over timestamp + method + path + body.

    `key_id` / `secret` are the literal values from the Ondo app including
    their `ondoKeyId_` / `ondoApiSecret_` prefixes (the header must carry the
    prefixed id; the secret is shown only once at key creation)."""

    def __init__(self, key_id: str, secret: str) -> None:
        self.key_id = key_id
        self.secret = secret

    def sign(self, timestamp: str, method: str, path: str, body: str) -> str:
        """Lowercase-hex HMAC-SHA256 of the four concatenated strings."""
        payload = timestamp + method.upper() + path + body
        return hmac.new(self.secret.encode(), payload.encode(),
                        hashlib.sha256).hexdigest()

    def ws_sign(self, ts_ms: int) -> str:
        """WebSocket API-key login signature (public depth feed needs none)."""
        payload = str(ts_ms) + "ondo_perps_ws_login"
        return hmac.new(self.secret.encode(), payload.encode(),
                        hashlib.sha256).hexdigest()


class OndoVenue:
    kind = "ondo"

    def __init__(self, conf: VenueConf, session: aiohttp.ClientSession,
                 settle_timeout_sec: float) -> None:
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.api_url = ONDO_API_URL
        self.ws_url = ONDO_WS_URL
        self.session = session
        self.settle_timeout = settle_timeout_sec
        self.book = OrderBook()
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0     # cumulative filled notional this session
        self.equity = None
        self.free = None
        self.start_equity = None
        self.fee_bps = conf.fee_bps
        self.cap_usd = conf.cap_usd
        self.orders_per_min = conf.orders_per_min
        self.last_traded_ts = 0.0
        self.account: Optional[OndoAccount] = None
        self.symbol = ""          # exchange-native, e.g. "NVDA-USD.P"
        self.tick_size = 0.0
        self.price_decimals = 2
        self.size_decimals = 0
        self.step_size = 0.0
        self.min_base = 0.0
        self.min_quote = 10.0
        self._cloid = int(time.time() * 1000)

    # ------------------------------------------------------------------ rest

    async def _public_get(self, path: str, params: Optional[dict] = None):
        async with self.session.get(
                self.api_url + path, params=params,
                timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
            r.raise_for_status()
            return await r.json()

    async def _signed_request(self, method: str, path: str,
                              params: Optional[dict] = None,
                              json_body: Optional[dict] = None):
        """HMAC-signed request → (body, err, unresolved) with the standard
        tri-classification: 429 → RATE_LIMITED err, other 4xx → err, 5xx/
        network/JSON → unresolved (the order may or may not have landed).

        The canonical path + query is signed EXACTLY as sent, and the JSON
        body is signed as the exact string placed on the wire (never re-encoded
        by aiohttp), so the server re-derives the same bytes."""
        assert self.account is not None
        query = urllib.parse.urlencode(params) if params else ""
        full_path = path + (("?" + query) if query else "")
        has_body = json_body is not None
        body_str = json.dumps(json_body, separators=(",", ":")) if has_body else ""
        ts = str(int(time.time() * 1000))
        headers = {"ONDO-KEY-ID": self.account.key_id,
                   "ONDO-TIMESTAMP": ts,
                   "ONDO-SIGN": self.account.sign(ts, method, full_path,
                                                  body_str)}
        if has_body:
            headers["Content-Type"] = "application/json"
        try:
            async with self.session.request(
                    method, self.api_url + full_path, headers=headers,
                    data=body_str if has_body else None,
                    timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
                text = await r.text()
                if r.status == 429:
                    return None, f"RATE_LIMITED: HTTP 429 {text[:150]}", False
                if 400 <= r.status < 500:
                    return None, f"HTTP {r.status}: {text[:250]}", False
                if r.status >= 500:
                    return None, None, True
                return json.loads(text), None, False
        except (asyncio.TimeoutError, aiohttp.ClientError,
                json.JSONDecodeError):
            return None, None, True

    # ---------------------------------------------------------------- market

    async def load_market(self) -> None:
        info = await self._public_get("/v1/markets")
        sym = (self.conf.symbol or "").strip()
        ok, result, _err = _unwrap(info)
        pairs = ((result or {}).get("perps") or {}).get("tradingPairs") or []
        entry = next((p for p in pairs
                      if str(p.get("market", "")).strip() == sym), None)
        if entry is None:
            raise RuntimeError(f"[{self.name}] {sym} not found on Ondo")
        # precision comes from the metric STRINGS, not a float round-trip
        base_inc = str(entry.get("baseIncrement") or "0.01")
        quote_inc = str(entry.get("quoteIncrement") or "0.01")
        self.symbol = str(entry["market"])
        self.size_decimals = _decimals_of(base_inc)
        self.price_decimals = _decimals_of(quote_inc)
        self.step_size = float(base_inc)
        self.tick_size = float(quote_inc)
        self.min_base = self.step_size     # smallest on-grid quantity
        self.min_quote = 10.0
        c = self.conf.ondo_creds
        if c and c.complete:
            # non-fatal signer setup so record-only → live upgrades share one
            # code path
            self.account = OndoAccount(c.key_id, c.secret)
        log.info("[%s] %s tick=%g step=%g pxDec=%d szDec=%d fee=%.1fbps",
                 self.name, self.symbol, self.tick_size, self.step_size,
                 self.price_decimals, self.size_decimals, self.fee_bps)

    def init_signer(self) -> None:
        c = self.conf.ondo_creds
        assert c is not None and c.complete, f"[{self.name}] missing credentials"
        if self.account is None:          # load_market already built it when
            self.account = OndoAccount(c.key_id, c.secret)
        log.info("[%s] key=%s", self.name, self.account.key_id)

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        return [asyncio.create_task(
            OndoBookFeed(self.name, self.ws_url, self.symbol, self.book,
                         notify).run(stop),
            name=f"book-{self.key}")]

    def ready_to_trade(self) -> bool:
        return self.account is not None

    async def warm_http(self) -> None:
        """Order-path keepalive ping (driven by the engine's keepalive loop)."""
        try:
            await self._public_get("/status")
        except Exception as e:
            log.debug("[%s] keepalive ping failed: %r", self.name, e)

    # ------------------------------------------------------------ price grid

    def px_round(self, px: float, round_up: bool) -> float:
        if px <= 0:
            return px
        f = 10.0 ** self.price_decimals
        v = math.ceil(px * f - 1e-9) / f if round_up else math.floor(px * f + 1e-9) / f
        return round(v, 8)

    # ------------------------------------------------------------- execution

    def _next_cloid(self) -> str:
        self._cloid += 1
        return f"o{self._cloid}"          # ^[A-Za-z0-9_-]{1,64}$

    async def send_taker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False) -> dict:
        assert self.account is not None and self.tick_size > 0
        if self.step_size > 0:
            # the engine floors to the pair-wide step; re-floor to OUR grid in
            # case the other leg's is finer — an off-grid qty is a rejection
            floored = floor_step(qty, self.step_size)
            if floored < self.min_base:
                return {"status": "send-failed", "filled_base": 0.0,
                        "avg_px": None, "err": f"qty {qty} below min base "
                        f"{self.min_base} after step flooring",
                        "unresolved": False}
            qty = floored
        coid = self._next_cloid()
        order = {"market": self.symbol,
                 "side": "buy" if is_buy else "sell",
                 "type": "limit",
                 "timeInForce": "IOC",
                 "price": f"{limit_px:.{self.price_decimals}f}",
                 "size": f"{qty:.{self.size_decimals}f}",
                 "reduceOnly": bool(reduce_only),
                 "clientOrderId": coid}

        body, err, unresolved = await self._signed_request(
            "POST", "/v1/perps/orders", json_body=order)
        if err is not None:
            return _err_result("send-failed", err)
        if not unresolved:
            ok, result, app_err = _unwrap(body)
            if not ok:
                return _err_result("send-failed", app_err or "rejected")
            res = self._parse_order(result)
            if res is not None:
                return res
        # unknown outcome (or non-terminal status): poll by client id
        deadline = time.time() + self.settle_timeout
        while time.time() < deadline:
            try:
                od = await self._query_order(coid)
            except Exception:
                od = None
            if od is not None and str(od.get("status", "")) in TERMINAL:
                return self._fill_result(od)
            await asyncio.sleep(POLL_INTERVAL)
        return {"status": "timeout", "filled_base": 0.0, "avg_px": None,
                "err": None, "unresolved": True}

    @staticmethod
    def _parse_order(o) -> Optional[dict]:
        """Terminal order → unified result, or None when the status is not yet
        terminal (the caller keeps polling). A reject at the application layer
        is treated as a clean miss only when the order never filled."""
        if not isinstance(o, dict):
            return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                    "err": f"unexpected response: {str(o)[:200]}",
                    "unresolved": False}
        if str(o.get("status", "")) in TERMINAL:
            return OndoVenue._fill_result(o)
        return None                      # open/pending → poll

    @staticmethod
    def _fill_result(o: dict) -> dict:
        """Terminal ApiOrder → unified result. An IOC that expired with a
        partial fill is economically a fill (the engine books filled_base)."""
        filled = _f(o.get("filledSize"))
        if filled <= 0:
            return {"status": "canceled", "filled_base": 0.0, "avg_px": None,
                    "err": None, "unresolved": False}
        return {"status": "filled", "filled_base": filled,
                "avg_px": _avg_fill_px(o), "err": None, "unresolved": False}

    async def _query_order(self, coid: str) -> Optional[dict]:
        body, err, unresolved = await self._signed_request(
            "GET", f"/v1/perps/orders/client:{coid}")
        if err is not None or unresolved:
            return None
        ok, result, _app_err = _unwrap(body)
        return result if ok and isinstance(result, dict) else None

    # -------------------------------------------------------------- accounts

    async def fetch_equity(self):
        if self.account is None:
            return None
        body, err, unresolved = await self._signed_request(
            "GET", "/v1/perps/balance")
        if err is not None or unresolved:
            log.warning("[%s] equity fetch failed: %s", self.name,
                        err or "unresolved")
            return None
        ok, result, app_err = _unwrap(body)
        if not ok or not isinstance(result, dict):
            log.warning("[%s] equity fetch failed: %s", self.name,
                        app_err or str(body)[:120])
            return None
        return (float(result.get("marginBalance") or 0.0),
                float(result.get("availableMargin") or 0.0))

    async def fetch_position(self) -> float:
        assert self.account is not None
        body, err, unresolved = await self._signed_request(
            "GET", "/v1/perps/positions")
        if err is not None or unresolved:
            raise RuntimeError(f"[{self.name}] position fetch failed: "
                               f"{err or 'unresolved'}")
        ok, result, app_err = _unwrap(body)
        if not ok:
            raise RuntimeError(f"[{self.name}] position fetch failed: "
                               f"{app_err}")
        positions = result if isinstance(result, list) \
            else (result or {}).get("positions") or []
        for p in positions:
            if str(p.get("market")) != self.symbol:
                continue
            qty = _f(p.get("netQuantity"))
            direction = str(p.get("direction") or "neutral")
            if direction == "short":
                return -abs(qty)        # signed: + long / - short
            if direction == "long":
                return abs(qty)
            return 0.0                 # neutral
        return 0.0

    async def close(self) -> None:
        pass


def _unwrap(body):
    """GenericResponse{success,error,error_code,result} → (ok, result, err)."""
    if not isinstance(body, dict):
        return True, body, None
    if body.get("success") is False:
        return False, None, str(body.get("error_code")
                                or body.get("error") or "unknown error")
    return True, body.get("result", body), None


def _err_result(status: str, msg: str) -> dict:
    """Reject → unified dict, mapping margin-class codes to the engine's
    pause status (the engine pauses a venue whose status contains "margin")."""
    low = msg.lower()
    if any(k in low for k in MARGIN_ERRORS) or "liquidat" in low:
        return {"status": "margin", "filled_base": 0.0, "avg_px": None,
                "err": msg, "unresolved": False}
    return {"status": status, "filled_base": 0.0, "avg_px": None,
            "err": msg, "unresolved": False}


def _avg_fill_px(o: dict) -> Optional[float]:
    """filledCost = filledSize × fill price, so the VWAP is their ratio. The
    orders endpoint carries no explicit average price."""
    filled = _f(o.get("filledSize"))
    cost = _f(o.get("filledCost"))
    if filled > 0 and cost > 0:
        return cost / filled
    return None


def _f(x) -> float:
    """Tolerant float('') for the API's decimal-string fields."""
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _decimals_of(s: str) -> int:
    """Decimal places of a numeric string like '0.0010' → 3."""
    s = s.strip()
    if "." not in s:
        return 0
    return len(s.rstrip("0").split(".")[1])