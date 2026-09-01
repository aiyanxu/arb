#!/usr/bin/env python3
"""Analyze recorded minute data and suggest config.yaml thresholds.

Reads the DuckDB database written by the built-in recorder
(logs/minutes.duckdb by default) and prints, per (symbol, hedge_venue)
pair found:

  * the premium distribution (midline candidates),
  * how often each candidate upper/lower band would have fired,
  * a ready-to-paste `thresholds:` snippet.

分析机器人自动采集的分钟级盘口数据，按 (symbol, hedge_venue) 分组输出
溢价分布、各档阈值的触发频率，以及可直接粘贴进 config.yaml 的
thresholds 建议值。

Usage:
    python3 tools/analyze.py                          # logs/minutes.duckdb, all pairs
    python3 tools/analyze.py --db path.duckdb --symbol SNDK --hedge-venue lighter-rh
    python3 tools/analyze.py --hours 24 --min-samples 10
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time

import duckdb

CANDIDATES = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 20.0]


def pctl(sorted_vals: list, q: float) -> float:
    """Linear-interpolated percentile of a pre-sorted list, q in [0, 100]."""
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * q / 100.0
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def load_rows(db: str, hours: float, min_samples: int,
              symbol: str, hedge_venue: str) -> list:
    cutoff = time.time() - hours * 3600 if hours > 0 else 0.0
    where = ["minute_ts >= ?", "samples >= ?"]
    params: list = [cutoff, min_samples]
    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    if hedge_venue:
        where.append("hedge_venue = ?")
        params.append(hedge_venue)
    con = duckdb.connect(db, read_only=True)
    try:
        rows = con.execute(
            f"SELECT minute_ts, premium_close_bps, premium_mean_bps, "
            f"sell_edge_max_bps, buy_edge_max_bps FROM minutes "
            f"WHERE {' AND '.join(where)} ORDER BY minute_ts",
            params).fetchall()
    finally:
        con.close()
    return [{"ts": r[0], "prem": r[1], "prem_mean": r[2],
             "sell_max": r[3], "buy_max": r[4]} for r in rows]


def load_combos(db: str) -> list:
    con = duckdb.connect(db, read_only=True)
    try:
        return con.execute(
            "SELECT DISTINCT symbol, hedge_venue FROM minutes "
            "ORDER BY symbol, hedge_venue").fetchall()
    finally:
        con.close()


def main() -> None:
    p = argparse.ArgumentParser(description="suggest thresholds from recorded "
                                            "minute data")
    p.add_argument("--db", default="logs/minutes.duckdb",
                   help="DuckDB database written by the recorder "
                        "(default: logs/minutes.duckdb)")
    p.add_argument("--symbol", default=None,
                   help="only analyze this symbol (default: all)")
    p.add_argument("--hedge-venue", default=None,
                   help="only analyze this hedge venue (default: all)")
    p.add_argument("--hours", type=float, default=0.0,
                   help="only use the last N hours (0 = all data)")
    p.add_argument("--min-samples", type=int, default=10,
                   help="skip minutes with fewer fresh samples than this")
    p.add_argument("--fees-bps", type=float, default=0.0,
                   help="SUM of both venues' taker fees in bps (each crossing "
                        "pays both legs); recorded edges are pre-fee, so this "
                        "is subtracted before counting firings (default 0.0 — "
                        "pass ~1.0 with a tradexyz hedge)")
    args = p.parse_args()

    if not os.path.exists(args.db):
        print(f"{args.db} not found — run the bot (even --record-only) to "
              f"collect data first / 未找到数据库文件，请先运行机器人采集数据",
              file=sys.stderr)
        sys.exit(1)
    try:
        combos = ([(args.symbol, args.hedge_venue)]
                  if args.symbol or args.hedge_venue else load_combos(args.db))
    except FileNotFoundError:
        print(f"{args.db} not found — run the bot (even --record-only) to "
              f"collect data first / 未找到数据库文件，请先运行机器人采集数据",
              file=sys.stderr)
        sys.exit(1)
    except duckdb.Error as e:
        print(f"cannot read {args.db}: {e}", file=sys.stderr)
        sys.exit(1)
    if not combos:
        print(f"no data in {args.db} yet — run the bot (even --record-only) "
              f"to collect data first / 数据库中还没有数据，请先运行机器人"
              f"采集数据", file=sys.stderr)
        sys.exit(1)

    # analyze each (symbol, hedge_venue) pair separately: premiums of
    # different pairs are unrelated, pooling them would be meaningless
    reports = 0
    for sym, venue in combos:
        try:
            rows = load_rows(args.db, args.hours, args.min_samples, sym, venue)
        except duckdb.Error as e:
            print(f"cannot read {args.db}: {e}", file=sys.stderr)
            sys.exit(1)
        if len(rows) < 30:
            label = " · ".join(x for x in (sym, venue) if x) or "data"
            print(f"only {len(rows)} usable minute(s) for {label} in "
                  f"{args.db} — collect at least a few hours before trusting "
                  f"the numbers / 数据太少，建议至少采集数小时", file=sys.stderr)
            continue
        if reports:
            print("\n" + "=" * 60 + "\n")
        report(sym, venue, args, rows)
        reports += 1
    if not reports:
        sys.exit(1)


def report(sym: str, venue: str, args, rows: list) -> None:
    """Per-pair analysis. Pure-Python stats unchanged from the CSV era."""
    span_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600.0 + 1 / 60.0
    prem = sorted(r["prem"] for r in rows)
    mean = sum(prem) / len(prem)
    var = sum((x - mean) ** 2 for x in prem) / len(prem)
    median = pctl(prem, 50)

    label = f"{sym} · {venue}" if sym else "all data"
    print(f"\n=== {label} [{args.db}]: {len(rows)} minutes over "
          f"{span_h:.1f}h ===\n")
    print("premium of Entropy over hedge, minute close (bps) / "
          "Entropy 相对对冲腿的溢价:")
    print(f"  mean {mean:+.2f}   std {math.sqrt(var):.2f}   "
          f"median {median:+.2f}")
    print(f"  p5 {pctl(prem, 5):+.2f}   p25 {pctl(prem, 25):+.2f}   "
          f"p75 {pctl(prem, 75):+.2f}   p95 {pctl(prem, 95):+.2f}")

    midline = round(median, 1) or 0.0   # normalize -0.0
    # room beyond the midline that was actually executable each minute, net
    # of taker fees (config thresholds are net-of-fee: the engine adds fees
    # on top, and recorded edges are pre-fee)
    fees = args.fees_bps
    sell_room = sorted((r["sell_max"] - midline - fees for r in rows),
                       reverse=True)
    buy_room = sorted((r["buy_max"] + midline - fees for r in rows),
                      reverse=True)

    print(f"\nwith midline_bps = {midline:+.1f} (median) and {fees:.1f} bps "
          f"round-trip taker fees, minutes each band would have fired / "
          f"各档净阈值触发的分钟数:")
    print(f"  {'band bps':>9} | {'SELL entropy':>17} | {'BUY entropy':>17}")
    print(f"  {'':>9} | {'minutes':>8} {'per day':>8} | "
          f"{'minutes':>8} {'per day':>8}")
    per_day = 24.0 / span_h if span_h > 0 else 0.0
    for t in CANDIDATES:
        s_hits = sum(1 for x in sell_room if x >= t)
        b_hits = sum(1 for x in buy_room if x >= t)
        print(f"  {t:>9.1f} | {s_hits:>8} {s_hits * per_day:>8.1f} | "
              f"{b_hits:>8} {b_hits * per_day:>8.1f}")

    # default suggestion: the band that fired in ~10% of minutes (p90 of the
    # fee-adjusted executable room), floored at 1 bps — tune from the table
    sug_upper = max(round(pctl(sorted(sell_room), 90) * 2) / 2, 1.0)
    sug_lower = max(round(pctl(sorted(buy_room), 90) * 2) / 2, 1.0)
    print(f"""
suggested starting point (fires ~10% of minutes, already net of the
{fees:.1f} bps fees passed via --fees-bps; a full round trip nets
>= upper+lower bps after fees) /
建议起点（约 10% 的分钟触发；已扣除 --fees-bps 传入的 {fees:.1f} bps 手续费，
一次完整往返扣费后净赚 >= upper+lower bps）:

thresholds:
  midline_bps: {midline}
  upper_bps: {sug_upper}
  lower_bps: {sug_lower}

Re-run with --hours to focus on recent regimes; premiums drift, so refresh
these numbers regularly. / 溢价中枢会漂移，请定期重新分析并更新配置。
""")


if __name__ == "__main__":
    main()
