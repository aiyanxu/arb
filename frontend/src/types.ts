// Shared client-side types mirroring the backend's JSON shapes.
// (entropy_arb/webapp/__init__.py is the source of truth.)

export interface VenueLeg {
  role: string
  name: string
  kind: string
  symbol: string
  fee_bps: number
  cap_usd: number
  bid: number | null
  ask: number | null
  mid: number | null
  fresh: boolean
  position: number
  cash: number
  equity: number | null
  volume_usd: number
  last_traded_ts: number
}

export interface EngineState {
  running: boolean
  record_only: boolean
  halted: boolean
  paused: boolean
  flatten_in_progress: boolean
  trades: number
  hedges: number
  exp_edge_usd: number
  fill_edge_usd: number
  uptime_sec: number
  pnl: number | null
  premium_bps: number | null
  band: [number, number]
  midline_bps: number
}

export interface FlattenState {
  running: boolean
  rounds: number
  result: 'flat' | 'not_flat' | 'error' | null
  error?: string
}

export interface Trade {
  ts: number
  direction: string
  qty: number
  notional: number
  prem_bps: number
  exp: number
  fill: number | null
  status: string
  ok: boolean
}

export interface Snapshot {
  mode: 'live' | 'artifact'
  symbol: string
  base_venue: string
  hedge_venue: string
  engine: EngineState | null
  venues: VenueLeg[]
  trades: Trade[]
  flatten_state: FlattenState | null
  ts: number
}

export interface Suggestion {
  suggested?: Record<string, number>
  current?: Record<string, number>
  minutes?: number
  drifted?: { key: string; current: number; suggested: number; rel: number }[]
  error?: string
}

export interface MinuteRow {
  ts: number
  prem: number
  sell_max: number
  buy_max: number
}
