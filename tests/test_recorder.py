"""Minute recorder: aggregation, rollover, DuckDB output.

Run:  python3 -m pytest tests/  (or  python3 tests/test_recorder.py)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import duckdb  # noqa: E402

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.recorder import MinuteRecorder, minute_table  # noqa: E402


def set_book(book, bid, ask):
    book.apply_hl([[{"px": str(bid), "sz": "10"}],
                   [{"px": str(ask), "sz": "10"}]])


def fetch_all(path, table, where="1=1", params=None):
    con = duckdb.connect(path, read_only=True)
    try:
        return con.execute(
            f'SELECT * FROM "{table}" WHERE {where} ORDER BY minute_ts',
            params or []).fetchall()
    finally:
        con.close()


def tables_of(path):
    con = duckdb.connect(path, read_only=True)
    try:
        return [r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() ORDER BY 1").fetchall()]
    finally:
        con.close()


def make_recorder(path, b_book, h_book, symbol="SNDK",
                  base_venue="entropy", venue="lighter-rh"):
    return MinuteRecorder(path, b_book, h_book, staleness_sec=1e9,
                          symbol=symbol, base_venue=base_venue,
                          hedge_venue=venue)


# column indices in the 20-column layout
# (minute_ts, time_utc, symbol, base_venue, hedge_venue, base_bid, base_ask,
#  hedge_bid, hedge_ask, p_open, p_high, p_low, p_close, p_mean, p_std,
#  s_mean, s_max, b_mean, b_max, samples)
I_SYM, I_BASE, I_HEDGE = 2, 3, 4
I_BBASE_BID, I_HEDGE_ASK = 5, 8
I_P_OPEN, I_P_HIGH, I_P_CLOSE, I_P_MEAN = 9, 10, 12, 13
I_S_MAX, I_B_MAX, I_SAMPLES = 16, 18, 19


def test_minute_aggregation_and_rollover():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.duckdb")
    rec = make_recorder(path, e_book, h_book)

    t0 = 1_700_000_000.0            # 20s into a minute (boundary at ...020)
    # minute 1: base 10 bps rich, then 20 bps rich
    set_book(e_book, 100.09, 100.11)   # mid 100.10
    set_book(h_book, 99.99, 100.01)    # mid 100.00
    rec.sample(t0)
    set_book(e_book, 100.19, 100.21)   # mid 100.20
    rec.sample(t0 + 10)
    # next minute: back to 10 bps rich -> flushes minute 1
    set_book(e_book, 100.09, 100.11)
    rec.sample(t0 + 45)
    rec.close()                        # flushes the partial minute 2

    rows = fetch_all(path, minute_table("SNDK", "entropy", "lighter-rh"))
    assert len(rows) == 2
    m1, m2 = rows
    assert m1[I_SYM] == "SNDK" and m1[I_BASE] == "entropy"
    assert m1[I_HEDGE] == "lighter-rh"
    assert m1[0] == int(t0 // 60) * 60 and m1[0] < m2[0]
    assert m1[I_SAMPLES] == 2 and m2[I_SAMPLES] == 1
    assert abs(m1[I_P_OPEN] - 10.0) < 0.2                       # premium open
    assert abs(m1[I_P_HIGH] - 20.0) < 0.2                       # premium high
    assert abs(m1[I_P_CLOSE] - 20.0) < 0.2                      # premium close
    assert abs(m1[I_P_MEAN] - 15.0) < 0.2                       # premium mean
    # executable edges: sell = base_bid/hedge_ask - 1, buy = hedge_bid/base_ask - 1
    assert abs(m2[I_S_MAX] - ((100.09 / 100.01 - 1) * 1e4)) < 0.05   # sell max
    assert abs(m2[I_B_MAX] - ((99.99 / 100.11 - 1) * 1e4)) < 0.05    # buy max
    # closes carry the last books
    assert m2[I_BBASE_BID] == 100.09 and m2[I_HEDGE_ASK] == 100.01

    # one table per (symbol, base, hedge) — exactly this combination's
    assert tables_of(path) == [minute_table("SNDK", "entropy", "lighter-rh")]


def test_stale_books_are_skipped():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.duckdb")
    rec = make_recorder(path, e_book, h_book)
    rec.sample(1_700_000_000.0)        # both books empty -> nothing recorded
    set_book(e_book, 100.0, 100.02)    # only one side fresh
    rec.sample(1_700_000_001.0)        # only one side fresh
    rec.close()
    assert rec.rows_written == 0
    assert not os.path.exists(path)    # lazy _open: no row, no db file


def test_same_minute_restart_overwrites():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.duckdb")
    set_book(e_book, 100.0, 100.02)
    set_book(h_book, 100.0, 100.02)

    t0 = 1_700_000_000.0
    rec = make_recorder(path, e_book, h_book)
    rec.sample(t0)
    rec.close()                        # partial minute written

    # restart inside the same minute: INSERT OR REPLACE overwrites
    rec = make_recorder(path, e_book, h_book)
    rec.sample(t0 + 5)
    rec.close()
    assert len(fetch_all(path, minute_table("SNDK", "entropy", "lighter-rh"))) == 1

    # next minute: a second row appears
    rec = make_recorder(path, e_book, h_book)
    rec.sample(t0 + 60)
    rec.close()
    rows = fetch_all(path, minute_table("SNDK", "entropy", "lighter-rh"))
    assert len(rows) == 2
    assert rows[0][0] != rows[1][0]    # distinct minutes, no dup PK


def test_per_combination_tables_separated():
    e_book, h_book = OrderBook(), OrderBook()
    set_book(e_book, 100.0, 100.02)
    set_book(h_book, 100.0, 100.02)
    path = os.path.join(tempfile.mkdtemp(), "minutes.duckdb")

    # same symbol, three different venue combinations -> three tables
    combos = [("entropy", "lighter-rh"), ("entropy", "tradexyz"),
              ("tradexyz", "lighter-rh")]
    for base_v, hedge_v in combos:
        rec = make_recorder(path, e_book, h_book,
                            base_venue=base_v, venue=hedge_v)
        rec.sample(1_700_000_000.0)
        rec.close()

    assert tables_of(path) == sorted(
        minute_table("SNDK", b, h) for b, h in combos)
    for base_v, hedge_v in combos:
        rows = fetch_all(path, minute_table("SNDK", base_v, hedge_v))
        assert len(rows) == 1
        assert rows[0][I_BASE] == base_v and rows[0][I_HEDGE] == hedge_v

    # same venues, different symbol -> its own table
    rec = make_recorder(path, e_book, h_book, symbol="TSLA")
    rec.sample(1_700_000_000.0)
    rec.close()
    tsla = fetch_all(path, minute_table("TSLA", "entropy", "lighter-rh"))
    assert len(tsla) == 1 and tsla[0][I_SYM] == "TSLA"


def test_table_name_sanitization():
    # name rule: lowercase, anything outside [a-z0-9_] becomes '_',
    # the three parts joined by '__'
    assert minute_table("SNDK", "entropy", "lighter-rh") == \
        "minutes_sndk__entropy__lighter_rh"
    assert minute_table("BRK.B", "entropy", "tradexyz") == \
        "minutes_brk_b__entropy__tradexyz"
    # documented collision — safe: rows carry the original columns and the
    # PK keeps them distinct
    assert minute_table("BTC-1", "entropy", "lighter") == \
        minute_table("BTC_1", "entropy", "lighter")
    for bad in (("///", "entropy", "lighter"), ("SNDK", "", "lighter"),
                ("SNDK", "entropy", "///")):
        try:
            minute_table(*bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad} must raise")

    # the recorded row keeps the ORIGINAL symbol even when the table name
    # is sanitized
    e_book, h_book = OrderBook(), OrderBook()
    set_book(e_book, 100.0, 100.02)
    set_book(h_book, 100.0, 100.02)
    path = os.path.join(tempfile.mkdtemp(), "minutes.duckdb")
    rec = make_recorder(path, e_book, h_book, symbol="BRK.B")
    rec.sample(1_700_000_000.0)
    rec.close()
    rows = fetch_all(path, "minutes_brk_b__entropy__lighter_rh")
    assert len(rows) == 1 and rows[0][I_SYM] == "BRK.B"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
