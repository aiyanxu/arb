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
         base: str | None = "entropy", hedge: str | None = "lighter-rh",
         map_text: str | None = None):
    map_file = (write_map(map_text) if map_text is not None
                else NO_MAP)
    return load_config(write_tmp(yaml_text), NO_ENV,
                       symbol=symbol, base_venue=base, hedge_venue=hedge,
                       symbol_map_file=map_file)


def test_example_config_loads():
    # the example file is self-sufficient: markets come from the YAML itself
    cfg = load_config(EXAMPLE, NO_ENV, symbol=None, base_venue=None,
                      hedge_venue=None, symbol_map_file=NO_MAP)
    assert cfg.symbol == "SNDK"
    assert cfg.base_venue == "entropy"
    assert cfg.base.kind == "hl" and cfg.base.hl_dex == "io"
    assert cfg.hedge_venue == "lighter-rh"
    assert cfg.hedge.kind == "lighter"
    assert cfg.hedge.lighter_profile.chain_id == 466324
    assert cfg.base.symbol == "SNDK" and cfg.hedge.symbol == "SNDK"
    assert cfg.recorder_enabled and cfg.recorder_db
    assert cfg.dashboard and cfg.log_file


def test_minimal_defaults():
    cfg = load(MINIMAL, hedge="lighter")
    assert cfg.midline_bps == 5.0 and cfg.upper_bps == 4.0 and cfg.lower_bps == 3.0
    assert cfg.base_venue == "entropy"       # absent key -> default
    assert cfg.hedge.label == "LIGHTER"
    assert cfg.hedge.lighter_profile.chain_id == 304
    assert cfg.take_fraction == 0.5          # defaults kick in
    assert cfg.recorder_enabled is True


def test_tradexyz_hedge():
    cfg = load(MINIMAL, hedge="tradexyz")
    assert cfg.hedge.kind == "hl" and cfg.hedge.hl_dex == "xyz"
    assert cfg.hedge.label == "XYZ"
    assert cfg.hedge.fee_bps == 1.0          # venue registry default


def test_base_venue_tradexyz():
    cfg = load(MINIMAL, base="tradexyz", hedge="entropy")
    assert cfg.base_venue == "tradexyz"
    assert cfg.base.kind == "hl" and cfg.base.hl_dex == "xyz"
    assert cfg.base.label == "XYZ"
    assert cfg.hedge.kind == "hl" and cfg.hedge.hl_dex == "io"
    assert cfg.hedge.label == "ENTROPY"


def test_base_venue_lighter():
    cfg = load(MINIMAL, base="lighter", hedge="entropy")
    assert cfg.base.kind == "lighter"
    assert cfg.base.label == "LIGHTER"
    assert cfg.base.lighter_profile.chain_id == 304
    assert cfg.hedge.kind == "hl" and cfg.hedge.hl_dex == "io"


def test_hedge_venue_entropy():
    # the mirror of the historical default: entropy as the hedge leg
    cfg = load(MINIMAL, base="lighter", hedge="entropy")
    assert cfg.hedge.key == "hedge" and cfg.hedge.label == "ENTROPY"
    assert cfg.base.key == "base"


def test_both_lighter_legs():
    # lighter base + lighter-rh hedge: separate deployments, separate creds
    cfg = load(MINIMAL, base="lighter", hedge="lighter-rh")
    assert cfg.base.lighter_profile.chain_id == 304
    assert cfg.hedge.lighter_profile.chain_id == 466324
    assert cfg.base.lighter_creds is not cfg.hedge.lighter_creds


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


def test_old_entropy_section_rename_hint():
    # configs from before the base-venue change get a pointed message
    expect_error(MINIMAL + "\nentropy:\n  dex: io\n",
                 "'entropy' was renamed to 'base'")


def test_same_venue_both_legs_rejected():
    expect_error(MINIMAL, "must differ", base="lighter-rh", hedge="lighter-rh")
    expect_error(MINIMAL, "must differ", base="entropy", hedge="entropy")


def test_same_hl_dex_rejected():
    # base dex xyz + hedge tradexyz (dex xyz) = one market, not an arb
    expect_error(MINIMAL + "\nbase:\n  dex: xyz\n",
                 "same Hyperliquid dex", base="entropy", hedge="tradexyz")
    # the mirror: base tradexyz + hedge dex xyz
    expect_error(MINIMAL + "\nhedge:\n  dex: xyz\n",
                 "same Hyperliquid dex", base="tradexyz", hedge="entropy")


