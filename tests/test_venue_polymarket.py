"""Polymarket Perps venue adapter unit tests — no network, no real credentials.

Signing is exercised against a throwaway proxy key and verified independently
via eth_account's recover (the same check the Polymarket backend performs).
The msgpack+keccak op hash is pinned to a fixed vector so an encoder drift
(e.g. a msgpack upgrade changing int/str encoding) fails loudly here instead
of as "invalid signature" against the live API. Run:
python3 -m pytest tests/test_venue_polymarket.py
"""
import asyncio
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from entropy_arb.book import OrderBook                      # noqa: E402
from entropy_arb.config import VenueConf                    # noqa: E402
from entropy_arb.feeds import PolymarketBookFeed            # noqa: E402
from entropy_arb.venue_polymarket import (                  # noqa: E402
    PolymarketAccount, PolymarketVenue, _compact_cancel, _compact_create,
    _op_hash)

# throwaway key — signs nothing real
KEY = "0x" + "11" * 32
PROXY = "0x63DD5aCC6b1aa0f563956C0e534DD30B6dcF7C4e"
SECRET = "test-secret"
COID_RE = re.compile(r"^[0-9a-f]{32}$")


def make_venue(**kw) -> PolymarketVenue:
    conf = VenueConf(key=kw.pop("key", "hedge"), kind="polymarket",
                     label="POLY", symbol=kw.pop("symbol", "BTC-USD"),
                     fee_bps=kw.pop("fee_bps", 4.0), cap_usd=1000.0,
                     orders_per_min=120, **kw)
    return PolymarketVenue(conf, session=None,
                           settle_timeout_sec=kw.pop("settle_timeout_sec", 1.0))


def run(coro):
    return asyncio.run(coro)


def signed_venue(**kw) -> PolymarketVenue:
    v = make_venue(**kw)
    v.account = PolymarketAccount(PROXY, SECRET, KEY)
    v.iid = 6
    return v


# ------------------------------------------------------------- primitives

def test_op_hash_known_vector():
    # pinned: a change in msgpack encoding or the compact shape breaks this
    h = _op_hash(_compact_cancel("f" * 32))
    assert h.hex() == "81e8b13f56f997e2e8310f28f0fd53e0" \
                      "798a4a2919822fba5a9db1530dfced47"
    h2 = _op_hash(_compact_create(6, True, "80755", "0.001", False, "0" * 31 + "1"))
    assert len(h2) == 32 and h2 != h


def test_compact_create_slot_order():
    c = _compact_create(6, True, "80755", "0.001", False, "ab" * 16)
    assert c[0] == "createOrders"
    row = c[1][0]
    # fixed slots: iid, buy, p BEFORE qty, tif, po, ro, c
    assert row == [6, True, "80755", "0.001", "ioc", False, False, "ab" * 16]


def test_sign_op_recovers_proxy_key():
    acct = PolymarketAccount(PROXY, SECRET, KEY)
    compact = _compact_create(6, False, "100", "0.5", True, "cd" * 16)
    sig, salt, ts = acct.sign_op(compact)

    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from entropy_arb.venue_polymarket import EIP712_DOMAIN, EIP712_TYPES
    struct = encode_typed_data(full_message={
        "types": EIP712_TYPES, "primaryType": "Op", "domain": EIP712_DOMAIN,
        "message": {"data": _op_hash(compact), "salt": salt, "ts": ts}})
    assert (Account.recover_message(struct, signature=sig).lower()
            == acct.wallet.address.lower())
    assert sig.startswith("0x") and len(sig) == 132
    assert 0 <= salt < 2 ** 64 and abs(ts - time.time() * 1000) < 60_000


def test_sign_op_salt_varies():
    acct = PolymarketAccount(PROXY, SECRET, KEY)
    compact = _compact_cancel("f" * 32)
    salts = [acct.sign_op(compact)[1] for _ in range(50)]
    assert len(set(salts)) == 50
    assert all(0 <= s < 2 ** 64 for s in salts)


