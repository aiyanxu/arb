#!/usr/bin/env python3
"""Analyze recorded minute data and suggest config.yaml thresholds.

Reads the DuckDB database written by the built-in recorder
(logs/minutes.duckdb by default), where each (symbol, base_venue,
hedge_venue) combination has its own
minutes_<symbol>__<base>__<hedge> table, and prints one report per
combination found:

  * the premium distribution (midline candidates),
  * how often each candidate upper/lower band would have fired,
  * a ready-to-paste `thresholds:` snippet.

Premium is measured for ONE (base, hedge) pair — the numbers are
pair-relative and never transfer across combinations.

Data recorded before the per-(base,hedge,symbol) split still sits in
older-shaped tables (the shared `minutes` table and per-symbol
`minutes_<symbol>` tables); the analyzer never reads them and points at
tools/migrate_per_symbol.py to move them.

分析机器人自动采集的分钟级盘口数据，按 (symbol, base_venue, hedge_venue)
组合分组输出溢价分布、各档阈值的触发频率，以及可直接粘贴进 config.yaml
的 thresholds 建议值。每个组合独立一张表；分表前的旧数据（共享 minutes
表与 minutes_<symbol> 表）不会被读取，需先运行
tools/migrate_per_symbol.py 迁移。

Usage:
    python3 tools/analyze.py                          # logs/minutes.duckdb, all combos
    python3 tools/analyze.py --db p.duckdb --symbol SNDK --base-venue entropy \
        --hedge-venue lighter-rh
    python3 tools/analyze.py --hours 24 --min-samples 10
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time

import duckdb

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
try:
    from entropy_arb.recorder import TABLE_PREFIX
except ImportError as e:
    raise SystemExit(
        "this tool needs the entropy_arb package — run it from the repo root "
        "or pip install entropy-arb / 需在仓库根目录运行或先安装 entropy-arb") from e

CANDIDATES = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 20.0]

# per-symbol tables start with the literal prefix "minutes_": the escaped
# '_' excludes both the legacy `minutes` table and decoys like `minutesfoo`
_TABLE_PATTERN = TABLE_PREFIX.replace("_", r"\_") + "%"

_DISCOVER_SQL = ("SELECT table_name FROM duckdb_tables() "
                 "WHERE schema_name = 'main' "
                 "AND table_name LIKE ? ESCAPE '\\' "
                 "ORDER BY table_name")


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


def discover_tables(db: str) -> list[str]:
    """Per-combination tables in the db (legacy/decoy names excluded)."""
    con = duckdb.connect(db, read_only=True)
    try:
        return [r[0] for r in
                con.execute(_DISCOVER_SQL, [_TABLE_PATTERN]).fetchall()]
    finally:
        con.close()


def _columns(con, table: str) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT column_name FROM duckdb_columns() "
        "WHERE schema_name = 'main' AND table_name = ? ORDER BY column_index",
        [table]).fetchall()]


def split_tables(db: str, tables: list[str]) -> tuple[list[str], list[str]]:
    """(current, old-shape) tables. Current tables carry the base_venue
    column; old-shape ones (per-symbol or shared-`minutes` era) are ignored
    here but reported so the user can migrate."""
    cur, old = [], []
    con = duckdb.connect(db, read_only=True)
    try:
        for t in tables:
            (cur if "base_venue" in _columns(con, t) else old).append(t)
    finally:
        con.close()
    return cur, old


def load_combos(db: str, tables: list[str]) -> list:
    """(table, symbol, base_venue, hedge_venue) quads, data-derived."""
    out = []
    con = duckdb.connect(db, read_only=True)
    try:
        for t in tables:
            rows = con.execute(
                f'SELECT DISTINCT symbol, base_venue, hedge_venue FROM "{t}" '
                f"WHERE symbol IS NOT NULL "
                f"ORDER BY symbol, base_venue, hedge_venue"
            ).fetchall()
            out += [(t, s, b, v) for s, b, v in rows]
    finally:
        con.close()
    return out


def legacy_minutes_rows(db: str) -> int:
    """Row count of the pre-split shared `minutes` table (0 if absent)."""
    con = duckdb.connect(db, read_only=True)
    try:
        has = con.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'minutes'"
        ).fetchone()
        if not has or not has[0]:
            return 0
        n = con.execute("SELECT count(*) FROM minutes").fetchone()
        return n[0] if n else 0
    finally:
        con.close()


def load_rows(db: str, table: str, hours: float, min_samples: int,
              symbol: str, base_venue: str, hedge_venue: str) -> list:
    cutoff = time.time() - hours * 3600 if hours > 0 else 0.0
    con = duckdb.connect(db, read_only=True)
    try:
        rows = con.execute(
            f"SELECT minute_ts, premium_close_bps, premium_mean_bps, "
            f"sell_edge_max_bps, buy_edge_max_bps FROM \"{table}\" "
            f"WHERE minute_ts >= ? AND samples >= ? "
            f"AND symbol = ? AND base_venue = ? AND hedge_venue = ? "
            f"ORDER BY minute_ts",
            [cutoff, min_samples, symbol, base_venue, hedge_venue]).fetchall()
    finally:
        con.close()
    return [{"ts": r[0], "prem": r[1], "prem_mean": r[2],
             "sell_max": r[3], "buy_max": r[4]} for r in rows]


def main() -> None:
    p = argparse.ArgumentParser(description="suggest thresholds from recorded "
                                            "minute data")
    p.add_argument("--db", default="logs/minutes.duckdb",
                   help="DuckDB database written by the recorder "
                        "(default: logs/minutes.duckdb)")
    p.add_argument("--symbol", default=None,
                   help="only analyze this symbol (default: all)")
    p.add_argument("--base-venue", default=None,
                   help="only analyze this base venue (default: all)")
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
        tables = discover_tables(args.db)
    except FileNotFoundError:
        print(f"{args.db} not found — run the bot (even --record-only) to "
              f"collect data first / 未找到数据库文件，请先运行机器人采集数据",
              file=sys.stderr)
        sys.exit(1)
    except duckdb.Error as e:
        print(f"cannot read {args.db}: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        legacy = legacy_minutes_rows(args.db)
        current, old_shape = split_tables(args.db, tables)
    except duckdb.Error as e:
        print(f"cannot read {args.db}: {e}", file=sys.stderr)
        sys.exit(1)
    if legacy or old_shape:
        parts = ([f"'minutes' ({legacy} row(s))"] if legacy else [])
        parts += [f"{t} (no base_venue column)" for t in old_shape]
        print(f"note: {args.db} still has old-shaped table(s) — "
              f"{', '.join(parts)} — ignored here; run: python3 "
              f"tools/migrate_per_symbol.py --db {args.db} / 旧表结构数据"
              f"需先迁移", file=sys.stderr)
    combos = [(t, s, b, v) for (t, s, b, v) in
              load_combos(args.db, current)
              if (not args.symbol or s == args.symbol)
              and (not args.base_venue or b == args.base_venue)
              and (not args.hedge_venue or v == args.hedge_venue)]
    if not combos:
        print(f"no data in {args.db} yet — run the bot (even --record-only) "
              f"to collect data first / 数据库中还没有数据，请先运行机器人"
              f"采集数据", file=sys.stderr)
        sys.exit(1)

    # analyze each (symbol, base, hedge) combination separately: premiums of
    # different combinations are unrelated, pooling them would be meaningless
    reports = 0
    for table, sym, base, venue in combos:
        try:
            rows = load_rows(args.db, table, args.hours, args.min_samples,
                             sym, base, venue)
        except duckdb.Error as e:
            print(f"cannot read {args.db}: {e}", file=sys.stderr)
            sys.exit(1)
        if len(rows) < 30:
            print(f"only {len(rows)} usable minute(s) for {sym} · {base}×"
                  f"{venue} in {args.db} — collect at least a few hours "
                  f"before trusting the numbers / 数据太少，建议至少采集数小时",
                  file=sys.stderr)
            continue
        if reports:
            print("\n" + "=" * 60 + "\n")
        report(sym, base, venue, args, rows)
        reports += 1
    if not reports:
        sys.exit(1)


def report(sym: str, base: str, venue: str, args, rows: list) -> None:
    """Per-combination analysis. Pure-Python stats unchanged from the CSV era."""
    span_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600.0 + 1 / 60.0
    prem = sorted(r["prem"] for r in rows)
    mean = sum(prem) / len(prem)
    var = sum((x - mean) ** 2 for x in prem) / len(prem)
    median = pctl(prem, 50)

    label = f"{sym} · {base}×{venue}"
    print(f"\n=== {label} [{args.db}]: {len(rows)} minutes over "
          f"{span_h:.1f}h ===\n")
    print(f"premium of {base} over {venue}, minute close (bps) / "
          f"{base} 相对 {venue} 的溢价:")
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
    print(f"  {'band bps':>9} | {'SELL ' + base:>17} | {'BUY ' + base:>17}")
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
