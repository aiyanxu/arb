"""flatten: one-shot close of both legs' positions for a (base, hedge, sym).

Run:  python3 -m pytest tests/  (or  python3 tests/test_flatten.py)
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.flatten import flatten_positions, MAX_ROUNDS  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def make_cfg():
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write("""
thresholds:
  midline_bps: 0.0
  upper_bps: 4.0
  lower_bps: 4.0
execution:
  hedge_slippage_bps: 20.0
  net_tolerance_base: 0.001
  staleness_sec: 10.0
""")
    f.close()
    return load_config(f.name, NO_ENV, symbol="SNDK",
                       hedge_venue="lighter-rh")


class FakeVenue:
    """Records send_taker calls; positions come from a scripted dict."""

    def __init__(self, key, name):
        self.key, self.name = key, name
        self.size_decimals = 4
        self.min_base = 1e-4
        self.fee_bps = 0.0
        self.position = 0.0
        self.book = OrderBook()
        self.set_book(100.0, 100.02)
        # chain state returned by fetch_position each call
        self.chain = 0.0
        # queued send_taker results, then a default filled result
        self.results = []
        self.sent = []

    def set_book(self, bid, ask, sz=50.0):
        self.book.apply_hl([[{"px": str(bid), "sz": str(sz)}],
                            [{"px": str(ask), "sz": str(sz)}]])

    def px_round(self, px, round_up):
        import math
        f = 10 ** 4
        v = math.ceil(px * f - 1e-9) / f if round_up \
            else math.floor(px * f + 1e-9) / f
        return round(v, 8)

    async def fetch_position(self):
        return self.chain

    async def send_taker(self, *, is_buy, qty, limit_px, reduce_only=False):
        self.sent.append({"is_buy": is_buy, "qty": qty,
                          "limit_px": limit_px,
                          "reduce_only": reduce_only})
        info = self.results.pop(0) if self.results else {
            "status": "finished", "filled_base": qty, "avg_px": 100.0,
            "err": None, "unresolved": False}
        if info.get("filled_base"):
            # a real venue: the chain position moves with the fill
            fill = info["filled_base"]
            self.chain = round(self.chain + (fill if is_buy else -fill), 12)
        return info

    def last_sent(self):
        return self.sent[-1] if self.sent else None


def run(coro):
    return asyncio.run(coro)


def test_flatten_when_already_flat():
    # no positions anywhere -> immediately True, no orders sent
    b, h = FakeVenue("base", "ENTROPY"), FakeVenue("hedge", "RH")
    assert run(flatten_positions(b, h, make_cfg())) is True
    assert b.sent == [] and h.sent == []


def test_flatten_closes_both_legs_reduce_only():
    cfg = make_cfg()
    b, h = FakeVenue("base", "ENTROPY"), FakeVenue("hedge", "RH")
    b.chain, h.chain = 2.0, -1.5           # long base, short hedge
    ok = run(flatten_positions(b, h, cfg))
    assert ok is True
    assert len(b.sent) == 1 and len(h.sent) == 1
    # base is long -> SELL (is_buy False), reduce-only, full size
    s = b.sent[0]
    assert s["is_buy"] is False and s["qty"] == 2.0
    assert s["reduce_only"] is True
    # hedge is short -> BUY (is_buy True)
    s2 = h.sent[0]
    assert s2["is_buy"] is True and s2["qty"] == 1.5
    assert s2["reduce_only"] is True


def test_flatten_price_protection_bounds():
    cfg = make_cfg()   # hedge_slippage_bps = 20 -> 0.002 around the touch
    b, h = FakeVenue("base", "ENTROPY"), FakeVenue("hedge", "RH")
    b.chain, h.chain = 1.0, -1.0    # base long -> SELL, hedge short -> BUY
    ok = run(flatten_positions(b, h, cfg))
    assert ok is True
    # sell bound: bid 100 * (1 - 0.002) = 99.8, rounded DOWN on the grid
    assert b.sent[0]["limit_px"] == 99.8, b.sent[0]
    # buy bound: ask 100.02 * (1 + 0.002) = 100.22004 -> grid-ceil 100.2201
    s = h.sent[0]
    assert s["is_buy"] is True and s["limit_px"] == 100.2201, s


def test_flatten_retries_until_flat():
    # first round's sell only partially fills -> a second round closes the
    # remainder read back from the "chain"
    cfg = make_cfg()
    b, h = FakeVenue("base", "ENTROPY"), FakeVenue("hedge", "RH")
    b.chain = 2.0
    b.results = [{"status": "finished", "filled_base": 1.25,
                  "avg_px": 100.0, "err": None, "unresolved": False}]
    ok = run(flatten_positions(b, h, cfg))
    assert ok is True
    assert len(b.sent) == 2
    assert b.sent[1]["qty"] == 0.75        # the remaining chain position


def test_flatten_ignores_dust():
    # a residual under net_tolerance_base counts as flat: no orders
    cfg = make_cfg()
    b, h = FakeVenue("base", "ENTROPY"), FakeVenue("hedge", "RH")
    b.chain = 0.0005                       # below net_tolerance_base (0.001)
    assert run(flatten_positions(b, h, cfg)) is True
    assert b.sent == []


def test_flatten_gives_up_and_reports():
    # every close order fails: bounded rounds, then False (caller exits 1)
    cfg = make_cfg()
    b, h = FakeVenue("base", "ENTROPY"), FakeVenue("hedge", "RH")
    b.chain = 2.0
    b.results = [{"status": "send-failed", "filled_base": 0.0,
                  "avg_px": None, "err": "boom", "unresolved": False}
                 ] * (MAX_ROUNDS + 1)
    ok = run(flatten_positions(b, h, cfg))
    assert ok is False
    assert len(b.sent) == MAX_ROUNDS       # bounded, not an infinite loop
    assert h.sent == []                    # hedge stayed flat, untouched


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