def test_cloid_shape_and_uniqueness():
    v = make_venue()
    ids = [v._next_cloid() for _ in range(10)]
    assert all(COID_RE.match(c) for c in ids)
    assert len(set(ids)) == 10
    assert set(ids[0]) != {"0"}                 # never all zeros


# ------------------------------------------------------------ price grid

def test_grid_decimals():
    v = make_venue()
    v.price_decimals = 2                        # ETH-USD
    assert v._grid_decimals(2500.85) == 1       # 6 sig figs → 0.1 grid
    assert v._grid_decimals(140.12) == 2        # 5 sig figs fit pdec
    v.price_decimals = 1                        # BTC-USD
    assert v._grid_decimals(80755.5) == 0       # 6 sig figs → 1 grid
    assert v._grid_decimals(80755.0) == 0
    v.price_decimals = 4                        # SOL-USD
    assert v._grid_decimals(140.123) == 2       # 6 sig figs at e=2 → 0.01 grid
    assert v._grid_decimals(14.123) == 3        # e=1 → 0.001 grid fits pdec


def test_px_round_sig_figs():
    v = make_venue()
    v.price_decimals = 2                        # ETH-USD at ~2500
    assert v.px_round(2500.85, round_up=False) == 2500.8
    assert v.px_round(2500.85, round_up=True) == 2500.9
    v.price_decimals = 1                        # BTC-USD at ~80k
    assert v.px_round(80755.5, round_up=False) == 80755
    assert v.px_round(80755.5, round_up=True) == 80756
    v.price_decimals = 3                        # SOL-USD at ~140
    assert v.px_round(140.123, round_up=False) == 140.12
    assert v.px_round(140.123, round_up=True) == 140.13
    v.price_decimals = 4                        # SOL-USD, finer pdec
    assert v.px_round(140.123, round_up=False) == 140.12
    assert v.px_round(14.1234, round_up=True) == 14.124
    assert v.px_round(0.0, round_up=True) == 0.0
    # idempotence — a rounded price is already on its own grid
    for px in (2500.85, 80755.5, 140.123, 99.999):
        assert v.px_round(v.px_round(px, True), True) == v.px_round(px, True)


def test_px_round_magnitude_crossing():
    # 9999.95 (pdec=2) → ceil crosses 10000 where the grid tightens to 1;
    # the answer must stay a legal 5-sig-fig price (10000, not 10000.0x)
    v = make_venue()
    v.price_decimals = 2
    assert v.px_round(9999.95, round_up=True) == 10000
    assert v.px_round(9999.95, round_up=False) == 9999.9


def test_fmt_px_strips_trailing_zeros():
    v = make_venue()
    v.price_decimals = 2
    assert v._fmt_px(2500.8) == "2500.8"        # not "2500.80"
    assert v._fmt_px(2501.0) == "2501"          # integer: no decimal point
    v.price_decimals = 0
    assert v._fmt_px(1000.0) == "1000"          # rstrip guard: not "1"
    v.price_decimals = 5
    assert v._fmt_px(0.05) == "0.05"            # not "0.05000"


def test_fmt_qty_and_floor_qty():
    v = make_venue()
    v.size_decimals = 5
    v.step_size = 1e-05
    v.min_base = 1e-05
    assert v._fmt_qty(0.00123) == "0.00123"
    assert v._floor_qty(0.001239) == 0.00123    # own grid flooring
    # 5-sig-fig quantity cap: 0.123456 (7 sf) → 0.12345
    assert v._fmt_qty(v._floor_qty(0.123456)) == "0.12345"


# ------------------------------------------------------------------ _parse_ack

def test_parse_ack_err_margin():
    r = PolymarketVenue._parse_ack(
        [{"status": "err", "error": "insufficient_margin"}])
    assert r["status"] == "margin" and not r["unresolved"]
    r = PolymarketVenue._parse_ack(
        [{"status": "err", "error": "account_liquidating"}])
    assert r["status"] == "margin"


