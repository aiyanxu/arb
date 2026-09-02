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


def make_recorder(path, e_book, h_book, symbol="SNDK", venue="lighter-rh"):
    return MinuteRecorder(path, e_book, h_book, staleness_sec=1e9,
                          symbol=symbol, hedge_venue=venue)


def test_minute_aggregation_and_rollover():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.duckdb")
    rec = make_recorder(path, e_book, h_book)

    t0 = 1_700_000_000.0            # 20s into a minute (boundary at ...020)
    # minute 1: entropy 10 bps rich, then 20 bps rich
    set_book(e_book, 100.09, 100.11)   # mid 100.10
    set_book(h_book, 99.99, 100.01)    # mid 100.00
    rec.sample(t0)
    set_book(e_book, 100.19, 100.21)   # mid 100.20
    rec.sample(t0 + 10)
    # next minute: back to 10 bps rich -> flushes minute 1
    set_book(e_book, 100.09, 100.11)
    rec.sample(t0 + 45)
    rec.close()                        # flushes the partial minute 2

    rows = fetch_all(path, minute_table("SNDK"))
    assert len(rows) == 2
    # (minute_ts, time_utc, symbol, hedge_venue, e_bid, e_ask, h_bid, h_ask,
    #  p_open, p_high, p_low, p_close, p_mean, p_std, s_mean, s_max,
    #  b_mean, b_max, samples)
    m1, m2 = rows
    assert m1[2] == "SNDK" and m1[3] == "lighter-rh"
    assert m1[0] == int(t0 // 60) * 60 and m1[0] < m2[0]
    assert m1[18] == 2 and m2[18] == 1
    assert abs(m1[8] - 10.0) < 0.2                       # premium open
    assert abs(m1[9] - 20.0) < 0.2                       # premium high
    assert abs(m1[11] - 20.0) < 0.2                      # premium close
    assert abs(m1[12] - 15.0) < 0.2                      # premium mean
    # executable edges: sell = bid_e/ask_h - 1, buy = bid_h/ask_e - 1
    assert abs(m2[15] - ((100.09 / 100.01 - 1) * 1e4)) < 0.05   # sell max
    assert abs(m2[17] - ((99.99 / 100.11 - 1) * 1e4)) < 0.05    # buy max
    # closes carry the last books
    assert m2[4] == 100.09 and m2[7] == 100.01

    # one table per symbol — exactly this symbol's table, no legacy one
    con = duckdb.connect(path, read_only=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() ORDER BY 1").fetchall()]
    finally:
        con.close()
    assert tables == [minute_table("SNDK")]


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
    assert len(fetch_all(path, minute_table("SNDK"))) == 1

    # next minute: a second row appears
    rec = make_recorder(path, e_book, h_book)
    rec.sample(t0 + 60)
    rec.close()
    rows = fetch_all(path, minute_table("SNDK"))
    assert len(rows) == 2
    assert rows[0][0] != rows[1][0]    # distinct minutes, no dup PK


def test_per_symbol_tables_separated():
    e_book, h_book = OrderBook(), OrderBook()
    set_book(e_book, 100.0, 100.02)
    set_book(h_book, 100.0, 100.02)
    path = os.path.join(tempfile.mkdtemp(), "minutes.duckdb")

    # two recorders, two symbols, one db file, same minute
    for sym in ("SNDK", "TSLA"):
        rec = make_recorder(path, e_book, h_book, symbol=sym)
        rec.sample(1_700_000_000.0)
        rec.close()

    con = duckdb.connect(path, read_only=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() ORDER BY 1").fetchall()]
    finally:
        con.close()
    assert tables == [minute_table("SNDK"), minute_table("TSLA")]

    sndk = fetch_all(path, minute_table("SNDK"))
    tsla = fetch_all(path, minute_table("TSLA"))
    assert len(sndk) == 1 and sndk[0][2] == "SNDK"
    assert len(tsla) == 1 and tsla[0][2] == "TSLA"


def test_symbol_sanitization():
    # name rule: lowercase, anything outside [a-z0-9_] becomes '_'
    assert minute_table("SNDK") == "minutes_sndk"
    assert minute_table("BRK.B") == "minutes_brk_b"
    # documented collision — safe: rows carry the original symbol and the
    # PK keeps them distinct
    assert minute_table("BTC-1") == minute_table("BTC_1")
    try:
        minute_table("///")
    except ValueError:
        pass
    else:
        raise AssertionError("empty sanitization must raise")

    # the recorded row keeps the ORIGINAL symbol even when the table name
    # is sanitized
    e_book, h_book = OrderBook(), OrderBook()
    set_book(e_book, 100.0, 100.02)
    set_book(h_book, 100.0, 100.02)
    path = os.path.join(tempfile.mkdtemp(), "minutes.duckdb")
    rec = make_recorder(path, e_book, h_book, symbol="BRK.B")
    rec.sample(1_700_000_000.0)
    rec.close()
    rows = fetch_all(path, "minutes_brk_b")
    assert len(rows) == 1 and rows[0][2] == "BRK.B"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
