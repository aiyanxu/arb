# entropy-arb frontend

React + TypeScript + Vite dashboard for the FastAPI backend
(`entropy_arb/webapp`). Built output is copied to `../entropy_arb/webapp/static/`
and served by `entropy-arb web`.

## Develop

```bash
cd frontend
npm install
npm run dev          # vite dev server; proxy /api + /ws to :8000 (see vite.config.ts)
```

## Build & ship

```bash
npm run build        # dist/ -> ../entropy_arb/webapp/static (copy)
```

Then `entropy-arb web` serves the SPA plus the JSON API from one port.

## Pages

Single-page dashboard: engine state (mode, HALT, uptime), premium vs band,
session PnL / edges, per-venue book + position cards, recent executions, and
the analyzer's threshold suggestion with drift highlighting. Live updates
arrive over `/ws/live` (2s snapshot push, auto-reconnect).
