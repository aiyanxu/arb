"""Automatic 1-minute orderbook data recorder.

While the bot runs (live or --record-only), both venues' actual order books
are sampled once per second and aggregated into one row per minute in a
DuckDB database (logs/minutes.duckdb by default). Each (symbol, base_venue,
hedge_venue) combination gets its own table, named
minutes_<symbol>__<base_venue>__<hedge_venue> (see minute_table()); this is
the dataset users analyze (tools/analyze.py) to choose
thresholds.midline_bps / upper_bps / lower_bps for config.yaml.

Definitions (all in bps, fees NOT included — the engine adds fees on top):

    premium    = (base_mid / hedge_mid - 1) * 1e4
                 the mid-to-mid premium of the base leg over the hedge leg;
                 its long-run center is what midline_bps hardcodes.
    sell_edge  = (base_bid / hedge_ask - 1) * 1e4
                 the EXECUTABLE premium for SELL-base/BUY-hedge; the
                 engine fires this direction when sell_edge clears
                 midline_bps + upper_bps (plus fees).
    buy_edge   = (hedge_bid / base_ask - 1) * 1e4
                 the executable premium for BUY-base/SELL-hedge; fires
                 when buy_edge clears lower_bps - midline_bps (plus fees).

Bid/ask columns are the minute's last fresh sample (close). A row is only
written for minutes with at least one sample where both books were fresh;
`samples` says how many of the ~60 seconds qualified.

The `symbol` / `base_venue` / `hedge_venue` columns identify the pair a row
belongs to; (symbol, base_venue, hedge_venue, minute_ts) is the primary key,
and rows are written with INSERT OR REPLACE — restarting within the same
minute overwrites the partial row instead of duplicating it. The connection
is opened per write and closed right after (DuckDB files are single-writer),
so tools/analyze.py can query the database while the bot keeps recording.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import duckdb

from .book import OrderBook

log = logging.getLogger("recorder")

TABLE_PREFIX = "minutes_"

# Column list + PK for per-(symbol, base_venue, hedge_venue) tables.
# Do not reorder: the INSERTs and the migration's positional SELECT rely on
# this order.
_TABLE_DDL = """(
    minute_ts BIGINT, time_utc TIMESTAMP,
    symbol VARCHAR, base_venue VARCHAR, hedge_venue VARCHAR,
    base_bid DOUBLE, base_ask DOUBLE,
    hedge_bid DOUBLE, hedge_ask DOUBLE,
    premium_open_bps DOUBLE, premium_high_bps DOUBLE,
    premium_low_bps DOUBLE, premium_close_bps DOUBLE,
    premium_mean_bps DOUBLE, premium_std_bps DOUBLE,
    sell_edge_mean_bps DOUBLE, sell_edge_max_bps DOUBLE,
    buy_edge_mean_bps DOUBLE, buy_edge_max_bps DOUBLE,
    samples INTEGER,
    PRIMARY KEY (symbol, base_venue, hedge_venue, minute_ts)
)"""


def minute_table(symbol: str, base_venue: str, hedge_venue: str) -> str:
    """Table name for one (symbol, base_venue, hedge_venue) combination:
    TABLE_PREFIX + the three parts joined by '__' and sanitized.

    Lowercase (DuckDB identifiers are case-insensitive, so case must not
    reach the name); anything outside [a-z0-9_] becomes '_'. Distinct
    combinations can collide on one name — that is safe: rows still carry
    the original columns, the PK keeps them distinct, and tools/analyze.py
    derives combos from the data, never the table name.
    """
    cleaned_parts = []
    for label, part in (("symbol", symbol), ("base_venue", base_venue),
                        ("hedge_venue", hedge_venue)):
        cleaned = re.sub(r"[^a-z0-9_]", "_", part.strip().lower()).strip("_")
        if not cleaned:
            raise ValueError(f"{label} {part!r} sanitizes to an empty table "
                             f"name component")
        cleaned_parts.append(cleaned)
    return TABLE_PREFIX + "__".join(cleaned_parts)


def create_table_sql(table: str) -> str:
    return f'CREATE TABLE IF NOT EXISTS "{table}" {_TABLE_DDL}'


class _MinuteAgg:
    __slots__ = ("minute", "n", "p_open", "p_high", "p_low", "p_close",
                 "p_sum", "p_sumsq", "s_sum", "s_max", "b_sum", "b_max",
                 "e_bid", "e_ask", "h_bid", "h_ask")

    def __init__(self, minute: int) -> None:
        self.minute = minute
        self.n = 0
        self.p_open = self.p_high = self.p_low = self.p_close = 0.0
        self.p_sum = self.p_sumsq = 0.0
        self.s_sum = 0.0
        self.s_max = -math.inf
        self.b_sum = 0.0
        self.b_max = -math.inf
        self.e_bid = self.e_ask = self.h_bid = self.h_ask = 0.0

    def add(self, e_bid: float, e_ask: float, h_bid: float, h_ask: float) -> None:
        e_mid = (e_bid + e_ask) / 2.0
        h_mid = (h_bid + h_ask) / 2.0
        prem = (e_mid / h_mid - 1.0) * 1e4
        sell_edge = (e_bid / h_ask - 1.0) * 1e4
        buy_edge = (h_bid / e_ask - 1.0) * 1e4
        if self.n == 0:
            self.p_open = self.p_high = self.p_low = prem
        self.n += 1
        self.p_high = max(self.p_high, prem)
        self.p_low = min(self.p_low, prem)
        self.p_close = prem
        self.p_sum += prem
        self.p_sumsq += prem * prem
        self.s_sum += sell_edge
        self.s_max = max(self.s_max, sell_edge)
        self.b_sum += buy_edge
        self.b_max = max(self.b_max, buy_edge)
        self.e_bid, self.e_ask, self.h_bid, self.h_ask = e_bid, e_ask, h_bid, h_ask

    def row(self) -> tuple:
        mean = self.p_sum / self.n
        var = max(self.p_sumsq / self.n - mean * mean, 0.0)
        ts = self.minute * 60
        return (ts,
                datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None),
                f"{self.e_bid:.10g}", f"{self.e_ask:.10g}",
                f"{self.h_bid:.10g}", f"{self.h_ask:.10g}",
                self.p_open, self.p_high, self.p_low, self.p_close,
                mean, math.sqrt(var),
                self.s_sum / self.n, self.s_max,
                self.b_sum / self.n, self.b_max,
                self.n)


class MinuteRecorder:
    def __init__(self, path: str, base_book: OrderBook, hedge_book: OrderBook,
                 staleness_sec: float, *, symbol: str, base_venue: str,
                 hedge_venue: str, interval_sec: float = 1.0) -> None:
        self.path = path
        self.base_book = base_book
        self.hedge_book = hedge_book
        self.staleness_sec = staleness_sec
        self.symbol = symbol
        self.base_venue = base_venue
        self.hedge_venue = hedge_venue
        self.table = minute_table(symbol, base_venue, hedge_venue)
        self.interval_sec = interval_sec
        self.rows_written = 0
        self._agg: Optional[_MinuteAgg] = None
        self._con = None

    def _open(self):
        """Connect and ensure the per-symbol table exists (lazily, first row)."""
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        last: Optional[duckdb.IOException] = None
        con = None
        for attempt in range(5):        # several recorders may share one file
            try:
                con = duckdb.connect(self.path)
                con.execute(create_table_sql(self.table))
            except duckdb.IOException as e:
                last = e
                con = None
                time.sleep(0.2 * (attempt + 1))
            else:
                break
        if con is None:                # every attempt failed on the file lock
            assert last is not None
            raise last
        if not getattr(self, "_announced", False):
            self._announced = True
            log.info("recording 1-minute orderbook data -> %s (table %s)",
                     self.path, self.table)
        return con

    def _flush_agg(self) -> None:
        if self._agg is None or self._agg.n == 0:
            self._agg = None
            return
        con = self._open()
        try:
            # INSERT OR REPLACE: restarting inside the same minute overwrites
            # the partial row instead of duplicating it
            row = list(self._agg.row())
            # column order: minute_ts, time_utc, symbol, base_venue,
            #               hedge_venue, base_bid, ...
            values = (row[:2] + [self.symbol, self.base_venue,
                                 self.hedge_venue] + row[2:])
            con.execute(
                f'INSERT OR REPLACE INTO "{self.table}" VALUES ('
                + ", ".join("?" * len(values)) + ")", values)
        finally:
            # single-writer files: release the lock so analyze.py can read
            con.close()
        self.rows_written += 1
        self._agg = None

    def sample(self, now: Optional[float] = None) -> None:
        """Take one sample; call ~1/sec. Rolls the minute over as needed."""
        now = time.time() if now is None else now
        minute = int(now // 60)
        if self._agg is not None and self._agg.minute != minute:
            self._flush_agg()
        if not (self.base_book.is_fresh(self.staleness_sec)
                and self.hedge_book.is_fresh(self.staleness_sec)):
            return
        e_bid, e_ask = self.base_book.best_bid(), self.base_book.best_ask()
        h_bid, h_ask = self.hedge_book.best_bid(), self.hedge_book.best_ask()
        if None in (e_bid, e_ask, h_bid, h_ask):
            return
        if self._agg is None:
            self._agg = _MinuteAgg(minute)
        self._agg.add(e_bid, e_ask, h_bid, h_ask)

    def close(self) -> None:
        """Flush the partial minute and release the database (on shutdown)."""
        self._flush_agg()

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    self.sample()
                except Exception:
                    log.exception("recorder sample failed")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.interval_sec)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.close()
            log.info("recorder stopped — %d minute row(s) written to %s",
                     self.rows_written, self.path)
