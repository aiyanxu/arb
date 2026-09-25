"""Scheduled threshold-drift check — re-derive the analyzer's suggestion.

Every `threshold_check.interval_sec` this re-derives the thresholds the
analyzer would suggest from the recorder's own minute data, compares them
with the values in config.yaml, and fires a notification (entropy_arb.notify)
when any of the three numbers drifted apart by more than the tolerance.

"10th percentile" (10分位) mapping — the same math tools/analyze.py uses to
produce its "suggested starting point", so the comparison is always against
exactly the numbers a user would paste into config.yaml:
    midline_bps -> median (p50) of the minute-close premium
    upper_bps   -> p90 of the fee-adjusted executable SELL room beyond the
                   midline   (floored at 1.0 bps, rounded to 0.5)
    lower_bps   -> p90 of the buy-side room likewise

Relative drift is measured against the CONFIG value (what the user
committed to), with an absolute floor on top: any move beyond
ABS_FLOOR_BPS (1.5 bps) counts as drifted even when the ratio is small —
10% of a large midline is already a silent loss of a fifth of a round
trip's net edge. A config value of 0 (a common midline) can't form a
ratio; there the floor is the whole test.

定时阈值巡检：每 interval_sec 用采集的分钟数据按 analyze 的建议算法重新
推导 thresholds（中位数→midline，p90→upper/lower），与 config.yaml 当前值
对比，任一值相对偏差超过容差（默认 10%）或绝对偏移超过 1.5 bps 时调用
notify.send() 发通知。
数据不足或库不可读时静默跳过——巡检绝不影响交易。

Usage: wired into the engine as a background task (threshold_check.enabled).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

log = logging.getLogger("threshold_check")

# analyze floors its suggested bands at 1 bps; mirror that here so the
# comparison is against the numbers a user would actually paste
SUGGEST_FLOOR_BPS = 1.0
# how much recorded history the derivation wants before numbers are trusted
MIN_MINUTES = 120
MIN_SAMPLES = 10          # analyze's default quality gate
HOURS = 0.0               # 0 = all recorded data (analyze's default)
# absolute drift floor, applied to every key alongside the relative test:
# a suggested move beyond this many bps counts as drifted even when the
# relative test passes (10% of a large midline is a lot of edge to lose
# silently), and it is the whole test when the config value is 0 (no ratio
# can be formed). Sized from recorded data: with ~120 usable minutes in a
# 2h window and this pair's observed dispersion (~5.6 bps std), the median's
# standard error is ~0.6 bps, so 1.5 bps (~2.3 sigma) stays quiet on
# sampling noise while flagging a real shift within one check interval.
ABS_FLOOR_BPS = 1.5

_KEYS = ("midline_bps", "upper_bps", "lower_bps")


def suggested_thresholds(rows: list, fees: float = 0.0) -> dict:
    """The three numbers the analyzer would suggest today, from minute rows
    (analyze.load_rows output). Same math as tools/analyze.py's report."""
    from .analyze import pctl
    prem = sorted(r["prem"] for r in rows)
    midline = round(pctl(prem, 50), 1) or 0.0    # normalize -0.0
    sell_room = sorted((r["sell_max"] - midline - fees for r in rows),
                       reverse=True)
    buy_room = sorted((r["buy_max"] + midline - fees for r in rows),
                      reverse=True)
    return {
        "midline_bps": midline,
        "upper_bps": max(round(pctl(sell_room, 90) * 2) / 2, 1.0),
        "lower_bps": max(round(pctl(buy_room, 90) * 2) / 2, SUGGEST_FLOOR_BPS),
    }


def drift_report(current: dict, suggested: dict,
                 tolerance: float) -> list:
    """[(key, current, suggested, rel)] for each value whose relative drift
    from the config value exceeds tolerance OR whose absolute move exceeds
    ABS_FLOOR_BPS. Relative to the config value (the user's committed
    number); a config value of 0 can't form a ratio, so only the floor
    applies there.
    """
    out = []
    for k in _KEYS:
        cur = float(current[k])
        sug = float(suggested[k])
        if abs(cur) < 1e-9:
            drifted = abs(sug) > ABS_FLOOR_BPS
            rel = float("inf")
        else:
            rel = abs(sug - cur) / abs(cur)
            # a hair of slack: 4.4 vs 4.0 is exactly 10% but floating point
            # makes it 0.10000000000000009
            drifted = (rel > tolerance + 1e-9
                       or abs(sug - cur) > ABS_FLOOR_BPS)
        if drifted:
            out.append((k, cur, sug, rel))
    return out


async def check_once(cfg) -> bool:
    """One check of the configured thresholds against recorded data.

    Returns True when a drift notification was sent. Reads the recorder's
    DuckDB read-only (the writer releases its lock between minutes), so the
    running bot is never blocked. Skips silently when there is no db or too
    little usable data — the check must never be louder than trading.
    """
    from . import notify
    db = cfg.recorder_db
    if not os.path.exists(db):
        log.info("threshold check: %s does not exist yet", db)
        return False
    try:
        from .analyze import load_rows
        from .recorder import minute_table
        rows = load_rows(db, minute_table(cfg.symbol, cfg.base_venue,
                                          cfg.hedge_venue),
                         cfg.threshold_check_hours,
                         cfg.threshold_check_min_samples,
                         cfg.symbol, cfg.base_venue, cfg.hedge_venue)
    except Exception as e:
        log.warning("threshold check could not read %s: %r", db, e)
        return False
    if len(rows) < max(cfg.threshold_check_min_minutes, 30):
        log.info("threshold check: only %d usable minute(s) — skipping",
                 len(rows))
        return False
    fees = cfg.base.fee_bps + cfg.hedge.fee_bps
    sug = suggested_thresholds(rows, fees=fees)
    current = {"midline_bps": cfg.midline_bps, "upper_bps": cfg.upper_bps,
               "lower_bps": cfg.lower_bps}
    drifted = drift_report(current, sug, cfg.threshold_check_tolerance)
    if not drifted:
        log.info("threshold check: config within tolerance")
        return False
    pair = f"{cfg.base_venue}×{cfg.hedge_venue}"
    report = format_drift_report(cfg.symbol, pair, current, sug, drifted)
    log.warning("threshold drift on %s (%s) — %d of 3 thresholds off",
                cfg.symbol, pair, len(drifted))
    notify.send(report)
    return True


def format_drift_report(symbol: str, pair: str, current: dict,
                        sug: dict, drifted: list) -> str:
    """The notification text: one line per drifted number."""
    lines = [f"threshold drift on {symbol} ({pair}) — re-run "
             f"`entropy-arb analyze` and update config.yaml:", ""]
    for k, cur, sug_v, rel in drifted:
        pct = "n/a (config 0)" if rel == float("inf") else f"{rel * 100:+.0f}%"
        lines.append(f"  {k}: config {cur:+.2f} -> suggested {sug_v:+.2f} "
                     f"({pct})")
    lines.append("")
    lines.append("premiums drift — a stale midline is a losing strategy. "
                 "Re-check with: entropy-arb analyze")
    return "\n".join(lines)


class ThresholdDriftChecker:

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.interval = cfg.threshold_check_interval_sec

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(),
                                       timeout=self.interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await check_once(self.cfg)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("threshold check round failed")