def test_parse_ack_err_rate_limited():
    r = PolymarketVenue._parse_ack(
        [{"status": "err", "error": "action_rate_limited"}])
    assert r["status"] == "send-failed"
    assert r["err"].startswith("RATE_LIMITED: ")


def test_parse_ack_err_other():
    r = PolymarketVenue._parse_ack(
        [{"status": "err", "error": "price exceeds allowed significant figures"}])
    assert r["status"] == "send-failed"
    assert "significant figures" in r["err"]


def test_parse_ack_ok_and_unparseable_return_none():
    # accepted → None (the ack is NOT the fill state — caller polls)
    assert PolymarketVenue._parse_ack([{"status": "ok", "oid": 123}]) is None
    # unreadable body → None too: the order may exist, never a clean failure
    assert PolymarketVenue._parse_ack(None) is None
    assert PolymarketVenue._parse_ack([]) is None
    assert PolymarketVenue._parse_ack({"weird": 1}) is None


# --------------------------------------------------------- _signed_request

class FakeResponse:
    def __init__(self, status, text):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """Returns canned responses; records method/URL/kwargs requested."""

    def __init__(self, statuses_and_texts):
        self.seq = list(statuses_and_texts)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        status, text = self.seq.pop(0)
        if isinstance(text, Exception):
            raise text
        return _resp(FakeResponse(status, text))

    def get(self, url, **kw):
        return self.request("GET", url, **kw)


def _resp(fr):
    async def ctx():
        return fr
    return _Ctx(ctx)


class _Ctx:
    def __init__(self, factory):
        self.factory = factory

    async def __aenter__(self):
        return await self.factory()

    async def __aexit__(self, *a):
        return False


def test_signed_request_classification():
    v = signed_venue()
    op = {"type": "cancelOrdersCOID", "args": ["f" * 32]}

    async def drive():
        out = {}
        s = FakeSession([(429, '{"status":"err","error":"ip_rate_limited"}')])
        v.session = s
        out["429"] = await v._signed_request("DELETE", "/x", op,
                                             _compact_cancel("f" * 32))
        s = FakeSession([(400, '{"status":"err","error":"bad"}')])
        v.session = s
        out["400"] = await v._signed_request("DELETE", "/x", op,
                                             _compact_cancel("f" * 32))
        s = FakeSession([(503, "service unavailable")])
        v.session = s
        out["503"] = await v._signed_request("DELETE", "/x", op,
                                             _compact_cancel("f" * 32))
        s = FakeSession([(200, '[{"status":"ok","oid":1}]')])
        v.session = s
        out["200"] = await v._signed_request("DELETE", "/x", op,
                                             _compact_cancel("f" * 32))
        s = FakeSession([(200, asyncio.TimeoutError())])
        v.session = s
        out["timeout"] = await v._signed_request("DELETE", "/x", op,
                                                 _compact_cancel("f" * 32))
        return out

    out = run(drive())
    err, unres = out["429"][1], out["429"][2]
    assert err.startswith("RATE_LIMITED: ") and unres is False
    assert out["400"][1].startswith("HTTP 400")
    assert out["503"] == (None, None, True)    # 503 = UNKNOWN outcome
    assert out["200"] == ([{"status": "ok", "oid": 1}], None, False)
    assert out["timeout"] == (None, None, True)


def test_signed_request_wire_shape():
    v = signed_venue()
    s = FakeSession([(200, '[{"status":"ok","oid":9}]')])
    v.session = s
    op = {"type": "createOrders", "args": [{"iid": 6, "buy": True}]}
    compact = _compact_create(6, True, "80755", "0.001", False, "ab" * 16)
    run(v._signed_request("POST", "/v1/trade/orders", op, compact))
    method, url, kw = s.calls[0]
    assert method == "POST" and url == v.api_url + "/v1/trade/orders"
    body = kw["json"]
    assert body["op"] == op                    # structured JSON goes on the wire
    assert set(body) >= {"op", "sig", "salt", "ts"}
    assert body["sig"].startswith("0x")
    assert isinstance(body["salt"], int) and isinstance(body["ts"], int)


