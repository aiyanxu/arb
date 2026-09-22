# entropy-arb

**[中文文档 / Chinese documentation → README.zh-CN.md](README.zh-CN.md)**

Open-source two-venue perp arbitrage bot. Both legs are configurable — either
can be any of the six supported venues (the two legs must differ); the
**base** leg is the premium's numerator, the **hedge** leg its denominator
(`base_venue` defaults to `entropy`):

| venue | venue | quote | taker fee | protocol |
|---|---|---|---|---|
| `entropy` | Entropy on Hyperliquid | USDC | 0 bps | HL l2Book (dex `io`) |
| `lighter` | Lighter mainnet | USDC | 0 bps | zkLighter ws (diff books, async settle) |
| `lighter-rh` | Lighter Robinhood chain | **USDG** | 0 bps | zkLighter ws |
| `tradexyz` | Hyperliquid trade.xyz dex | USDC | ~1 bps | HL l2Book, sync IOC settle |
| `aster` | Aster DEX V3 perps | USDT | ~4.5 bps | Aster fapi ws (top-20 snapshots), sync IOC settle |
| `polymarket` | Polymarket Perps | pUSD | ~4 bps | Polymarket perps ws (100 ms full-book snapshots), IOC + REST poll settle |

> **Referral links** — signing up through these supports this project:
> - Entropy — Tier 4 referral, 100% rebates: <https://entropy.io/?r=yourquantguy>
> - Lighter Robinhood chain: <https://robinhoodchain.lighter.xyz/?referral=QUANT>
> - trade.xyz (Hyperliquid): <https://app.hyperliquid.xyz/join/QUANTGUY>

When the same symbol trades rich on one venue and cheap on the other, the bot
simultaneously sells the rich book and buys the cheap book with taker orders,
carrying a delta-neutral position until the premium reverts and the opposite
crossing unwinds it. Every price it acts on is the **actual order book of the
exchange that will fill the order** — Hyperliquid books come from the official
websocket (`wss://api.hyperliquid.xyz/ws`), Lighter books from Lighter's
official websocket.

While it runs — even with no credentials and no strategy — it records both
books to **1-minute DuckDB bars**, and the bundled analyzer turns that data
into the three numbers that define the whole strategy.

## The signal

The band is three numbers in `config.yaml`, derived by you from recorded
data — **measured for one (base, hedge) pair**: swap either leg and the
numbers no longer apply, re-run the analyzer:

```
premium_bps = (base price / hedge price − 1) × 10 000

                          ┌──────────────  SELL base + BUY hedge
midline + upper  ───────────────────────────────────────────────────
                                       ▲
midline          ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┼ ─ ─   the premium's usual level
                                       ▼
midline − lower  ───────────────────────────────────────────────────
                          └──────────────  BUY base + SELL hedge
```

- `midline_bps` — where the premium normally sits. Cross-venue premiums are
  rarely centered at zero (different oracles, different quote assets, listing
  premia), so a zero-centered band would fire one direction only, cap out and
  never unwind. Measure where the premium actually sits and type it in.
- `upper_bps` / `lower_bps` — the entry bands on each side of the midline.

Both hurdles are applied to **executable** prices (base bid vs hedge ask,
and vice versa) and are **net of both venues' taker fees** — the engine adds
fees on top before a slice qualifies. A full round trip therefore nets
**≥ upper + lower bps after fees by construction**.

One consequence worth understanding: with `midline_bps: 5`, the buy-base
hurdle is `lower − midline`, which can be **negative**. That is intentional —
if the base leg is persistently 5 bps rich, buying it at a 0 bps premium is
5 bps cheap versus its own equilibrium, and that trade is the profitable
unwind of an earlier sell at `midline + upper`. It also means a **wrong
midline loses money**: if you type `midline_bps: 5` while the true premium
sits at 0, the bot happily buys the base leg at fair value all day. Measure
first, then trade — that is what the recorder and analyzer are for.

## Quick start

```bash
git clone https://github.com/your-quantguy/entropy-arb.git && cd entropy-arb
python3 -m venv .venv && source .venv/bin/activate
pip install -e .          # data collection needs only this

cp config.example.yaml config.yaml       # the strategy (thresholds, sizing, risk)
cp .env.example .env                     # credentials — required to trade
```

