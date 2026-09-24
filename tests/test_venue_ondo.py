"""Ondo Perps venue adapter unit tests — no network, no real credentials.

The HMAC signature is exercised against a throwaway key and verified
independently via stdlib hashlib (the exact re-derivation the API performs).
Run: python3 -m pytest tests/test_venue_ondo.py
"""
import asyncio
import hashlib
import hmac
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from entropy_arb.book import OrderBook                      # noqa: E402
from entropy_arb.config import OndoCreds, VenueConf         # noqa: E402
from entropy_arb.feeds import OndoBookFeed                  # noqa: E402
from entropy_arb.venue_ondo import (                        # noqa: E402
    OndoAccount, OndoVenue, _decimals_of)

KEY_ID = "ondoKeyId_test"
SECRET = "ondoApiSecret_test"
CLOID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def make_venue(**kw) -> OndoVenue:
    conf = VenueConf(key=kw.pop("key", "hedge"), kind="ondo",
                     label="ONDO", symbol=kw.pop("symbol", "NVDA-USD.P"),
                     fee_bps=kw.pop("fee_bps", 2.5), cap_usd=1000.0,
                     orders_per_min=120, **kw)
    return OndoVenue(conf, session=None, settle_timeout_sec=1.0)


# ------------------------------------------------------------- primitives

def test_sign_matches_stdlib_hmac():
    acct = OndoAccount(KEY_ID, SECRET)
    ts, method, path, body = "1700000000000", "POST", "/v1/perps/orders", '{"a":1}'
    sig = acct.sign(ts, method, path, body)
    expect = hmac.new(SECRET.encode(),
                      (ts + method + path + body).encode(),
                      hashlib.sha256).hexdigest()
    assert sig == expect


def test_sign_get_empty_body_and_upper_method():
    acct = OndoAccount(KEY_ID, SECRET)
    sig = acct.sign("1700000000000", "get", "/v1/markets", "")
    expect = hmac.new(SECRET.encode(),
                      b"1700000000000GET/v1/markets",
                      hashlib.sha256).hexdigest()
    assert sig == expect


def test_ws_sign():
    acct = OndoAccount(KEY_ID, SECRET)
    sig = acct.ws_sign(1700000000000)
    expect = hmac.new(SECRET.encode(),
                      b"1700000000000ondo_perps_ws_login",
                      hashlib.sha256).hexdigest()
    assert sig == expect


def test_creds_complete():
    assert OndoCreds(KEY_ID, SECRET).complete
    assert not OndoCreds(None, SECRET).complete
    assert not OndoCreds(KEY_ID, None).complete


def test_decimals_of():
    assert _decimals_of("0.010") == 2
    assert _decimals_of("0.1") == 1
    assert _decimals_of("1") == 0
    assert _decimals_of("0.00010") == 4


def test_px_round_tick_grid():
    v = make_venue()
    v.price_decimals = 2
    assert v.px_round(77756.444, False) == 77756.44
    assert v.px_round(77756.444, True) == 77756.45
    assert v.px_round(0.0, True) == 0.0


def test_cloid_shape_and_uniqueness():
    v = make_venue()
    ids = [v._next_cloid() for _ in range(10)]
    assert all(CLOID_RE.match(c) for c in ids)
    assert len(set(ids)) == 10


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
    """Returns canned responses; records the request args actually sent."""

    def __init__(self, statuses_and_texts):
        self.seq = list(statuses_and_texts)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
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


def signed_venue() -> OndoVenue:
    v = make_venue()
    v.account = OndoAccount(KEY_ID, SECRET)
    return v


def test_signed_request_classification():
    v = signed_venue()

    async def drive():
        out = {}
        s = FakeSession([(429, '{"error_code":"too_many_requests"}')])
        v.session = s
        out["429"] = await v._signed_request("GET", "/v1/perps/balance")
        s = FakeSession([(400, '{"error_code":"insufficient_margin"}')])
        v.session = s
        out["400"] = await v._signed_request("POST", "/v1/perps/orders",
                                             json_body={"x": 1})
        s = FakeSession([(503, "unavailable")])
        v.session = s
        out["503"] = await v._signed_request("GET", "/v1/perps/positions")
        s = FakeSession([(200, '{"success":true,"result":{}}')])
        v.session = s
        out["200"] = await v._signed_request("GET", "/v1/perps/balance")
        s = FakeSession([(200, asyncio.TimeoutError())])
        v.session = s
        out["timeout"] = await v._signed_request("GET", "/v1/perps/balance")
        return out

    out = run(drive())
    err, unres = out["429"][1], out["429"][2]
    assert err.startswith("RATE_LIMITED: ") and unres is False
    assert out["400"][1].startswith("HTTP 400")
    assert out["503"] == (None, None, True)
    assert out["200"] == ({"success": True, "result": {}}, None, False)
    assert out["timeout"] == (None, None, True)


