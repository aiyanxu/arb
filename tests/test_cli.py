"""CLI wiring: --config selects the file whose content configures the run.

Run:  python3 -m pytest tests/  (or  python3 tests/test_cli.py)
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..")
# an env-file path that never exists: creds stay unset, tests stay isolated
NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")
GOOD = """symbol: SNDK
base_venue: entropy
hedge_venue: lighter-rh
thresholds:
  midline_bps: 5.0
  upper_bps: 4.0
  lower_bps: 3.0
"""


def write_tmp(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


def run_cli(args):
    return subprocess.run(
        [sys.executable, "-m", "entropy_arb"] + args,
        cwd=ROOT, capture_output=True, text=True)


def test_config_flag_missing_file_clean_error():
    # the flag's path is what gets opened: a bogus path must be named, not
    # the default config.yaml
    missing = os.path.join(tempfile.gettempdir(), "no-such-entropy-arb.yaml")
    r = run_cli(["--config", missing])
    assert r.returncode == 2
    assert "config error" in r.stderr
    assert f"config file '{missing}' not found" in r.stderr


def test_config_flag_selects_the_given_file():
    # cwd holds a valid config.yaml; only a file passed via --config can
    # produce this error, so the flag must override the default path
    bad = write_tmp(GOOD.replace("hedge_venue: lighter-rh",
                                 "hedge_venue: entropy"))
    r = run_cli(["--config", bad])
    assert r.returncode == 2
    assert "must differ" in r.stderr


def test_config_content_drives_engine_config(monkeypatch):
    # the Config handed to Engine must come from the --config file's content:
    # two runs, two files with different symbols -> two different Configs
    from entropy_arb import cli
    seen = {}

    class FakeEngine:
        def __init__(self, cfg, record_only=False):
            seen["cfg"] = cfg
            seen["record_only"] = record_only

        def request_stop(self):
            pass

        async def run(self):
            seen["ran"] = True

    monkeypatch.setattr(cli, "Engine", FakeEngine)
    for sym in ("AAA", "BBB"):
        cfg_file = write_tmp(GOOD.replace("symbol: SNDK", f"symbol: {sym}"))
        monkeypatch.setattr(sys, "argv",
                            ["entropy-arb", "--config", cfg_file,
                             "--env-file", NO_ENV,
                             "--record-only", "--no-dashboard"])
        cli.main()
        assert seen["ran"]
        assert seen["record_only"]
        assert seen["cfg"].symbol == sym
        assert seen["cfg"].base_venue == "entropy"
        assert seen["cfg"].hedge_venue == "lighter-rh"


if __name__ == "__main__":
    test_config_flag_missing_file_clean_error()
    test_config_flag_selects_the_given_file()
    print("OK")