The markets live in `config.yaml`: `symbol` (traded on both venues),
`base_venue` and `hedge_venue` (each one of `entropy`, `lighter`,
`lighter-rh`, `tradexyz`, `aster`, `polymarket`; the two legs must differ;
`base_venue` defaults to
`entropy`). The `--symbol` / `--base` / `--hedge` flags override them for a
single run.

There is **no paper mode** — the bot either collects data (`--record-only`)
or trades live. Validate with recorded data and tiny position caps, not with
simulated fills.

**1. Collect data first** (no credentials needed):

```bash
entropy-arb --record-only                  # markets from config.yaml
entropy-arb --record-only --symbol SNDK --base entropy --hedge lighter-rh
entropy-arb --record-only --symbol SNDK --base lighter --hedge entropy
```

Let it run for at least a few hours (a day is better — premiums have
intraday regimes). It writes `logs/minutes.duckdb` (DuckDB, one table per
(symbol, base, hedge) combination).

**2. Analyze and set your thresholds:**

```bash
entropy-arb analyze                        # == python3 tools/analyze.py
```

It prints the premium distribution, how often each candidate band would have
fired, and a ready-to-paste `thresholds:` block for `config.yaml`. Pass
`--config config.yaml` to take the defaults from the strategy file: the
recorder db, the symbol and both venues, and the pair's actual taker fees
(`--db` / `--symbol` / `--base-venue` / `--hedge-venue` / `--fees-bps` still
override it per flag):

```bash
entropy-arb analyze --config config.yaml
```

**3. Go live** — fill in `.env`, install the signing SDKs, and start with
the smallest position caps that clear the venue minimums:

```bash
pip install -e ".[live]"
entropy-arb              # or with overrides: --symbol SNDK --base entropy --hedge lighter-rh
```

Running without `--record-only` sends real orders immediately once both
feeds are fresh and the band is crossed.

**One-shot flatten.** To close both legs' actual exchange positions for a
pair — e.g. after a HALT, a crash mid-position, or before tearing a market
down — without starting the strategy:

```bash
entropy-arb flatten --symbol SNDK --base entropy --hedge lighter-rh
# or take the pair from a config file (--symbol/--base/--hedge still override):
entropy-arb flatten --config config-btc.yaml
```

It reads each venue's real position, sends reduce-only taker orders with
`hedge_slippage_bps` price protection against the live book, and retries
until both venues are flat (exit 0); residual dust below
`net_tolerance_base` counts as flat. Needs credentials (it sends real
orders); the strategy and recorder are never started.

**Stop the running bot first.** A live run (not `--record-only`) writes its
pid to `/tmp/entropy-arb-<symbol>-<base>-<hedge>.pid` on startup and removes
it on exit. `entropy-arb flatten` for the same pair reads that file and
stops the bot (SIGTERM, then SIGKILL after a bounded wait) before closing
positions — so a flatten can't race the engine's own orders. The pid file
never blocks trading: an unwritable or stale file is a warning, and flatten
verifies the recorded process really is entropy-arb (via `ps`) before
signaling it, ignoring gone or reused pids.

**Dashboard.** On a terminal the bot shows a live Rich dashboard: both
books with age/spread, positions and caps, equity and session PnL, the
executable premium of each direction against its full hurdle (fees and
inventory surcharge included, ● = armed), recorder progress, the last
executions, and a tail of the log (the full log goes to `logging.file`,
default `logs/engine.log`). It works in `--record-only` too. Add `--cn` to
display the dashboard in Chinese. Use `--no-dashboard` for plain console
logs (nohup/systemd — off-terminal runs fall back automatically), or set
`logging.dashboard: false`.

**Telegram notifications (capability, opt-in by code).** The `notify`
module provides a one-shot send API — nothing is forwarded automatically:

```python
from entropy_arb import notify
notify.send("base +2 -> 0, hedge -1.5 -> 0 — both legs flat")
```

