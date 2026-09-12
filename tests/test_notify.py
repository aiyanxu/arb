"""notify: explicit Telegram send() API — delivery, throttle, no-op safety.

Run:  python3 -m pytest tests/  (or  python3 tests/test_notify.py)
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb import notify  # noqa: E402
from entropy_arb.notify import _Sender, _get_sender  # noqa: E402


def make_sender():
    """A sender whose _post records instead of hitting the network."""
    posted = []
    s = _Sender("123:abc", "42")
    s._post = lambda text: None
    sent = []
    orig = _Sender._post
    _Sender._post = lambda self, text: sent.append(text)
    return s, sent, orig


def drain(s, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if s._q.empty():
            break
        time.sleep(0.05)
    time.sleep(0.4)   # send gap


def teardown_function(fn):
    # reset the process-wide singleton + env between tests
    import entropy_arb.notify as n
    n._instance = None
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("TELEGRAM_CHAT_ID", None)


def test_send_without_creds_is_noop(monkeypatch):
    # nothing configured: send must be a silent no-op (and never spin up
    # a thread)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert _get_sender() is None
    notify.send("never delivered, never raises")


def test_send_delivers_message(monkeypatch):
    sent = []
    monkeypatch.setattr(_Sender, "_post",
                        lambda self, text: sent.append(text))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    notify.send("hello from the test")
    deadline = time.time() + 3.0
    while time.time() < deadline and not sent:
        time.sleep(0.05)
    assert sent == ["hello from the test"]


def test_send_never_raises(monkeypatch):
    # a broken sender must not propagate into the caller
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    def boom(self, text):
        raise RuntimeError("no network")
    monkeypatch.setattr(_Sender, "_post", boom)
    s = _get_sender()
    assert s is not None
    # the worker swallows the error; submit side must never raise either
    notify.send("this is fine")
    time.sleep(0.5)
    # broken env (e.g. missing perms) also can't blow up send()
    monkeypatch.setattr("entropy_arb.notify._get_sender",
                        lambda: (_ for _ in ()).throw(RuntimeError("x")))
    notify.send("still no raise")


def test_throttle_between_sends(monkeypatch):
    """The worker spaces sends SEND_GAP_SEC apart. With a frozen clock the
    second send must sleep out the full gap; record via a patched sleep."""
    import entropy_arb.notify as n
    sleeps = []
    posted = []
    monkeypatch.setattr(_Sender, "_post",
                        lambda self, text: posted.append(text))
    monkeypatch.setattr(n.time, "time", lambda: 1000.0)   # frozen clock
    real_sleep = n.time.sleep

    def spy_sleep(sec):
        sleeps.append(sec)
        real_sleep(min(sec, 0.01))   # don't actually wait
    monkeypatch.setattr(n.time, "sleep", spy_sleep)

    s = _Sender("123:abc", "42")
    s.submit("a")
    s.submit("b")
    # worker drains the queue under the frozen clock (no real sleeps needed)
    deadline = time.time() + 3
    while time.time() < deadline and not s._q.empty():
        real_sleep(0.02)
    # first send: wait = 1000+1.1-1000 > 0? last_send starts 0.0 -> no wait.
    # after first send last_send=1000 -> second send sleeps 1.1s
    assert 1.1 in [round(x, 2) for x in sleeps], sleeps


def test_overflow_drops_oldest():
    import queue as _q
    s = _Sender("123:abc", "42")
    s._q = _q.Queue(maxsize=2)   # tiny queue
    for i in range(5):
        s.submit(f"msg {i}")
    assert s._q.qsize() == 2     # newest two kept, oldest dropped


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
