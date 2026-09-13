"""`entropy-arb analyze --config <file>` — config.yaml as the source of
analyze defaults (recorder db, symbol/venues, the pair's taker fees), with
every explicit flag (--db / --symbol / --base-venue / --hedge-venue /
--fees-bps) still winning.

Run:  python3 -m pytest tests/  (or  python3 tests/test_analyze_config.py)
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import ConfigError, analyze_defaults_from_config  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from test_tools import build_db, tmp_db  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
NO_SUCH = os.path.join(tempfile.gettempdir(), "no-such-entropy-arb.yaml")

CFG = """\
symbol: {symbol}
base_venue: {base}
hedge_venue: {hedge}
thresholds:
  midline_bps: 5.0
  upper_bps: 4.0
  lower_bps: 3.0
{extra}"""


def write_cfg(symbol="SNDK", base="entropy", hedge="lighter-rh",
              extra="") -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(CFG.format(symbol=symbol, base=base, hedge=hedge, extra=extra))
    f.close()
    return f.name


def run_analyze(args):
    return subprocess.run(
        [sys.executable, "-m", "entropy_arb", "analyze"] + args,
        cwd=ROOT, capture_output=True, text=True)


# ------------------------------------------------------------ helper layer

def test_analyze_defaults_reads_markets_and_fees():
    # recorder.db / markets / fee sum all come from the file; no .env, no
    # credentials involved
    p = write_cfg()
    try:
        d = analyze_defaults_from_config(p)
        assert d["db"] == "logs/minutes.duckdb"
        assert d["symbol"] == "SNDK"
        assert d["base_venue"] == "entropy"
        assert d["hedge_venue"] == "lighter-rh"
        assert d["base_symbol"] == "SNDK" and d["hedge_symbol"] == "SNDK"
        assert d["fees_bps"] == 0.0 + 0.0
    finally:
        os.unlink(p)


def test_analyze_defaults_recorder_db_and_fee_overrides():
    p = write_cfg(extra="""
recorder:
  db: data/other.duckdb
base:
  taker_fee_bps: 1.5
