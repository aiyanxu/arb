"""entropy-arb web backend — FastAPI, REST + one WebSocket.

Serves the built React app (frontend/ -> webapp/static) and a JSON API:
live state, recent trades, minute bars, and the analyzer's threshold
suggestion. Read-only by design: no order endpoints, no credential data —
the trading path stays in the engine/CLI.

Data sources:
  * an Engine running in-process (`entropy-arb web` starts one) — live
    books/positions/PnL straight from the objects
  * otherwise read-only artifacts: logs/minutes.duckdb (recorder bars),
    logs/trades.csv (fills), config.yaml (thresholds) — the dashboard still
    works while the bot runs in another process (docker/systemd).

前后端分离的后端：FastAPI 提供 REST + 一个 WebSocket 实时推送，并托管
frontend/ 构建出的静态页面。既可内嵌引擎（`entropy-arb web` 启动）也可
纯文件模式（读取 duckdb/csv/config，适合 bot 在别的进程运行的场景）。
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from ..config import Config

log = logging.getLogger("web")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "static")

WS_INTERVAL_SEC = 2.0


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
    """Everything the dashboard shows for a live in-process engine."""
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
            "trades": eng.trades, "hedges": eng.hedges,
            "exp_edge_usd": eng.total_exp_edge,
            "fill_edge_usd": eng.total_fill_edge,
            "uptime_sec": time.time() - eng.start_ts,
        },
        "pnl": pnl,
        "premium_bps": eng.premium_bps(),
        "band": [cfg.midline_bps - cfg.lower_bps,
                 cfg.midline_bps + cfg.upper_bps],
        "midline_bps": cfg.midline_bps,
        "venues": [venue_view(v, cfg.staleness_sec)
                   for v in eng.venues.values()],
        "trades": list(eng.recent_trades)[-20:],
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
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
        allow_headers=["*"])

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
            t = getattr(web.state, "engine_task", None)
            if t is not None:
                t.cancel()

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
            snap["engine"] = {"running": False, "record_only": None}
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