def test_signed_request_headers_and_canonical_string():
    v = signed_venue()
    s = FakeSession([(200, "{}")])
    v.session = s
    run(v._signed_request("POST", "/v1/perps/orders",
                          params={"market": "NVDA-USD.P"},
                          json_body={"side": "buy", "size": "0.01"}))
    method, url, kw = s.calls[0]
    headers = kw["headers"]
    assert headers["ONDO-KEY-ID"] == KEY_ID
    assert headers["ONDO-TIMESTAMP"].isdigit()
    ts = headers["ONDO-TIMESTAMP"]
    assert url.endswith("/v1/perps/orders?market=NVDA-USD.P")
    # the body was signed as the exact string placed on the wire
    assert kw["data"] == '{"side":"buy","size":"0.01"}'
    # canonical string = ts + METHOD + pathWithQuery + body (lowercase hex)
    expect = hmac.new(SECRET.encode(),
                      (ts + "POST" + "/v1/perps/orders?market=NVDA-USD.P"
                       + '{"side":"buy","size":"0.01"}').encode(),
                      hashlib.sha256).hexdigest()
    assert headers["ONDO-SIGN"] == expect


# -------------------------------------------------------------- send_taker

def _fill_order(filled="0.5", cost="38850.0"):
    return {"success": True, "result": {
        "status": "fullyfilled", "filledSize": filled, "filledCost": cost}}

def _canceled_order():
    return {"success": True, "result": {"status": "canceled",
                                        "filledSize": "0", "filledCost": "0"}}

def _app_err(error_code):
    return {"success": False, "error_code": error_code,
            "error": "x"}


def test_send_taker_fullyfilled():
    v = signed_venue()
    v.size_decimals = 3
    v.price_decimals = 2
    v.symbol = "NVDA-USD.P"
    v.tick_size = 0.01
    captured = {}

    async def fake_signed(method, path, params=None, json_body=None):
        captured.update(json_body, method=method, path=path)
        return _fill_order(), None, False
    v._signed_request = fake_signed  # type: ignore

    res = run(v.send_taker(is_buy=True, qty=0.5, limit_px=77700.0))
    assert res["status"] == "filled" and res["filled_base"] == 0.5
    assert res["avg_px"] == pytest.approx(38850.0 / 0.5)
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/perps/orders"
    assert captured["market"] == "NVDA-USD.P"
    assert captured["side"] == "buy"
    assert captured["type"] == "limit" and captured["timeInForce"] == "IOC"
    assert captured["price"] == "77700.00"      # fixed decimals
    assert captured["size"] == "0.500"
    assert captured["reduceOnly"] is False
    assert CLOID_RE.match(captured["clientOrderId"])


def test_send_taker_canceled_no_fill():
    v = signed_venue()
    v.size_decimals = 2
    v.price_decimals = 2
    v.tick_size = 0.01

    async def fake_signed(method, path, params=None, json_body=None):
        return _canceled_order(), None, False
    v._signed_request = fake_signed  # type: ignore

    res = run(v.send_taker(is_buy=False, qty=0.05, limit_px=100.0))
    assert res["status"] == "canceled" and res["filled_base"] == 0.0
    assert res["avg_px"] is None and not res["unresolved"]


def test_send_taker_margin_rejection():
    v = signed_venue()
    v.size_decimals = 2
    v.price_decimals = 2
    v.tick_size = 0.01

    async def fake_signed(method, path, params=None, json_body=None):
        return _app_err("insufficient_margin"), None, False
    v._signed_request = fake_signed  # type: ignore

    res = run(v.send_taker(is_buy=True, qty=0.05, limit_px=100.0))
    assert res["status"] == "margin" and "insufficient_margin" in res["err"]
    assert not res["unresolved"]


def test_send_taker_below_min_base_fails_closed():
    v = signed_venue()
    v.step_size = 0.5
    v.min_base = 0.5
    v.tick_size = 0.01
    sent = []

    async def fake_signed(method, path, params=None, json_body=None):
        sent.append(json_body)
        return _fill_order(), None, False
    v._signed_request = fake_signed  # type: ignore

    res = run(v.send_taker(is_buy=True, qty=0.3, limit_px=100.0))
    assert res["status"] == "send-failed" and not sent


def test_send_taker_5xx_then_poll_finds_fill():
    v = signed_venue()
    v.size_decimals = 2
    v.price_decimals = 2
    v.tick_size = 0.01
    v.symbol = "NVDA-USD.P"
    calls = {"n": 0}

    async def fake_signed(method, path, params=None, json_body=None):
        if method == "GET":                    # order-status poll
            calls["n"] += 1
            if calls["n"] == 1:
                return None, None, True        # not yet ingested
            return _fill_order(), None, False
        return None, None, True                # order POST → 5xx unknown
    v._signed_request = fake_signed  # type: ignore

    res = run(v.send_taker(is_buy=True, qty=0.4, limit_px=100.0))
    assert res["status"] == "filled" and res["filled_base"] == 0.5
    assert res["avg_px"] == pytest.approx(38850.0 / 0.5)
    assert not res["unresolved"]