def test_private_get_wire_shape():
    v = signed_venue()
    s = FakeSession([(200, '{"positions":[]}')])
    v.session = s
    run(v._private_get("/v1/account/portfolio"))
    _, url, kw = s.calls[0]
    assert url == v.api_url + "/v1/account/portfolio"
    assert kw["headers"]["polymarket-proxy"] == PROXY.lower()
    assert kw["headers"]["polymarket-secret"] == SECRET


def test_signed_request_signing_failure_is_clean_error():
    v = make_venue()
    import types
    bad = types.SimpleNamespace(
        sign_op=lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
    v.account = bad
    body, err, unresolved = run(v._signed_request(
        "DELETE", "/x", {"type": "cancelOrdersCOID", "args": []},
        _compact_cancel("f" * 32)))
    assert body is None and err and "signing failed" in err and "boom" in err
    assert unresolved is False


# -------------------------------------------------------------- send_taker

ORDER_FILLED = {"order_id": 12345, "client_order_id": "", "status": "filled",
                "filled_quantity": "0.5", "resting_quantity": "0",
                "instrument_id": 6, "buy": True, "price": "80755",
                "quantity": "0.5", "tif": "ioc"}
FILLS = {"data": [{"trade_id": 1, "order_id": 12345, "instrument_id": 6,
                   "price": "80700", "quantity": "0.3", "taker": True,
                   "fee": "0", "timestamp": 1},
                  {"trade_id": 2, "order_id": 12345, "instrument_id": 6,
                   "price": "80720", "quantity": "0.2", "taker": True,
                   "fee": "0", "timestamp": 2}],
         "more": False}


def wire_venue(v, ack, order=None, fills=None):
    """Fake _signed_request (order POST) and _private_get (orders/fills polls).
    The orders poll echoes the queried coid into the returned order — the real
    API matches on the client_order_id query param."""
    state = {"captured": {}, "order_polls": 0, "fills_polls": 0}

    async def fake_signed(method, path, op, compact):
        state["captured"] = {"method": method, "path": path, "op": op,
                             "compact": compact}
        return ack, None, False
    v._signed_request = fake_signed  # type: ignore

    async def fake_private_get(path, params=None):
        if path == "/v1/account/orders":
            state["order_polls"] += 1
            if isinstance(order, list):
                od = order[min(state["order_polls"] - 1, len(order) - 1)]
            else:
                od = order
            if not od:
                return [], None, False
            od = dict(od, client_order_id=(params or {}).get(
                "client_order_id", od.get("client_order_id")))
            return [od], None, False
        if path == "/v1/account/fills":
            state["fills_polls"] += 1
            return (fills if fills is not None else {"data": []}), None, False
        return None, "unexpected path", False
    v._private_get = fake_private_get  # type: ignore
    return state


def test_send_taker_ok_path_formats_op():
    v = signed_venue()
    v.price_decimals = 1
    v.size_decimals = 5
    v.step_size = 1e-05
    v.min_base = 1e-05
    v.iid = 6
    state = wire_venue(v, [{"status": "ok", "oid": 12345}],
                       order=ORDER_FILLED, fills=FILLS)

    res = run(v.send_taker(is_buy=True, qty=0.5, limit_px=80755.44))
    assert res["status"] == "filled" and res["filled_base"] == 0.5
    assert res["avg_px"] == pytest.approx((80700 * 0.3 + 80720 * 0.2) / 0.5)
    assert not res["unresolved"]
    op = state["captured"]["op"]
    assert state["captured"]["path"] == "/v1/trade/orders"
    args = op["args"][0]
    assert args["iid"] == 6 and args["buy"] is True
    # at 80k magnitude even pdec=1 collapses to integers (5-sig-fig rule;
    # the live BTC-USD book quotes integer prices)
    assert args["p"] == "80755"
    assert args["qty"] == "0.5"                 # stripped, not "0.50000"
    assert args["tif"] == "ioc" and args["po"] is False
    assert args["ro"] is False
    assert COID_RE.match(args["c"])
    # compact mirrors the wire fields in slot order (p before qty)
    assert state["captured"]["compact"][1][0][:4] == [6, True, "80755", "0.5"]


def test_send_taker_multi_fill_vwap():
    v = signed_venue()
    v.price_decimals = 1
    v.size_decimals = 5
    v.step_size = 1e-05
    v.min_base = 1e-05
    od = dict(ORDER_FILLED, filled_quantity="0.5")
    wire_venue(v, [{"status": "ok", "oid": 12345}], order=od, fills=FILLS)
    res = run(v.send_taker(is_buy=True, qty=0.5, limit_px=80755.0))
    # VWAP over BOTH fills, not the last price
    assert res["avg_px"] == pytest.approx(80708.0)   # (80700*.3+80720*.2)/.5


def test_send_taker_qty_floored_and_min_base():
    v = signed_venue()
    v.price_decimals = 1
    v.size_decimals = 3
    v.step_size = 0.001
    v.min_base = 0.001
    state = wire_venue(v, [{"status": "ok", "oid": 1}],
                       order=dict(ORDER_FILLED, filled_quantity="0"))
    res = run(v.send_taker(is_buy=False, qty=0.9999, limit_px=100.0))
    assert res["status"] == "canceled"          # ioc_no_fill-style: no fill
    assert state["captured"]["op"]["args"][0]["qty"] == "0.999"

    v.min_base = 0.5
    state = wire_venue(v, [{"status": "ok", "oid": 1}])
    res = run(v.send_taker(is_buy=True, qty=0.3, limit_px=100.0))
    assert res["status"] == "send-failed" and "min base" in res["err"]
    assert state["captured"] == {}              # never sent


def test_send_taker_reduce_only_flag():
    v = signed_venue()
    v.price_decimals = 1
    v.size_decimals = 5
    v.step_size = 1e-05
    v.min_base = 1e-05
    state = wire_venue(v, [{"status": "ok", "oid": 1}],
                       order=dict(ORDER_FILLED, filled_quantity="0"))
    run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0, reduce_only=True))
    args = state["captured"]["op"]["args"][0]
    assert args["ro"] is True
    assert state["captured"]["compact"][1][0][6] is True


