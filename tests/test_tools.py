"""tools/analyze.py + tools/migrate_per_symbol.py: per-combination tables.

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

# the pre-(base,hedge,symbol) 19-column shape, as a literal so building
# legacy fixtures keeps working no matter how recorder.py evolves
LEGACY_DDL = """(
    minute_ts BIGINT, time_utc TIMESTAMP,
    symbol VARCHAR, hedge_venue VARCHAR,
    entropy_bid DOUBLE, entropy_ask DOUBLE,
    hedge_bid DOUBLE, hedge_ask DOUBLE,
    premium_open_bps DOUBLE, premium_high_bps DOUBLE,
    premium_low_bps DOUBLE, premium_close_bps DOUBLE,
    premium_mean_bps DOUBLE, premium_std_bps DOUBLE,
    sell_edge_mean_bps DOUBLE, sell_edge_max_bps DOUBLE,
    buy_edge_mean_bps DOUBLE, buy_edge_max_bps DOUBLE,
    samples INTEGER,
    PRIMARY KEY (symbol, hedge_venue, minute_ts)
)"""


def _insert_rows(con, table, symbol, venue, n_minutes, base="entropy",
                 t0=1_700_000_000, shape="new"):
    """n_minutes deterministic rows (every minute passes analyze's gates).

    shape="new": 20-column layout (base_venue + base_bid/ask names).
    shape="old": 19-column legacy layout (entropy_bid/ask names).
    """
    from datetime import datetime, timezone
    values = []
    for i in range(n_minutes):
        ts = (t0 // 60 + i) * 60
        tu = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        if shape == "new":
            values.append((ts, tu, symbol, base, venue,
                           100.0, 100.02, 99.99, 100.01,
                           10.0, 12.0, 8.0, 10.0, 10.0, 1.5,
                           8.0, 14.0, 8.0, 14.0, 30))
        else:
            values.append((ts, tu, symbol, venue,
                           100.0, 100.02, 99.99, 100.01,
                           10.0, 12.0, 8.0, 10.0, 10.0, 1.5,
                           8.0, 14.0, 8.0, 14.0, 30))
    n = 20 if shape == "new" else 19
    con.executemany(
        f'INSERT OR REPLACE INTO "{table}" VALUES ('
        + ", ".join("?" * n) + ")", values)


def build_db(path, combos, n_minutes=40):
    """Per-combination tables: combos are (symbol, base, hedge) triples."""
    con = duckdb.connect(path)
    try:
        for symbol, base, hedge in combos:
            table = minute_table(symbol, base, hedge)
            con.execute(create_table_sql(table))
            _insert_rows(con, table, symbol, hedge, n_minutes, base=base)
    finally:
        con.close()
    return path


def build_legacy_db(path, symbols=("SNDK",), venues=("lighter-rh",),
                    n_minutes=40, per_symbol_tables=False):
    """Old-shaped data. per_symbol_tables=False -> one shared `minutes`
    table; True -> one old-shape `minutes_<symbol>` table per symbol."""
    con = duckdb.connect(path)
    try:
        if per_symbol_tables:
            for sym in symbols:
                t = "minutes_" + sym.lower()
                con.execute(f'CREATE TABLE "{t}" {LEGACY_DDL}')
                for v in venues:
                    _insert_rows(con, t, sym, v, n_minutes, shape="old")
        else:
            con.execute(f'CREATE TABLE minutes {LEGACY_DDL}')
            for sym in symbols:
                for v in venues:
                    _insert_rows(con, "minutes", sym, v, n_minutes,
                                 shape="old")
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


# ------------------------------------------------------------------ analyze

def test_discover_tables():
    p = build_db(tmp_db(), [("SNDK", "entropy", "lighter-rh"),
                            ("TSLA", "tradexyz", "lighter")])
    con = duckdb.connect(p)
    try:
        # legacy table with rows, a decoy, and an unrelated table
        con.execute(f"CREATE TABLE minutes {LEGACY_DDL}")
        _insert_rows(con, "minutes", "SNDK", "lighter-rh", 3, shape="old")
        con.execute("CREATE TABLE minutesfoo (x INT)")
        con.execute("CREATE TABLE other (x INT)")
    finally:
        con.close()
    assert analyze.discover_tables(p) == [
        "minutes_sndk__entropy__lighter_rh", "minutes_tsla__tradexyz__lighter"]


def test_split_tables_old_vs_new():
    p = tmp_db()
    con = duckdb.connect(p)
    try:
        con.execute(create_table_sql(minute_table("SNDK", "entropy", "lighter")))
        con.execute(f'CREATE TABLE minutes_tsla {LEGACY_DDL}')
    finally:
        con.close()
    current, old = analyze.split_tables(p, ["minutes_sndk__entropy__lighter",
                                            "minutes_tsla"])
    assert current == ["minutes_sndk__entropy__lighter"]
    assert old == ["minutes_tsla"]


def test_load_combos_data_driven():
    # one table, two hedge venues -> two independent combos, data-derived
    p = tmp_db()
    con = duckdb.connect(p)
    try:
        t = minute_table("SNDK", "entropy", "lighter")
        # the table name says lighter, but the DATA has two hedge venues —
        # combos must come from the rows, not the name
        con.execute(create_table_sql(t))
        _insert_rows(con, t, "SNDK", "lighter", 5, base="entropy")
        _insert_rows(con, t, "SNDK", "tradexyz", 5, base="entropy")
    finally:
        con.close()
    combos = analyze.load_combos(p, [t])
    assert combos == [(t, "SNDK", "entropy", "lighter"),
                      (t, "SNDK", "entropy", "tradexyz")]
    assert analyze.legacy_minutes_rows(p) == 0


def test_analyze_subprocess():
    p = build_db(tmp_db(), [("SNDK", "entropy", "lighter-rh")])
    r = subprocess.run(
        [sys.executable, os.path.join(_TOOLS, "analyze.py"), "--db", p],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "SNDK · entropy×lighter-rh" in r.stdout
    assert "SELL entropy" in r.stdout and "BUY entropy" in r.stdout
    assert "thresholds:" in r.stdout


def test_analyze_filters():
    p = build_db(tmp_db(), [("SNDK", "entropy", "lighter-rh"),
                            ("TSLA", "tradexyz", "lighter")])
    base = [sys.executable, os.path.join(_TOOLS, "analyze.py"), "--db", p]

    r = subprocess.run(base + ["--symbol", "TSLA"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "TSLA · tradexyz×lighter" in r.stdout
    assert "SNDK" not in r.stdout

    r = subprocess.run(base + ["--base-venue", "tradexyz"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "TSLA · tradexyz×lighter" in r.stdout
    assert "SNDK" not in r.stdout

    r = subprocess.run(base + ["--hedge-venue", "lighter-rh"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "SNDK · entropy×lighter-rh" in r.stdout
    assert "TSLA" not in r.stdout

    r = subprocess.run(base + ["--symbol", "NOPE"],
                       capture_output=True, text=True)
    assert r.returncode == 1
    assert "no data" in r.stderr


def test_analyze_old_shape_hint():
    # old per-symbol table only: hint, exit 1, no report
    p = build_legacy_db(tmp_db(), per_symbol_tables=True)
    r = subprocess.run(
        [sys.executable, os.path.join(_TOOLS, "analyze.py"), "--db", p],
        capture_output=True, text=True)
    assert r.returncode == 1
    assert "migrate_per_symbol" in r.stderr
    assert "thresholds:" not in r.stdout   # never reports from old tables

    # shared legacy `minutes` only: same behavior
    p2 = build_legacy_db(tmp_db(), per_symbol_tables=False)
    r = subprocess.run(
        [sys.executable, os.path.join(_TOOLS, "analyze.py"), "--db", p2],
        capture_output=True, text=True)
    assert r.returncode == 1
    assert "migrate_per_symbol" in r.stderr
    assert "thresholds:" not in r.stdout


# ---------------------------------------------------------------- migration

def test_migrate_shared_minutes_table():
    # shape (a): the pre-split shared `minutes` table, two (symbol, venue)
    p = build_legacy_db(tmp_db(), symbols=("SNDK", "TSLA"),
                        venues=("lighter-rh", "tradexyz"),
                        per_symbol_tables=False)
    assert migrate.main(["--db", p]) == 0
    expect = sorted(minute_table(s, "entropy", v)
                    for s in ("SNDK", "TSLA") for v in ("lighter-rh", "tradexyz"))
    assert tables_of(p) == sorted(["minutes"] + expect)
    for s in ("SNDK", "TSLA"):
        for v in ("lighter-rh", "tradexyz"):
            assert count_rows(p, minute_table(s, "entropy", v)) == 40


def test_migrate_per_symbol_tables():
    # shape (b): old per-symbol tables carrying two hedge venues each
    p = build_legacy_db(tmp_db(), symbols=("SNDK", "TSLA"),
                        venues=("lighter-rh", "tradexyz"),
                        per_symbol_tables=True)
    assert migrate.main(["--db", p]) == 0
    expect = sorted(minute_table(s, "entropy", v)
                    for s in ("SNDK", "TSLA") for v in ("lighter-rh", "tradexyz"))
    assert tables_of(p) == sorted(
        ["minutes_sndk", "minutes_tsla"] + expect)
    for s in ("SNDK", "TSLA"):
        for v in ("lighter-rh", "tradexyz"):
            assert count_rows(p, minute_table(s, "entropy", v)) == 40


def test_migrate_both_shapes_and_order():
    # both legacy shapes present: shared table first (older), per-symbol
    # second so its rows win on overlapping minutes
    p = tmp_db()
    con = duckdb.connect(p)
    try:
        con.execute(f"CREATE TABLE minutes {LEGACY_DDL}")
        _insert_rows(con, "minutes", "SNDK", "tradexyz", 40, shape="old")
        con.execute(f'CREATE TABLE minutes_sndk {LEGACY_DDL}')
        _insert_rows(con, "minutes_sndk", "SNDK", "tradexyz", 40,
                     t0=1_700_000_000 + 40 * 60, shape="old")
    finally:
        con.close()
    assert migrate.main(["--db", p]) == 0
    t = minute_table("SNDK", "entropy", "tradexyz")
    # 40 old + 40 newer minutes = 80 distinct minute_ts
    assert count_rows(p, t) == 80
    assert tables_of(p) == sorted(["minutes", "minutes_sndk", t])


def test_migrate_idempotent_and_drop_old():
    p = build_legacy_db(tmp_db(), per_symbol_tables=True)
    assert migrate.main(["--db", p]) == 0
    t = minute_table("SNDK", "entropy", "lighter-rh")
    assert count_rows(p, t) == 40
    # re-run: same counts, sources still there
    assert migrate.main(["--db", p]) == 0
    assert count_rows(p, t) == 40
    assert "minutes_sndk" in tables_of(p)
    # drop-old removes the sources
    assert migrate.main(["--db", p, "--drop-old"]) == 0
    assert tables_of(p) == [t]


def test_migrate_skips_null_hedge_rows():
    # a malformed legacy table (NULL symbol/hedge_venue — the PK on real
    # legacy tables prevented these, but a hand-built table may lack it):
    # the NULL filter keeps the migration from violating the NOT NULL PK
    p = tmp_db()
    con = duckdb.connect(p)
    try:
        cols = LEGACY_DDL[LEGACY_DDL.index("("):]
        # strip the PRIMARY KEY clause (with its trailing newline) so NULLs
        # are allowed in
        no_pk = cols[:cols.index("    PRIMARY KEY")].rstrip().rstrip(",")
        con.execute(f"CREATE TABLE minutes {no_pk})")
        con.execute(f"INSERT INTO minutes VALUES (1_700_000_060, "
                    f"timestamp '2023-11-14 22:14:00', 'SNDK', NULL, "
                    f"1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1)")
        con.execute(f"INSERT INTO minutes VALUES (1_700_000_120, "
                    f"timestamp '2023-11-14 22:15:00', NULL, 'tradexyz', "
                    f"1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1)")
    finally:
        con.close()
    assert migrate.main(["--db", p]) == 0
    assert tables_of(p) == ["minutes"]   # nothing migrated, no crash


def test_migrate_nothing_to_do():
    empty = tmp_db("empty.duckdb")
    duckdb.connect(empty).close()
    assert migrate.main(["--db", empty]) == 0
    # a db with only current-shape tables: also nothing to do
    p = build_db(tmp_db(), [("SNDK", "entropy", "lighter-rh")])
    assert migrate.main(["--db", p]) == 0
    assert tables_of(p) == ["minutes_sndk__entropy__lighter_rh"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
