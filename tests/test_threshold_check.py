"""threshold_check: scheduled re-derivation of thresholds vs config.yaml.

Run:  python3 -m pytest tests/  (or  python3 tests/test_threshold_check.py)
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import duckdb  # noqa: E402

from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.recorder import create_table_sql, minute_table  # noqa: E402
from entropy_arb.threshold_check import (  # noqa: E402
    check_once, drift_report, suggested_thresholds, ThresholdDriftChecker)

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")

TABLE = minute_table("SNDK", "entropy", "lighter-rh")


def make_cfg(db, enabled=True, tolerance=0.10, midline=0.0, upper=4.0,
             lower=4.0, min_minutes=30):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(f"""
thresholds:
  midline_bps: {midline}
  upper_bps: {upper}
  lower_bps: {lower}
recorder:
  db: {db}
threshold_check:
  enabled: {'true' if enabled else 'false'}
  tolerance: {tolerance}
  min_minutes: {min_minutes}
  interval_sec: 0.05
""")
    f.close()
    return load_config(f.name, NO_ENV, symbol="SNDK",
                       hedge_venue="lighter-rh")


def build_db(path, prem=10.0, sell_max=14.0, buy_max=14.0, n_minutes=60):
    """Minutes with a constant premium and fixed executable edges.

    With prem=10: suggested midline=10, upper=sell_max-10, lower=buy_max+10
    — the same math tools/analyze.py applies (sell room = sell_max - mid,
    buy room = buy_max + midline)."""
    con = duckdb.connect(path)
    try:
        con.execute(create_table_sql(TABLE))
        vals = []
        from datetime import datetime, timezone
        for i in range(n_minutes):
            ts = (1_700_000_000 // 60 + i) * 60
            tu = datetime.fromtimestamp(ts, tz=timezone.utc).replace(
                tzinfo=None)
            vals.append((ts, tu, "SNDK", "entropy", "lighter-rh",
                         100.0, 100.02, 99.99, 100.01,
                         prem, prem, prem, prem, prem, 0.0,
                         sell_max - 1.0, sell_max,
                         buy_max - 1.0, buy_max, 30))
        con.executemany(f'INSERT INTO "{TABLE}" VALUES ('
                        + ", ".join("?" * 20) + ")", vals)
    finally:
        con.close()
    return path


# ------------------------------------------------------------ derivation

def test_suggested_matches_analyze_math():
    # constant prem=10 -> midline 10; sell room = 14-10 = 4 every minute ->
    # p90 = 4; buy room = 24+10 = 34 -> lower 34 (analyze's buy_room formula)
    rows = [{"ts": i, "prem": 10.0, "sell_max": 14.0, "buy_max": 24.0}
            for i in range(60)]
    sug = suggested_thresholds(rows, fees=0.0)
    assert sug == {"midline_bps": 10.0, "upper_bps": 4.0, "lower_bps": 34.0}


def test_drift_report_tolerance_boundary():
    cur = {"midline_bps": 10.0, "upper_bps": 4.0, "lower_bps": 4.0}
    # exactly 10%: not drifted (the requirement says "超过" / beyond)
    assert drift_report(cur, {**cur, "lower_bps": 4.4}, 0.10) == []
    # 11.25%: flagged
    got = drift_report(cur, {**cur, "lower_bps": 4.45}, 0.10)
    assert [k for k, *_ in got] == ["lower_bps"]


def test_drift_report_zero_config_value():
    # config 0 -> no relative denominator; an absolute move > 1.5 bps counts
    cur = {"midline_bps": 0.0, "upper_bps": 4.0, "lower_bps": 4.0}
    sug = {"midline_bps": 3.0, "upper_bps": 4.0, "lower_bps": 4.0}
    assert [k for k, *_ in drift_report(cur, sug, 0.10)] == ["midline_bps"]
    # suggested stays ~0: quiet
    sug0 = {"midline_bps": 0.5, "upper_bps": 4.0, "lower_bps": 4.0}
    assert drift_report(cur, sug0, 0.10) == []


def test_drift_report_abs_floor_flags_small_relative_moves():
    # a large config value can hide a real loss behind the relative test:
    # 20 -> 21.8 is 9% (inside 10% tolerance) but 1.8 bps of edge, above the
    # 1.5 bps floor -> flagged
    cur = {"midline_bps": 20.0, "upper_bps": 4.0, "lower_bps": 4.0}
    sug = {"midline_bps": 21.8, "upper_bps": 4.0, "lower_bps": 4.0}
    assert [k for k, *_ in drift_report(cur, sug, 0.10)] == ["midline_bps"]
    # small move inside both tests stays quiet
    quiet = {"midline_bps": 21.0, "upper_bps": 4.0, "lower_bps": 4.0}
    assert drift_report(cur, quiet, 0.10) == []


# ------------------------------------------------------------ end-to-end

def test_check_once_skips_without_db():
    cfg = make_cfg(os.path.join(tempfile.mkdtemp(), "no-such.duckdb"))
    assert asyncio.run(check_once(cfg)) is False


def test_check_once_quiet_when_config_matches():
    db = build_db(os.path.join(tempfile.mkdtemp(), "m.duckdb"), prem=10.0,
                  sell_max=14.0, buy_max=14.0)
    # suggested: midline 10, upper 4 (14-10), lower 24 (14+10)
    cfg = make_cfg(db, midline=10.0, upper=4.0, lower=24.0)
    assert asyncio.run(check_once(cfg)) is False


def test_check_once_fires_on_drift(monkeypatch):
    sent = []
    import entropy_arb.notify as n
    monkeypatch.setattr(n, "send", lambda text: sent.append(text))
    db = build_db(tempfile.mkdtemp() + "/minutes.duckdb", prem=10.0,
                  sell_max=14.0, buy_max=24.0)
    cfg = make_cfg(db, midline=3.0, upper=4.0, lower=4.0)
    assert asyncio.run(check_once(cfg)) is True
    assert len(sent) == 1
    body = sent[0]
    assert "SNDK" in body and "midline_bps" in body
    assert "entropy-arb analyze" in body


def test_check_once_skips_small_data(monkeypatch):
    sent = []
    import entropy_arb.notify as n
    monkeypatch.setattr(n, "send", lambda t: sent.append(t))
    db = build_db(os.path.join(tempfile.mkdtemp(), "d.duckdb"), n_minutes=5)
    cfg = make_cfg(db)
    assert asyncio.run(check_once(cfg)) is False   # below min_minutes
    assert sent == []


def test_checker_loop_fires_and_stops():
    # the periodic task exits promptly on a pre-set stop event
    cfg = make_cfg(os.path.join(tempfile.mkdtemp(), "no-such.duckdb"))
    checker = ThresholdDriftChecker(cfg)
    stop = asyncio.Event()

    async def go():
        stop.set()
        await asyncio.wait_for(checker.run(stop), timeout=5)
    asyncio.run(go())


def run(coro):
    return asyncio.run(coro)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