def test_send_taker_ack_err_margin():
    v = signed_venue()
    state = wire_venue(v, [{"status": "err", "error": "insufficient_margin"}])
    res = run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0))
    assert res["status"] == "margin" and not res["unresolved"]
    assert state["order_polls"] == 0            # rejected outright — no polls


def test_send_taker_ack_err_rate_limited():
    v = signed_venue()
    wire_venue(v, [{"status": "err", "error": "action_rate_limited"}])
    res = run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0))
    assert res["status"] == "send-failed"
    assert res["err"].startswith("RATE_LIMITED: ")


def test_send_taker_http_429_is_rate_limited():
    v = signed_venue()

    async def fake_signed(method, path, op, compact):
        return None, "RATE_LIMITED: HTTP 429 too many", False
    v._signed_request = fake_signed  # type: ignore
    res = run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0))
    assert res["status"] == "send-failed"
    assert res["err"].startswith("RATE_LIMITED: ")


def test_send_taker_5xx_then_poll_finds_fill():
    v = signed_venue()
    v.price_decimals = 1
    v.size_decimals = 5
    v.step_size = 1e-05
    v.min_base = 1e-05
    v.settle_timeout = 2.0
    polls = {"n": 0}

    async def fake_signed(method, path, op, compact):
        return None, None, True                 # POST → 5xx, outcome unknown
    v._signed_request = fake_signed  # type: ignore

    async def fake_private_get(path, params=None):
        if path == "/v1/account/orders":
            polls["n"] += 1
            if polls["n"] == 1:
                return None, None, True         # not yet ingested
            od = dict(ORDER_FILLED, client_order_id=(params or {}).get(
                "client_order_id"))
            return [od], None, False
        return FILLS, None, False
    v._private_get = fake_private_get  # type: ignore

    res = run(v.send_taker(is_buy=True, qty=0.5, limit_px=80755.0))
    assert res["status"] == "filled" and res["filled_base"] == 0.5
    assert res["avg_px"] == pytest.approx(80708.0) and not res["unresolved"]