Setup: create a bot with @BotFather, put `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` in `.env` (see `.env.example`). Without credentials every
`send()` is a silent no-op; with them, messages are throttled to ~1/s
(Telegram's per-chat limit) by a background thread and never block or raise
— notification failures cannot affect trading. `notify.drain()` waits for
pending sends at shutdown. Where/when to call `send()` is up to you.

One built-in caller: with `threshold_check.enabled: true` in `config.yaml`,
a scheduled task re-derives the analyzer's suggested thresholds from the
recorded minute data every `interval_sec` (default 30 min) and sends a
Telegram notification when any of `midline_bps` / `upper_bps` / `lower_bps`
drifts more than `tolerance` (10%) from the configured value — only a
notification; config.yaml is never touched.

notification; config.yaml is never touched.

## Web dashboard (React + FastAPI)

A separate front/back-end web app ships with the bot:

- **Backend** (`entropy_arb/webapp/`, FastAPI): JSON API (`/api/health`,
  `/api/config`, `/api/live`, `/api/trades`, `/api/minutes`,
  `/api/threshold-suggestion`, `/api/flatten/status`) plus a `/ws/live`
  WebSocket pushing a full snapshot every 2s. No credential data ever
  leaves the process; the trading path stays in the engine/CLI.
- **Frontend** (`frontend/`, React 19 + TypeScript + Vite): single-page
  dashboard — engine mode/HALT/PAUSED/uptime, premium vs band, session PnL
  and edges, per-venue book/position cards, recent executions, threshold
  suggestion with drift highlighting, and a controls card (see below).
  Live updates over the websocket with auto-reconnect.

Run it (installs fastapi/uvicorn, starts an in-process engine):

```bash
pip install -e ".[web]"
entropy-arb web --record-only         # data-collection session + dashboard on :8000
entropy-arb web                       # live engine attached (real orders!)
# open http://127.0.0.1:8000
```

Without an embedded engine (point the tool at a config while the bot runs
elsewhere) the dashboard degrades to artifact mode: recorded minute bars,
trades.csv fills, config thresholds — everything except live books (and
the controls, which need the engine in-process).

**Position controls.** With an embedded engine the dashboard can also
pause the strategy and close positions — both risk-*reducing* operations,
never risk-opening:

- `POST /api/engine/pause` / `resume` — stop/restart new entries; hedging
  and reconcile keep running either way. Resume is refused while the
  engine is HALTED.
- `POST /api/positions/flatten` — close BOTH legs with reduce-only IOC
  takers (the same code as `entropy-arb flatten`), retried until flat.
  The engine stays paused afterwards; re-arm explicitly with resume.
- Progress (round counter, flat/not-flat) is published in the ws snapshot
  and `GET /api/flatten/status`.

Controls are **disabled by default**: set `ARB_WEB_TOKEN=<random secret>`
in `.env` and send `Authorization: Bearer <token>` (the web UI asks for
it once and stores it in localStorage). Keep the server on 127.0.0.1 or
behind an authenticated reverse proxy with TLS. If the bot runs in
another process (docker/systemd), manage positions from that process
with the CLI — the web app in artifact mode answers 409 on control
endpoints.

To change the frontend: edit `frontend/src`, `npm run build` inside
`frontend/`, then copy `frontend/dist/` to `entropy_arb/webapp/static/`
(or ship the API alone — the built SPA is optional).

## Docker

The image contains no secrets or config — `config.yaml`, `symbol_map.yaml`
and `.env` are mounted at runtime (excluded by `.dockerignore`).

Build both variants:

```bash
docker build --target record-only -t entropy-arb:record-only .   # base deps only
docker build --target live       -t entropy-arb:live .           # + signing SDKs
```

Record-only data collection (no credentials needed):

```bash
mkdir -p logs
docker run --rm \
  -v ./config.yaml:/app/config.yaml:ro \
  -v ./symbol_map.yaml:/app/symbol_map.yaml:ro \
  -v ./logs:/app/logs \
  entropy-arb:record-only
```

Live trading, headless and restarted on failure:

```bash
docker run -d --name entropy-arb \
  --restart unless-stopped --init --stop-grace-period 30s \
  --env-file .env \
  -v ./config.yaml:/app/config.yaml:ro \
  -v ./symbol_map.yaml:/app/symbol_map.yaml:ro \
  -v ./logs:/app/logs \
  entropy-arb:live --no-dashboard
```

Or with docker compose (recommended — see `docker-compose.yml`):

```bash
cp config.example.yaml config.yaml && cp .env.example .env   # fill both in
docker compose up -d --build                    # live
docker compose logs -f                          # console log
docker compose --profile record up -d --build   # record-only variant instead
docker compose down                             # SIGTERM -> graceful shutdown
```

Notes:

- Recorded data lands on the host in `./logs` (`minutes.duckdb`,
  `trades.csv`). In `--no-dashboard` mode there is **no** `logs/engine.log`
  — console output goes to `docker logs` / `docker compose logs` instead.
- No ports are exposed (the bot is outbound-only). The default image command
  is the safe `--record-only --no-dashboard`; going live is always an
  explicit `command:` / argument override.
- The Rich dashboard cannot render over `docker logs`; use
  `docker compose logs -f`, or run outside Docker for the dashboard.
- Rebuild the image after code changes. For reproducible live deploys pin
  the `lighter-sdk` commit in `pyproject.toml` (see the comment there).
- On a Linux host, `./logs` must be writable by uid 1000 — add
  `user: "1000:1000"` to the compose service (Docker Desktop maps this).
- Analyze recorded data inside the container:

```bash
docker run --rm --entrypoint entropy-arb -v ./logs:/app/logs \
  entropy-arb:record-only analyze
```

## Data collection & analysis

The recorder runs automatically in every mode (`recorder.enabled: true`).
Once per second it samples both live books; once per minute it writes a row
to a DuckDB database (default `logs/minutes.duckdb`, key
`recorder.db`). Each (symbol, base_venue, hedge_venue) combination gets its
own table, named `minutes_<symbol>__<base>__<hedge>` (parts lowercased,
characters outside `[a-z0-9_]` replaced with `_` — e.g. SNDK on
entropy×lighter-rh → `minutes_sndk__entropy__lighter_rh`); running several
combinations against the same db file keeps their data fully separated. All
tables share the layout below:

| column | meaning |
|---|---|
| `minute_ts`, `time_utc` | minute start (epoch seconds, ISO UTC) |
| `symbol`, `base_venue`, `hedge_venue` | combination this row belongs to (part of the primary key) |
| `base_bid/ask`, `hedge_bid/ask` | last fresh top-of-book of the minute |
| `premium_open/high/low/close/mean/std_bps` | mid-to-mid premium of the base over the hedge |
| `sell_edge_mean/max_bps` | executable premium for SELL base (base bid / hedge ask − 1) |
| `buy_edge_mean/max_bps` | executable premium for BUY base (hedge bid / base ask − 1) |
| `samples` | how many of the ~60 seconds both books were fresh |

Recorded edges are pre-fee; the analyzer subtracts `--fees-bps` (pass the
**sum** of both venues' taker fees — default 0.0 for the zero-fee venues,
~1.0 with a `tradexyz` leg) before counting firings, so its table and
suggestions translate directly into config values. With `--config` the fee
sum, db and pair come from the config file automatically (an explicit
`--fees-bps` still wins). `--hours 24` restricts to
recent data; premiums drift, so re-run it regularly and update
`config.yaml`. Premiums are pair-relative — re-measure whenever either leg's
venue changes.

### Querying the data / 查询数据

The database is a plain DuckDB file — query it directly:

```bash
duckdb logs/minutes.duckdb   # or: python3 -m duckdb logs/minutes.duckdb
```

```sql
SELECT minute_ts, premium_close_bps, sell_edge_max_bps
FROM minutes_sndk__entropy__lighter_rh
WHERE symbol = 'SNDK' AND base_venue = 'entropy'
  AND hedge_venue = 'lighter-rh'
ORDER BY minute_ts DESC LIMIT 20;
```

Rows are keyed on `(symbol, base_venue, hedge_venue, minute_ts)` and written
with `INSERT OR REPLACE`, so restarting the bot never duplicates a minute.
The recorder releases the database between minute writes, so the file can
be queried while the bot is running.

Data recorded before the per-combination split lives in older-shaped tables
(the shared `minutes` table and per-symbol `minutes_<symbol>` tables), which
the analyzer ignores. Migrate once (stop the bot first) — every migrated row
is stamped `base_venue='entropy'` (the historical base leg):

```bash
python3 tools/migrate_per_symbol.py --db logs/minutes.duckdb [--drop-old]
```

The script is idempotent and keeps the old tables unless `--drop-old`
is passed.

Migrating an old CSV (`logs/minutes.csv` from before this change): rows lack
the venue columns, so fill in the values you ran with:

```sql
-- duckdb logs/minutes.duckdb, with the old CSV in place
INSERT OR REPLACE INTO minutes_sndk__entropy__lighter_rh
SELECT minute_ts, time_utc, 'SNDK', 'entropy', 'lighter-rh',
       * EXCLUDE (minute_ts, time_utc)
FROM read_csv('logs/minutes.csv');
```

## Configuration

Strategy and the markets live in `config.yaml` (validated — unknown keys are
startup errors), credentials in `.env`. Full commented reference:
[config.example.yaml](config.example.yaml). The essentials:

| key | meaning | default |
|---|---|---|
| `symbol` | market traded on both legs | — |
| `base_venue` | the base leg (premium numerator): `entropy` / `lighter` / `lighter-rh` / `tradexyz` / `aster` / `polymarket` | `entropy` |
| `hedge_venue` | the hedge leg (premium denominator): any venue ≠ `base_venue` | — |
| `thresholds.midline_bps` | premium center for THIS pair (measure it!) | — |
| `thresholds.upper_bps` / `lower_bps` | entry bands (> 0) | — |
| `base.dex` / `hedge.dex` | dex name for a Hyperliquid leg | `io` / `xyz` |
| `*.taker_fee_bps` | per-leg taker fee (must not be below the venue default) | per venue |
| `*.max_position_usd` | per-leg position cap | 1000 |
| `*.max_orders_per_min` | per-leg send budget (sliding 60 s) | per venue (120; lighter 30) |
| `sizing.take_fraction` | fraction of crossable depth taken | 0.5 |
| `sizing.max_order_notional_usd` | per-slice cap | 500 |
| `inventory.scale_bps` / `floor_frac` | inventory ladder (extra bps past `floor_frac` of the cap) | 10 / 0.5 |
| `execution.premium_persist_sec` | edge must persist before firing | 0.3 |
| `execution.*` | slippage bounds, timeouts, reconcile cadence… | see file |
| `recorder.*` | minute-data recorder | on, `logs/minutes.duckdb` |
| `logging.dashboard` / `logging.file` | Rich dashboard on a tty; log file while it runs | on, `logs/engine.log` |

## Credentials (`.env`, live only)

Credentials are keyed by **venue** — whichever leg a venue is on, it reads
its own block.

- **entropy / tradexyz (Hyperliquid)** — create an API ("agent") wallet at
  <https://app.hyperliquid.xyz/API>. `HL_PRIVATE_KEY` is the **agent** key,
  `HL_ACCOUNT_ADDRESS` your main account address. When both legs are
  Hyperliquid venues (e.g. entropy×tradexyz, either order) they share this
  account by default (one nonce sequence is handled internally); set
  `HL_PRIVATE_KEY_XYZ` / `HL_ACCOUNT_ADDRESS_XYZ` to split them. Fund the
  dex-specific clearinghouses you trade.
- **lighter** (mainnet) — `LIGHTER_ACCOUNT_INDEX`, `LIGHTER_API_KEY_INDEX`,
  `LIGHTER_API_PRIVATE_KEY`.
- **lighter-rh** (Robinhood chain) — `LIGHTER_RH_ACCOUNT_INDEX`,
  `LIGHTER_RH_API_KEY_INDEX`, `LIGHTER_RH_API_PRIVATE_KEY`.
  Mainnet and the Robinhood chain are separate accounts and separate keys
  (see [lighter-python](https://github.com/elliottech/lighter-python)).
  Either may be base or hedge — each reads its own block by venue name.
- **Lighter nonce coordination** — the bot manages order nonces itself
  (server-seeded, strictly increasing, `skip_nonce` mode) instead of the
  SDK's default manager, whose failure-decrement caused the
  `code=21104 invalid nonce` storms. Each Lighter account+key gets one
  counter per process. When **several processes trade the same Lighter
  account** (several symbol pairs, or a CLI bot plus a live
  `entropy-arb web` session), set `LIGHTER_NONCE_REDIS_URL` (e.g.
  `redis://localhost:6379/0`; the compose stack runs a Redis and sets it
  automatically) so they share one counter instead of colliding. Starting
  a second live bot on the *same pair* is refused at startup.
- **polymarket** — run `python tools/polymarket_make_proxy.py
  --owner-key 0x...` once: it generates a fresh PROXY keypair and has
  your OWNER wallet EIP-712-sign the createProxy ceremony. It prints
  `POLYMARKET_PROXY_ADDRESS` / `POLYMARKET_PROXY_PRIVATE_KEY` /
  `POLYMARKET_PROXY_SECRET` for `.env`. The credential expires after
  ~1 week — re-run the script and replace all three together (an
  operational cadence to plan for). Trading signs with the PROXY key,
  never the owner key.
- **aster** — create a Pro API wallet at
  <https://www.asterdex.com/en/api-wallet> (switch to "Pro API" at the top).
  `ASTER_PRIVATE_KEY` is the **API (agent) wallet's** key;
  `ASTER_ACCOUNT_ADDRESS` is your **main wallet address** — it participates
  in every signature and cannot be derived from the API key, so both are
  required. The account must be in **one-way** position mode (hedge mode is
  refused at startup), and the host clock should be NTP-synced (signed
  requests carry a microsecond nonce).

## How execution works

- Both legs are **taker** orders sent concurrently: Lighter market orders
  with average-price protection settling on the authenticated account
  websocket; Hyperliquid IOC limits settling synchronously (with
  orderStatus polling for unknown outcomes).
- A **persistence gate** (`premium_persist_sec`) arms each direction and only
  fires if the edge survives — one-tick phantoms are filtered.
- **Inventory ladder**: past `floor_frac` of a venue's cap, adding to the
  position requires linearly more edge, up to `scale_bps` extra at the cap.
- **Net-delta hedge**: if legs fill unevenly, the imbalance is immediately
  reduced (reduce-only, price-protected), and positions are reconciled
  against the chain every `reconcile_sec`.
- **Failure containment**: a rate-limited venue pauses briefly; an
  unreachable venue (e.g. exchange maintenance) pauses trading and is probed
  every `venue_probe_sec` until it recovers; `max_consecutive_errors`
  execution pathologies halt the engine entirely.
- **Live-only**: there is no simulated-fill mode. `--record-only` is the
  risk-free way to run it; anything else trades real money.

## Layout

```
entropy_arb/cli.py       CLI entry — entropy-arb / python -m entropy_arb
entropy_arb/__main__.py  python -m entropy_arb launcher
entropy_arb/config.py    YAML + .env contract, validation
entropy_arb/book.py      order books + fee-aware crossing/sizing math
entropy_arb/feeds.py     official HL ws + zkLighter ws + Aster ws + Polymarket ws book feeds
entropy_arb/venue_hl.py  Hyperliquid dex adapter (Entropy, tradexyz)
entropy_arb/venue_lighter.py  zkLighter adapter (mainnet, Robinhood chain)
entropy_arb/lighter_nonce.py  Lighter order-nonce allocators (local / Redis)
entropy_arb/venue_aster.py    Aster DEX V3 adapter (EIP-712 signed REST)
entropy_arb/venue_polymarket.py  Polymarket Perps adapter (proxy-wallet, msgpack+EIP-712 signed REST)
entropy_arb/engine.py    the two-venue strategy loop + shared venue factory
entropy_arb/flatten.py   one-shot close of both legs (`entropy-arb flatten`)
entropy_arb/notify.py    telegram send API — call notify.send() where wanted
entropy_arb/threshold_check.py  scheduled config-vs-data threshold drift check
entropy_arb/webapp/       FastAPI backend + built React dashboard (entropy-arb web)
frontend/                 React + TS + Vite source for the web dashboard
entropy_arb/dashboard.py Rich terminal dashboard
entropy_arb/recorder.py  1-minute orderbook bars
entropy_arb/analyze.py    analyzer core — also `entropy-arb analyze` / tools/analyze.py
tests/                   python3 -m pytest tests/
```

## Known risks

- **A wrong midline is a losing strategy.** The premium center drifts;
  re-measure regularly and keep `config.yaml` current.
- **USDG basis** (`lighter-rh`): the hedge quotes in USDG. Part of any
  persistent premium is the stablecoin itself; your midline absorbs the
  level, but a USDG *move* is real PnL.
- **Funding**: two venues, two independent funding rates; carry is not
  modeled. Position caps bound it — keep them modest.
- **Thin books**: Entropy depth can be tiny; `take_fraction` and notional
  caps keep clips small, but slippage on the hedge leg after a partial fill
  is real.
- **Market hours**: for equity perps (e.g. SNDK), off-hours oracle regimes
  differ per venue; consider wider bands or not trading them.
- **One-leg risk**: a leg can fail after the other filled. The bot hedges
  and reconciles automatically, but you should still watch it.

Use at your own risk. This is trading software operating with real money;
nothing here is investment advice. Start with tiny position caps.

## License

[MIT](LICENSE)
