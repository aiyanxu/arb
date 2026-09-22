"""Lighter order-nonce management (replaces the SDK's default manager).

The SDK's OptimisticNonceManager caused code=21104 "invalid nonce" in two
ways: it *decrements* its counter after a failed send (`acknowledge_failure`)
so the next order reuses a nonce the sequencer may already have consumed,
and it only ever syncs from the server after the resulting rejection. On top
of that, every process trading the same Lighter account owns a private
counter, so two live bots on one account collide constantly.

This module draws nonces itself instead:

- every order is sent with skip_nonce=1 (L2TxAttributes.SkipNonce), which
  relaxes the server's rule from `new_nonce == old_nonce + 1` to
  `2^47-1 > new_nonce > old_nonce` — gaps are fine, only monotonicity
  matters. A drawn-but-unconsumed nonce (HTTP timeout, API-level reject)
  is therefore harmless.
- the counter only ever moves forward: no rollback on failure, resync
  sets it to max(server nonce - 1, current) so it can never cause a
  reuse.
- the one remaining failure mode is being *behind* the server (another
  process consumed nonces) → 21104 → the venue resyncs from
  GET /api/v1/nextNonce and the next order succeeds. Self-healing.

Two implementations share these invariants:

- LocalNonceAllocator — one asyncio.Lock + counter in this process. The
  default; correct for the one-bot-one-account deployment.
- RedisNonceAllocator — one Redis INCR counter per
  (api host, account_index, api_key_index), shared by every process
  pointed at the same LIGHTER_NONCE_REDIS_URL. The send lock spans
  draw → HTTP completion (not settlement) so transactions on one key
  reach the sequencer in nonce order — same guarantee the SDK's
  per-key lock gives inside one process, here across processes.

Race notes (Redis):
- cold start, two processes: both see the key missing, both fetch the
  server nonce N, both SET N-1 NX — exactly one write wins; subsequent
  INCRs interleave atomically with no duplicates.
- Redis restart (the key is deliberately not persisted): the next INCR
  returns a small value ≤ our local high-water mark → re-seed at
  max(server nonce - 1, high-water) and INCR again. Even a slightly low
  re-seed costs at most one rejected order, healed by the 21104 resync.

Neither the Lighter SDK nor redis is imported at module scope, so
record-only installs (no live extras) can still run these tests.
"""
from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Awaitable, Callable, Optional, Type
from urllib.parse import urlparse

log = logging.getLogger("lighter")

# an error/rejection that means our counter is behind the server's
NONCE_REJECT = re.compile(r"invalid nonce|(^|\D)21104(\D|$)", re.I)


class NonceLockBusy(RuntimeError):
    """Send-lock acquisition timed out — the order was NOT sent."""


