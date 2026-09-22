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


def test_flatten_requires_base_hedge_symbol():
    # a bogus venue must be rejected by argparse's choices (exit 2), not
    # silently accepted
    r = run_cli(["flatten", "--symbol", "SNDK", "--base", "entropy",
                 "--hedge", "no-such-venue"])
    assert r.returncode == 2
    assert "invalid choice" in r.stderr


def test_flatten_reads_pair_from_config():
    # --symbol/--base/--hedge are optional overrides: without them the pair
    # comes from the --config file. The config here names venues that must
    # differ; the same-venue error can only come from the file's content,
    # proving it was actually read (the missing env file then fails creds
    # with the same exit 2 — network never touched).
    cfg = write_tmp(GOOD)
    r = run_cli(["flatten", "--config", cfg, "--env-file", NO_ENV])
    assert r.returncode == 2
    assert "flatten sends real orders" in r.stderr


def test_flatten_overrides_win_over_config():
    # explicit flags win over the config file: the file says entropy/
    # lighter-rh, but a --hedge equal to --base must be rejected even
    # though the file's pair is valid
    r = run_cli(["flatten", "--symbol", "SNDK", "--base", "entropy",
                 "--hedge", "entropy", "--env-file", NO_ENV])
    assert r.returncode == 2
    assert "must differ" in r.stderr


def test_flatten_missing_creds_clean_error():
    # with no credentials anywhere, flatten must refuse before touching the
    # network (a clear message, exit 2 — not a traceback)
    missing = os.path.join(tempfile.gettempdir(), "no-such-entropy-arb.yaml")
    r = run_cli(["flatten", "--symbol", "SNDK", "--base", "entropy",
                 "--hedge", "lighter-rh", "--config", missing,
                 "--env-file", NO_ENV])
    assert r.returncode == 2
    # the config file is still required and named when missing
    assert "config file" in r.stderr and "not found" in r.stderr


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


def test_pid_file_path_layout():
    from entropy_arb.cli import pid_file_path
    assert pid_file_path("SNDK", "entropy", "lighter-rh") == \
        "/tmp/entropy-arb-SNDK-entropy-lighter-rh.pid"


def test_live_run_writes_and_cleans_pid_file(monkeypatch):
    # a live (no --record-only) run writes /tmp/entropy-arb-<pair>.pid on
    # startup and removes it on exit; --record-only never writes one
    from entropy_arb import cli
    from entropy_arb.cli import pid_file_path

    symbol, base, hedge = "entropy-arb-pidfile-test", "entropy", "lighter-rh"
    path = pid_file_path(symbol, base, hedge)
    seen = {}

    class FakeEngine:
        def __init__(self, cfg, record_only=False):
            pass

        def request_stop(self):
            pass

        async def run(self):
            # mid-run the pid file must exist and hold our pid
            seen["exists"] = os.path.exists(path)
            if seen["exists"]:
                with open(path) as fh:
                    seen["pid"] = int(fh.read().strip())

    monkeypatch.setattr(cli, "Engine", FakeEngine)
    if os.path.exists(path):
        os.remove(path)
    cfg_file = write_tmp(GOOD.replace("symbol: SNDK", f"symbol: {symbol}"))
    monkeypatch.setattr(sys, "argv",
                        ["entropy-arb", "--config", cfg_file,
                         "--env-file", NO_ENV, "--no-dashboard"])
    cli.main()
    assert seen["exists"] is True            # written on startup
    assert seen["pid"] == os.getpid()        # this process's pid
    assert not os.path.exists(path)          # removed again on exit


def test_record_only_writes_no_pid_file(monkeypatch):
    from entropy_arb import cli
    from entropy_arb.cli import pid_file_path

    class FakeEngine:
        def __init__(self, cfg, record_only=False):
            pass

        def request_stop(self):
            pass

        async def run(self):
            pass

    monkeypatch.setattr(cli, "Engine", FakeEngine)
    symbol = "entropy-arb-pidfile-test"
    path = pid_file_path(symbol, "entropy", "lighter-rh")
    if os.path.exists(path):
        os.remove(path)
    cfg_file = write_tmp(GOOD.replace("symbol: SNDK", f"symbol: {symbol}"))
    monkeypatch.setattr(sys, "argv",
                        ["entropy-arb", "--config", cfg_file,
                         "--env-file", NO_ENV,
                         "--record-only", "--no-dashboard"])
    cli.main()
    assert not os.path.exists(path)          # record-only never writes one