def test_dex_on_lighter_rejected():
    expect_error(MINIMAL + "\nbase:\n  dex: io\n",
                 "only applies to Hyperliquid", base="lighter", hedge="entropy")
    expect_error(MINIMAL + "\nhedge:\n  dex: io\n",
                 "only applies to Hyperliquid", base="entropy", hedge="lighter")


def test_fee_below_venue_default_rejected():
    # tradexyz charges ~1 bps; configuring 0 would make thresholds too loose
    expect_error(MINIMAL + "\nbase:\n  taker_fee_bps: 0.0\n",
                 "below the venue default", base="tradexyz", hedge="entropy")
    expect_error(MINIMAL + "\nhedge:\n  taker_fee_bps: 0.5\n",
                 "below the venue default", base="entropy", hedge="tradexyz")


def test_fee_at_or_above_default_ok():
    # explicit higher fees are fine (only underestimates are rejected)
    cfg = load(MINIMAL + "\nhedge:\n  taker_fee_bps: 1.5\n", hedge="tradexyz")
    assert cfg.hedge.fee_bps == 1.5
    cfg = load(MINIMAL + "\nbase:\n  taker_fee_bps: 0.0\n")   # entropy default
    assert cfg.base.fee_bps == 0.0


def test_markets_in_config():
    # symbol / venues now live in the YAML; the load() helper's explicit
    # values stand in for what the CLI would pass
    cfg = load("symbol: ETH\nbase_venue: tradexyz\nhedge_venue: tradexyz\n"
               + MINIMAL,
               symbol="SNDK", base="entropy", hedge="lighter-rh")
    assert cfg.symbol == "SNDK"                      # CLI wins over the file
    assert cfg.base_venue == "entropy"
    assert cfg.hedge_venue == "lighter-rh"
    # with no CLI value the YAML decides
    cfg = load("symbol: ETH\nbase_venue: lighter\nhedge_venue: tradexyz\n"
               + MINIMAL,
               symbol=None, base=None, hedge=None)
    assert cfg.symbol == "ETH"
    assert cfg.base_venue == "lighter" and cfg.hedge_venue == "tradexyz"
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
    expect_error(MINIMAL + "\nbase_venue: binance\n",
                 "base_venue must be one of", base=None, hedge=None)
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
    expect_error(MINIMAL, "--base", base="binance")
    expect_error(MINIMAL, "--symbol", symbol="")


def test_symbol_map_hedge_hit():
    cfg = load(MINIMAL, hedge="tradexyz",
               map_text="SNDK:\n  tradexyz: TTSLA\n")
    assert cfg.symbol == "SNDK"                      # canonical stays CLI
    assert cfg.base.symbol == "SNDK"                 # unlisted venue falls back
    assert cfg.hedge.symbol == "TTSLA"


def test_symbol_map_base_hit():
    cfg = load(MINIMAL, map_text="SNDK:\n  entropy: ESNDK\n")
    assert cfg.base.symbol == "ESNDK"
    assert cfg.hedge.symbol == "SNDK"


def test_symbol_map_by_base_venue_name():
    # base=tradexyz resolves the map under its own venue name
    cfg = load(MINIMAL, base="tradexyz", hedge="entropy",
               map_text="SNDK:\n  tradexyz: TTSLA\n")
    assert cfg.base.symbol == "TTSLA"
    assert cfg.hedge.symbol == "SNDK"


def test_symbol_map_lighter_rh():
    cfg = load(MINIMAL, symbol="BTC", map_text="BTC:\n  lighter-rh: BTC-USD\n")
    assert cfg.hedge.symbol == "BTC-USD"
    assert cfg.base.symbol == "BTC"


def test_symbol_map_miss_falls_back():
    cfg = load(MINIMAL, map_text="BTC:\n  lighter-rh: BTC-USD\n")
    assert cfg.symbol == "SNDK"
    assert cfg.base.symbol == "SNDK" and cfg.hedge.symbol == "SNDK"


def test_symbol_map_missing_file():
    # NO_MAP never exists -> pure CLI passthrough
    cfg = load(MINIMAL)
    assert cfg.base.symbol == "SNDK" and cfg.hedge.symbol == "SNDK"


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
