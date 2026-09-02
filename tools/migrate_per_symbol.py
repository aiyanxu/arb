#!/usr/bin/env python3
"""One-time migration: split the legacy shared `minutes` table per symbol.

Every symbol in the legacy table gets its own minutes_<symbol> table (same
columns, same primary key). Stop the bot first — DuckDB files are
single-writer. Idempotent: re-running just re-runs INSERT OR REPLACE with
no effect. The legacy table is kept unless --drop-old is passed.

一次性迁移：把旧的共享 `minutes` 表按 symbol 拆分为 minutes_<symbol>
分表（列与主键不变）。请先停止机器人再运行。可重复执行；默认保留旧表，
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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="split the legacy `minutes` "
                                            "table into per-symbol tables")
    p.add_argument("--db", default="logs/minutes.duckdb",
                   help="DuckDB database written by the recorder "
                        "(default: logs/minutes.duckdb)")
    p.add_argument("--drop-old", action="store_true",
                   help="drop the legacy 'minutes' table after a successful "
                        "migration (default: keep it)")
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
        has = con.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'minutes'"
        ).fetchone()
        if not has or not has[0]:
            print(f"nothing to migrate in {args.db}")
            return 0
        symbols = [r[0] for r in con.execute(
            "SELECT DISTINCT symbol FROM minutes "
            "WHERE symbol IS NOT NULL ORDER BY symbol").fetchall()]
        con.execute("BEGIN TRANSACTION")
        for sym in symbols:
            tbl = minute_table(sym)
            con.execute(create_table_sql(tbl))
            # column order matches by construction: both tables use the
            # same _TABLE_DDL
            con.execute(f'INSERT OR REPLACE INTO "{tbl}" '
                        f"SELECT * FROM minutes WHERE symbol = ?", [sym])
            n = con.execute(f'SELECT count(*) FROM "{tbl}"').fetchone()
            print(f"{sym!r} -> {tbl}: {n[0] if n else 0} row(s)")
        if args.drop_old:
            con.execute("DROP TABLE minutes")
            print("dropped legacy table 'minutes'")
        con.execute("COMMIT")
        print(f"migrated {len(symbols)} symbol(s) in {args.db}")
        return 0
    except duckdb.Error as e:
        con.execute("ROLLBACK")
        print(f"migration failed: {e} / 迁移失败", file=sys.stderr)
        return 1
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
