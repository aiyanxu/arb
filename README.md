# entropy-arb

**[中文文档 / Chinese documentation → README.zh-CN.md](README.zh-CN.md)**

Open-source two-venue perp arbitrage bot. Both legs are configurable — either
can be any of the four supported venues (the two legs must differ); the
**base** leg is the premium's numerator, the **hedge** leg its denominator
(`base_venue` defaults to `entropy`):

| venue | venue | quote | taker fee | protocol |
|---|---|---|---|---|
| `entropy` | Entropy on Hyperliquid | USDC | 0 bps | HL l2Book (dex `io`) |
| `lighter` | Lighter mainnet | USDC | 0 bps | zkLighter ws (diff books, async settle) |
| `lighter-rh` | Lighter Robinhood chain | **USDG** | 0 bps | zkLighter ws |
| `tradexyz` | Hyperliquid trade.xyz dex | USDC | ~1 bps | HL l2Book, sync IOC settle |

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
`lighter-rh`, `tradexyz`; the two legs must differ; `base_venue` defaults to
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
python3 tools/analyze.py
```

It prints the premium distribution, how often each candidate band would have
fired, and a ready-to-paste `thresholds:` block for `config.yaml`.

**3. Go live** — fill in `.env`, install the signing SDKs, and start with
the smallest position caps that clear the venue minimums:

```bash
pip install -e ".[live]"
entropy-arb              # or with overrides: --symbol SNDK --base entropy --hedge lighter-rh
```

Running without `--record-only` sends real orders immediately once both
feeds are fresh and the band is crossed.

**Dashboard.** On a terminal the bot shows a live Rich dashboard: both
books with age/spread, positions and caps, equity and session PnL, the
executable premium of each direction against its full hurdle (fees and
inventory surcharge included, ● = armed), recorder progress, the last
executions, and a tail of the log (the full log goes to `logging.file`,
default `logs/engine.log`). It works in `--record-only` too. Add `--cn` to
display the dashboard in Chinese. Use `--no-dashboard` for plain console
logs (nohup/systemd — off-terminal runs fall back automatically), or set
`logging.dashboard: false`.

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
docker run --rm --entrypoint python -v ./logs:/app/logs \
  entropy-arb:record-only tools/analyze.py
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
suggestions translate directly into config values. `--hours 24` restricts to
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
| `base_venue` | the base leg (premium numerator): `entropy` / `lighter` / `lighter-rh` / `tradexyz` | `entropy` |
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
- **lighter / lighter-rh** — `LIGHTER_ACCOUNT_INDEX`, `LIGHTER_API_KEY_INDEX`,
  `LIGHTER_API_PRIVATE_KEY`, registered on the **same deployment** as the
  Lighter leg (mainnet and the Robinhood chain are separate accounts and
  keys — see [lighter-python](https://github.com/elliottech/lighter-python)).
  When **both** legs are Lighter deployments, the hedge leg reads
  `LIGHTER_HEDGE_ACCOUNT_INDEX` / `LIGHTER_HEDGE_API_KEY_INDEX` /
  `LIGHTER_HEDGE_API_PRIVATE_KEY` instead (no fallback — the deployments
  have separate accounts).

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
entropy_arb/feeds.py     official HL ws + zkLighter ws book feeds
entropy_arb/venue_hl.py  Hyperliquid dex adapter (Entropy, tradexyz)
entropy_arb/venue_lighter.py  zkLighter adapter (mainnet, Robinhood chain)
entropy_arb/engine.py    the two-venue strategy loop
entropy_arb/dashboard.py Rich terminal dashboard
entropy_arb/recorder.py  1-minute orderbook bars
tools/analyze.py         minutes.duckdb -> suggested thresholds (per combination)
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
