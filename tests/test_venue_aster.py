"""Aster venue adapter unit tests — no network, no real credentials.

Signing is exercised against a throwaway key and verified independently via
eth_account's recover (the same check the Aster backend performs). Run:
python3 -m pytest tests/test_venue_aster.py
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
from entropy_arb.feeds import AsterBookFeed                 # noqa: E402
from entropy_arb.venue_aster import (                       # noqa: E402
    AsterAccount, AsterNonceAllocator, AsterVenue, _decimals_of)

# throwaway key — signs nothing real
KEY = "0x" + "11" * 32
USER = "0x63DD5aCC6b1aa0f563956C0e534DD30B6dcF7C4e"
CLOID_RE = re.compile(r"^[\.A-Z\:/a-z0-9_-]{1,36}$")


def make_venue(**kw) -> AsterVenue:
    conf = VenueConf(key=kw.pop("key", "hedge"), kind="aster",
                     label="ASTER", symbol=kw.pop("symbol", "BTCUSDT"),
                     fee_bps=kw.pop("fee_bps", 4.5), cap_usd=1000.0,
                     orders_per_min=120, **kw)
    return AsterVenue(conf, session=None, settle_timeout_sec=kw.pop(
        "settle_timeout_sec", 1.0) if False else 1.0)


# ------------------------------------------------------------- primitives

def test_nonce_allocator_strictly_increasing():
    a = AsterNonceAllocator()
    vals = [a.next() for _ in range(1000)]
    assert all(b > c for c, b in zip(vals, vals[1:]))
    assert vals[0] > time.time() * 1e6 - 2e6      # microsecond scale


def test_sign_params_recovers_signer():
    acct = AsterAccount(KEY, USER)
    q = acct.sign_params({"symbol": "BTCUSDT", "side": "BUY",
                          "quantity": "0.001"})
    assert q.startswith("symbol=BTCUSDT&side=BUY&quantity=0.001")
    assert f"user={USER.lower()}" in q
    assert f"signer={acct.signer}" in q
    assert "nonce=" in q and q.count("signature=") == 1

    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from entropy_arb.venue_aster import EIP712_DOMAIN, EIP712_TYPES
    query, _, sig = q.rpartition("&signature=")
    struct = encode_typed_data(full_message={
        "types": EIP712_TYPES, "primaryType": "Message",
        "domain": EIP712_DOMAIN, "message": {"msg": query}})
    assert (Account.recover_message(struct, signature=sig).lower()
            == acct.signer)


def test_sign_params_nonce_never_repeats():
    acct = AsterAccount(KEY, USER)
    nonces = []
    for _ in range(50):
        q = acct.sign_params({"x": "1"})
        import urllib.parse
        params = dict(urllib.parse.parse_qsl(q))
        nonces.append(int(params["nonce"]))
    assert len(set(nonces)) == 50


def test_cloid_shape_and_uniqueness():
    v = make_venue()
    ids = [v._next_cloid() for _ in range(10)]
    assert all(CLOID_RE.match(c) for c in ids)
    assert len(set(ids)) == 10


def test_px_round_tick_grid():
    v = make_venue()
    v.price_decimals = 1                       # tick 0.1
    assert v.px_round(77756.44, round_up=False) == 77756.4
    assert v.px_round(77756.44, round_up=True) == 77756.5
    v.price_decimals = 2                       # tick 0.01
    assert v.px_round(0.123, round_up=True) == 0.13
    assert v.px_round(0.123, round_up=False) == 0.12
    assert v.px_round(0.0, round_up=True) == 0.0


def test_decimals_of():
    assert _decimals_of("0.00010") == 4        # trailing zeros stripped
    assert _decimals_of("0.1") == 1
    assert _decimals_of("1") == 0
    assert _decimals_of("0.001") == 3


# ------------------------------------------------------------------ _parse

def test_parse_filled():
    r = AsterVenue._parse({"status": "FILLED", "executedQty": "0.5",
                           "avgPrice": "77700.0"})
    assert r == {"status": "filled", "filled_base": 0.5, "avg_px": 77700.0,
                 "err": None, "unresolved": False}


def test_parse_expired_no_fill_is_canceled():
    r = AsterVenue._parse({"status": "EXPIRED", "executedQty": "0",
                           "avgPrice": "0"})
    assert r["status"] == "canceled" and r["filled_base"] == 0.0
    assert r["avg_px"] is None and not r["unresolved"]


def test_parse_expired_partial_fill_counts_as_filled():
    # an IOC that expired with a partial fill must book the fill
    r = AsterVenue._parse({"status": "EXPIRED", "executedQty": "0.2",
                           "avgPrice": "0"})
    assert r["status"] == "filled" and r["filled_base"] == 0.2
    assert r["avg_px"] is None                 # "0" → engine falls back


def test_parse_error_body():
    r = AsterVenue._parse({"code": -1121, "msg": "Invalid symbol."})
    assert r["status"] == "send-failed" and "Invalid symbol" in r["err"]
    assert not r["unresolved"]


def test_parse_rate_limited_prefix():
    r = AsterVenue._parse({"code": -1015, "msg": "Too many requests."})
    assert r["err"].startswith("RATE_LIMITED: ")
    r = AsterVenue._parse({"code": -1003, "msg": "Too many request weight "
                                                "used."})
    assert r["err"].startswith("RATE_LIMITED: ")


def test_parse_margin_status():
    # engine pauses a venue whose status contains "margin"
    r = AsterVenue._parse({"code": -2019, "msg": "Margin is insufficient."})
    assert r["status"] == "margin"


def test_parse_non_terminal_is_unresolved():
    r = AsterVenue._parse({"status": "NEW"})
    assert r["status"] == "resting?" and r["unresolved"]
    r = AsterVenue._parse(None)                # defensive: not a dict
    assert r["status"] == "send-failed"


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
    """Returns canned responses; records the URL actually requested."""

    def __init__(self, statuses_and_texts):
        self.seq = list(statuses_and_texts)
        self.urls = []

    def request(self, method, url, **kw):
        self.urls.append((method, url))
        status, text = self.seq.pop(0)
        if isinstance(text, Exception):
            raise text
        return _resp(FakeResponse(status, text))


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


def run(coro):
    return asyncio.run(coro)


def signed_venue():
    v = make_venue()
    v.account = AsterAccount(KEY, USER)
    return v


def test_signed_request_classification():
    v = signed_venue()

    async def drive():
        out = {}
        s = FakeSession([(429, '{"code":-1003,"msg":"Too many"}')])
        v.session = s
        out["429"] = await v._signed_request("POST", "/x", {})
        s = FakeSession([(400, '{"code":-1121,"msg":"bad"}')])
        v.session = s
        out["400"] = await v._signed_request("GET", "/x", {})
        s = FakeSession([(503, "service unavailable")])
        v.session = s
        out["503"] = await v._signed_request("GET", "/x", {})
        s = FakeSession([(200, '{"ok": true}')])
        v.session = s
        out["200"] = await v._signed_request("GET", "/x", {})
        s = FakeSession([(200, asyncio.TimeoutError())])
        v.session = s
        out["timeout"] = await v._signed_request("GET", "/x", {})
        return out

    out = run(drive())
    err, unres = out["429"][1], out["429"][2]
    assert err.startswith("RATE_LIMITED: ") and unres is False
    assert out["400"][1].startswith("HTTP 400")
    assert out["503"] == (None, None, True)    # 503 = UNKNOWN outcome
    assert out["200"] == ({"ok": True}, None, False)
    assert out["timeout"] == (None, None, True)


def test_signed_request_signature_in_url():
    v = signed_venue()
    s = FakeSession([(200, "{}")])
    v.session = s
    run(v._signed_request("POST", "/fapi/v3/order",
                          {"symbol": "BTCUSDT", "side": "BUY"}))
    method, url = s.urls[0]
    assert method == "POST"
    assert url.startswith(v.api_url + "/fapi/v3/order?")
    assert "signature=" in url and "user=" in url and "signer=" in url
    assert "symbol=BTCUSDT" in url and "side=BUY" in url


def test_signed_request_signing_failure_is_clean_error():
    v = make_venue()
    import types
    bad = types.SimpleNamespace(
        sign_params=lambda params: (_ for _ in ()).throw(RuntimeError("boom")))
    v.account = bad
    body, err, unresolved = run(v._signed_request("GET", "/x", {}))
    assert body is None and err and "signing failed" in err and "boom" in err
    assert unresolved is False


# -------------------------------------------------------------- send_taker

def test_send_taker_ok_path_formats_params():
    v = signed_venue()
    v.size_decimals = 3
    v.price_decimals = 1
    v.symbol = "BTCUSDT"
    v.tick_size = 0.1
    captured = {}

    async def fake_signed(method, path, params):
        captured.update(params, method=method, path=path)
        return {"status": "FILLED", "executedQty": "0.5",
                "avgPrice": "77700.0"}, None, False
    v._signed_request = fake_signed  # type: ignore

    res = run(v.send_taker(is_buy=True, qty=0.5, limit_px=77756.44))
    assert res["status"] == "filled" and res["filled_base"] == 0.5
    assert captured["method"] == "POST"
    assert captured["path"] == "/fapi/v3/order"
    assert captured["quantity"] == "0.500"     # fixed decimals, never 1e-05
    assert captured["price"] == "77756.4"
    assert captured["reduceOnly"] == "false"   # lowercase
    assert captured["timeInForce"] == "IOC"
    assert captured["newOrderRespType"] == "RESULT"
    assert CLOID_RE.match(captured["newClientOrderId"])


def test_send_taker_qty_floored_to_own_step():
    v = signed_venue()
    v.size_decimals = 3
    v.price_decimals = 1
    v.symbol = "BTCUSDT"
    v.tick_size = 0.1
    v.step_size = 0.01                         # finer qty comes from the pair
    captured = {}

    async def fake_signed(method, path, params):
        captured.update(params)
        return {"status": "EXPIRED", "executedQty": "0",
                "avgPrice": "0"}, None, False
    v._signed_request = fake_signed  # type: ignore

    res = run(v.send_taker(is_buy=False, qty=0.999, limit_px=100.0))
    assert res["status"] == "canceled"         # no fill — clean miss
    assert captured["quantity"] == "0.990"     # floored, fixed 3 decimals


def test_send_taker_below_min_base_fails_closed():
    v = signed_venue()
    v.size_decimals = 3
    v.price_decimals = 1
    v.tick_size = 0.1
    v.step_size = 0.5
    v.min_base = 0.5
    sent = []

    async def fake_signed(method, path, params):
        sent.append(params)
        return {"status": "FILLED"}, None, False
    v._signed_request = fake_signed  # type: ignore

    res = run(v.send_taker(is_buy=True, qty=0.3, limit_px=100.0))
    assert res["status"] == "send-failed" and not sent


def test_send_taker_5xx_then_poll_finds_fill():
    v = signed_venue()
    v.size_decimals = 3
    v.price_decimals = 1
    v.symbol = "BTCUSDT"
    v.tick_size = 0.1
    calls = {"n": 0}

    async def fake_signed(method, path, params):
        if method == "GET":                    # order-status poll
            calls["n"] += 1
            if calls["n"] == 1:
                return None, None, True        # not yet ingested
            return {"status": "FILLED", "executedQty": "0.4",
                    "avgPrice": "77701.0"}, None, False
        return None, None, True                # order POST → 5xx unknown
    v._signed_request = fake_signed  # type: ignore

    res = run(v.send_taker(is_buy=True, qty=0.4, limit_px=100.0))
    assert res["status"] == "filled" and res["filled_base"] == 0.4
    assert res["avg_px"] == 77701.0 and not res["unresolved"]


def test_send_taker_poll_timeout_is_unresolved():
    v = signed_venue()
    v.size_decimals = 3
    v.price_decimals = 1
    v.tick_size = 0.1
    v.settle_timeout = 0.4                     # short — test must be fast

    async def fake_signed(method, path, params):
        if method == "GET":                    # order-status poll: never lands
            return None
        return None, None, True                # order POST → 5xx unknown
    v._signed_request = fake_signed  # type: ignore

    res = run(v.send_taker(is_buy=True, qty=0.4, limit_px=100.0))
    assert res["status"] == "timeout" and res["unresolved"]


# ------------------------------------------------------------- load_market

def canned_exchange_info(entry=None):
    if entry is None:
        entry = {"symbol": "BTCUSDT", "status": "TRADING",
                 "contractType": "PERPETUAL", "quantityPrecision": 3,
                 "filters": [
                     {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                     {"filterType": "LOT_SIZE", "stepSize": "0.001",
                      "minQty": "0.001"},
                     {"filterType": "MIN_NOTIONAL", "notional": "5"}]}
    return {"symbols": [entry]}


def test_load_market_parses_filters():
    v = make_venue()

    async def fake_get(path, params=None):
        return canned_exchange_info()
    v._public_get = fake_get  # type: ignore

    run(v.load_market())
    assert v.symbol == "BTCUSDT"
    assert v.tick_size == 0.1 and v.price_decimals == 1
    assert v.size_decimals == 3 and v.step_size == 0.001
    assert v.min_base == 0.001 and v.min_quote == 5.0
    assert v.account is None                   # no creds → no signing


def test_load_market_step_coarser_than_precision_wins():
    # stepSize "0.01" is coarser than quantityPrecision 3 → grid follows the
    # step so the engine's floor never produces off-grid quantities
    entry = canned_exchange_info()
    entry["symbols"][0]["quantityPrecision"] = 3
    for f in entry["symbols"][0]["filters"]:
        if f["filterType"] == "LOT_SIZE":
            f["stepSize"] = "0.01"
    v = make_venue()

    async def fake_get(path, params=None):
        return entry
    v._public_get = fake_get  # type: ignore

    run(v.load_market())
    assert v.size_decimals == 2 and v.step_size == 0.01


def test_load_market_rejects_missing_and_non_trading():
    v = make_venue(symbol="NOPEUSDT")

    async def fake_get(path, params=None):
        return canned_exchange_info()
    v._public_get = fake_get  # type: ignore

    with pytest.raises(RuntimeError, match="not found"):
        run(v.load_market())

    entry = canned_exchange_info()
    entry["symbols"][0]["status"] = "PRE_SETTLE"
    v2 = make_venue()

    async def fake_get2(path, params=None):
        return entry
    v2._public_get = fake_get2  # type: ignore

    with pytest.raises(RuntimeError, match="not TRADING"):
        run(v2.load_market())


def test_load_market_rejects_non_perpetual():
    entry = canned_exchange_info()
    entry["symbols"][0]["contractType"] = "CURRENT_QUARTER"
    v = make_venue()

    async def fake_get(path, params=None):
        return entry
    v._public_get = fake_get  # type: ignore

    with pytest.raises(RuntimeError, match="perpetual"):
        run(v.load_market())


# ------------------------------------------------------------ accounts

def test_fetch_position_one_way_ok_and_hedge_rejected():
    v = signed_venue()
    v.symbol = "BTCUSDT"

    async def ok(method, path, params):
        return [{"symbol": "BTCUSDT", "positionAmt": "-1.5",
                 "positionSide": "BOTH"}], None, False
    v._signed_request = ok  # type: ignore
    assert run(v.fetch_position()) == -1.5

    async def hedge(method, path, params):
        return [{"positionAmt": "1.0", "positionSide": "LONG"},
                {"positionAmt": "-1.0", "positionSide": "SHORT"}], None, False
    v._signed_request = hedge  # type: ignore
    with pytest.raises(RuntimeError, match="Hedge position mode"):
        run(v.fetch_position())


def test_fetch_equity_requires_account():
    v = make_venue()                            # account None (record-only)
    assert run(v.fetch_equity()) is None


def test_fetch_equity_maps_fields():
    v = signed_venue()

    async def fake(method, path, params):
        return {"totalMarginBalance": "123.5",
                "availableBalance": "45.25"}, None, False
    v._signed_request = fake  # type: ignore
    assert run(v.fetch_equity()) == (123.5, 45.25)


# ---------------------------------------------------------------- book/feed

def test_apply_aster_full_rebuild():
    b = OrderBook()
    b.apply_aster([["99", "5"], ["98", "0"]], [["100", "2"]])
    assert b.ready and b.bids == {99.0: 5.0} and b.asks == {100.0: 2.0}
    # a new snapshot fully replaces the old one (old levels vanish)
    b.apply_aster([["98.5", "1"]], [["100.5", "3"]])
    assert b.bids == {98.5: 1.0} and b.asks == {100.5: 3.0}


def test_feed_frame_handling():
    b = OrderBook()
    notified = []
    feed = AsterBookFeed("ASTER", "wss://x", "btcusdt", b,
                         lambda: notified.append(1))
    feed._on_frame({"e": "depthUpdate", "s": "BTCUSDT", "u": 100,
                    "b": [["10", "1"]], "a": [["11", "1"]]})
    assert b.best_bid() == 10.0 and b.best_ask() == 11.0
    feed._on_frame({"e": "depthUpdate", "s": "BTCUSDT", "u": 90,
                    "b": [], "a": []})         # stale u → dropped
    assert b.best_bid() == 10.0
    feed._on_frame({"e": "depthUpdate", "s": "ETHUSDT", "u": 999,
                    "b": [], "a": []})         # other symbol → ignored
    feed._on_frame({"e": "aggTrade", "s": "BTCUSDT", "u": 999})   # ignored
    assert len(notified) == 1
    feed._on_frame({"e": "depthUpdate", "s": "BTCUSDT", "u": 101,
                    "b": [["9", "1"]], "a": []})   # full rebuild
    assert b.best_bid() == 9.0 and b.best_ask() is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:44s} OK")
