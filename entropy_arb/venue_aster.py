"""Aster DEX V3 futures venue adapter (fapi.asterdex.com, USDT-margined).

Market metadata and the book come from public REST + the official websocket
(see feeds.AsterBookFeed) — no signing library needed. Trading and account
queries sign requests EIP-712 with `eth_account` (already a live dependency);
--record-only data collection needs neither the credentials nor the library.

IOC limit orders (newOrderRespType=RESULT) settle synchronously in the order
response; unknown outcomes (5xx / 503 "status unknown" / timeout) fall back
to order-status polling inside send_taker(), so the engine sees the same
unified result shape as the other venues: {status, filled_base, avg_px, err,
unresolved}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import urllib.parse
from typing import Optional

import aiohttp

from .book import OrderBook, floor_step
from .config import ASTER_API_URL, ASTER_WS_URL, VenueConf
from .feeds import AsterBookFeed

log = logging.getLogger("aster")

REST_TIMEOUT = 10.0
POLL_INTERVAL = 0.5        # unresolved-order polling cadence (same as HL)

# Terminal order statuses per the V3 ENUM definitions (IOC never rests).
TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}

# Signed requests: the urlencoded param string IS the EIP-712 message.
EIP712_DOMAIN = {
    "name": "AsterSignTransaction",
    "version": "1",
    "chainId": 1666,
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}
EIP712_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Message": [{"name": "msg", "type": "string"}],
}


class AsterNonceAllocator:
    """Microsecond nonce, strictly increasing (Aster: per agent address)."""

    def __init__(self) -> None:
        self._last = 0

    def next(self) -> int:
        self._last = max(self._last + 1, time.time_ns() // 1000)
        return self._last


class AsterAccount:
    """EIP-712 signer. `user` is the master wallet, `signer` the API wallet —
    the address pair cannot be derived from one another, so both are required."""

    def __init__(self, private_key: str, account_address: str) -> None:
        from eth_account import Account                      # lazy — live only
        self._Account = Account
        self.wallet = Account.from_key(private_key)
        self.user = account_address.lower()
        self.signer = self.wallet.address.lower()
        self.nonces = AsterNonceAllocator()

    def sign_params(self, params: dict) -> str:
        """Return the final query string: urlencoded params + user/signer/
        nonce + signature. Sign exactly the string that will be sent."""
        nonce = self.nonces.next()
        wire = dict(params)
        wire["user"] = self.user
        wire["signer"] = self.signer
        wire["nonce"] = nonce
        query = urllib.parse.urlencode(wire)
        sig = self._Account.sign_typed_data(
            self.wallet.key,
            full_message={"types": EIP712_TYPES, "primaryType": "Message",
                          "domain": EIP712_DOMAIN, "message": {"msg": query}})
        return f"{query}&signature={sig.signature.hex()}"


class AsterVenue:
    kind = "aster"

    def __init__(self, conf: VenueConf, session: aiohttp.ClientSession,
                 settle_timeout_sec: float) -> None:
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.api_url = ASTER_API_URL
        self.ws_url = ASTER_WS_URL
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
        self.account: Optional[AsterAccount] = None
        self.symbol = ""          # exchange-native, e.g. "BTCUSDT"
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

    async def _signed_request(self, method: str, path: str, params: dict):
        """Classification mirrors HLVenue._post_exchange: signing failure or
        429 → RATE_LIMITED err, other 4xx → err, 5xx/network/JSON →
        unresolved. 503 is explicitly "execution status UNKNOWN" per the API
        docs — never a clean failure."""
        assert self.account is not None
        try:
            query = self.account.sign_params(params)
        except Exception as e:
            return None, f"signing failed: {e!r}", False
        url = f"{self.api_url}{path}?{query}"    # GET and POST both take query
        try:
            async with self.session.request(
                    method, url,
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
        info = await self._public_get("/fapi/v3/exchangeInfo")
        sym = (self.conf.symbol or "").upper()
        entry = next((s for s in info.get("symbols") or []
                      if s.get("symbol") == sym), None)
        if entry is None:
            raise RuntimeError(f"[{self.name}] {sym} not found on Aster")
        if entry.get("status") != "TRADING":
            raise RuntimeError(f"[{self.name}] {sym} status is "
                               f"{entry.get('status')!r}, not TRADING")
        if entry.get("contractType") != "PERPETUAL":
            raise RuntimeError(f"[{self.name}] {sym} is not a perpetual "
                               f"({entry.get('contractType')!r})")
        filters = {f.get("filterType"): f for f in entry.get("filters") or []}
        tick = str(filters["PRICE_FILTER"]["tickSize"])
        step = str(filters["LOT_SIZE"]["stepSize"])
        self.symbol = sym
        self.tick_size = float(tick)
        # decimals from the STRINGS, not the floats (0.00010 → 4, no drift)
        self.price_decimals = _decimals_of(tick)
        self.size_decimals = min(int(entry.get("quantityPrecision") or 0),
                                 _decimals_of(step))
        self.step_size = float(step)
        self.min_base = float(filters["LOT_SIZE"]["minQty"])
        self.min_quote = float(filters["MIN_NOTIONAL"]["notional"])
        if self.conf.aster_creds and self.conf.aster_creds.complete:
            # non-fatal preflight: real taker fee (never lowered — a smaller
            # fee would systematically loosen the thresholds) + signer setup
            # so record-only → live upgrades share one code path
            c = self.conf.aster_creds
            try:
                self.account = AsterAccount(c.private_key, c.account_address)
                await self._calibrate_fee()
            except Exception as e:
                log.warning("[%s] fee/signer preflight failed: %r — keeping "
                            "%.1f bps default", self.name, e, self.fee_bps)
                self.account = None
        log.info("[%s] %s tick=%g szDec=%d minQty=%g minNtl=$%g fee=%.1fbps",
                 self.name, self.symbol, self.tick_size, self.size_decimals,
                 self.min_base, self.min_quote, self.fee_bps)

    async def _calibrate_fee(self) -> None:
        body, err, unresolved = await self._signed_request(
            "GET", "/fapi/v3/commissionRate",
            {"symbol": self.symbol})
        if err is not None or unresolved or not isinstance(body, dict):
            raise RuntimeError(f"commissionRate failed: {err or body}")
        actual = float(body.get("takerCommissionRate") or 0) * 1e4
        if actual > self.fee_bps:
            log.info("[%s] taker fee calibrated: %.1f → %.1f bps",
                     self.name, self.fee_bps, actual)
            self.fee_bps = actual

    def init_signer(self) -> None:
        c = self.conf.aster_creds
        assert c is not None and c.complete, f"[{self.name}] missing credentials"
        if self.account is None:          # load_market already built it when
            self.account = AsterAccount(c.private_key,  # creds were present
                                        c.account_address)
        log.info("[%s] signer=%s user=%s%s", self.name,
                 self.account.signer, self.account.user,
                 "" if self.account.signer == self.account.user
                 else " (agent mode)")

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        return [asyncio.create_task(
            AsterBookFeed(self.name, self.ws_url, self.conf.symbol, self.book,
                          notify).run(stop),
            name=f"book-{self.key}")]

    def ready_to_trade(self) -> bool:
        return self.account is not None

    async def warm_http(self) -> None:
        """Order-path keepalive ping (driven by the engine's keepalive loop)."""
        try:
            await self._public_get("/fapi/v3/time")
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
        return f"a{self._cloid}"          # ^[\.A-Z\:/a-z0-9_-]{1,36}$

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
        cloid = self._next_cloid()
        params = {"symbol": self.symbol,
                  "side": "BUY" if is_buy else "SELL",
                  "type": "LIMIT",
                  "timeInForce": "IOC",
                  "quantity": f"{qty:.{self.size_decimals}f}",
                  "price": f"{limit_px:.{self.price_decimals}f}",
                  "reduceOnly": "true" if reduce_only else "false",
                  "newClientOrderId": cloid,
                  "newOrderRespType": "RESULT"}

        body, err, unresolved = await self._signed_request(
            "POST", "/fapi/v3/order", params)
        if err is not None:
            return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                    "err": err, "unresolved": False}
        if not unresolved:
            res = self._parse(body)
            if not res.get("unresolved"):
                return res
        # unknown outcome: poll order status by client id until the deadline
        deadline = time.time() + self.settle_timeout
        while time.time() < deadline:
            try:
                st = await self._query_order(cloid)
            except Exception:
                st = None
            if st and str(st.get("status", "")) in TERMINAL:
                return self._fill_result(st)
            await asyncio.sleep(POLL_INTERVAL)
        return {"status": "timeout", "filled_base": 0.0, "avg_px": None,
                "err": None, "unresolved": True}

    async def _query_order(self, cloid: str) -> Optional[dict]:
        body, err, unresolved = await self._signed_request(
            "GET", "/fapi/v3/order",
            {"symbol": self.symbol, "origClientOrderId": cloid})
        if err is not None or unresolved:
            return None
        # not-found (-2013) just means not yet ingested — keep polling
        return body if isinstance(body, dict) and body.get("status") else None

    @staticmethod
    def _parse(body: dict) -> dict:
        def fail(msg: str) -> dict:
            low = msg.lower()
            if "rate limit" in low or "too many" in low:
                msg = "RATE_LIMITED: " + msg
            return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                    "err": msg, "unresolved": False}

        if not isinstance(body, dict):
            return fail(f"unexpected response: {str(body)[:200]}")
        code = body.get("code", 0)
        if isinstance(code, int) and code < 0:
            msg = str(body.get("msg") or code)
            # -2019: margin exhausted — the engine pauses the venue on this
            if "margin" in msg.lower():
                return {"status": "margin", "filled_base": 0.0, "avg_px": None,
                        "err": msg, "unresolved": False}
            return fail(msg)
        status = str(body.get("status") or "")
        if status == "FILLED":
            return {"status": "filled",
                    "filled_base": float(body.get("executedQty") or 0),
                    "avg_px": _avg_px(body), "err": None, "unresolved": False}
        if status in TERMINAL:
            return AsterVenue._fill_result(body)
        # RESULT+IOC should always terminate; NEW/PARTIALLY_FILLED is
        # defensive — treat as unsettled so send_taker keeps polling
        return {"status": "resting?", "filled_base": 0.0, "avg_px": None,
                "err": None, "unresolved": True}

    @staticmethod
    def _fill_result(o: dict) -> dict:
        """Terminal order → unified result. An IOC that expired with a partial
        fill is economically a fill (the engine books filled_base)."""
        filled = float(o.get("executedQty") or 0)
        if filled > 0:
            return {"status": "filled", "filled_base": filled,
                    "avg_px": _avg_px(o), "err": None, "unresolved": False}
        return {"status": "canceled", "filled_base": 0.0, "avg_px": None,
                "err": None, "unresolved": False}

    # -------------------------------------------------------------- accounts

    async def fetch_equity(self):
        if self.account is None:
            return None
        body, err, unresolved = await self._signed_request(
            "GET", "/fapi/v3/accountWithJoinMargin", {})
        if err is not None or unresolved or not isinstance(body, dict):
            log.warning("[%s] equity fetch failed: %s", self.name,
                        err or str(body)[:120])
            return None
        return (float(body.get("totalMarginBalance") or 0.0),
                float(body.get("availableBalance") or 0.0))

    async def fetch_position(self) -> float:
        assert self.account is not None
        body, err, unresolved = await self._signed_request(
            "GET", "/fapi/v3/positionRisk", {"symbol": self.symbol})
        if err is not None or unresolved or not isinstance(body, list):
            raise RuntimeError(f"[{self.name}] position fetch failed: "
                               f"{err or str(body)[:120]}")
        pos = 0.0
        for p in body:
            side = p.get("positionSide")
            if side not in (None, "BOTH"):
                # one-way mode required: hedge mode would change the meaning
                # of positionAmt and reduceOnly — refuse rather than guess
                raise RuntimeError(
                    f"[{self.name}] account is in Hedge position mode "
                    f"(positionSide={side}) — switch it to One-way in the "
                    f"Aster app / 账户处于双向持仓模式，请在 Aster APP 中"
                    f"切换为单向持仓")
            pos = float(p.get("positionAmt") or 0.0)
        return pos

    async def close(self) -> None:
        pass


def _avg_px(o: dict) -> Optional[float]:
    """avgPrice is "0" when nothing filled (or unknown) — normalize to None so
    the engine falls back to the plan limit for accounting."""
    try:
        avg = float(o.get("avgPrice") or 0)
    except (TypeError, ValueError):
        return None
    return avg if avg > 0 else None


def _decimals_of(s: str) -> int:
    """Decimal places of a numeric string like '0.00010' → 4."""
    s = s.strip()
    if "." not in s:
        return 0
    return len(s.rstrip("0").split(".")[1])