def test_send_taker_poll_timeout_is_unresolved():
    v = signed_venue()
    v.settle_timeout = 0.4                      # short — test must be fast

    async def fake_signed(method, path, op, compact):
        return None, None, True
    v._signed_request = fake_signed  # type: ignore

    async def fake_private_get(path, params=None):
        return None, None, True                 # polls never land
    v._private_get = fake_private_get  # type: ignore

    res = run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0))
    assert res["status"] == "timeout" and res["unresolved"]


def test_send_taker_partial_fill_counts_as_filled():
    v = signed_venue()
    v.price_decimals = 1
    v.size_decimals = 5
    v.step_size = 1e-05
    v.min_base = 1e-05
    od = {"order_id": 77, "client_order_id": "", "status": "ioc_expired",
          "filled_quantity": "0.2", "resting_quantity": "0",
          "instrument_id": 6, "buy": True, "price": "80755",
          "quantity": "0.5", "tif": "ioc"}
    fills = {"data": [{"trade_id": 1, "order_id": 77, "instrument_id": 6,
                       "price": "80740", "quantity": "0.2", "taker": True,
                       "fee": "0", "timestamp": 1}], "more": False}
    wire_venue(v, [{"status": "ok", "oid": 77}], order=od, fills=fills)
    res = run(v.send_taker(is_buy=True, qty=0.5, limit_px=80755.0))
    assert res["status"] == "filled" and res["filled_base"] == 0.2
    assert res["avg_px"] == pytest.approx(80740.0)


def test_send_taker_no_fills_visible_avg_px_none():
    v = signed_venue()
    v.price_decimals = 1
    v.size_decimals = 5
    v.step_size = 1e-05
    v.min_base = 1e-05
    v.settle_timeout = 2.0
    wire_venue(v, [{"status": "ok", "oid": 12345}], order=ORDER_FILLED,
               fills={"data": []})
    res = run(v.send_taker(is_buy=True, qty=0.5, limit_px=80755.0))
    # filled_base is authoritative; avg_px None → engine falls back to the
    # plan limit for accounting
    assert res["status"] == "filled" and res["filled_base"] == 0.5
    assert res["avg_px"] is None


# ------------------------------------------------------------- load_market

def canned_instruments(entry=None):
    if entry is None:
        entry = {"instrument_id": 6, "instrument_type": "perpetual",
                 "category": "crypto", "symbol": "BTC-USD",
                 "base_asset": "BTC", "quote_asset": "USD",
                 "funding_interval": "1h", "quantity_decimals": 5,
                 "price_decimals": 1, "price_bounds": "0.1",
                 "min_notional": "10", "max_leverage": 20,
                 "isolated_only": False, "risk_tiers": []}
    return [entry]


def test_load_market_parses_instrument():
    v = make_venue(symbol="BTC-USD")

    async def fake_get(path, params=None):
        assert path == "/v1/info/instruments"
        return canned_instruments()
    v._public_get = fake_get  # type: ignore

    run(v.load_market())
    assert v.symbol == "BTC-USD" and v.iid == 6
    assert v.price_decimals == 1 and v.tick_size == pytest.approx(0.1)
    assert v.size_decimals == 5 and v.step_size == pytest.approx(1e-05)
    assert v.min_base == pytest.approx(1e-05) and v.min_quote == 10.0
    assert v.account is None                   # no creds → no signing


