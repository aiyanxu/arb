#!/usr/bin/env python3
"""entropy-arb CLI — the package's command-line entry point.

    # collect minute data only — no strategy, no credentials needed
    entropy-arb --record-only            # markets come from config.yaml
    # equivalent: python -m entropy_arb --record-only

    # per-run override — wins over config.yaml
    entropy-arb --record-only --symbol SNDK --base entropy --hedge lighter-rh
    entropy-arb --record-only --symbol SNDK --base lighter --hedge entropy

    # LIVE trading: real orders, real money (needs .env credentials)
    entropy-arb

    # one-shot close BOTH legs' positions for a (base, hedge, symbol) pair
    entropy-arb flatten --symbol SNDK --base entropy --hedge lighter-rh

    # analyze recorded minute data -> suggested thresholds (no config needed)
    entropy-arb analyze [--db logs/minutes.duckdb] [--symbol SNDK] ...
    entropy-arb analyze --config config.yaml   # db/pair/fees from config.yaml

The markets you trade live in config.yaml (symbol:, base_venue:,
hedge_venue:); --symbol, --base and --hedge are optional per-run overrides
that win over the file. Either leg may be any of entropy / lighter /
lighter-rh / tradexyz / aster / polymarket (the two legs must differ;
base_venue defaults
to entropy). Venue-native symbol names can differ (e.g. trade.xyz lists SNDK
as TTSLA) — put those in symbol_map.yaml, loaded automatically when the
file exists. Add --cn for a Chinese-language dashboard. There is no paper
mode. Collect data with --record-only, set your thresholds with
`entropy-arb analyze`, then go live with small position caps.

On a terminal the bot shows a live Rich dashboard (books, signal, positions,
PnL, last executions) and writes log lines to logging.file; use
--no-dashboard for plain console logs (nohup/systemd). Strategy lives in
config.yaml, credentials in .env — see the README (English) /
README.zh-CN.md (中文).
"""
import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys

from entropy_arb.config import VENUES, ConfigError, load_config
from entropy_arb.engine import Engine


def setup_logging(level: str, log_file: str | None = None,
                  extra_handler: logging.Handler | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S")
    if log_file:
        d = os.path.dirname(log_file)
        if d:
            os.makedirs(d, exist_ok=True)
        h = logging.FileHandler(log_file)
    else:
        h = logging.StreamHandler()
    h.setFormatter(fmt)
    root.addHandler(h)
    if extra_handler is not None:
        root.addHandler(extra_handler)
    logging.getLogger("websockets").setLevel(logging.WARNING)


async def amain(cfg, record_only: bool, use_dashboard: bool, force_tty: bool,
                log_buffer, lang: str) -> None:
    eng = Engine(cfg, record_only=record_only)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, eng.request_stop)
    if not use_dashboard:
        await eng.run()
        return
    from entropy_arb.dashboard import Dashboard
    dash = Dashboard(eng, log_buffer, cfg.log_file, force_terminal=force_tty,
                     lang=lang)
    dash_task = asyncio.create_task(dash.run(), name="dashboard")
    try:
        await eng.run()
    finally:
        eng.request_stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(dash_task, timeout=5)
        if not dash_task.done():
            dash_task.cancel()


def analyze_entry(argv: list[str]) -> None:
    """`entropy-arb analyze [flags]` — the threshold analyzer.

    Runs before any config/.env loading: the analyzer reads the recorder's
    DuckDB only and needs neither config.yaml nor credentials.
    """
    from entropy_arb.analyze import main as analyze_main
    analyze_main(argv)


def flatten_entry(args) -> None:
    """`entropy-arb flatten --symbol S --base A --hedge B` — one-shot close.

    Loads config.yaml (+ .env credentials — flatten sends real orders),
    then closes both legs' actual exchange positions with reduce-only
    orders. Any per-run overrides win over config.yaml, same as the
    trading path. Never starts the strategy or the recorder.
    """
    from entropy_arb.flatten import run_flatten
    try:
        cfg = load_config(args.config, args.env_file,
                          symbol=args.symbol, base_venue=args.base,
                          hedge_venue=args.hedge,
                          symbol_map_file=args.symbol_map)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)
    if not cfg.creds_complete:
        print("flatten sends real orders and needs credentials for both "
              "venues in the env file / 清仓会发送真实订单，需要在环境文件中"
              "配置两个交易所的密钥", file=sys.stderr)
        sys.exit(2)
    setup_logging(cfg.log_level)
    try:
        asyncio.run(run_flatten(cfg))
    except (RuntimeError, ConfigError) as e:
        print(f"flatten failed: {e}", file=sys.stderr)
        sys.exit(1)


