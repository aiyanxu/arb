#!/usr/bin/env python3
"""One-time migration: move old-shaped minute tables to
per-(symbol, base_venue, hedge_venue) tables.

Handles both legacy shapes, stamping every migrated row with
base_venue='entropy' (the historical truth — entropy was always the base
leg before it became configurable):

  a. the pre-split shared `minutes` table (symbol + hedge_venue columns)
  b. per-symbol `minutes_<symbol>` tables (no base_venue column)

Target tables are named minutes_<symbol>__<base>__<hedge> with the 20-column
layout (base_venue column, base_bid/base_ask). Stop the bot first — DuckDB
files are single-writer. Idempotent: re-running just re-runs INSERT OR
REPLACE with no effect. Source tables are kept unless --drop-old is passed.

一次性迁移：把两种旧结构的分钟表迁入按 (symbol, base_venue, hedge_venue)
组合的分表（全部盖 base_venue='entropy' 章——历史上 entropy 一直是 base
腿）：a) 分表前的共享 `minutes` 表；b) 按 symbol 分表的
`minutes_<symbol>` 表。请先停止机器人再运行。可重复执行；默认保留旧表，
传入 --drop-old 可在迁移成功后删除。

Usage:
    python3 tools/migrate_per_symbol.py --db logs/minutes.duckdb [--drop-old]
"""
from __future__ import annotations

import argparse
import os
import sys

import duckdb

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
try:
    from entropy_arb.recorder import create_table_sql, minute_table
except ImportError as e:
    raise SystemExit(
        "this tool needs the entropy_arb package — run it from the repo root "
        "or pip install entropy-arb / 需在仓库根目录运行或先安装 entropy-arb") from e

LEGACY_BASE = "entropy"   # historical truth: entropy was always the base leg

# old 19-column shape -> new 20-column order, stamping base_venue. DuckDB
# matches INSERT ... SELECT by position.
_COPY_SQL = (
    'INSERT OR REPLACE INTO "{dst}" SELECT minute_ts, time_utc, symbol, '
    "'{base}' AS base_venue, hedge_venue, "
    "entropy_bid, entropy_ask, hedge_bid, hedge_ask, "
    "premium_open_bps, premium_high_bps, premium_low_bps, premium_close_bps, "
    "premium_mean_bps, premium_std_bps, sell_edge_mean_bps, sell_edge_max_bps, "
    "buy_edge_mean_bps, buy_edge_max_bps, samples "
    'FROM "{src}" WHERE symbol IS NOT NULL AND hedge_venue IS NOT NULL '
    "AND symbol = ? AND hedge_venue = ?")


def _columns(con, table: str) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT column_name FROM duckdb_columns() "
        "WHERE schema_name = 'main' AND table_name = ? ORDER BY column_index",
        [table]).fetchall()]


def _minutes_tables(con) -> list[str]:
    # same escaped-prefix rule as analyze.py; excludes the shared `minutes`
    return [r[0] for r in con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main' "
        "AND table_name LIKE 'minutes\\_%' ESCAPE '\\' ORDER BY table_name"
    ).fetchall()]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="move old-shaped minute tables "
                                            "to per-(base, hedge, symbol) "
                                            "tables")
    p.add_argument("--db", default="logs/minutes.duckdb",
                   help="DuckDB database written by the recorder "
                        "(default: logs/minutes.duckdb)")
    p.add_argument("--drop-old", action="store_true",
                   help="drop the old-shaped source tables after a "
                        "successful migration (default: keep them)")
    args = p.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"{args.db} not found / 未找到数据库文件", file=sys.stderr)
        return 1
    try:
        # fails loudly if the bot still holds the single-writer lock
        con = duckdb.connect(args.db)
    except duckdb.IOException as e:
        print(f"cannot open {args.db} for writing ({e}) — stop the bot "
              f"first / 数据库被占用，请先停止机器人", file=sys.stderr)
        return 1
    try:
        # sources, oldest first: the shared table pre-dates per-symbol
        # tables, so on overlapping minutes the newer rows win (last write)
        sources = []
        has = con.execute("SELECT count(*) FROM duckdb_tables() "
                          "WHERE table_name = 'minutes'").fetchone()
        if has and has[0]:
            sources.append("minutes")
        sources += [t for t in _minutes_tables(con)
                    if "base_venue" not in _columns(con, t)]
        if not sources:
            print(f"nothing to migrate in {args.db}")
            return 0

        pairs = []                      # (src_table, symbol, hedge_venue)
        for src in sources:
            rows = con.execute(
                f'SELECT DISTINCT symbol, hedge_venue FROM "{src}" '
                f"WHERE symbol IS NOT NULL AND hedge_venue IS NOT NULL "
                f"ORDER BY symbol, hedge_venue").fetchall()
            pairs += [(src, s, v) for s, v in rows]
        if not pairs:
            print("old-shaped table(s) present but empty — nothing to "
                  "migrate / 旧表为空，无需迁移")
            return 0

        con.execute("BEGIN TRANSACTION")
        for src, sym, hv in pairs:
            dst = minute_table(sym, LEGACY_BASE, hv)
            con.execute(create_table_sql(dst))
            con.execute(_COPY_SQL.format(dst=dst, src=src, base=LEGACY_BASE),
                        [sym, hv])
            n = con.execute(f'SELECT count(*) FROM "{dst}"').fetchone()
            print(f"{src}: {sym!r} x {LEGACY_BASE} x {hv!r} -> {dst}: "
                  f"{n[0] if n else 0} row(s)")
        if args.drop_old:
            for src in dict.fromkeys(s for s, _, _ in pairs):  # keep order
                con.execute(f'DROP TABLE "{src}"')
            print(f"dropped {len(dict.fromkeys(s for s, _, _ in pairs))} "
                  f"old-shaped table(s)")
        con.execute("COMMIT")
        print(f"migrated {len(pairs)} combination(s) from "
              f"{len(dict.fromkeys(s for s, _, _ in pairs))} table(s) "
              f"in {args.db}")
        return 0
    except duckdb.Error as e:
        con.execute("ROLLBACK")
        print(f"migration failed: {e} / 迁移失败", file=sys.stderr)
        return 1
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
