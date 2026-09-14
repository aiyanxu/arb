"""Engine signal math: midline band directions, inventory ladder, scan.

Run:  python3 -m pytest tests/  (or  python3 tests/test_engine.py)
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def make_cfg(midline=5.0, upper=4.0, lower=3.0):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(f"""
thresholds:
  midline_bps: {midline}
  upper_bps: {upper}
  lower_bps: {lower}
execution:
  premium_persist_sec: 0.0
""")
    f.close()
    return load_config(f.name, NO_ENV,
                       symbol="SNDK", hedge_venue="lighter-rh")


class StubVenue:
    def __init__(self, key, label, cap=10000.0, fee=0.0):
        self.key, self.name = key, label
        self.cap_usd, self.fee_bps = cap, fee
        self.size_decimals, self.min_base, self.min_quote = 4, 1e-4, 10.0
        self.position, self.cash = 0.0, 0.0
        self.orders_per_min = 30
        self.last_traded_ts = 0.0
        self.book = OrderBook()

    def ready_to_trade(self):
        return True

    def set_book(self, bid, ask, sz=50.0):
        self.book.apply_hl([[{"px": str(bid), "sz": str(sz)}],
                            [{"px": str(ask), "sz": str(sz)}]])


def make_engine(**thr):
    cfg = make_cfg(**thr)
    eng = Engine(cfg)
    eng.base = StubVenue("base", "ENTROPY")
    eng.hedge = StubVenue("hedge", "RH")
    eng.venues = {"base": eng.base, "hedge": eng.hedge}
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 10.0
    return eng


def approx(a, b, tol=1e-9):
    assert abs(a - b) <= tol, f"{a} != {b}"


def test_eff_threshold_directions():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    b, h = eng.base, eng.hedge
    # sell base: hurdle = midline + upper = 9
    approx(eng._eff_threshold(buy=h, sell=b), 9.0)
    # buy base: hurdle = lower - midline = -2 (unwind side of a positive
    # midline is deliberately cheap — that's what completes the round trip)
    approx(eng._eff_threshold(buy=b, sell=h), -2.0)
    # round trip nets upper + lower regardless of midline sign
    for m in (-7.0, 0.0, 12.5):
        eng.cfg.midline_bps = m
        total = eng._eff_threshold(buy=h, sell=b) + eng._eff_threshold(buy=b, sell=h)
        approx(total, 7.0)


def test_premium_bps_direction():
    # premium = base mid / hedge mid - 1: base rich -> positive
    eng = make_engine()
    eng.base.set_book(100.14, 100.16)   # base mid 100.15
    eng.hedge.set_book(99.99, 100.01)   # hedge mid 100.00
    approx(eng.premium_bps(), 15.0)
    # swap the books: the same prices with base cheap -> negative
    eng.base.set_book(99.99, 100.01)
    eng.hedge.set_book(100.14, 100.16)
    approx(eng.premium_bps(), -14.977533699451762)
    # missing book -> None
    eng.hedge.book = OrderBook()
    assert eng.premium_bps() is None


def test_inventory_ladder():
    eng = make_engine()
    eng.cfg.inventory_scale_bps, eng.cfg.inventory_floor_frac = 10.0, 0.5
    b, h = eng.base, eng.hedge
    b.set_book(99.9, 100.1)   # mid 100
    h.set_book(99.9, 100.1)
    approx(eng._inv_add_bps(b, h), 0.0)          # flat: dead zone
    b.position = 90.0                             # long $9k of $10k cap
    v = eng._inv_add_bps(b, h)                    # buying base adds long
    assert 7.5 < v < 8.5, v                       # u=0.9 -> ~+8
    approx(eng._inv_add_bps(h, b), 0.0)           # selling base reduces
    h.position = -90.0                            # hedge short $9k too
    v2 = eng._inv_add_bps(b, h)                   # both legs add -> max()
    assert abs(v2 - v) < 0.6, (v, v2)             # max, not sum


def run_scan(eng):
    async def go():
        # first pass arms the direction, second passes the persistence gate
        # (premium_persist_sec is 0 in the test config)
        eng._scan(__import__("time").time())
        return eng._scan(__import__("time").time())
    return asyncio.run(go())


def test_scan_fires_sell_base_above_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # base 15 bps rich vs hedge: above midline+upper=9 -> sell base
    eng.base.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    best = run_scan(eng)
    assert best is not None
    buy, sell, plan = best
    assert sell.key == "base" and buy.key == "hedge"
    assert plan.exp_edge_usd > 0


def test_scan_quiet_inside_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # base 5 bps rich = exactly on the midline: inside the band, no trade
    eng.base.set_book(100.04, 100.06)
    eng.hedge.set_book(99.99, 100.01)
    assert run_scan(eng) is None


def test_scan_fires_buy_base_below_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # base 5 bps CHEAP (premium -5): below midline-lower=+2 -> buy base
    eng.base.set_book(99.94, 99.96)
    eng.hedge.set_book(99.99, 100.01)
    best = run_scan(eng)
    assert best is not None
    buy, sell, plan = best
    assert buy.key == "base" and sell.key == "hedge"


def test_scan_respects_position_caps():
    eng = make_engine(midline=0.0, upper=1.0, lower=1.0)
    eng.base.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    eng.base.position = -100.0   # base already short at its cap
    eng.base.cap_usd = 10000.0
    eng.hedge.position = 100.0
    eng.hedge.cap_usd = 10000.0
    assert run_scan(eng) is None


# --------------------------------------------------------- pause / flatten

def test_pause_blocks_evaluate_and_resume():
    eng = make_engine(midline=0.0, upper=1.0, lower=1.0)
    eng.base.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    # without pause the direction arms (persist sec is 0)
    assert run_scan(eng) is not None
    eng2 = make_engine(midline=0.0, upper=1.0, lower=1.0)
    eng2.base.set_book(100.14, 100.16)
    eng2.hedge.set_book(99.99, 100.01)
    eng2.request_pause()
    assert eng2.paused is True
    # _evaluate returns immediately when paused: nothing armed, no task
    asyncio.run(eng2._evaluate())
    assert eng2._armed == {"sell_base": None, "buy_base": None}
    assert eng2._exec_tasks == set()
    # resume re-enables evaluation and returns True
    assert eng2.request_resume() is True
    assert eng2.paused is False
    assert run_scan(eng2) is not None


def test_resume_refused_while_halted():
    eng = make_engine()
    eng.request_pause()
    eng.halted = True
    assert eng.request_resume() is False
    assert eng.paused is True   # unchanged
    eng.halted = False
    assert eng.request_resume() is True


class FlattenableVenue(StubVenue):
    """StubVenue with the fetch/send surface flatten_positions drives."""

    def __init__(self, key, label, position):
        super().__init__(key, label)
        self.position = position
        self.sent = []
        self.set_book(100.0, 100.02)

    def px_round(self, px: float, round_up: bool) -> float:
        return round(px, 4)

    async def fetch_position(self):
        # each reduce-only IOC fully fills against the fresh stub book
        return 0.0 if self.sent else self.position

    async def send_taker(self, *, is_buy, qty, limit_px, reduce_only=False):
        self.sent.append((is_buy, qty, limit_px, reduce_only))
        assert reduce_only, "flatten must never send an opening order"
        self.position += qty if is_buy else -qty
        return {"status": "finished", "filled_base": qty, "avg_px": limit_px,
                "unresolved": False}


def test_flatten_all_closes_both_legs():
    eng = make_engine(midline=0.0, upper=1.0, lower=1.0)
    eng.base = FlattenableVenue("base", "ENTROPY", position=0.5)
    eng.hedge = FlattenableVenue("hedge", "RH", position=-0.3)
    eng.venues = {"base": eng.base, "hedge": eng.hedge}
    eng.markets_ready = True

    async def go():
        ok = await eng.flatten_all()
        return ok

    ok = asyncio.run(go())
    assert ok is True
    assert eng.paused is True            # stays paused after flattening
    assert eng.flatten_in_progress is False
    assert eng.flatten_state["result"] == "flat"
    # base was long 0.5 -> SELL (is_buy False), hedge short -0.3 -> BUY
    assert [(i, round(q, 6)) for i, q, _, _ in eng.base.sent] == [(False, 0.5)]
    assert [(i, round(q, 6)) for i, q, _, _ in eng.hedge.sent] == [(True, 0.3)]
    # venue locks released afterwards
    assert not eng._vlock("base").locked()
    assert not eng._vlock("hedge").locked()


def test_flatten_all_refuses_record_only():
    eng = make_engine()
    eng.record_only = True
    assert asyncio.run(eng.flatten_all()) is False
    assert eng.flatten_state["result"] == "error"
    assert "record-only" in eng.flatten_state["error"]


def test_flatten_all_waits_for_inflight_execution():
    eng = make_engine()
    eng.base = FlattenableVenue("base", "ENTROPY", position=0.5)
    eng.hedge = FlattenableVenue("hedge", "RH", position=-0.3)
    eng.venues = {"base": eng.base, "hedge": eng.hedge}
    eng.markets_ready = True

    async def go():
        blocker = asyncio.Event()
        done = asyncio.Event()

        async def fake_exec():
            await blocker.wait()

        t = asyncio.create_task(fake_exec())
        eng._exec_tasks.add(t)
        task = asyncio.create_task(eng.flatten_all())
        await asyncio.sleep(0.05)          # flatten_all is now waiting on it
        assert eng.flatten_in_progress is True
        assert eng.paused is True
        blocker.set()                      # the execution settles
        ok = await asyncio.wait_for(task, 5)
        t.cancel()
        assert ok is True
        assert eng.flatten_state["result"] == "flat"

    asyncio.run(go())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
