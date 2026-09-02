"""tools/analyze.py + tools/migrate_per_symbol.py: per-symbol table handling.

Run:  python3 -m pytest tests/  (or  python3 tests/test_tools.py)
"""
import importlib.util
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import duckdb  # noqa: E402

from entropy_arb.recorder import create_table_sql, minute_table  # noqa: E402

_TOOLS = os.path.join(os.path.dirname(__file__), "..", "tools")


def _load_tool(name):
    path = os.path.join(_TOOLS, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


analyze = _load_tool("analyze")
migrate = _load_tool("migrate_per_symbol")


def _insert_rows(con, table, symbol, venue, n_minutes, t0=1_700_000_000):
    """n_minutes deterministic rows (every minute passes analyze's gates)."""
    from datetime import datetime, timezone
    values = []
    for i in range(n_minutes):
        ts = (t0 // 60 + i) * 60
        values.append((ts, datetime.fromtimestamp(ts, tz=timezone.utc)
                       .replace(tzinfo=None), symbol, venue,
                       100.0, 100.02, 99.99, 100.01,
                       10.0, 12.0, 8.0, 10.0, 10.0, 1.5,
                       8.0, 14.0, 8.0, 14.0, 30))
    con.executemany(
        f'INSERT OR REPLACE INTO "{table}" VALUES ('
        + ", ".join("?" * 19) + ")", values)


def build_db(path, combos, n_minutes=40):
    """Create per-symbol tables holding one table+rows per (symbol, venue)."""
    con = duckdb.connect(path)
    try:
        for symbol, venue in combos:
            table = minute_table(symbol)
            con.execute(create_table_sql(table))
            _insert_rows(con, table, symbol, venue, n_minutes)
    finally:
        con.close()
    return path


def build_legacy_db(path, symbols=("SNDK",), n_minutes=40):
    """Recreate the pre-split shared `minutes` table with rows."""
    con = duckdb.connect(path)
    try:
        con.execute(create_table_sql("minutes"))
        for sym in symbols:
            _insert_rows(con, "minutes", sym, "lighter-rh", n_minutes)
    finally:
        con.close()
    return path


def tmp_db(name="minutes.duckdb"):
    return os.path.join(tempfile.mkdtemp(), name)


def tables_of(path):
    con = duckdb.connect(path, read_only=True)
    try:
        return [r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() ORDER BY 1").fetchall()]
    finally:
        con.close()


def count_rows(path, table):
    con = duckdb.connect(path, read_only=True)
    try:
        return con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    finally:
        con.close()


def test_discover_tables():
    p = build_db(tmp_db(), [("SNDK", "lighter-rh"), ("TSLA", "tradexyz")])
    con = duckdb.connect(p)
    try:
        # legacy table with rows, a decoy, and an unrelated table
        con.execute(create_table_sql("minutes"))
        _insert_rows(con, "minutes", "SNDK", "lighter-rh", 3)
        con.execute("CREATE TABLE minutesfoo (x INT)")
        con.execute("CREATE TABLE other (x INT)")
    finally:
        con.close()
    assert analyze.discover_tables(p) == ["minutes_sndk", "minutes_tsla"]


def test_load_combos_data_driven():
    # one table, two venues -> two independent combos, derived from data
    p = tmp_db()
    con = duckdb.connect(p)
    try:
        t = minute_table("SNDK")
        con.execute(create_table_sql(t))
        _insert_rows(con, t, "SNDK", "lighter-rh", 5)
        _insert_rows(con, t, "SNDK", "tradexyz", 5)
    finally:
        con.close()
    combos = analyze.load_combos(p, [t])
    assert combos == [(t, "SNDK", "lighter-rh"), (t, "SNDK", "tradexyz")]
    assert analyze.legacy_minutes_rows(p) == 0


def test_analyze_subprocess():
    p = build_db(tmp_db(), [("SNDK", "lighter-rh")])
    r = subprocess.run(
        [sys.executable, os.path.join(_TOOLS, "analyze.py"), "--db", p],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "SNDK · lighter-rh" in r.stdout
    assert "thresholds:" in r.stdout


def test_analyze_symbol_filter():
    p = build_db(tmp_db(), [("SNDK", "lighter-rh"), ("TSLA", "tradexyz")])
    base = [sys.executable, os.path.join(_TOOLS, "analyze.py"), "--db", p]

    r = subprocess.run(base + ["--symbol", "TSLA"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "TSLA · tradexyz" in r.stdout
    assert "SNDK" not in r.stdout

    r = subprocess.run(base + ["--symbol", "NOPE"],
                       capture_output=True, text=True)
    assert r.returncode == 1
    assert "no data" in r.stderr


def test_analyze_legacy_hint():
    p = build_legacy_db(tmp_db())
    r = subprocess.run(
        [sys.executable, os.path.join(_TOOLS, "analyze.py"), "--db", p],
        capture_output=True, text=True)
    assert r.returncode == 1
    assert "migrate_per_symbol" in r.stderr
    assert "thresholds:" not in r.stdout   # never reports from the old table


def test_migrate_splits_and_is_idempotent():
    p = build_legacy_db(tmp_db(), symbols=("SNDK", "TSLA"))

    assert migrate.main(["--db", p]) == 0
    assert tables_of(p) == ["minutes", "minutes_sndk", "minutes_tsla"]
    assert count_rows(p, "minutes_sndk") == 40
    assert count_rows(p, "minutes_tsla") == 40
    assert count_rows(p, "minutes") == 80          # legacy kept by default

    # idempotent: same counts on re-run
    assert migrate.main(["--db", p]) == 0
    assert count_rows(p, "minutes_sndk") == 40
    assert count_rows(p, "minutes_tsla") == 40

    assert migrate.main(["--db", p, "--drop-old"]) == 0
    assert tables_of(p) == ["minutes_sndk", "minutes_tsla"]

    # an analyze run now works off the migrated tables
    r = subprocess.run(
        [sys.executable, os.path.join(_TOOLS, "analyze.py"), "--db", p],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    # fresh db with no legacy table: nothing to do
    empty = tmp_db("empty.duckdb")
    duckdb.connect(empty).close()
    assert migrate.main(["--db", empty]) == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
