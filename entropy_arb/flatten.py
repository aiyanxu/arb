"""One-shot position flattener: close BOTH legs' real positions.

`entropy-arb flatten --symbol X --base A --hedge B` connects to the two
venues, reads each leg's actual on-exchange position (never a local
notion), and sends reduce-only IOC takers against the live book until
both venues are back to flat — with price protection (hedge_slippage_bps
around the touch) and a bounded number of retry rounds. Exits 0 only
when both venues are flat.

This is the manual counterpart of the engine's reconcile/hedge paths and
the HALTED state's advice ("flatten manually and restart"): e.g. after a
crash mid-round-trip or before tearing down a market. It never opens
risk: orders are reduce-only, so the worst case is an unfilled leg that
the next round retries.

一键清仓：读取并平掉指定 (base, hedge, symbol) 两腿在交易所的真实持仓。
只发 reduce-only 限价单（价格保护），重复若干轮直到两边归零，退出码 0
表示已清仓完毕。

Usage:
    entropy-arb flatten --symbol SNDK --base entropy --hedge lighter-rh
    entropy-arb flatten --config config-btc.yaml   # pair from the config file
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time

import aiohttp

from .book import floor_step

log = logging.getLogger("flatten")

# how hard to try before giving up and telling the user to look themselves
MAX_ROUNDS = 5
# a remaining position at or below this fraction of the min order size is
# reported instead of chased forever (unfillable dust)
DUST_FRAC = 0.5


async def flatten_positions(base, hedge, cfg, on_round=None) -> bool:
    """Drive both venues to flat. Returns True when both legs are flat.

    Either leg may already be flat. Each round: refresh positions from the
    venue, then (with the book fresh) cross the touch with a reduce-only
    IOC bound by hedge_slippage_bps around it. The caller holds no other
    engine tasks, so there is nothing to race the orders. `on_round(n)`,
    when given, is called after each round (1-based) — the webapp's
    flatten_all uses it to surface progress.
    """
    slip = cfg.hedge_slippage_bps / 1e4
    for round_no in range(1, MAX_ROUNDS + 1):
        flat = True
        for v in (base, hedge):
            pos = await v.fetch_position()
            v.position = pos
            if abs(pos) <= cfg.net_tolerance_base:
                log.info("[%s] flat", v.name)
                continue
            flat = False
            is_sell = pos > 0
            qty = floor_step(abs(pos), 10 ** -v.size_decimals)
            if qty <= 0 or qty < v.min_base:
                print(f"[{v.name}] dust position {pos:+.8g} below the "
                      f"tradable minimum — ignored / 持仓低于最小下单量，"
                      f"视为已清仓", file=sys.stderr)
                continue
            ref = v.book.best_bid() if is_sell else v.book.best_ask()
            if ref is None:
                log.error("[%s] no book — is the bot's feed running? "
                          "cannot price the close order", v.name)
                continue
            # price protection: cross the touch but never beyond
            # hedge_slippage_bps from it
            limit = v.px_round(ref * (1 - slip), False) if is_sell \
                else v.px_round(ref * (1 + slip), True)
            log.warning("[%s] flatten %s %.6g @<=%.6g (reduce-only)",
                        v.name, "SELL" if is_sell else "BUY", qty, limit)
            info = await v.send_taker(is_buy=not is_sell, qty=qty,
                                      limit_px=limit, reduce_only=True)
            if info.get("err") or info.get("unresolved"):
                log.error("[%s] close order problem: %s", v.name,
                          info.get("err") or info.get("status"))
            else:
                fill = info["filled_base"]
                log.info("[%s] %s %.6g/%.6g", v.name, info["status"],
                         fill, qty)
        if flat:
            return True
        if on_round is not None:
            try:
                on_round(round_no)
            except Exception:
                pass  # progress reporting must never break the flatten
        if round_no < MAX_ROUNDS:
            await asyncio.sleep(1.0)   # let ws settlements settle before re-read
    return False


async def run_flatten(cfg) -> None:
    """Build the two venues, init signers, flatten, report, clean up."""
    from .engine import make_venue

    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(
        keepalive_timeout=75.0, ttl_dns_cache=300))
    stop = asyncio.Event()
    base = make_venue(cfg, cfg.base, session)
    hedge = make_venue(cfg, cfg.hedge, session)
    try:
        await asyncio.gather(base.load_market(), hedge.load_market())
        base.init_signer()
        hedge.init_signer()
        # seed nonce counters before the first close order (duck-typed:
        # only Lighter venues have prime())
        for v in (base, hedge):
            prime = getattr(v, "prime", None)
            if prime is not None:
                await prime()
        # order books come from the venues' own websockets — start them so
        # the close orders can be priced
        tasks = base.start_tasks(stop, lambda: None, live=True)
        tasks += hedge.start_tasks(stop, lambda: None, live=True)
        # give the feeds a moment; a cold book means an unpriceable order
        deadline = time.time() + cfg.staleness_sec + 2.0
        while time.time() < deadline:
            if base.book.is_fresh(cfg.staleness_sec) and \
                    hedge.book.is_fresh(cfg.staleness_sec):
                break
            await asyncio.sleep(0.2)
        if not (base.book.is_fresh(cfg.staleness_sec)
                and hedge.book.is_fresh(cfg.staleness_sec)):
            raise RuntimeError(
                "order book feed did not come up — flatten needs live books "
                "to price close orders / 行情未就绪，无法定价平仓单")

        ok = await flatten_positions(base, hedge, cfg)
        if ok:
            log.info("both venues flat — done / 两腿均已平仓")
        else:
            raise RuntimeError(
                "not flat after %d rounds — check positions manually "
                "(e.g. on each venue's web UI) / 多轮平仓后仍有残余持仓，"
                "请到交易所手动确认" % MAX_ROUNDS)
    finally:
        stop.set()
        for v in (base, hedge):
            try:
                await v.close()
            except Exception:
                pass
        await session.close()