def test_send_taker_poll_timeout_is_unresolved():
    v = signed_venue()
    v.size_decimals = 2
    v.price_decimals = 2
    v.tick_size = 0.01
    v.settle_timeout = 0.4                     # short — test must be fast

    async def fake_signed(method, path, params=None, json_body=None):
        if method == "GET":
            return None, None, True            # poll never lands
        return None, None, True                # order POST → 5xx unknown
    v._signed_request = fake_signed  # type: ignore

    res = run(v.send_taker(is_buy=True, qty=0.4, limit_px=100.0))
    assert res["status"] == "timeout" and res["unresolved"]


# ------------------------------------------------------------- load_market

def canned_markets():
    return {"success": True, "result": {"perps": {"tradingPairs": [
        {"market": "NVDA-USD.P", "baseIncrement": "0.01",
         "quoteIncrement": "0.01"},
        {"market": "AAPL-USD.P", "baseIncrement": "0.01",
         "quoteIncrement": "0.01"},
    ]}}}


def test_load_market_parses_increments():
    v = make_venue()

    async def fake_get(path, params=None):
        return canned_markets()
    v._public_get = fake_get  # type: ignore

    run(v.load_market())
    assert v.symbol == "NVDA-USD.P"
    assert v.price_decimals == 2 and v.tick_size == 0.01
    assert v.size_decimals == 2 and v.step_size == 0.01
    assert v.min_base == 0.01
    assert v.account is None                    # no creds → no signing


def test_load_market_builds_account_with_creds():
    v = make_venue(ondo_creds=OndoCreds(KEY_ID, SECRET))

    async def fake_get(path, params=None):
        return canned_markets()
    v._public_get = fake_get  # type: ignore

    run(v.load_market())
    assert v.account is not None and v.account.key_id == KEY_ID


def test_load_market_rejects_missing_symbol():
    v = make_venue(symbol="NOPE-USD.P")

    async def fake_get(path, params=None):
        return canned_markets()
    v._public_get = fake_get  # type: ignore

    with pytest.raises(RuntimeError, match="not found"):
        run(v.load_market())


# -------------------------------------------------------------- accounts

def test_fetch_position_maps_direction():
    v = signed_venue()
    v.symbol = "NVDA-USD.P"

    async def fake(method, path, params=None, json_body=None):
        return {"success": True, "result": [
            {"market": "NVDA-USD.P", "direction": "short", "netQuantity": "1.5"},
            {"market": "AAPL-USD.P", "direction": "long", "netQuantity": "2"},
        ]}, None, False
    v._signed_request = fake  # type: ignore
    assert run(v.fetch_position()) == -1.5


def test_fetch_position_neutral_is_zero():
    v = signed_venue()
    v.symbol = "NVDA-USD.P"

    async def fake(method, path, params=None, json_body=None):
        return {"success": True, "result": [
            {"market": "NVDA-USD.P", "direction": "neutral",
             "netQuantity": "0"},
        ]}, None, False
    v._signed_request = fake  # type: ignore
    assert run(v.fetch_position()) == 0.0


def test_fetch_equity_requires_account():
    v = make_venue()                            # account None (record-only)
    assert run(v.fetch_equity()) is None


def test_fetch_equity_maps_fields():
    v = signed_venue()

    async def fake(method, path, params=None, json_body=None):
        return {"success": True, "result": {"marginBalance": "123.5",
                                            "availableMargin": "45.25"}}, \
            None, False
    v._signed_request = fake  # type: ignore
    assert run(v.fetch_equity()) == (123.5, 45.25)


# ---------------------------------------------------------------- book/feed

def test_feed_frame_handling():
    b = OrderBook()
    notified = []
    feed = OndoBookFeed("ONDO", "wss://x", "NVDA-USD.P", b,
                        lambda: notified.append(1))
    feed._on_frame({"type": "update", "channel": "depthBooksPerps",
                    "data": [{"market": "NVDA-USD.P",
                              "bids": [["10", "1"]], "asks": [["11", "1"]]}]})
    assert b.best_bid() == 10.0 and b.best_ask() == 11.0
    feed._on_frame({"type": "subscribed", "channel": "depthBooksPerps",
                    "data": []})                  # ack → ignored
    assert len(notified) == 1
    feed._on_frame({"type": "update", "channel": "depthBooksPerps",
                    "data": [{"market": "AAPL-USD.P",    # other market
                              "bids": [], "asks": []}]})
    assert len(notified) == 1
    feed._on_frame({"type": "update", "channel": "depthBooksPerps",
                    "data": [{"market": "NVDA-USD.P",
                              "bids": [["9", "1"]], "asks": []}]})  # rebuild
    assert b.best_bid() == 9.0 and b.best_ask() is None
    assert len(notified) == 2


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:44s} OK")