def web_entry(args) -> None:
    """`entropy-arb web [--host H] [--port P] [--record-only]` — dashboard.

    Runs the FastAPI backend (REST + /ws/live) with the built React frontend
    and, by default, an in-process engine (--record-only for a credential-
    free data-collection session) so the dashboard shows live state. The
    web server never sends orders itself; the trading path is untouched.
    """
    from entropy_arb.webapp import make_app
    try:
        cfg = load_config(args.config, args.env_file,
                          symbol=args.symbol, base_venue=args.base,
                          hedge_venue=args.hedge,
                          symbol_map_file=args.symbol_map)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)
    setup_logging(cfg.log_level)

    import uvicorn
    eng = Engine(cfg, record_only=args.record_only)
    app = make_app(cfg, engine=eng)
    logging.getLogger("web").warning(
        "dashboard on http://%s:%d — engine %s", args.host, args.port,
        "record-only" if args.record_only else "LIVE (real orders)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "web":
        p = argparse.ArgumentParser(
            prog="entropy-arb web",
            description="web dashboard: FastAPI backend + built React "
                        "frontend, with an in-process engine (real orders "
                        "unless --record-only). Read-only until "
                        "ARB_WEB_TOKEN is set in the env file — then "
                        "pause/resume/flatten are enabled under Bearer "
                        "auth; the server never opens positions (flatten "
                        "is reduce-only). / Web 仪表盘：FastAPI 后端 + "
                        "React 前端，内嵌引擎实时展示状态；设置 "
                        "ARB_WEB_TOKEN 后可暂停/恢复/一键平仓（只降风险，"
                        "不下开仓单）。")
        p.add_argument("--host", default="127.0.0.1",
                       help="bind address (default: 127.0.0.1)")
        p.add_argument("--port", type=int, default=8000,
                       help="port (default: 8000)")
        p.add_argument("--record-only", action="store_true",
                       help="run the embedded engine in record-only mode "
                            "(no credentials, no orders)")
        p.add_argument("--symbol", default=None,
                       help="override the symbol from config.yaml")
        p.add_argument("--base", default=None, choices=VENUES,
                       metavar="VENUE")
        p.add_argument("--hedge", default=None, choices=VENUES,
                       metavar="VENUE")
        p.add_argument("--config", default="config.yaml",
                       help="strategy config (default: config.yaml)")
        p.add_argument("--env-file", default=".env",
                       help="credentials file (default: .env)")
        p.add_argument("--symbol-map", default="symbol_map.yaml",
                       help="symbol -> venue symbol overrides")
        web_entry(p.parse_args(sys.argv[2:]))
        return

    p = argparse.ArgumentParser(
        description="Two-venue LIVE arbitrage: any of entropy / lighter / "
                    "lighter-rh / tradexyz / aster / polymarket as base, "
                    "any other as "
                    "hedge. Without --record-only, real orders are sent. "
                    "Subcommands: `analyze` (thresholds from recorded data), "
                    "`flatten` (close both legs), `web` (dashboard UI + "
                    "API).")
    if len(sys.argv) > 1 and sys.argv[1] == "analyze":
        analyze_entry(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "flatten":
        p = argparse.ArgumentParser(
            prog="entropy-arb flatten",
            description="one-shot close of BOTH legs' real exchange "
                        "positions for a (base, hedge, symbol) pair — "
                        "reduce-only takers with price protection, "
                        "retried until flat. Sends real orders: needs .env "
                        "credentials. / 一键清仓指定组合两腿的真实持仓，"
                        "只发 reduce-only 平仓单（价格保护），重复直到清零。"
                        "会发送真实订单，需要 .env 密钥。")
        p.add_argument("--symbol", required=True,
                       help="symbol whose position to close, e.g. SNDK")
        p.add_argument("--base", required=True, choices=VENUES,
                       metavar="VENUE",
                       help=f"base leg venue, one of: {', '.join(VENUES)}")
        p.add_argument("--hedge", required=True, choices=VENUES,
                       metavar="VENUE",
                       help="hedge leg venue, must differ from --base")
        p.add_argument("--config", default="config.yaml",
                       help="strategy config (default: config.yaml)")
        p.add_argument("--env-file", default=".env",
                       help="credentials file (default: .env)")
        p.add_argument("--symbol-map", default="symbol_map.yaml",
                       help="symbol -> venue symbol overrides "
                            "(default: symbol_map.yaml, missing file = no "
                            "overrides / symbol 映射表，文件不存在则为空)")
        flatten_entry(p.parse_args(sys.argv[2:]))
        return

    p = argparse.ArgumentParser(
        description="Two-venue LIVE arbitrage: any of entropy / lighter / "
                    "lighter-rh / tradexyz / aster / polymarket as base, "
                    "any other as "
                    "hedge. Without --record-only, real orders are sent. "
                    "Subcommand `analyze` suggests thresholds from recorded "
                    "data; `flatten` closes both legs' positions for a pair "
                    "instead of trading.")
    p.add_argument("--symbol", default=None,
                   help="override the symbol from config.yaml, e.g. SNDK / "
                        "覆盖 config.yaml 中的交易品种")
    p.add_argument("--base", default=None, choices=VENUES, metavar="VENUE",
                   help=f"override the base venue from config.yaml, one of: "
                        f"{', '.join(VENUES)} / 覆盖 config.yaml 中的 base 腿")
    p.add_argument("--hedge", default=None, choices=VENUES, metavar="VENUE",
                   help=f"override the hedge venue from config.yaml, one of: "
                         f"{', '.join(VENUES)} / 覆盖 config.yaml 中的"
                         f"对冲腿")
    p.add_argument("--config", default="config.yaml",
                   help="strategy config (default: config.yaml)")
    p.add_argument("--env-file", default=".env",
                   help="credentials file (default: .env)")
    p.add_argument("--symbol-map", default="symbol_map.yaml",
                   help="symbol -> venue symbol overrides "
                        "(default: symbol_map.yaml, missing file = no "
                        "overrides / symbol 映射表，文件不存在则为空)")
    p.add_argument("--record-only", action="store_true",
                   help="only collect minute data, run no strategy, send no "
                        "orders (needs no credentials)")
    p.add_argument("--cn", action="store_true",
                   help="display the dashboard in Chinese / 仪表盘使用中文")
    disp = p.add_mutually_exclusive_group()
    disp.add_argument("--dashboard", action="store_true",
                      help="force the Rich dashboard even without a tty")
    disp.add_argument("--no-dashboard", action="store_true",
                      help="plain console logs instead of the dashboard")
    args = p.parse_args()

    try:
        cfg = load_config(args.config, args.env_file,
                          symbol=args.symbol, base_venue=args.base,
                          hedge_venue=args.hedge,
                          symbol_map_file=args.symbol_map)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)

    use_dashboard = (cfg.dashboard or args.dashboard) and not args.no_dashboard
    force_tty = args.dashboard
    if use_dashboard and not (sys.stdout.isatty() or force_tty):
        use_dashboard = False

    log_buffer = None
    if use_dashboard:
        from entropy_arb.dashboard import BufferLogHandler
        log_buffer = BufferLogHandler()
        setup_logging(cfg.log_level, log_file=cfg.log_file,
                      extra_handler=log_buffer)
    else:
        setup_logging(cfg.log_level)

    try:
        asyncio.run(amain(cfg, record_only=args.record_only,
                          use_dashboard=use_dashboard, force_tty=force_tty,
                          log_buffer=log_buffer,
                          lang="zh" if args.cn else "en"))
    except RuntimeError as e:
        # startup failures (missing credentials, market not found, venue
        # unreachable) — a clean message, not a traceback
        print(f"startup error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
