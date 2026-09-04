"""Polymarket Perps venue adapter (api.perpetuals.polymarket.com, pUSD-margined).

Market metadata and the book come from public REST + the official websocket
(see feeds.PolymarketBookFeed) — no credentials needed for --record-only.
Trading authenticates with a proxy wallet (one-time createProxy ceremony, see
tools/polymarket_make_proxy.py): private GETs carry the polymarket-proxy /
polymarket-secret headers; /v1/trade/* routes additionally EIP-712-sign (with
`eth_account`) the keccak256 of a MessagePack-encoded compact op array.

IOC limit orders are acknowledged synchronously ({"status":"ok","oid":N}) but
the ack is NOT the fill state, and the orders endpoint carries no average
price — send_taker() polls the order by client id until terminal and computes
the fill VWAP from /v1/account/fills, so the engine sees the same unified
result shape as the other venues: {status, filled_base, avg_px, err,
unresolved}.

Prices obey a dual constraint: <= price_decimals AND <= 5 significant figures
for non-integer prices (ETH at ~2500 with pdec=2 trades on a 0.1 grid, not
0.01) — px_round()/_fmt_px() derive the effective grid from the magnitude.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Optional

import aiohttp

from .book import OrderBook, floor_step
from .config import POLYMARKET_API_URL, POLYMARKET_WS_URL, VenueConf
from .feeds import PolymarketBookFeed

log = logging.getLogger("polymarket")

REST_TIMEOUT = 10.0
POLL_INTERVAL = 0.5        # unresolved-order polling cadence (same as HL/Aster)
FILL_RETRIES = 3           # fills can lag the terminal order status
FILL_RETRY_SEC = 0.25

# Terminal order statuses per the trading docs (IOC never rests).
TERMINAL = {"filled", "ioc_no_fill", "ioc_expired", "stp_cancelled",
            "canceled", "rejected"}

# Margin-class rejections — the engine pauses the venue on status "margin".
MARGIN_ERRORS = ("insufficient_margin", "margin_below_required_initial",
                 "account_liquidating", "insufficient_balance")

# EIP-712 Op typed data over the msgpack op hash (chainId 137 = Polygon, even
# though perps settle nowhere near it — the domain is just an anti-replay tag).
EIP712_DOMAIN = {"name": "Polymarket", "version": "1", "chainId": 137}
EIP712_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
    ],
    "Op": [
        {"name": "data", "type": "bytes32"},
        {"name": "salt", "type": "uint64"},
        {"name": "ts", "type": "uint64"},
    ],
}


def _op_hash(compact: list) -> bytes:
    """keccak256 of the MessagePack-encoded compact op — the exact bytes the
    server re-derives. Compact rows hold only ints/bools/strs, where python
    msgpack and the TS reference (@msgpack/msgpack) encode identically."""
    import msgpack                          # lazy — live only
    from eth_utils import keccak
    return keccak(msgpack.packb(compact))


def _compact_create(iid: int, buy: bool, p: str, qty: str, ro: bool,
                    c: str) -> list:
    """Compact createOrders op — fixed slot order, p BEFORE qty, undefined
    entries dropped (a taker order always carries all eight slots)."""
    return ["createOrders", [[iid, buy, p, qty, "ioc", False, ro, c]]]


def _compact_cancel(coid: str) -> list:
    return ["cancelOrdersCOID", [coid]]


class PolymarketAccount:
    """Proxy-wallet signer: private ops are EIP-712 signed with the PROXY key
    (never the owner key), which is what the createProxy ceremony delegates."""

    def __init__(self, proxy_address: str, proxy_secret: str,
                 private_key: str) -> None:
        from eth_account import Account      # lazy — live only
        self._Account = Account
        self.proxy = proxy_address.lower()
        self.secret = proxy_secret
        self.wallet = Account.from_key(private_key)

    def sign_op(self, compact: list) -> tuple:
        """Sign the op hash → (sig, salt, ts). The bytes32 message field takes
        RAW 32 bytes — a hex string would fail eth_account's bounds check."""
        import secrets
        op_hash = _op_hash(compact)
        salt = secrets.randbits(63)
        ts = int(time.time() * 1000)
        signed = self._Account.sign_typed_data(
            self.wallet.key,
            full_message={"types": EIP712_TYPES, "primaryType": "Op",
                          "domain": EIP712_DOMAIN,
                          "message": {"data": op_hash, "salt": salt, "ts": ts}})
        return "0x" + signed.signature.hex(), salt, ts


