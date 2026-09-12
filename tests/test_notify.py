"""notify: TelegramHandler level gating, heartbeat, batching, never-raises.

Run:  python3 -m pytest tests/  (or  python3 tests/test_notify.py)
"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb import notify  # noqa: E402
from entropy_arb.notify import TelegramHandler  # noqa: E402


def make_handler(**kw):
    """A handler whose _send records instead of hitting the network."""
    sent = []
    h = TelegramHandler("123:abc", "42", **kw)
    h._send = lambda text: sent.append(text)
    return h, sent


def drain(h, timeout=3.0):
    """Wait until the worker has sent everything queued."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if h._q.empty():
            break
        time.sleep(0.05)
    time.sleep(0.4)   # batch grace + send gap


def rec(name, level, msg):
    return logging.LogRecord(name, level, "p", 1, msg, None, None)


def stop(h):
    h._stopped.set()


def teardown_function(fn):
    # tests attach handlers to the root logger via notify.attach: clean up
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, TelegramHandler):
            h._stopped.set()
            root.removeHandler(h)


def test_level_gate():
    h, sent = make_handler(level="WARNING")
    h.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    # the level gate lives in Logger.callHandlers (record.levelno >=
    # hdlr.level), so exercise the real path: attach to a logger and log
    lg = logging.getLogger("notify-test-gate")
    lg.setLevel(logging.DEBUG)
    lg.addHandler(h)
    try:
        lg.info("info stays local")
        lg.warning("warning goes out")
    finally:
        lg.removeHandler(h)
    drain(h)
    assert len(sent) == 1
    assert "warning goes out" in sent[0] and "info stays local" not in sent[0]
    stop(h)


def test_heartbeat_suppression():
    h, sent = make_handler(level="INFO", heartbeat_sec=3600.0)
    h.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    h.handle(rec("status", logging.INFO, "[status] first heartbeat"))
    h.handle(rec("status", logging.INFO, "[status] within window: dropped"))
    h.handle(rec("engine", logging.WARNING, "real warning always passes"))
    drain(h)
    body = "\n".join(sent)
    assert "first heartbeat" in body
    assert "within window" not in body
    assert "real warning" in body
    stop(h)


def test_own_logs_never_forwarded():
    # a telegram send failure logs to the 'telegram' logger — that record
    # must be dropped, or a broken token produces a feedback loop
    h, sent = make_handler(level="DEBUG")
    h.handle(rec("telegram", logging.WARNING, "telegram send failed: ..."))
    drain(h)
    assert sent == []
    stop(h)


def test_burst_becomes_one_message():
    h, sent = make_handler(level="INFO")
    h.setFormatter(logging.Formatter("%(message)s"))
    for i in range(5):
        h.handle(rec("engine", logging.ERROR, f"line {i}"))
    drain(h)
    assert len(sent) == 1, sent
    for i in range(5):
        assert f"line {i}" in sent[0]
    stop(h)


def test_emit_never_raises():
    # a broken formatter must not propagate into the caller (the engine)
    h, sent = make_handler(level="INFO")
    h.setFormatter(None)   # format() will blow up
    try:
        h.handle(rec("engine", logging.ERROR, "boom"))
    except Exception as e:   # pragma: no cover
        raise AssertionError(f"emit raised: {e!r}")
    stop(h)


def test_attach_requires_enabled_and_creds(monkeypatch):
    from entropy_arb.config import load_config

    def cfg_with(telegram_yaml):
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        f.write(telegram_yaml + """
thresholds: {midline_bps: 0.0, upper_bps: 4.0, lower_bps: 4.0}
""")
        f.close()
        return load_config(f.name, "/tmp/no-such.env", symbol="SNDK",
                           hedge_venue="lighter-rh")

    # disabled: off, even with creds present
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    assert notify.attach(cfg_with("telegram: {enabled: false}\n")) is None

    # enabled + creds: attached once to the root logger
    h = notify.attach(cfg_with("telegram: {enabled: true}\n"))
    assert h is not None and any(
        x is h for x in logging.getLogger().handlers)
    stop(h)

    # enabled but no creds anywhere: refused, not raised
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    assert notify.attach(cfg_with("telegram: {enabled: true}\n")) is None


def test_config_section_overrides_env(monkeypatch):
    from entropy_arb.config import load_config
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:env-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write("""
thresholds: {midline_bps: 0.0, upper_bps: 4.0, lower_bps: 4.0}
telegram:
  enabled: true
  bot_token: "123:yaml-token"
  min_level: ERROR
""")
    f.close()
    cfg = load_config(f.name, "/tmp/no-such.env", symbol="SNDK",
                      hedge_venue="lighter-rh")
    h = notify.attach(cfg)
    assert h is not None
    assert h.token == "123:yaml-token"          # yaml wins over env
    assert h.level == logging.ERROR             # min_level parsed
    stop(h)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