def test_load_market_base_asset_fallback():
    v = make_venue(symbol="BTC")               # CLI symbol without -USD

    async def fake_get(path, params=None):
        return canned_instruments()
    v._public_get = fake_get  # type: ignore
    run(v.load_market())
    assert v.symbol == "BTC-USD"

    v2 = make_venue(symbol="BTC")              # two candidates → ambiguous

    async def fake_get2(path, params=None):
        return canned_instruments() + canned_instruments(
            {"instrument_id": 60, "instrument_type": "perpetual",
             "category": "crypto", "symbol": "BTC-EUR", "base_asset": "BTC",
             "quote_asset": "EUR", "quantity_decimals": 5,
             "price_decimals": 1, "min_notional": "10"})
    v2._public_get = fake_get2  # type: ignore
    with pytest.raises(RuntimeError, match="ambiguous"):
        run(v2.load_market())


def test_load_market_rejects_missing_and_non_perp():
    v = make_venue(symbol="NOPE-USD")

    async def fake_get(path, params=None):
        return canned_instruments()
    v._public_get = fake_get  # type: ignore
    with pytest.raises(RuntimeError, match="not found"):
        run(v.load_market())

    v2 = make_venue(symbol="BTC-USD")

    async def fake_get2(path, params=None):
        return canned_instruments(
            {"instrument_id": 6, "instrument_type": "expiry",
             "symbol": "BTC-USD", "base_asset": "BTC",
             "quantity_decimals": 5, "price_decimals": 1,
             "min_notional": "10"})
    v2._public_get = fake_get2  # type: ignore
    with pytest.raises(RuntimeError, match="not a perpetual"):
        run(v2.load_market())


def test_load_market_fee_calibration_raises_only():
    from entropy_arb.config import PolymarketCreds
    conf = VenueConf(key="hedge", kind="polymarket", label="POLY",
                     symbol="BTC-USD", fee_bps=4.0, cap_usd=1000.0,
                     orders_per_min=120,
                     polymarket_creds=PolymarketCreds(PROXY, SECRET, KEY))
    fees_high = {"fee_schedule": [
        {"instrument_type": "perpetual", "category": "crypto",
         "taker_fee_rate": "0.0006", "maker_fee_rate": "0"}]}
    fees_low = {"fee_schedule": [
        {"instrument_type": "perpetual", "category": "crypto",
         "taker_fee_rate": "0.0002", "maker_fee_rate": "0"}]}

    def venue_with(fees):
        v = PolymarketVenue(conf, session=None, settle_timeout_sec=1.0)

        async def fake_get(path, params=None):
            if path == "/v1/info/instruments":
                return canned_instruments()
            assert path == "/v1/info/fees"
            return fees
        v._public_get = fake_get  # type: ignore
        return v

    v = venue_with(fees_high)
    run(v.load_market())
    assert v.fee_bps == pytest.approx(6.0) and v.account is not None
    v2 = venue_with(fees_low)
    run(v2.load_market())
    assert v2.fee_bps == pytest.approx(4.0)    # never lowered


def test_load_market_fee_schedule_missing_category(caplog):
    from entropy_arb.config import PolymarketCreds
    conf = VenueConf(key="hedge", kind="polymarket", label="POLY",
                     symbol="BTC-USD", fee_bps=4.0, cap_usd=1000.0,
                     orders_per_min=120,
                     polymarket_creds=PolymarketCreds(PROXY, SECRET, KEY))
    v = PolymarketVenue(conf, session=None, settle_timeout_sec=1.0)
    equity_only = {"fee_schedule": [
        {"instrument_type": "perpetual", "category": "equity",
         "taker_fee_rate": "0.0004", "maker_fee_rate": "0.000125"}]}

    async def fake_get(path, params=None):
        if path == "/v1/info/instruments":
            return canned_instruments()
        return equity_only
    v._public_get = fake_get  # type: ignore

    with caplog.at_level("WARNING", logger="polymarket"):
        run(v.load_market())
    assert v.fee_bps == pytest.approx(4.0)      # default kept
    assert sum(1 for r in caplog.records
               if "fee schedule" in r.getMessage()) == 1