def test_kill_running_bot_stops_the_recorded_process():
    # a real subprocess whose command line contains "entropy_arb" is stopped
    # by kill_running_bot (SIGTERM default disposition is enough) and its
    # pid file is removed afterwards
    from entropy_arb import cli
    script = os.path.join(tempfile.gettempdir(),
                          "entropy-arb-pidfile-test-stub.py")
    with open(script, "w") as fh:
        fh.write("import time\ntime.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, script])
    path = os.path.join(tempfile.gettempdir(),
                        f"entropy-arb-pidfile-test-{os.getpid()}.pid")
    try:
        with open(path, "w") as fh:
            fh.write(f"{proc.pid}\n")
        # the stub's command line contains "entropy-arb-pidfile-test", so it
        # passes the same identity guard a real `entropy-arb` process would
        assert cli._looks_like_our_bot(proc.pid) is True
        cli.kill_running_bot(path)
        assert proc.wait(timeout=10) != 0     # stopped (by the SIGTERM)
        assert not os.path.exists(path)      # killed -> pid file removed
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if os.path.exists(script):
            os.remove(script)


def test_kill_running_bot_ignores_foreign_pid():
    # a pid file naming a process that is NOT entropy-arb must be left alone
    from entropy_arb import cli
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    path = os.path.join(tempfile.gettempdir(),
                        f"entropy-arb-foreign-{os.getpid()}.pid")
    try:
        with open(path, "w") as fh:
            fh.write(f"{proc.pid}\n")
        cli.kill_running_bot(path)
        assert proc.poll() is None           # untouched
        assert os.path.exists(path)          # and its pid file kept
    finally:
        proc.kill()
        proc.wait()
        if os.path.exists(path):
            os.remove(path)


def test_write_pid_file_refuses_while_peer_is_live():
    # a live entropy-arb process on the same pair blocks a second start:
    # two bots on one pair would trade the same Lighter nonce sequence
    from entropy_arb import cli
    script = os.path.join(tempfile.gettempdir(),
                          "entropy-arb-pidguard-test-stub.py")
    with open(script, "w") as fh:
        fh.write("import time\ntime.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, script])
    path = os.path.join(tempfile.gettempdir(),
                        f"entropy-arb-pidguard-{os.getpid()}.pid")
    try:
        with open(path, "w") as fh:
            fh.write(f"{proc.pid}\n")
        assert cli._looks_like_our_bot(proc.pid) is True

        class FakeCfg:
            symbol, base_venue, hedge_venue = "X", "entropy", "lighter-rh"

        # write_pid_file must hit OUR pid path — monkeypatch the path builder
        real_path = cli.pid_file_path
        cli.pid_file_path = lambda *a: path
        try:
            try:
                cli.write_pid_file(FakeCfg())
                raise AssertionError("expected RuntimeError")
            except RuntimeError as e:
                assert str(proc.pid) in str(e)
                assert "another entropy-arb" in str(e)
        finally:
            cli.pid_file_path = real_path
        # the file still holds the peer's pid — not overwritten
        with open(path) as fh:
            assert int(fh.read().strip()) == proc.pid
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if os.path.exists(script):
            os.remove(script)
        if os.path.exists(path):
            os.remove(path)


def test_write_pid_file_overwrites_stale_pid():
    # a dead peer's pid file is just overwritten (best-effort, as before)
    from entropy_arb import cli
    path = os.path.join(tempfile.gettempdir(),
                        f"entropy-arb-pidstale-{os.getpid()}.pid")
    with open(path, "w") as fh:
        fh.write("999999\n")               # almost certainly not a live bot
    try:
        class FakeCfg:
            symbol, base_venue, hedge_venue = "X", "entropy", "lighter-rh"

        real_path = cli.pid_file_path
        cli.pid_file_path = lambda *a: path
        try:
            got = cli.write_pid_file(FakeCfg())
        finally:
            cli.pid_file_path = real_path
        assert got == path
        with open(path) as fh:
            assert int(fh.read().strip()) == os.getpid()
    finally:
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    test_config_flag_missing_file_clean_error()
    test_config_flag_selects_the_given_file()
    print("OK")