class PolymarketVenue:
    kind = "polymarket"

    def __init__(self, conf: VenueConf, session: aiohttp.ClientSession,
                 settle_timeout_sec: float) -> None:
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.api_url = POLYMARKET_API_URL
        self.ws_url = POLYMARKET_WS_URL
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
        self.account: Optional[PolymarketAccount] = None
        self.symbol = ""          # exchange-native, e.g. "BTC-USD"
        self.iid = -1             # instrument id — required on every wire op
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

    async def _private_get(self, path: str,
                           params: Optional[dict] = None):
        """Header-authenticated account read → (body, err, unresolved) with
        the same tri-classification as _signed_request."""
        assert self.account is not None
        headers = {"polymarket-proxy": self.account.proxy,
                   "polymarket-secret": self.account.secret}
        try:
            async with self.session.get(
                    self.api_url + path, params=params, headers=headers,
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

    async def _signed_request(self, method: str, path: str, op: dict,
                              compact: list):
        """Signed /v1/trade/* call: classification mirrors HLVenue/
        AsterVenue — signing failure or 429 → RATE_LIMITED err, other 4xx →
        err, 5xx/network/JSON → unresolved (the order may or may not have
        landed; send_taker polls it out)."""
        assert self.account is not None
        try:
            sig, salt, ts = self.account.sign_op(compact)
        except Exception as e:
            return None, f"signing failed: {e!r}", False
        body = {"op": op, "sig": sig, "salt": salt, "ts": ts}
        try:
            async with self.session.request(
                    method, self.api_url + path, json=body,
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
        insts = await self._public_get("/v1/info/instruments")
        sym = (self.conf.symbol or "").strip()
        entry = next((i for i in insts
                      if str(i.get("symbol", "")).strip() == sym), None)
        if entry is None and "-" not in sym:
            # CLI symbol "BTC" → the BTC-USD instrument (any quote would do;
            # more than one candidate is ambiguous, not a guess we make)
            cands = [i for i in insts
                     if str(i.get("base_asset", "")).strip() == sym]
            if len(cands) == 1:
                entry = cands[0]
            elif len(cands) > 1:
                raise RuntimeError(
                    f"[{self.name}] {sym} is ambiguous on Polymarket: "
                    f"{[c.get('symbol') for c in cands]} — use the full "
                    f"symbol or symbol_map.yaml")
        if entry is None:
            raise RuntimeError(f"[{self.name}] {sym} not found on Polymarket")
        if entry.get("instrument_type") != "perpetual":
            raise RuntimeError(f"[{self.name}] {sym} is not a perpetual "
                               f"({entry.get('instrument_type')!r})")
        self.symbol = str(entry["symbol"])
        self.iid = int(entry["instrument_id"])
        self.price_decimals = int(entry["price_decimals"])
        self.size_decimals = int(entry["quantity_decimals"])
        self.tick_size = 10.0 ** -self.price_decimals
        self.step_size = 10.0 ** -self.size_decimals
        self.min_base = self.step_size     # smallest on-grid quantity
        self.min_quote = float(entry["min_notional"])
        c = self.conf.polymarket_creds
        if c and c.complete:
            # non-fatal preflight: real taker fee (never lowered — a smaller
            # fee would systematically loosen the thresholds) + signer setup
            # so record-only → live upgrades share one code path
            try:
                self.account = PolymarketAccount(c.proxy_address, c.proxy_secret,
                                                 c.private_key)
                await self._calibrate_fee(entry)
            except Exception as e:
                log.warning("[%s] fee/signer preflight failed: %r — keeping "
                            "%.1f bps default", self.name, e, self.fee_bps)
                self.account = None
        log.info("[%s] %s iid=%d tick=%g pxDec=%d szDec=%d minNtl=$%g "
                 "fee=%.1fbps", self.name, self.symbol, self.iid,
                 self.tick_size, self.price_decimals, self.size_decimals,
                 self.min_quote, self.fee_bps)

    async def _calibrate_fee(self, entry: dict) -> None:
        """Public fee schedule, matched by instrument category. The schedule
        currently omits some categories (crypto at the time of writing) —
        missing means keep the default, never lower it."""
        info = await self._public_get("/v1/info/fees")
        schedule = (info or {}).get("fee_schedule") or []
        row = next((r for r in schedule
                    if r.get("instrument_type") == entry.get("instrument_type")
                    and r.get("category") == entry.get("category")), None)
        if row is None:
            log.warning("[%s] no %s/%s entry in the fee schedule — keeping "
                        "%.1f bps default", self.name,
                        entry.get("instrument_type"), entry.get("category"),
                        self.fee_bps)
            return
        actual = float(row.get("taker_fee_rate") or 0) * 1e4
        if actual > self.fee_bps:
            log.info("[%s] taker fee calibrated: %.1f → %.1f bps",
                     self.name, self.fee_bps, actual)
            self.fee_bps = actual

    def init_signer(self) -> None:
        c = self.conf.polymarket_creds
        assert c is not None and c.complete, \
            f"[{self.name}] missing credentials"
        if self.account is None:          # load_market already built it when
            self.account = PolymarketAccount(  # creds were present
                c.proxy_address, c.proxy_secret, c.private_key)
        log.info("[%s] proxy=%s%s", self.name, self.account.proxy,
                 "" if self.account.proxy == self.account.wallet.address.lower()
                 else f" (signer={self.account.wallet.address.lower()})")

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        return [asyncio.create_task(
            PolymarketBookFeed(self.name, self.ws_url, self.iid, self.book,
                               notify).run(stop),
            name=f"book-{self.key}")]

    def ready_to_trade(self) -> bool:
        return self.account is not None

    async def warm_http(self) -> None:
        """Order-path keepalive ping (driven by the engine's keepalive loop)."""
        try:
            await self._public_get("/v1/info/ping")
        except Exception as e:
            log.debug("[%s] keepalive ping failed: %r", self.name, e)

    # ------------------------------------------------------------ price grid

    def _grid_decimals(self, px: float, max_decimals: Optional[int] = None):
        """Effective grid under the 5-significant-figure rule: at magnitude
        10^e at most 4-e decimals survive (ETH 2500.85, pdec=2, e=3 → 0.1
        grid). max_decimals is the venue's own precision cap (price_decimals
        for prices, size_decimals for quantities)."""
        if px <= 0:
            return self.price_decimals if max_decimals is None else max_decimals
        cap = self.price_decimals if max_decimals is None else max_decimals
        e = math.floor(math.log10(px))
        return max(0, min(cap, 4 - e))

    def px_round(self, px: float, round_up: bool) -> float:
        if px <= 0:
            return px
        d = self._grid_decimals(px)
        f = 10.0 ** d
        v = math.ceil(px * f - 1e-9) / f if round_up \
            else math.floor(px * f + 1e-9) / f
        # re-derive at the ROUNDED magnitude — a ceil can cross a power of ten
        # and tighten the grid (9999.95 → 10000 is integer, 5 sig figs legal)
        v = round(v, 8)
        d2 = self._grid_decimals(v)
        if d2 < d:
            f2 = 10.0 ** d2
            v = math.ceil(v * f2 - 1e-9) / f2 if round_up \
                else math.floor(v * f2 + 1e-9) / f2
            v = round(v, 8)
        return v

    def _fmt_px(self, px: float) -> str:
        """Wire price: on-grid, no trailing zeros (integer prices carry no
        decimal point and are exempt from the sig-fig cap)."""
        s = f"{self.px_round(px, False):.{self.price_decimals}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s or "0"

    def _fmt_qty(self, qty: float) -> str:
        s = f"{qty:.{self.size_decimals}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s or "0"

    def _floor_qty(self, qty: float) -> float:
        """Floor to OUR grid — the engine floors to the pair-wide step, which
        can be finer than this venue's own precision or 5-sig-fig quantity
        grid (both constraints apply to quantities too)."""
        if self.step_size > 0:
            qty = floor_step(qty, self.step_size)
        d = self._grid_decimals(qty, max_decimals=self.size_decimals)
        if d < self.size_decimals:
            f = 10.0 ** d
            qty = math.floor(qty * f + 1e-9) / f
        return qty

    # ------------------------------------------------------------- execution

    def _next_cloid(self) -> str:
        self._cloid += 1
        return f"{self._cloid:032x}"        # ^[0-9a-f]{32}$, never all zeros

    async def send_taker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False) -> dict:
        assert self.account is not None and self.iid >= 0
        qty = self._floor_qty(qty)
        if qty < self.min_base:
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": f"qty {qty} below min base "
                    f"{self.min_base} after step flooring", "unresolved": False}
        coid = self._next_cloid()
        sent_ms = int(time.time() * 1000)
        p, q = self._fmt_px(limit_px), self._fmt_qty(qty)
        args = [{"iid": self.iid, "buy": bool(is_buy), "p": p, "qty": q,
                 "tif": "ioc", "po": False, "ro": bool(reduce_only), "c": coid}]
        op = {"type": "createOrders", "args": args}
        compact = _compact_create(self.iid, bool(is_buy), p, q,
                                  bool(reduce_only), coid)

        body, err, unresolved = await self._signed_request(
            "POST", "/v1/trade/orders", op, compact)
        if err is not None:
            return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                    "err": err, "unresolved": False}
        if not unresolved:
            rej = self._parse_ack(body)
            if rej is not None:
                return rej
        # accepted (or unreadable — the order may exist, never assume a clean
        # failure): the ack is NOT the fill state, poll by client id
        deadline = time.time() + self.settle_timeout
        while time.time() < deadline:
            try:
                od = await self._query_order(coid)
            except Exception:
                od = None
            if od is not None and str(od.get("status", "")) in TERMINAL:
                return await self._fill_result(od, sent_ms)
            await asyncio.sleep(POLL_INTERVAL)
        return {"status": "timeout", "filled_base": 0.0, "avg_px": None,
                "err": None, "unresolved": True}

    @staticmethod
    def _parse_ack(body) -> Optional[dict]:
        """Explicit venue rejection from the create ack, or None when the
        order was accepted (or the body is unreadable — then the outcome is
        unknown and the caller must poll, never assume a clean failure)."""
        if not isinstance(body, list) or not body:
            return None
        r = body[0]
        if not isinstance(r, dict) or r.get("status") != "err":
            return None
        msg = str(r.get("error") or r.get("msg") or "unknown error")
        low = msg.lower()
        if any(k in low for k in MARGIN_ERRORS) or "liquidat" in low:
            return {"status": "margin", "filled_base": 0.0, "avg_px": None,
                    "err": msg, "unresolved": False}
        if "rate_limited" in low or "rate limit" in low or "too many" in low:
            return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                    "err": "RATE_LIMITED: " + msg, "unresolved": False}
        return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                "err": msg, "unresolved": False}

    async def _query_order(self, coid: str) -> Optional[dict]:
        body, err, unresolved = await self._private_get(
            "/v1/account/orders", {"client_order_id": coid})
        if err is not None or unresolved or not isinstance(body, list):
            return None
        for o in body:
            if isinstance(o, dict) and o.get("client_order_id") == coid:
                return o
        return None          # not yet ingested — keep polling

    async def _fill_result(self, od: dict, sent_ms: int) -> dict:
        """Terminal OrderData → unified result. An IOC that expired with a
        partial fill is economically a fill (the engine books filled_base)."""
        filled = _f(od.get("filled_quantity"))
        if filled <= 0:
            return {"status": "canceled", "filled_base": 0.0, "avg_px": None,
                    "err": None, "unresolved": False}
        avg = await self._avg_fill_px(od.get("order_id"), sent_ms, filled)
        return {"status": "filled", "filled_base": filled, "avg_px": avg,
                "err": None, "unresolved": False}

    async def _avg_fill_px(self, order_id, sent_ms: int,
                           expect: float) -> Optional[float]:
        """VWAP from /v1/account/fills — the orders endpoint has no avg price.
        The endpoint has no order_id filter either (client-side match on the
        order_id + a timestamp window); fills can lag the terminal status, so
        retry briefly while the visible quantity is short."""
        for attempt in range(FILL_RETRIES):
            body, err, unresolved = await self._private_get(
                "/v1/account/fills",
                {"start_timestamp": sent_ms - 5000})
            if err is None and not unresolved and isinstance(body, dict):
                notional = qty = 0.0
                for t in body.get("data") or []:
                    if t.get("order_id") != order_id:
                        continue
                    if int(_f(t.get("instrument_id"))) != self.iid:
                        continue
                    p, q = _f(t.get("price")), _f(t.get("quantity"))
                    notional += p * q
                    qty += q
                if qty > 0 and qty >= expect - 1e-12:
                    return notional / qty
                if qty > 0 and attempt == FILL_RETRIES - 1:
                    return notional / qty   # partial visibility beats nothing
            if attempt < FILL_RETRIES - 1:
                await asyncio.sleep(FILL_RETRY_SEC)
        log.warning("[%s] no fills visible for order %s (filled %.8g) — "
                    "avg_px unknown", self.name, order_id, expect)
        return None

    async def cancel_order(self, coid: str):
        """Cancel by client order id — used by tests and the signing smoke
        check (cancelling a non-existent coid proves the signature chain)."""
        assert self.account is not None
        op = {"type": "cancelOrdersCOID", "args": [coid]}
        return await self._signed_request(
            "DELETE", "/v1/trade/orders-coid", op, _compact_cancel(coid))

    # -------------------------------------------------------------- accounts

    async def fetch_equity(self):
        if self.account is None:
            return None
        body, err, unresolved = await self._private_get("/v1/account/portfolio")
        if err is not None or unresolved or not isinstance(body, dict):
            log.warning("[%s] equity fetch failed: %s", self.name,
                        err or str(body)[:120])
            return None
        m = body.get("margin") or {}
        return (float(m.get("total_account_value") or 0.0),
                float(m.get("available_order_margin") or 0.0))

    async def fetch_position(self) -> float:
        assert self.account is not None
        body, err, unresolved = await self._private_get("/v1/account/portfolio")
        if err is not None or unresolved or not isinstance(body, dict):
            raise RuntimeError(f"[{self.name}] position fetch failed: "
                               f"{err or str(body)[:120]}")
        for p in body.get("positions") or []:
            if int(_f(p.get("instrument_id"))) == self.iid:
                return _f(p.get("size"))    # signed: + long / - short
        return 0.0

    async def close(self) -> None:
        pass


def _f(x) -> float:
    """Tolerant float('') for the API's decimal-string fields."""
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0
