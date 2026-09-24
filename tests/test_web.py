"""webapp: FastAPI backend — REST endpoints, WS stream, static frontend.

Run:  python3 -m pytest tests/  (or  python3 tests/test_web.py)
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.webapp import make_app  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def make_cfg(**thresholds):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    thr = thresholds or {}
    f.write(f"""
thresholds:
  midline_bps: {thr.get('midline', 5.0)}
  upper_bps: 4.0
  lower_bps: 4.0
recorder:
  db: {os.path.join(tempfile.mkdtemp(), 'no-such.duckdb')}
""")
    f.close()
    return load_config(f.name, NO_ENV, symbol="SNDK",
                       hedge_venue="lighter-rh")


class StubVenue:
    def __init__(self, key, name):
        from entropy_arb.book import OrderBook
        self.key, self.name, self.kind = key, name, "hl"
        self.conf = type("C", (), {"symbol": "SNDK"})()
        self.fee_bps, self.cap_usd = 0.0, 1000.0
        self.position, self.cash = 0.5, -50.0
        self.equity, self.volume_usd = 1000.0, 250.0
        self.last_traded_ts = 0.0
        self.book = OrderBook()
        self.book.apply_hl([[{"px": "100.0", "sz": "5"}],
                            [{"px": "100.02", "sz": "50"}]])


class StubEngine:
    """Just enough Engine surface for snapshot()."""
    def __init__(self):
        self.base = StubVenue("base", "ENTROPY")
        self.hedge = StubVenue("hedge", "RH")
        self.venues = {"base": self.base, "hedge": self.hedge}
        self.stop = asyncio.Event()
        self._update_evt = asyncio.Event()
        self.record_only = True
        self.halted = False
        self.paused = False
        self.flatten_in_progress = False
        self.flatten_state = None
        self.trades, self.hedges = 2, 1
        self.total_exp_edge, self.total_fill_edge = 0.5, 0.42
        self.start_ts = asyncio.get_event_loop().time() if False else 0.0
        import time
        self.start_ts = time.time() - 60.0
        self.recent_trades = []
        self.cfg = None

    def session_pnl(self):
        return 12.34

    def premium_bps(self):
        return 11.0

    def request_stop(self):
        self.stop.set()

    def request_pause(self):
        self.paused = True

    def request_resume(self):
        if self.halted:
            return False
        self.paused = False
        return True

    async def flatten_all(self):
        self.flatten_state = {"running": False, "rounds": 1,
                              "result": "flat"}
        return True

    async def run(self):
        await asyncio.Event().wait()


def test_health_and_config():
    app = make_app(make_cfg(), engine=None)
    with TestClient(app) as c:
        r = c.get("/api/health")
        assert r.status_code == 200 and r.json()["ok"] is True
        cfg = c.get("/api/config").json()
        assert cfg["symbol"] == "SNDK"
        assert cfg["thresholds"]["upper_bps"] == 4.0
        assert "private" not in str(cfg).lower()   # no credential fields


def test_live_with_engine():
    import time
    eng = StubEngine()
    with TestClient(make_app(make_cfg(), engine=eng)) as client:
        snap = client.get("/api/live").json()
        assert snap["mode"] == "live"
        assert snap["engine"]["trades"] == 2
        assert snap["venues"][0]["bid"] == 100.0
        assert snap["symbol"] == "SNDK"


# Fields frontend/src/types.ts EngineState declares. The built React app reads
# every one of them under snap.engine; an absent key renders as `undefined`
# and throws on first index (e.g. band[1]) — which is what the dashboard's
# "Cannot read properties of undefined (reading '1')" crash was. Keep in sync.
ENGINE_STATE_FIELDS = {
    "running", "record_only", "halted", "paused", "flatten_in_progress",
    "trades", "hedges", "exp_edge_usd", "fill_edge_usd", "uptime_sec",
    "pnl", "premium_bps", "band", "midline_bps",
}


def test_live_engine_has_every_frontend_field():
    """Engine mode: snap.engine must carry all of EngineState, in-place."""
    eng = StubEngine()
    with TestClient(make_app(make_cfg(), engine=eng)) as client:
        eng_state = client.get("/api/live").json()["engine"]
    assert ENGINE_STATE_FIELDS <= set(eng_state), \
        f"missing: {ENGINE_STATE_FIELDS - set(eng_state)}"
    # the four stat fields are engine-scoped now, not snapshot-root
    assert eng_state["premium_bps"] == 11.0
    assert eng_state["pnl"] == 12.34
    assert eng_state["band"][0] == 1.0 and eng_state["band"][1] == 9.0
    assert eng_state["midline_bps"] == 5.0


def test_live_artifact_mode_has_every_frontend_field():
    """Artifact mode (no engine): the stub must still satisfy EngineState —
    the dashboard renders this path and would crash on a short dict."""
    with TestClient(make_app(make_cfg(), engine=None)) as client:
        eng_state = client.get("/api/live").json()["engine"]
    assert ENGINE_STATE_FIELDS <= set(eng_state), \
        f"missing: {ENGINE_STATE_FIELDS - set(eng_state)}"
    # band is indexed unconditionally by the UI, so it needs two slots even
    # with no engine (nulls render as "—" and skip the cls comparison)
    assert eng_state["band"] == [None, None]
    assert eng_state["premium_bps"] is None and eng_state["pnl"] is None


def test_trades_csv_fallback():
    import tempfile
    csv_path = os.path.join(tempfile.mkdtemp(), "trades.csv")
    with open(csv_path, "w") as fh:
        fh.write("ts,direction,buy_venue,sell_venue,qty,buy_limit,"
                 "sell_limit,buy_notional,sell_notional,exp_edge_usd,"
                 "gross_edge_usd,marginal_premium_bps,midline_bps,"
                 "inv_add_bps,ok,buy_fill,sell_fill,buy_status,sell_status,"
                 "fill_edge_usd\n")
        fh.write("1789000000.0,sell_base,ENTROPY,RH,1.0,100,100,100,100,"
                "0.1,0.1,10.0,0,0,True,1,1,finished,finished,0.02\n")
    cfg = make_cfg()
    cfg.trades_csv = csv_path
    with TestClient(make_app(cfg, engine=None)) as client:
        body = client.get("/api/trades").json()
        assert body["source"] == "csv"
        assert len(body["rows"]) == 1
        assert body["rows"][0]["direction"] == "sell_base"


def test_threshold_suggestion_shape():
    # no db -> clean error, not a 500
    with TestClient(make_app(make_cfg())) as client:
        body = client.get("/api/threshold-suggestion").json()
        assert "error" in body or "reason" in body


def test_static_frontend_served():
    with TestClient(make_app(make_cfg())) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert b"html" in r.content[:200].lower() or b"entropy" in r.content


# ------------------------------------------------------------ control API

import pytest  # noqa: E402


def test_control_disabled_without_token():
    eng = StubEngine()
    with TestClient(make_app(make_cfg(), engine=eng)) as c:
        r = c.post("/api/engine/pause")
        assert r.status_code == 403
        assert "ARB_WEB_TOKEN" in r.json()["detail"]
        r = c.post("/api/positions/flatten")
        assert r.status_code == 403
        assert eng.paused is False    # nothing happened


def test_control_auth_rejects_wrong_token():
    os.environ["ARB_WEB_TOKEN"] = "sekrit"
    try:
        eng = StubEngine()
        with TestClient(make_app(make_cfg(), engine=eng)) as c:
            assert c.post("/api/engine/pause").status_code == 403
            assert c.post("/api/engine/pause",
                          headers={"Authorization": "Bearer wrong"}
                          ).status_code == 403
            assert eng.paused is False
    finally:
        os.environ.pop("ARB_WEB_TOKEN", None)


def test_pause_resume_with_token():
    os.environ["ARB_WEB_TOKEN"] = "sekrit"
    try:
        eng = StubEngine()
        with TestClient(make_app(make_cfg(), engine=eng)) as c:
            auth = {"Authorization": "Bearer sekrit"}
            r = c.post("/api/engine/pause", headers=auth)
            assert r.status_code == 200 and r.json()["paused"] is True
            assert eng.paused is True
            r = c.post("/api/engine/resume", headers=auth)
            assert r.status_code == 200 and r.json()["paused"] is False
            # snapshot carries the new state
            snap = c.get("/api/live").json()
            assert snap["engine"]["paused"] is False
            # halted engine: resume -> 409
            eng.halted = True
            eng.paused = True
            r = c.post("/api/engine/resume", headers=auth)
            assert r.status_code == 409
            assert eng.paused is True
    finally:
        os.environ.pop("ARB_WEB_TOKEN", None)


def test_flatten_endpoint_lifecycle():
    os.environ["ARB_WEB_TOKEN"] = "sekrit"
    try:
        eng = StubEngine()
        eng.record_only = False     # a live engine
        with TestClient(make_app(make_cfg(), engine=eng)) as c:
            auth = {"Authorization": "Bearer sekrit"}
            r = c.post("/api/positions/flatten", headers=auth)
            assert r.status_code == 200
            status = c.get("/api/flatten/status").json()
            assert status["flat"] is True
            assert status["state"]["result"] == "flat"
            # snapshot publishes the state too
            snap = c.get("/api/live").json()
            assert snap["flatten_state"]["result"] == "flat"
    finally:
        os.environ.pop("ARB_WEB_TOKEN", None)


def test_flatten_refused_record_only():
    os.environ["ARB_WEB_TOKEN"] = "sekrit"
    try:
        eng = StubEngine()          # record_only=True by default
        with TestClient(make_app(make_cfg(), engine=eng)) as c:
            r = c.post("/api/positions/flatten",
                       headers={"Authorization": "Bearer sekrit"})
            assert r.status_code == 409
            assert "record-only" in r.json()["detail"]
    finally:
        os.environ.pop("ARB_WEB_TOKEN", None)


def test_control_needs_engine_artifact_mode():
    os.environ["ARB_WEB_TOKEN"] = "sekrit"
    try:
        with TestClient(make_app(make_cfg(), engine=None)) as c:
            for path in ("/api/engine/pause", "/api/engine/resume",
                         "/api/positions/flatten"):
                r = c.post(path, headers={"Authorization": "Bearer sekrit"})
                assert r.status_code == 409, path
                assert "no engine" in r.json()["detail"]
    finally:
        os.environ.pop("ARB_WEB_TOKEN", None)


def test_flatten_status_without_engine():
    with TestClient(make_app(make_cfg(), engine=None)) as c:
        body = c.get("/api/flatten/status").json()
        assert body == {"running": False, "state": None}


# -------------------------------------------------------- threshold hot-update

def test_thresholds_update_with_token():
    os.environ["ARB_WEB_TOKEN"] = "sekrit"
    try:
        cfg = make_cfg()
        eng = StubEngine()
        eng.cfg = cfg
        with TestClient(make_app(cfg, engine=eng)) as c:
            auth = {"Authorization": "Bearer sekrit"}
            r = c.post("/api/config/thresholds", json={"upper_bps": 6.5,
                                                       "lower_bps": 3.0},
                       headers=auth)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["previous"]["upper_bps"] == 4.0
            assert body["current"] == {"midline_bps": 5.0,
                                       "upper_bps": 6.5, "lower_bps": 3.0}
            # applied in place on the shared Config
            assert cfg.upper_bps == 6.5 and cfg.lower_bps == 3.0
            # midline untouched when absent
            assert cfg.midline_bps == 5.0
            # /api/config reflects the new values immediately
            assert c.get("/api/config").json()["thresholds"]["upper_bps"] == 6.5
    finally:
        os.environ.pop("ARB_WEB_TOKEN", None)


def test_thresholds_update_partial_set_is_atomic_dataclass():
    os.environ["ARB_WEB_TOKEN"] = "sekrit"
    try:
        cfg = make_cfg()
        eng = StubEngine()
        eng.cfg = cfg
        with TestClient(make_app(cfg, engine=eng)) as c:
            auth = {"Authorization": "Bearer sekrit"}
            # only midline changes; band values stay
            r = c.post("/api/config/thresholds", json={"midline_bps": 2.0},
                       headers=auth)
            assert r.status_code == 200
            assert cfg.midline_bps == 2.0
            assert cfg.upper_bps == 4.0 and cfg.lower_bps == 4.0
    finally:
        os.environ.pop("ARB_WEB_TOKEN", None)


def test_thresholds_update_validation():
    os.environ["ARB_WEB_TOKEN"] = "sekrit"
    try:
        cfg = make_cfg()
        eng = StubEngine()
        eng.cfg = cfg
        with TestClient(make_app(cfg, engine=eng)) as c:
            auth = {"Authorization": "Bearer sekrit"}
            # unknown key
            assert c.post("/api/config/thresholds",
                          json={"nope": 1}, headers=auth).status_code == 400
            # non-numeric / bool
            assert c.post("/api/config/thresholds",
                          json={"upper_bps": "4"}, headers=auth).status_code == 400
            assert c.post("/api/config/thresholds",
                          json={"upper_bps": True}, headers=auth).status_code == 400
            # band must stay positive
            assert c.post("/api/config/thresholds",
                          json={"upper_bps": -1}, headers=auth).status_code == 400
            # nothing mutated on any rejected request
            assert cfg.upper_bps == 4.0
    finally:
        os.environ.pop("ARB_WEB_TOKEN", None)


def test_thresholds_update_requires_auth_and_engine():
    # no token
    eng = StubEngine()
    eng.cfg = make_cfg()
    with TestClient(make_app(make_cfg(), engine=eng)) as c:
        assert c.post("/api/config/thresholds",
                      json={"upper_bps": 6.5}).status_code == 403
    # token but no in-process engine (artifact mode)
    os.environ["ARB_WEB_TOKEN"] = "sekrit"
    try:
        with TestClient(make_app(make_cfg(), engine=None)) as c:
            r = c.post("/api/config/thresholds", json={"upper_bps": 6.5},
                       headers={"Authorization": "Bearer sekrit"})
            assert r.status_code == 409
            assert "no engine" in r.json()["detail"]
    finally:
        os.environ.pop("ARB_WEB_TOKEN", None)


def test_thresholds_update_persists_to_config_file():
    os.environ["ARB_WEB_TOKEN"] = "sekrit"
    try:
        cfg = make_cfg()
        eng = StubEngine()
        eng.cfg = cfg
        with TestClient(make_app(cfg, engine=eng)) as c:
            auth = {"Authorization": "Bearer sekrit"}
            r = c.post("/api/config/thresholds",
                       json={"upper_bps": 7.5, "persist": True}, headers=auth)
            assert r.status_code == 200, r.text
            assert r.json()["persisted"] is True
        # the yaml on disk now carries the new value (atomically replaced)
        with open(cfg.config_path) as fh:
            assert "upper_bps: 7.5" in fh.read()
        # a restart (fresh load) sees it too
        cfg2 = load_config(cfg.config_path, NO_ENV, symbol="SNDK",
                           hedge_venue="lighter-rh")
        assert cfg2.upper_bps == 7.5
    finally:
        os.environ.pop("ARB_WEB_TOKEN", None)


def test_thresholds_update_without_persist_leaves_file_alone():
    os.environ["ARB_WEB_TOKEN"] = "sekrit"
    try:
        cfg = make_cfg()
        eng = StubEngine()
        eng.cfg = cfg
        before = open(cfg.config_path).read()
        with TestClient(make_app(cfg, engine=eng)) as c:
            auth = {"Authorization": "Bearer sekrit"}
            r = c.post("/api/config/thresholds", json={"upper_bps": 9.0},
                       headers=auth)
            assert r.status_code == 200
            assert r.json()["persisted"] is False
        # in-memory change applied, but the file is untouched
        assert cfg.upper_bps == 9.0
        assert open(cfg.config_path).read() == before
    finally:
        os.environ.pop("ARB_WEB_TOKEN", None)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
