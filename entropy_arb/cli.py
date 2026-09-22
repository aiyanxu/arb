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
    entropy-arb flatten                 # pair comes from config.yaml

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
import subprocess
import sys
import time

from entropy_arb.config import VENUES, ConfigError, load_config
from entropy_arb.engine import Engine


# Live-trading pid files live in /tmp: written by the trading path on
# startup, read by `flatten` so it can kill the running bot before closing
# positions (a flatten while the engine still trades would race it).
# 任意 venue 均可作为 base 或 hedge 腿，文件名用 launch 时的 venue 名。
def pid_file_path(symbol: str, base: str, hedge: str) -> str:
    return f"/tmp/entropy-arb-{symbol}-{base}-{hedge}.pid"


def write_pid_file(cfg) -> str | None:
    """Record this process's pid for the live (symbol, base, hedge) pair.

    Fails fast when another live entropy-arb process already holds the
    pair's pid file: two bots on one pair trade the same accounts (and on
    Lighter, the same nonce sequence) — that collision is how the
    'invalid nonce' storms happened. A stale file (process gone or pid
    reused by another program) is still just overwritten — pid files are
    only advisory, an unwritable file never stops a record-only run.
    """
    path = pid_file_path(cfg.symbol, cfg.base_venue, cfg.hedge_venue)
    old_pid = read_pid_file(path)
    if old_pid is not None and old_pid != os.getpid() \
            and _looks_like_our_bot(old_pid):
        raise RuntimeError(
            f"another entropy-arb process (pid {old_pid}) is live for "
            f"{cfg.symbol}/{cfg.base_venue}/{cfg.hedge_venue} — stop it "
            f"(or `entropy-arb flatten`) before starting a second bot on "
            f"the same pair / 已有进程 (pid {old_pid}) 正在运行同一交易对，"
            f"请先停止它再启动 / pid file: {path}")
    try:
        with open(path, "w") as fh:
            fh.write(f"{os.getpid()}\n")
        logging.getLogger("cli").info("pid %d written to %s",
                                      os.getpid(), path)
        return path
    except OSError as e:
        logging.getLogger("cli").warning("could not write pid file %s: %s",
                                         path, e)
        return None


def read_pid_file(path: str) -> int | None:
    """Read a pid from a pid file; None when missing/corrupt."""
    try:
        with open(path) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _looks_like_our_bot(pid: int) -> bool:
    """True when `pid` is a live entropy-arb process.

    A pid file can outlive its bot — /tmp is cleaned lazily and pids get
    reused — so before signaling anything, verify the recorded process
    really is an entropy-arb process (ps shows the command line). Anything
    else (gone, or reused by another program) is ignored, not killed.
    """
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             timeout=5, capture_output=True, text=True).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    cmd = out.strip()
    return "entropy_arb" in cmd or "entropy-arb" in cmd


def kill_running_bot(path: str) -> None:
    """Stop the live bot recorded in a pid file, if it is still running.

    SIGTERM gives the engine's signal handlers (eng.request_stop) a clean
    shutdown; after a bounded wait a stubborn process gets SIGKILL. A pid
    that is gone or was reused by another program is ignored, and the file
    is removed once its process is gone.
    """
    pid = read_pid_file(path)
    if pid is None or pid == os.getpid():
        return
    if not _looks_like_our_bot(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as e:
        print(f"cannot stop the running bot (pid {pid}): {e}",
              file=sys.stderr)
        return
    for _ in range(50):                     # up to ~5 s for a clean exit
        time.sleep(0.1)
        try:
            os.kill(pid, 0)                 # still alive?
        except ProcessLookupError:
            break
        except PermissionError:
            break                           # alive, not ours to signal again
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    with contextlib.suppress(OSError):
        os.remove(path)


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
    pid_path = None
    if not record_only:
        # live trading: publish our pid so `flatten` can find and stop this
        # process before closing positions
        pid_path = write_pid_file(cfg)
    eng = Engine(cfg, record_only=record_only)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, eng.request_stop)
    if not use_dashboard:
        try:
            await eng.run()
        finally:
            if pid_path:
                with contextlib.suppress(OSError):
                    os.remove(pid_path)
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
        if pid_path:
            with contextlib.suppress(OSError):
                os.remove(pid_path)


def analyze_entry(argv: list[str]) -> None:
    """`entropy-arb analyze [flags]` — the threshold analyzer.

    Runs before any config/.env loading: the analyzer reads the recorder's
    DuckDB only and needs neither config.yaml nor credentials.
    """
    from entropy_arb.analyze import main as analyze_main
    analyze_main(argv)


def flatten_entry(args) -> None:
    """`entropy-arb flatten [--symbol S --base A --hedge B]` — one-shot close.

    The pair comes from config.yaml (symbol:, base_venue:, hedge_venue:);
    --symbol / --base / --hedge are optional per-run overrides that win over
    the file, same as the trading path. Loads config.yaml (+ .env
    credentials — flatten sends real orders), then closes both legs' actual
    exchange positions with reduce-only orders. Never starts the strategy
    or the recorder.
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
    # if the live bot is running for this pair, stop it first — a flatten
    # while the engine still trades would race the close orders
    kill_running_bot(pid_file_path(cfg.symbol, cfg.base_venue,
                                   cfg.hedge_venue))
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
    # live web sessions publish their pid too, so the flatten CLI can stop
    # the dashboard+engine the same way it stops the plain trading process
    pid_path = None
    try:
        pid_path = None if args.record_only else write_pid_file(cfg)
    except RuntimeError as e:
        print(f"startup error: {e}", file=sys.stderr)
        sys.exit(1)
    logging.getLogger("web").warning(
        "dashboard on http://%s:%d — engine %s", args.host, args.port,
        "record-only" if args.record_only else "LIVE (real orders)")
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        if pid_path:
            with contextlib.suppress(OSError):
                os.remove(pid_path)


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
        p.add_argument("--symbol", default=None,
                       help="override the symbol from config.yaml, e.g. SNDK "
                            "(required only when config.yaml has no "
                            "symbol / 未指定时读取 config.yaml 的 symbol)")
        p.add_argument("--base", default=None, choices=VENUES,
                       metavar="VENUE",
                       help=f"override the base venue from config.yaml, one "
                            f"of: {', '.join(VENUES)} / 未指定时读取 "
                            f"config.yaml 的 base_venue")
        p.add_argument("--hedge", default=None, choices=VENUES,
                       metavar="VENUE",
                       help="override the hedge venue from config.yaml "
                            "(must differ from --base) / 未指定时读取 "
                            "config.yaml 的 hedge_venue")
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
