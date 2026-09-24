"""entropy-arb web backend — FastAPI, REST + one WebSocket.

Serves the built React app (frontend/ -> webapp/static) and a JSON API:
live state, recent trades, minute bars, and the analyzer's threshold
suggestion, plus a live threshold hot-update. Risk-reducing control endpoints
(pause / resume / flatten / set thresholds the embedded engine) exist but are
DISABLED until ARB_WEB_TOKEN is set in the env file — requests then must carry
`Authorization: Bearer <token>`. No endpoint can open a position: flatten is
reduce-only, pause/resume only gate the strategy loop, the threshold update
only moves band numbers. Credential data never leaves the process.

Data sources:
  * an Engine running in-process (`entropy-arb web` starts one) — live
    books/positions/PnL straight from the objects
  * otherwise read-only artifacts: logs/minutes.duckdb (recorder bars),
    logs/trades.csv (fills), config.yaml (thresholds) — the dashboard still
    works while the bot runs in another process (docker/systemd). Control
    endpoints return 409 in this mode: an engine in another process can
    only be driven by its own process (use the CLI).

前后端分离的后端：FastAPI 提供 REST + 一个 WebSocket 实时推送，并托管
frontend/ 构建出的静态页面。既可内嵌引擎（`entropy-arb web` 启动）也可
纯文件模式（读取 duckdb/csv/config，适合 bot 在别的进程运行的场景）。
管理端点（暂停/恢复/平仓）默认关闭：在环境文件里设置 ARB_WEB_TOKEN
后以 Bearer token 启用；任何端点都不能开仓，平仓只发 reduce-only 单。
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import secrets
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, \
    WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from ..config import Config, persist_thresholds

log = logging.getLogger("web")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "static")

WS_INTERVAL_SEC = 2.0


def web_token(env_file: str = ".env") -> Optional[str]:
    """The control-endpoint token from the environment, or None (disabled).

    Read lazily per request via the loaded dotenv state, so tests can set
    os.environ directly and long-running servers pick up .env edits only
    on restart (load_dotenv does not override already-set vars).
    """
    tok = os.getenv("ARB_WEB_TOKEN")
    return tok.strip() if tok not in (None, "") else None


def require_token(req: Request) -> None:
    """Bearer-token gate for POST control endpoints (403 when unset/wrong)."""
    tok = web_token()
    if not tok:
        raise HTTPException(
            403, "control endpoints disabled — set ARB_WEB_TOKEN in the env "
                 "file to enable / 管理接口未启用：请在环境文件中设置 "
                 "ARB_WEB_TOKEN")
    auth = req.headers.get("authorization", "")
    supplied = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not supplied or not secrets.compare_digest(supplied, tok):
        raise HTTPException(403, "invalid or missing bearer token")


def require_engine(req: Request):
    """The in-process engine, or 409 — cross-process control is not
    supported (drive an engine in another process via the CLI)."""
    eng = req.app.state.engine
    if eng is None or getattr(eng, "base", None) is None:
        raise HTTPException(
            409, "no engine in this process — management needs `entropy-arb "
                 "web` (an engine running elsewhere can only be driven by "
                 "its own process, e.g. the flatten CLI) / 管理功能需要 "
                 "entropy-arb web 内嵌引擎")
    return eng


def csv_trades(path: str, limit: int) -> list:
    """Tail of the bot's trades.csv as dicts (newest last)."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return []
    out = []
    for r in rows[-limit:]:
        def f(key):
            try:
                return float(r.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0
        out.append({
            "ts": f(r.get("ts") or 0), "direction": r.get("direction"),
            "qty": f("qty"), "notional": f("buy_notional"),
            "prem_bps": f("marginal_premium_bps"),
            "exp": f("exp_edge_usd"), "fill": f("fill_edge_usd"),
            "ok": r.get("ok") == "True",
            "status": f"{r.get('buy_status')}/{r.get('sell_status')}",
        })
    return out


def venue_view(v, staleness_sec: float) -> dict:
    """One leg's public state (conf carries no credentials)."""
    return {
        "role": v.key, "name": v.name, "kind": v.kind,
        "symbol": v.conf.symbol, "fee_bps": v.fee_bps, "cap_usd": v.cap_usd,
        "bid": v.book.best_bid(), "ask": v.book.best_ask(),
        "mid": v.book.mid(),
        "fresh": v.book.is_fresh(staleness_sec),
        "position": v.position, "cash": v.cash, "equity": v.equity,
        "volume_usd": v.volume_usd,
        "last_traded_ts": v.last_traded_ts,
    }


def engine_snapshot(eng, cfg) -> dict:
    """Everything the dashboard shows for a live in-process engine.

    pnl / premium_bps / band / midline_bps belong to the engine object, not
    the snapshot root — the built React app reads them as `snap.engine.*`
    (frontend/src/types.ts EngineState). Keep the two in sync.
    """
    pnl = eng.session_pnl()
    return {
        "mode": "live",
        "symbol": cfg.symbol,
        "base_venue": cfg.base_venue,
        "hedge_venue": cfg.hedge_venue,
        "engine": {
            "running": not eng.stop.is_set(),
            "record_only": eng.record_only,
            "halted": eng.halted,
            "paused": eng.paused,
            "flatten_in_progress": eng.flatten_in_progress,
            "trades": eng.trades, "hedges": eng.hedges,
            "exp_edge_usd": eng.total_exp_edge,
            "fill_edge_usd": eng.total_fill_edge,
            "uptime_sec": time.time() - eng.start_ts,
            "pnl": pnl,
            "premium_bps": eng.premium_bps(),
            "band": [cfg.midline_bps - cfg.lower_bps,
                     cfg.midline_bps + cfg.upper_bps],
            "midline_bps": cfg.midline_bps,
        },
        "venues": [venue_view(v, cfg.staleness_sec)
                   for v in eng.venues.values()],
        "trades": list(eng.recent_trades)[-20:],
        "flatten_state": eng.flatten_state,
        "ts": time.time(),
    }


def snapshot(cfg: Config, engine) -> dict:
    """Live snapshot when an engine is attached; else artifact-mode stub."""
    if engine is None or getattr(engine, "base", None) is None:
        return {
            "mode": "artifact", "symbol": cfg.symbol,
            "base_venue": cfg.base_venue, "hedge_venue": cfg.hedge_venue,
            "engine": None, "venues": [], "trades": [], "ts": time.time()}
    return engine_snapshot(engine, cfg)


def make_app(cfg: Config, engine=None) -> FastAPI:
    web = FastAPI(title="entropy-arb")
    web.state.engine = engine
    web.state.cfg = cfg
    web.add_middleware(
        CORSMiddleware,
        # same-origin deployment + the vite dev server; the wildcard invited
        # any page the operator browses to read the (tokenless) API
        allow_origin_regex=r"https?://(127\.0\.0\.1|localhost)"
                           r"(:\d+)?",
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"])

    # in-flight webapp flatten task, so /api/positions/flatten can 409 on a
    # double click instead of stacking flatten rounds
    web.state.flatten_task = None

    # embedded engine lifecycle: run alongside the server so the dashboard
    # shows live books/positions; SIGINT/SIGTERM stop it cleanly
    if engine is not None:
        @web.on_event("startup")
        async def _start_engine():
            web.state.engine_task = asyncio.create_task(engine.run(),
                                                        name="engine")

        @web.on_event("shutdown")
        async def _stop_engine():
            engine.request_stop()
            t = getattr(web.state, "flatten_task", None)
            if t is not None:
                t.cancel()
            t = getattr(web.state, "engine_task", None)
            if t is not None:
                t.cancel()

    def _authed(req: Request):
        require_token(req)
        return require_engine(req)

    # ------------------------------------------------------------- routes

    @web.get("/api/health")
    async def health():
        return {"ok": True, "ts": time.time()}

    @web.get("/api/config")
    async def config_view():
        """Strategy-relevant config — credentials never appear here."""
        return {
            "symbol": cfg.symbol,
            "base_venue": cfg.base_venue,
            "hedge_venue": cfg.hedge_venue,
            "thresholds": {"midline_bps": cfg.midline_bps,
                           "upper_bps": cfg.upper_bps,
                           "lower_bps": cfg.lower_bps},
            "sizing": {"max_order_notional": cfg.max_order_notional,
                       "min_order_notional": cfg.min_order_notional,
                       "take_fraction": cfg.take_fraction},
            "fees": {"base": cfg.base.fee_bps, "hedge": cfg.hedge.fee_bps},
            "recorder_db": cfg.recorder_db,
            "threshold_check": cfg.threshold_check or {},
        }

    @web.get("/api/live")
    async def live_view():
        snap = snapshot(cfg, web.state.engine)
        if snap["engine"] is None:
            # artifact mode: a truthy stub so the dashboard still renders its
            # stats row. Every field the frontend reads must exist (null =
            # "—"), or React throws on the first undefined.
            snap["engine"] = {
                "running": False, "record_only": None, "halted": False,
                "paused": False, "flatten_in_progress": False,
                "trades": 0, "hedges": 0, "exp_edge_usd": 0.0,
                "fill_edge_usd": 0.0, "uptime_sec": 0.0,
                "pnl": None, "premium_bps": None, "band": [None, None],
                "midline_bps": None,
            }
        return snap

    @web.get("/api/trades")
    async def trades_view(limit: int = 100):
        eng = web.state.engine
        if eng is not None:
            return {"source": "engine",
                    "rows": list(eng.recent_trades)[-limit:]}
        return {"source": "csv", "rows": csv_trades(cfg.trades_csv, limit)}

    @web.get("/api/threshold-suggestion")
    async def threshold_suggestion():
        """The drift checker's comparison, computed on demand."""
        try:
            from .. import threshold_check as tc
            from ..analyze import load_rows
            from ..recorder import minute_table
            rows = load_rows(cfg.recorder_db,
                             minute_table(cfg.symbol, cfg.base_venue,
                                          cfg.hedge_venue),
                             cfg.threshold_check_hours,
                             cfg.threshold_check_min_samples,
                             cfg.symbol, cfg.base_venue, cfg.hedge_venue)
        except FileNotFoundError as e:
            return {"error": "no recorded data yet — run the bot first"}
        except Exception as e:
            return {"error": f"cannot read {cfg.recorder_db}: {e!r}"}
        if len(rows) < 30:
            return {"error": f"only {len(rows)} usable minute(s) — collect "
                             f"more data first"}
        fees = cfg.base.fee_bps + cfg.hedge.fee_bps
        sug = tc.suggested_thresholds(rows, fees=fees)
        current = {"midline_bps": cfg.midline_bps,
                   "upper_bps": cfg.upper_bps, "lower_bps": cfg.lower_bps}
        drifted = tc.drift_report(current, sug,
                                  cfg.threshold_check_tolerance)
        return {"suggested": sug, "current": current, "minutes": len(rows),
                "drifted": [{"key": k, "current": c, "suggested": s, "rel": r}
                            for k, c, s, r in drifted]}

    @web.get("/api/minutes")
    async def minutes_view(limit: int = 500):
        """Recent minute bars (premium close + executable edges) for charts."""
        try:
            from ..analyze import load_rows
            from ..recorder import minute_table
            rows = load_rows(cfg.recorder_db,
                             minute_table(cfg.symbol, cfg.base_venue,
                                          cfg.hedge_venue),
                             0.0, 0, cfg.symbol, cfg.base_venue,
                             cfg.hedge_venue)
        except Exception as e:
            return {"rows": [], "error": f"cannot read minutes db: {e!r}"}
        return {"rows": [{"ts": r["ts"], "prem": r["prem"],
                          "sell_max": r["sell_max"], "buy_max": r["buy_max"]}
                         for r in rows[-limit:]]}

    # -------------------------------------------------- control (POST)

    @web.post("/api/engine/pause")
    async def engine_pause(req: Request):
        """Stop opening new positions (hedging + reconcile keep running)."""
        eng = _authed(req)
        eng.request_pause()
        return {"ok": True, "paused": True}

    @web.post("/api/engine/resume")
    async def engine_resume(req: Request):
        """Resume the strategy loop (refused while the engine is halted)."""
        eng = _authed(req)
        if not eng.request_resume():
            raise HTTPException(409, "engine is HALTED — resume refused; "
                                     "flatten and restart")
        return {"ok": True, "paused": False}

    @web.post("/api/config/thresholds")
    async def thresholds_update(req: Request):
        """Hot-update the strategy thresholds mid-run. Validates and applies
        the provided keys in place on the shared Config, then pokes the
        strategy loop so the next evaluation uses them immediately (the
        engine reads them on every scan — no restart, no venue rebuild).

        POST (not PATCH): the CORS allow-list and every other control
        endpoint are POST. Without `persist`, the change is in-memory only
        and a restart reverts to config.yaml; with `"persist": true` it is
        also written back to config.yaml (comment-preserving) so a restart
        reloads exactly what is running now."""
        eng = _authed(req)
        try:
            body = await req.json()
        except Exception:
            raise HTTPException(400, "body must be a JSON object")
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be a JSON object")
        keys = ("midline_bps", "upper_bps", "lower_bps")
        unknown = [k for k in body if k not in keys and k != "persist"]
        if unknown:
            raise HTTPException(400, f"unknown key(s): {', '.join(unknown)}")
        persist = body.get("persist", False)
        if not isinstance(persist, bool):
            raise HTTPException(400, "persist must be a boolean")
        new: dict = {}
        for k in keys:
            if k in body:
                v = body[k]
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    raise HTTPException(400, f"{k} must be a number")
                new[k] = float(v)
        if not new:
            raise HTTPException(400, "no threshold keys provided")
        # same validity rules load_config enforces for the band
        if new.get("upper_bps", 1.0) <= 0:
            raise HTTPException(400, "upper_bps must be > 0")
        if new.get("lower_bps", 1.0) <= 0:
            raise HTTPException(400, "lower_bps must be > 0")
        prev = {k: getattr(cfg, k) for k in keys}
        # no await between these assigns → the engine reads all three as one
        # atomic group under the asyncio event loop (never a half-applied set)
        for k, v in new.items():
            setattr(cfg, k, v)
        eng._update_evt.set()  # a queued opportunity can fire at the new band
        current = {k: getattr(cfg, k) for k in keys}
        log.warning("thresholds updated via control API: %s -> %s%s", prev,
                    current, " (persisted)" if persist else "")
        if persist:
            try:
                persist_thresholds(cfg)
            except OSError as e:
                # hot-update already applied in memory; report the persistence
                # failure but don't fail the whole request — the strategy is
                # already running the new band, only the restart durability is
                # unavailable
                log.error("threshold persist failed: %r", e)
                return {"ok": True, "previous": prev, "current": current,
                        "persisted": False,
                        "persist_error": str(e)}
        return {"ok": True, "previous": prev, "current": current,
                "persisted": persist}

    @web.post("/api/positions/flatten")
    async def positions_flatten(req: Request):
        """Close BOTH legs' positions (reduce-only, like `entropy-arb
        flatten`). Runs in the background; poll /api/flatten/status or the
        ws snapshot for progress. Stays paused afterwards."""
        eng = _authed(req)
        if web.state.flatten_task is not None and \
                not web.state.flatten_task.done():
            raise HTTPException(409, "flatten already running")
        if getattr(eng, "record_only", False):
            raise HTTPException(409, "record-only session holds no "
                                     "credentials — nothing to flatten")
        web.state.flatten_task = asyncio.create_task(eng.flatten_all(),
                                                     name="web-flatten")
        return {"ok": True, "started": True}

    @web.get("/api/flatten/status")
    async def flatten_status():
        """Progress of the webapp-driven flatten (no auth: state only,
        mirrors what /ws/live already publishes)."""
        t = web.state.flatten_task
        state = {"running": bool(t is not None and not t.done()),
                 "state": getattr(web.state.engine, "flatten_state", None)
                 if web.state.engine is not None else None}
        if t is not None and t.done() and not t.cancelled():
            state["flat"] = t.result()
        return state

    # ------------------------------------------------------- websocket

    @web.websocket("/ws/live")
    async def ws_live(sock: WebSocket):
        """Push a snapshot every 2s; exits when the client disconnects
        (a dead socket raises on send)."""
        await sock.accept()
        try:
            while True:
                await sock.send_json(snapshot(cfg, web.state.engine))
                await asyncio.sleep(2.0)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.debug("ws closed: %r", e)

    # ------------------------------------------------------- static SPA

    if os.path.isdir(STATIC_DIR) and os.path.exists(
            os.path.join(STATIC_DIR, "index.html")):
        web.mount("/", StaticFiles(directory=STATIC_DIR, html=True),
                  name="static")
    else:
        @web.get("/")
        async def no_frontend():
            return {"hint": "frontend not built — run: cd frontend && "
                            "npm install && npm run build",
                    "api": ["/api/health", "/api/config", "/api/live",
                            "/api/trades", "/api/minutes",
                            "/api/threshold-suggestion", "/ws/live"]}

    return web