""")
    try:
        d = analyze_defaults_from_config(p, base_venue="tradexyz")
        assert d["db"] == "data/other.duckdb"
        assert d["fees_bps"] == 1.5 + 0.0     # base override wins (>= default)
    finally:
        os.unlink(p)


def test_analyze_defaults_cli_args_win():
    p = write_cfg(symbol="TSLA", base="lighter", hedge="aster")
    try:
        d = analyze_defaults_from_config(p, symbol="BTC", base_venue="entropy",
                             hedge_venue="lighter-rh")
        assert d["symbol"] == "BTC"
        assert d["base_venue"] == "entropy" and d["hedge_venue"] == "lighter-rh"
        assert d["fees_bps"] == 0.0
    finally:
        os.unlink(p)


def test_analyze_defaults_fees_sum_both_legs():
    # both legs' fees are summed: a base override plus a venue default
    p = write_cfg(base="tradexyz", hedge="polymarket",
                  extra="base:\n  taker_fee_bps: 2.0\n")
    try:
        d = analyze_defaults_from_config(p)
        assert d["fees_bps"] == 2.0 + 4.0   # tradexyz override + POLY default
    finally:
        os.unlink(p)


def test_analyze_defaults_no_thresholds_needed():
    # analyze DERIVES thresholds — a config without the thresholds section
    # must load fine here (load_config would reject it)
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write("symbol: SNDK\nbase_venue: entropy\nhedge_venue: lighter\n")
    f.close()
    try:
        d = analyze_defaults_from_config(f.name)
        assert d["fees_bps"] == 0.0 and d["symbol"] == "SNDK"
    finally:
        os.unlink(f.name)


def test_analyze_defaults_symbol_required():
    # no symbol from CLI or yaml -> loud error, never a silent empty filter
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write("base_venue: entropy\nhedge_venue: lighter\n")
    f.close()
    try:
        try:
            analyze_defaults_from_config(f.name)
            raise AssertionError("expected ConfigError")
        except ConfigError as e:
            assert "symbol is required" in str(e)
    finally:
        os.unlink(f.name)


def test_analyze_defaults_missing_file():
    try:
        analyze_defaults_from_config(NO_SUCH)
        raise AssertionError("expected ConfigError")
    except ConfigError as e:
        assert "not found" in str(e)


def test_analyze_defaults_bad_venue():
    try:
        analyze_defaults_from_config(write_cfg(base="binance"))
        raise AssertionError("expected ConfigError")
    except ConfigError as e:
        assert "base_venue must be one of" in str(e)


def test_analyze_defaults_same_venue_rejected():
    try:
        analyze_defaults_from_config(write_cfg(base="entropy", hedge="entropy"))
        raise AssertionError("expected ConfigError")
    except ConfigError as e:
        assert "must differ" in str(e)


def test_analyze_defaults_fee_below_default_rejected():
    # same fee rule as load_config: under-quoting a fee is a startup error
    try:
        analyze_defaults_from_config(write_cfg(extra="hedge:\n  taker_fee_bps: 0.0\n",
                                   hedge="aster"))
        raise AssertionError("expected ConfigError")
    except ConfigError as e:
        assert "below the venue default" in str(e)


# ------------------------------------------------------------ end-to-end

def test_analyze_config_subprocess():
    # --config alone: db + combo + fees all come from the file
    db = build_db(tmp_db(), [("SNDK", "entropy", "lighter-rh"),
                             ("TSLA", "tradexyz", "lighter")])
    p = write_cfg(extra=f"recorder:\n  db: {db}\n")
    try:
        r = run_analyze(["--config", p])
        assert r.returncode == 0, r.stderr
        assert "SNDK · entropy×lighter-rh" in r.stdout
        assert "TSLA" not in r.stdout                 # other combo filtered out
        assert f"(from {p})" in r.stdout              # fee source shown
        assert "thresholds:" in r.stdout
    finally:
        os.unlink(p)


def test_analyze_config_missing_file_clean_error():
    # a config error is exit 2, same convention as the trading path
    r = run_analyze(["--config", NO_SUCH])
    assert r.returncode == 2
    assert "config error" in r.stderr
    assert f"config file '{NO_SUCH}' not found" in r.stderr


def test_analyze_config_no_data_clean_error():
    # the config's recorder.db does not exist -> the analyze not-found path
    p = write_cfg(extra="recorder:\n  db: /nonexistent/x.duckdb\n")
    try:
        r = run_analyze(["--config", p])
        assert r.returncode == 1
        assert "not found" in r.stderr
        assert "/nonexistent/x.duckdb" in r.stderr
    finally:
        os.unlink(p)


def test_analyze_config_db_flag_wins():
    # explicit --db beats the config's recorder.db
    p = write_cfg(extra="recorder:\n  db: /nonexistent/x.duckdb\n")
    db = build_db(tmp_db(), [("SNDK", "entropy", "lighter-rh")])
    try:
        r = run_analyze(["--config", p, "--db", db])
        assert r.returncode == 0, r.stderr
        assert "SNDK · entropy×lighter-rh" in r.stdout
    finally:
        os.unlink(p)


def test_analyze_config_symbol_flag_wins():
    # explicit --symbol / --base-venue / --hedge-venue beat the config's
    # markets (the config still supplies whatever flags are NOT passed)
    db = build_db(tmp_db(), [("SNDK", "entropy", "lighter-rh"),
                             ("TSLA", "tradexyz", "lighter")])
    p = write_cfg(symbol="SNDK", base="entropy", hedge="lighter-rh",
                  extra=f"recorder:\n  db: {db}\n")
    try:
        r = run_analyze(["--config", p, "--symbol", "TSLA",
                         "--base-venue", "tradexyz", "--hedge-venue", "lighter"])
        assert r.returncode == 0, r.stderr
        assert "TSLA · tradexyz×lighter" in r.stdout
        assert "SNDK" not in r.stdout
    finally:
        os.unlink(p)


def test_analyze_config_fees_from_config():
    # the config's fee override flows into the firing table: 10 bps of fees
    # (base override 10 + hedge 0) kills the 8-bps band that fires 100% with
    # the default fee of 0 — and the report says where the fee came from
    p = write_cfg(extra="base:\n  taker_fee_bps: 10.0\n")
    db = build_db(tmp_db(), [("SNDK", "entropy", "lighter-rh")])
    try:
        r = run_analyze(["--config", p, "--db", db])
        assert r.returncode == 0, r.stderr
        assert "10.0 bps" in r.stdout
        assert f"(from {p})" in r.stdout
        for line in r.stdout.splitlines():
            if line.strip().startswith("8.0"):
                # fixture edges are 8/14 bps: a 10 bps fee empties the band
                assert line.split("|")[1].split()[0] == "0", line

        plain = run_analyze(["--db", db])
        assert plain.returncode == 0, plain.stderr
        assert "0.0 bps" in plain.stdout and "(from " not in plain.stdout
    finally:
        os.unlink(p)


def test_analyze_config_fees_flag_wins():
    # --fees-bps on the command line overrides the config-derived fee sum
    p = write_cfg(extra="base:\n  taker_fee_bps: 10.0\n")
    db = build_db(tmp_db(), [("SNDK", "entropy", "lighter-rh")])
    try:
        r = run_analyze(["--config", p, "--db", db, "--fees-bps", "0.0"])
        assert r.returncode == 0, r.stderr
        # no "(from config)" annotation: the fee came from the CLI flag
        assert "bps round-trip taker fees, minutes" in r.stdout
        for line in r.stdout.splitlines():
            if line.strip().startswith("8.0"):
                # 0 fees: fixture buy edge 14 - midline 10 = 4... the BUY
                # column is the room beyond the midline; fixture buy_max 14
                # - midline 10 = 14 bps room, so the 8-bps BUY band fires
                # in all 40 fixture minutes
                assert line.split("|")[2].split()[0] == "40", line
    finally:
        os.unlink(p)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
