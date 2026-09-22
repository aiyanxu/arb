"""Lighter nonce allocator tests — no network, no SDK, no real Redis.

Local: an injected fake fetch stands in for GET /api/v1/nextNonce.
Redis: fakeredis with one FakeServer shared by two clients emulates two
processes trading the same account (this is the bug's scenario).

Run:  python3 -m pytest tests/test_venue_lighter.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fakeredis import FakeServer  # noqa: E402
import fakeredis.aioredis  # noqa: E402

from entropy_arb.lighter_nonce import (                # noqa: E402
    BaseLighterNonceAllocator, LocalNonceAllocator, NonceLockBusy,
    RedisNonceAllocator, make_nonce_allocator, nonce_rejected)


def run(coro):
    return asyncio.run(coro)


class FakeFetch:
    """Scripted server nextNonce: a fixed value or a per-call sequence."""

    def __init__(self, value=100, seq=None):
        self.seq = list(seq) if seq is not None else None
        self.value = value
        self.calls = 0

    async def __call__(self) -> int:
        self.calls += 1
        if self.seq is not None:
            return self.seq.pop(0) if self.seq else self.value
        return self.value


def make_local(fetch=None):
    return LocalNonceAllocator(fetch or FakeFetch(), "LIGHTER:42")


def make_redis_pair(fetch=None,
                    api_url="https://mainnet.zklighter.elliot.ai",
                    acct=42, key_idx=1):
    """Two allocators on one FakeServer — two processes, one account."""
    server = FakeServer()
    out = []
    for _ in range(2):
        a = RedisNonceAllocator.__new__(RedisNonceAllocator)
        # bypass __init__'s real from_url; wire a fake client instead
        BaseLighterNonceAllocator.__init__(a, fetch or FakeFetch(),
                                            f"{api_url}:{acct}")
        a._redis = fakeredis.aioredis.FakeRedis(server=server)
        from urllib.parse import urlparse
        a.key = (f"arb:nonce:{urlparse(api_url).hostname}:"
                 f"{acct}:{key_idx}")
        a._lock_name = a.key + ":send"
        a._hw = 0
        a._seen = False
        out.append(a)
    return out[0], out[1]


# ------------------------------------------------------------- local

def test_local_seeds_from_server_then_increments():
    a = make_local(FakeFetch(100))
    assert run(a.draw()) == 100
    assert run(a.draw()) == 101
    assert run(a.draw()) == 102


def test_local_never_reuses_after_failure():
    # a failed send has no rollback path — the next draw must still advance
    a = make_local(FakeFetch(100))
    first = run(a.draw())
    second = run(a.draw())          # pretend the first order failed
    assert second == first + 1


def test_local_resync_only_bumps_up():
    a = make_local()                # server says 100
    for _ in range(6):
        run(a.draw())               # counter now 105
    a._fetch = FakeFetch(103)       # lagging server read must not rewind
    run(a.resync_from_server())
    assert run(a.draw()) == 106
    a._fetch = FakeFetch(200)       # server far ahead -> jump to it
    run(a.resync_from_server())
    assert run(a.draw()) == 200


def test_local_concurrent_draws_unique():
    a = make_local(FakeFetch(0))

    async def many():
        return sorted(await asyncio.gather(*(a.draw() for _ in range(200))))

    vals = run(many())
    assert len(set(vals)) == 200
    assert vals == sorted(vals)


def test_local_lock_serializes():
    a = make_local()

    async def go():
        out = []
        async with a.send_lock():
            out.append("in")
        return out

    assert run(go()) == ["in"]


# -------------------------------------------------------------- redis

def test_redis_seed_nx_race_single_winner():
    # two cold processes: both fetch 100, both SET NX — one write wins,
    # the merged draw sequence has no duplicates
    fetch_a, fetch_b = FakeFetch(100), FakeFetch(100)
    a, b = make_redis_pair()
    a._fetch, b._fetch = fetch_a, fetch_b

    async def go():
        await a._ensure_seeded()
        await b._ensure_seeded()
        drawn = [await a.draw(), await b.draw(), await a.draw()]
        return drawn

    drawn = run(go())
    assert drawn == [100, 101, 102]
    assert fetch_a.calls == 1          # a seeded (key was missing)
    assert fetch_b.calls == 0          # b saw a's key via GET — no fetch


def test_redis_two_allocators_never_duplicate():
    a, b = make_redis_pair(FakeFetch(500))

    async def go():
        out = []
        for _ in range(50):
            out.append(await a.draw())
            out.append(await b.draw())
        return out

    vals = run(go())
    assert len(set(vals)) == 100
    assert vals == sorted(vals)
    assert vals[0] == 500


def test_redis_resync_never_lowers():
    a, _ = make_redis_pair(FakeFetch(100))
    run(a.draw())                     # counter at 100
    a._fetch = FakeFetch(98)          # lagging read
    run(a.resync_from_server())
    assert run(a.draw()) == 101       # not 98/99


def test_redis_restart_reseeds():
    a, _ = make_redis_pair(FakeFetch(100))
    run(a.draw())                     # counter at 100, _hw = 100

    async def restart():
        await a._redis.delete(a.key)  # Redis lost the key
        a._fetch = FakeFetch(106)
        return await a.draw()

    assert run(restart()) == 106


def test_redis_restart_high_water_wins():
    # our own in-flight (unlanded) nonce must not be re-drawn even when
    # the server does not know about it yet
    a, _ = make_redis_pair(FakeFetch(100))
    run(a.draw())                     # _hw = 100

    async def restart():
        await a._redis.delete(a.key)
        a._fetch = FakeFetch(50)      # server read lags far behind
        return await a.draw()

    assert run(restart()) == 101      # max(server-1, _hw)+1


def test_redis_lock_blocks_second_process():
    a, b = make_redis_pair(FakeFetch(100))

    async def go():
        async with a.send_lock():
            try:
                async with b.send_lock():
                    return "UNEXPECTED"
            except NonceLockBusy:
                return "busy"

    assert run(go()) == "busy"


def test_redis_send_lock_wrapper_raises_busy():
    a, b = make_redis_pair(FakeFetch(100))

    async def go():
        async with a.send_lock():
            try:
                async with b.send_lock():
                    return "UNEXPECTED"
            except NonceLockBusy:
                return "busy"

    assert run(go()) == "busy"


def test_redis_send_lock_releases():
    a, b = make_redis_pair(FakeFetch(100))

    async def go():
        async with a.send_lock():
            pass
        async with b.send_lock():      # must not raise — a released cleanly
            return "ok"

    assert run(go()) == "ok"


def test_nonce_rejected_patterns():
    assert nonce_rejected(
        "HTTP response body: code=21104 message='invalid nonce'", None)
    assert nonce_rejected(None, type("R", (), {"code": 21104,
                                               "message": "x"})())
    assert nonce_rejected(None, type("R", (), {"code": 1,
                                               "message": "invalid nonce"})())
    assert not nonce_rejected("rate limit exceeded", None)
    assert not nonce_rejected(None, None)


def test_make_allocator_selects_by_env():
    # covered fully in test_config.py once VenueConf grows the field;
    # here only the local default is exercised (no redis import happens)
    from entropy_arb.config import LighterCreds, LighterProfile, VenueConf
    prof = LighterProfile("mainnet", "https://mainnet.zklighter.elliot.ai",
                          "wss://x", 304)
    creds = LighterCreds(42, 1, "0x" + "11" * 32)
    conf = VenueConf(key="base", kind="lighter", label="LIGHTER",
                     symbol="SNDK", fee_bps=0.0, cap_usd=1000.0,
                     orders_per_min=30, lighter_profile=prof,
                     lighter_creds=creds)
    assert isinstance(make_nonce_allocator(conf, FakeFetch(1)),
                     LocalNonceAllocator)


# ------------------------------------------------------------ venue level


class SpyAllocator(LocalNonceAllocator):
    """Local allocator that records resync calls for assertions."""

    def __init__(self, fetch):
        super().__init__(fetch, "SPY")
        self.resyncs = 0

    async def resync_from_server(self):
        self.resyncs += 1
        await super().resync_from_server()


def make_venue(conf=None):
    from entropy_arb.venue_lighter import LighterVenue
    if conf is None:
        from entropy_arb.config import (LighterCreds, LighterProfile,
                                         VenueConf)
        prof = LighterProfile(
            "mainnet", "https://mainnet.zklighter.elliot.ai", "wss://x",
            304)
        conf = VenueConf(key="base", kind="lighter", label="LIGHTER",
                         symbol="SNDK", fee_bps=0.0, cap_usd=1000.0,
                         orders_per_min=30, lighter_profile=prof,
                         lighter_creds=LighterCreds(42, 1, "0x" + "11" * 32))
    return LighterVenue(conf, session=None, settle_timeout_sec=1.0)


class FakeSigner:
    """Records create_order kwargs; returns a scripted (tx, resp, err)."""

    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    async def create_order(self, **kw):
        self.calls.append(kw)
        return self.results.pop(0) if self.results else (None, None, None)


def signed_venue(result=None, fetch=None):
    """Venue wired with a fake signer + spy allocator; no SDK, no network."""
    v = make_venue()
    v.signer = FakeSigner(result and [result])
    v.size_decimals, v.price_decimals = 4, 2
    v.market_id = 7
    v.nonce = SpyAllocator(fetch or FakeFetch(100))
    return v


def test_send_taker_passes_explicit_nonce_api_key_skip_nonce():
    v = signed_venue(result=(None, type("R", (), {"code": 200})(), None))
    r = run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0))
    assert r["err"] is None
    kw = v.signer.calls[0]
    assert kw["nonce"] == 100                       # allocator's first draw
    assert kw["api_key_index"] == 1                  # creds' key index
    assert kw["skip_nonce"] == 1                     # SKIP_NONCE_ON
    assert kw["client_order_index"] > 0
    r = run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0))
    assert v.signer.calls[1]["nonce"] == 101         # strictly advancing


def test_client_order_index_stays_within_48_bit_field():
    """The sequencer rejects coi > 2**48-1, so the seed and every increment
    must stay inside the field."""
    from entropy_arb.venue_lighter import COI_MAX

    v = signed_venue()
    assert 0 < v._coi <= COI_MAX
    seen = []
    for _ in range(3):
        r = run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0))
        assert r["status"] == "sent-unconfirmed"
        seen.append(v.signer.calls[-1]["client_order_index"])
    assert all(0 < c <= COI_MAX for c in seen)
    assert seen == sorted(seen) and len(set(seen)) == 3
    v._coi = COI_MAX                                 # ceiling wraps, never overflows
    assert v._next_coi() == 1


def test_send_taker_invalid_nonce_err_triggers_resync():
    v = signed_venue(result=(None, None,
                             "HTTP response body: code=21104 "
                             "message='invalid nonce' additional_properties={}"))
    r = run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0))
    assert r["status"] == "send-failed" and not r["unresolved"]
    assert "invalid nonce" in r["err"]
    assert v.nonce.resyncs == 1


def test_send_taker_resp_code_21104_triggers_resync():
    v = signed_venue(result=(None, type("R", (), {"code": 21104,
                                                  "message": "x"})(), None))
    r = run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0))
    assert r["status"] == "send-failed"
    assert v.nonce.resyncs == 1


def test_send_taker_other_error_no_resync():
    v = signed_venue(result=(None, None, "some other error"))
    r = run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0))
    assert r["status"] == "send-failed"
    assert v.nonce.resyncs == 0


def test_send_taker_nonce_lock_busy_is_clean_failure():
    import entropy_arb.lighter_nonce as ln

    v = signed_venue()

    class BusyLock:
        async def __aenter__(self):
            raise ln.NonceLockBusy("peer holds it")

        async def __aexit__(self, *a):
            return False

    v.nonce.send_lock = BusyLock  # type: ignore[method-assign]
    r = run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0))
    assert r["status"] == "send-failed" and not r["unresolved"]
    assert r["err"].startswith("NONCE_LOCK: ")
    assert v.signer.calls == []              # the order was never sent


def test_send_taker_success_then_failure_advances_not_rewinds():
    ok = (None, type("R", (), {"code": 200})(), None)
    v = signed_venue()                       # server fetch says 100
    v.signer.results = [ok, ok]
    run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0))   # nonce 100
    run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0))   # nonce 101
    # a failed send (rate limit etc.) must not let the next one reuse 101
    v.signer.results = [(None, None, "rate limit")]
    run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0))   # nonce 102, fails
    v.signer.results = [ok]
    run(v.send_taker(is_buy=True, qty=0.5, limit_px=100.0))
    assert v.signer.calls[-1]["nonce"] == 103


def test_init_signer_uses_noop_manager(monkeypatch):
    try:
        import lighter
        from lighter.nonce_manager import NonceManagerType
    except ImportError:
        import pytest
        pytest.skip("lighter SDK not installed")
    from entropy_arb.venue_lighter import LighterVenue  # noqa: F401

    v = make_venue()
    captured = {}

    class StubSigner:
        def __init__(self, **kw):
            captured.update(kw)

        def check_client(self):
            return None

    monkeypatch.setattr(lighter, "SignerClient", StubSigner)
    # init_signer imports SignerClient lazily from the `lighter` package,
    # so patching the package attribute is enough
    v.init_signer()
    assert captured["nonce_management_type"] is NonceManagerType.NONE
    assert captured["account_index"] == 42
    assert captured["api_private_keys"] == {1: "0x" + "11" * 32}
    assert v.nonce is not None
    assert isinstance(v.nonce, LocalNonceAllocator)      # env unset


def test_prime_seeds_counter():
    v = signed_venue()
    run(v.prime())
    assert v.nonce._next == 100        # drew 100; next order signs 101


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:44s} OK")