# ------------------------------------------------------------ accounts

PORTFOLIO = {"positions": [
    {"instrument_id": 7, "symbol": "ETH-USD", "size": "3.0"},
    {"instrument_id": 6, "symbol": "BTC-USD", "size": "-1.5"}],
    "margin": {"total_account_value": "123.5",
               "available_order_margin": "45.25"},
    "withdrawable": "45.25", "in_liquidation": False}


def test_fetch_position_signed_size():
    v = signed_venue()
    v.iid = 6

    async def fake(path, params=None):
        return PORTFOLIO, None, False
    v._private_get = fake  # type: ignore
    assert run(v.fetch_position()) == -1.5      # + long / - short

    async def empty(path, params=None):
        return {"positions": []}, None, False
    v._private_get = empty  # type: ignore
    assert run(v.fetch_position()) == 0.0

    async def failing(path, params=None):
        return None, "HTTP 401: nope", False
    v._private_get = failing  # type: ignore
    with pytest.raises(RuntimeError, match="position fetch failed"):
        run(v.fetch_position())


def test_fetch_equity_maps_fields_and_requires_account():
    v = make_venue()                            # account None (record-only)
    assert run(v.fetch_equity()) is None

    v = signed_venue()

    async def fake(path, params=None):
        return PORTFOLIO, None, False
    v._private_get = fake  # type: ignore
    assert run(v.fetch_equity()) == (123.5, 45.25)


def test_cancel_order_compact_and_wire():
    v = signed_venue()
    s = FakeSession([(200, '[{"status":"err","error":"order_not_found"}]')])
    v.session = s
    body, err, unresolved = run(v.cancel_order("f" * 32))
    assert err is None and not unresolved
    method, url, kw = s.calls[0]
    assert method == "DELETE" and url.endswith("/v1/trade/orders-coid")
    assert kw["json"]["op"] == {"type": "cancelOrdersCOID", "args": ["f" * 32]}


# ---------------------------------------------------------------- book/feed

def test_apply_book_pairs_full_rebuild():
    # the feed reuses OrderBook.apply_aster — the ["px","sz"] shape is identical
    b = OrderBook()
    b.apply_aster([["99", "5"], ["98", "0"]], [["100", "2"]])
    assert b.ready and b.bids == {99.0: 5.0} and b.asks == {100.0: 2.0}
    b.apply_aster([["98.5", "1"]], [["100.5", "3"]])
    assert b.bids == {98.5: 1.0} and b.asks == {100.5: 3.0}


def test_feed_frame_handling():
    b = OrderBook()
    notified = []
    feed = PolymarketBookFeed("POLY", "wss://x", 6, b,
                              lambda: notified.append(1))
    feed._on_frame({"ch": "book::6", "sq": 100,
                    "data": {"b": [["10", "1"]], "a": [["11", "1"]]}})
    assert b.best_bid() == 10.0 and b.best_ask() == 11.0
    feed._on_frame({"ch": "book::6", "sq": 90, "data": {"b": [], "a": []}})
    assert b.best_bid() == 10.0                 # stale sq → dropped
    feed._on_frame({"ch": "book::7", "sq": 200, "data": {"b": [], "a": []}})
    feed._on_frame({"id": 1, "data": [{"status": "ok"}]})   # sub-ack
    feed._on_frame({"ch": "tickers::6", "sq": 300, "data": {}})
    assert len(notified) == 1
    feed._on_frame({"ch": "book::6", "sq": 101,
                    "data": {"b": [["9", "1"]], "a": []}})  # full rebuild
    assert b.best_bid() == 9.0 and b.best_ask() is None
    assert len(notified) == 2


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:48s} OK")
