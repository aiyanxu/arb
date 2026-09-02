"""Config loading: example file, validation, CLI-selected markets.

Run:  python3 -m pytest tests/  (or  python3 tests/test_config.py)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import ConfigError, load_config  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
EXAMPLE = os.path.join(ROOT, "config.example.yaml")
NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")
# a symbol_map path that never exists: no overrides, tests stay isolated
NO_MAP = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such-map.yaml")


def write_tmp(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


def write_map(text: str) -> str:
    return write_tmp(text)


MINIMAL = """
thresholds:
  midline_bps: 5.0
  upper_bps: 4.0
  lower_bps: 3.0
"""


def load(yaml_text: str, symbol: str | None = "SNDK",
         hedge: str | None = "lighter-rh", map_text: str | None = None):
    map_file = (write_map(map_text) if map_text is not None
                else NO_MAP)
    return load_config(write_tmp(yaml_text), NO_ENV,
                       symbol=symbol, hedge_venue=hedge,
                       symbol_map_file=map_file)


def test_example_config_loads():
    # the example file is self-sufficient: markets come from the YAML itself
    cfg = load_config(EXAMPLE, NO_ENV, symbol=None,
                      hedge_venue=None, symbol_map_file=NO_MAP)
    assert cfg.symbol == "SNDK"
    assert cfg.entropy.kind == "hl" and cfg.entropy.hl_dex == "io"
    assert cfg.hedge_venue == "lighter-rh"
    assert cfg.hedge.kind == "lighter"
    assert cfg.hedge.lighter_profile.chain_id == 466324
    assert cfg.entropy.symbol == "SNDK" and cfg.hedge.symbol == "SNDK"
    assert cfg.recorder_enabled and cfg.recorder_db
    assert cfg.dashboard and cfg.log_file


def test_minimal_defaults():
    cfg = load(MINIMAL, hedge="lighter")
    assert cfg.midline_bps == 5.0 and cfg.upper_bps == 4.0 and cfg.lower_bps == 3.0
    assert cfg.hedge.label == "LIGHTER"
    assert cfg.hedge.lighter_profile.chain_id == 304
    assert cfg.take_fraction == 0.5          # defaults kick in
    assert cfg.recorder_enabled is True


def test_tradexyz_hedge():
    cfg = load(MINIMAL, hedge="tradexyz")
    assert cfg.hedge.kind == "hl" and cfg.hedge.hl_dex == "xyz"
    assert cfg.hedge.label == "XYZ"


def expect_error(yaml_text: str, needle: str, **kw):
    try:
        load(yaml_text, **kw)
    except ConfigError as e:
        assert needle in str(e), f"{needle!r} not in {e}"
        return
    raise AssertionError(f"expected ConfigError containing {needle!r}")


def test_unknown_key_rejected():
    expect_error(MINIMAL + "\nthresholdz:\n  x: 1\n",
                 "unknown config key 'thresholdz'")
    expect_error(MINIMAL + "\nsizing:\n  take_fractionn: 0.5\n",
                 "sizing.take_fractionn")


def test_markets_in_config():
    # symbol / hedge_venue now live in the YAML; the load() helper's
    # explicit values stand in for what the CLI would pass
    cfg = load("symbol: ETH\nhedge_venue: tradexyz\n" + MINIMAL,
               symbol="SNDK", hedge="lighter-rh")
    assert cfg.symbol == "SNDK"                      # CLI wins over the file
    assert cfg.hedge_venue == "lighter-rh"
    # with no CLI value the YAML decides
    cfg = load("symbol: ETH\nhedge_venue: tradexyz\n" + MINIMAL,
               symbol=None, hedge=None)
    assert cfg.symbol == "ETH"
    assert cfg.hedge_venue == "tradexyz"
    assert cfg.hedge.kind == "hl" and cfg.hedge.hl_dex == "xyz"


def test_markets_required():
    # neither the CLI nor config.yaml provides a market -> loud error
    expect_error(MINIMAL, "symbol is required", symbol=None, hedge=None)
    # symbol resolves from the helper default, hedge is missing
    expect_error(MINIMAL, "hedge_venue must be one of",
                 symbol="SNDK", hedge=None)


def test_bad_config_market_values():
    # hedge=None lets the YAML value through; symbol keeps the helper
    # default so the symbol check passes first
    expect_error(MINIMAL + "\nhedge_venue: binance\n",
                 "hedge_venue must be one of", hedge=None)
    expect_error(MINIMAL + "\nsymbol: ''\n", "symbol is required",
                 symbol=None, hedge=None)
    # YAML 1.1 gotchas: a bare NO/on is a bool, digits are ints
    expect_error(MINIMAL + "\nsymbol: 5\n", "'symbol' must be a string",
                 symbol=None, hedge=None)
    expect_error(MINIMAL + "\nhedge_venue: true\n",
                 "'hedge_venue' must be a string", symbol=None, hedge=None)


def test_bad_cli_markets():
    expect_error(MINIMAL, "--hedge", hedge="binance")
    expect_error(MINIMAL, "--symbol", symbol="")


def test_symbol_map_hedge_hit():
    cfg = load(MINIMAL, hedge="tradexyz",
               map_text="SNDK:\n  tradexyz: TTSLA\n")
    assert cfg.symbol == "SNDK"                      # canonical stays CLI
    assert cfg.entropy.symbol == "SNDK"              # unlisted venue falls back
    assert cfg.hedge.symbol == "TTSLA"


def test_symbol_map_entropy_hit():
    cfg = load(MINIMAL, map_text="SNDK:\n  entropy: ESNDK\n")
    assert cfg.entropy.symbol == "ESNDK"
    assert cfg.hedge.symbol == "SNDK"


def test_symbol_map_lighter_rh():
    cfg = load(MINIMAL, symbol="BTC", map_text="BTC:\n  lighter-rh: BTC-USD\n")
    assert cfg.hedge.symbol == "BTC-USD"
    assert cfg.entropy.symbol == "BTC"


def test_symbol_map_miss_falls_back():
    cfg = load(MINIMAL, map_text="BTC:\n  lighter-rh: BTC-USD\n")
    assert cfg.symbol == "SNDK"
    assert cfg.entropy.symbol == "SNDK" and cfg.hedge.symbol == "SNDK"


def test_symbol_map_missing_file():
    # NO_MAP never exists -> pure CLI passthrough
    cfg = load(MINIMAL)
    assert cfg.entropy.symbol == "SNDK" and cfg.hedge.symbol == "SNDK"


def test_symbol_map_bad_venue():
    expect_error(MINIMAL, "not a venue",
                 map_text="SNDK:\n  binance: XSNDK\n")


def test_symbol_map_bad_value():
    expect_error(MINIMAL, "must be a non-empty string",
                 map_text="SNDK:\n  tradexyz: ''\n")
    expect_error(MINIMAL, "must be a non-empty string",
                 map_text="SNDK:\n  tradexyz: 5\n")


def test_symbol_map_non_str_key():
    # a bare NO/ON key is a YAML 1.1 bool -> rejected, not silently ignored
    expect_error(MINIMAL, "symbol keys must be non-empty strings",
                 map_text="NO:\n  tradexyz: TTSLA\n")


def test_symbol_map_non_mapping_entry():
    expect_error(MINIMAL, "must be a mapping of venue -> symbol",
                 map_text="SNDK: TTSLA\n")


def test_missing_thresholds():
    expect_error("recorder:\n  enabled: true\n", "thresholds.")


def test_nonpositive_band():
    expect_error("thresholds:\n"
                 "  midline_bps: 5\n  upper_bps: 0\n  lower_bps: 3\n",
                 "must be > 0")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