class _SendLock:
    """Protocol-shaped alias: any async context manager that serializes a
    draw → HTTP send critical section (asyncio.Lock or a Redis lock)."""

    async def __aenter__(self) -> None:
        raise NotImplementedError

    async def __aexit__(self, exc_type: Type[BaseException] | None,
                        exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        raise NotImplementedError


def nonce_rejected(err, resp) -> bool:
    """True when a create_order result means 'your nonce was stale'."""
    if err is not None and NONCE_REJECT.search(str(err)):
        return True
    code = getattr(resp, "code", None)
    if code is not None and NONCE_REJECT.search(str(code)):
        return True
    msg = getattr(resp, "message", None)
    return bool(msg and NONCE_REJECT.search(str(msg)))


class BaseLighterNonceAllocator(ABC):
    """Strictly-increasing nonce source for one
    (api host, account_index, api_key_index).

    Invariants: never reuses a nonce, never rolls back, gaps allowed
    (orders carry skip_nonce=1). draw()/resync_from_server() must be
    called while holding send_lock().
    """

    def __init__(self, fetch_next_nonce: Callable[[], Awaitable[int]],
                 key_desc: str) -> None:
        self._fetch = fetch_next_nonce
        self._key_desc = key_desc

    @abstractmethod
    async def draw(self) -> int:
        """Next nonce to sign with (strictly greater than every previous)."""

    @abstractmethod
    async def resync_from_server(self) -> None:
        """Re-anchor to the server's next nonce. Only ever moves forward."""

    @abstractmethod
    def send_lock(self) -> "_SendLock":
        """Context manager serializing draw → HTTP send across sharers."""

    async def ping(self) -> None:
        """Connectivity probe (best-effort; Local is a no-op)."""

    async def close(self) -> None:
        """Release resources (best-effort; Local is a no-op)."""

    async def prime(self) -> int:
        """Seed the counter and verify reachability at startup.

        Takes the send lock itself (a trading peer may be mid-draw).
        Returns the nonce the next order will use.
        """
        async with self.send_lock():
            n = await self.draw()
            log.info("[%s] nonce primed — next order signs nonce %d",
                     self._key_desc, n)
            return n


class LocalNonceAllocator(BaseLighterNonceAllocator):
    """In-process counter: correct for one bot per Lighter account."""

    def __init__(self, fetch_next_nonce: Callable[[], Awaitable[int]],
                 key_desc: str) -> None:
        super().__init__(fetch_next_nonce, key_desc)
        self._lock = asyncio.Lock()
        self._next: Optional[int] = None   # last drawn; None = not seeded

    async def draw(self) -> int:
        if self._next is None:
            self._next = (await self._fetch()) - 1
        self._next += 1
        return self._next

    async def resync_from_server(self) -> None:
        n = (await self._fetch()) - 1
        if self._next is not None and n < self._next:
            n = self._next            # a lagging server read must not rewind
        self._next = n

    def send_lock(self) -> "_SendLock":
        return self._lock  # type: ignore[return-value]  # asyncio.Lock fits


class RedisNonceAllocator(BaseLighterNonceAllocator):
    """Shared counter for several processes trading one Lighter account.

    One Redis key per (api host, account_index, api_key_index); INCR hands
    out strictly-increasing nonces to everyone. The key is intentionally
    not persisted by the server (see docker-compose) — a lost key re-seeds
    from the exchange, which is always more current than any saved copy.
    """

    def __init__(self, redis_url: str, api_url: str, account_index: int,
                 api_key_index: int,
                 fetch_next_nonce: Callable[[], Awaitable[int]]) -> None:
        import redis.asyncio as aioredis   # lazy: live-extra only  # noqa

        key_desc = f"{urlparse(api_url).hostname}:{account_index}"
        super().__init__(fetch_next_nonce, key_desc)
        self._redis = aioredis.from_url(redis_url)
        self.key = f"arb:nonce:{urlparse(api_url).hostname}:" \
                   f"{account_index}:{api_key_index}"
        self._lock_name = self.key + ":send"
        self._hw = 0          # highest nonce this process has drawn
        self._seen = False    # this process has confirmed the key exists

    # ------------------------------------------------------------- locking

    def send_lock(self) -> "_SendLock":
        @asynccontextmanager
        async def _ctx():
            # auto-release 30s > any sane HTTP timeout: a hung sender at
            # worst lets the next process in (→ 21104 → resync heals);
            # blocking 5s: a contended peer means *someone* is trading, we
            # would rather fail this order than queue unbounded.
            lock = self._redis.lock(self._lock_name, timeout=30.0,
                                    blocking_timeout=5.0)
            try:
                # acquire() returns False (rather than raising) when
                # blocking_timeout expires — both mean "someone else is
                # mid-send on this key".
                if not await lock.acquire():
                    raise NonceLockBusy(
                        f"send lock {self._lock_name} busy after 5s")
            except NonceLockBusy:
                raise
            except Exception as e:            # LockError, connection errors
                raise NonceLockBusy(f"{type(e).__name__}: {e}") from e
            try:
                yield
            finally:
                try:
                    await lock.release()
                except Exception:
                    pass          # token mismatch after auto-release is benign

        return _ctx()  # type: ignore[return-value]

    # ------------------------------------------------------------- counter

    async def _ensure_seeded(self) -> None:
        if self._seen:
            return
        if await self._redis.get(self.key) is not None:
            self._seen = True
            return
        server_n = await self._fetch()
        await self._redis.set(self.key, max(server_n - 1, self._hw), nx=True)
        self._seen = True    # NX: both cold starters write, one wins — fine

    async def draw(self) -> int:
        await self._ensure_seeded()
        n = int(await self._redis.incr(self.key))
        if n <= self._hw:
            # Redis lost the key (restart) — re-anchor to the server and
            # this process's own high-water mark, then draw again.
            server_n = await self._fetch()
            await self._redis.set(self.key, max(server_n - 1, self._hw))
            n = int(await self._redis.incr(self.key))
        self._hw = n
        return n

    async def resync_from_server(self) -> None:
        server_n = await self._fetch()
        cur = int(await self._redis.get(self.key) or 0)
        await self._redis.set(self.key, max(server_n - 1, cur, self._hw))
        self._seen = True

    # ---------------------------------------------------------- lifecycle

    async def ping(self) -> None:
        await self._redis.ping()

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:
            pass


def make_nonce_allocator(conf, fetch_next_nonce: Callable[[], Awaitable[int]]
                         ) -> BaseLighterNonceAllocator:
    """Local allocator unless a shared Redis is configured for Lighter.

    `conf` is the leg's VenueConf (needs lighter_profile, lighter_creds and
    the optional lighter_nonce_redis_url); `fetch_next_nonce` an async
    () -> int returning GET /api/v1/nextNonce's value.
    """
    c = conf.lighter_creds
    url = getattr(conf, "lighter_nonce_redis_url", None)
    if url:
        alloc = RedisNonceAllocator(url, conf.lighter_profile.api_url,
                                    c.account_index, c.api_key_index,
                                    fetch_next_nonce)
        log.info("[%s] nonce source: redis %s (key %s)",
                 conf.label, url, alloc.key)
    else:
        alloc = LocalNonceAllocator(fetch_next_nonce,
                                    f"{conf.label}:{c.account_index}")
        log.info("[%s] nonce source: local counter — if other processes "
                 "trade this account, set LIGHTER_NONCE_REDIS_URL",
                 conf.label)
    return alloc
