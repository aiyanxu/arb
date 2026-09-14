// entropy-arb dashboard — live state via /ws/live, artifact data via REST.
// Control endpoints (pause/resume/flatten) are token-gated server-side:
// ARB_WEB_TOKEN in the env file enables them; the token lives in
// localStorage and is sent as a Bearer header.
import { useEffect, useState } from 'react'
import { useLive } from './useLive'
import type { FlattenState, Suggestion, VenueLeg } from './types'

const TOKEN_KEY = 'arb.web.token'

function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

async function control(path: string): Promise<{ ok: boolean; status: number; detail?: string }> {
  const token = getToken()
  try {
    const r = await fetch(path, {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (r.ok) return { ok: true, status: r.status }
    let detail = `HTTP ${r.status}`
    try {
      const body = await r.json()
      if (typeof body.detail === 'string') detail = body.detail
    } catch { /* non-json error body */ }
    return { ok: false, status: r.status, detail }
  } catch (e) {
    return { ok: false, status: 0, detail: String(e) }
  }
}

function num(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return v.toFixed(digits)
}

function VenueCard({ v }: { v: VenueLeg }) {
  return (
    <div className={`card ${v.fresh ? '' : 'stale'}`}>
      <div className="card-title">
        {v.name} <span className="muted">({v.role})</span>
        {!v.fresh && <span className="badge bad">STALE</span>}
      </div>
      <div className="kv"><span>bid/ask</span>
        <b>{num(v.bid, 4)} / {num(v.ask, 4)}</b></div>
      <div className="kv"><span>position</span>
        <b>{v.position >= 0 ? '+' : ''}{num(v.position, 4)}</b>
      </div>
      <div className="kv"><span>equity</span>
        <b>{v.equity === null ? '—' : `$${num(v.equity)}`}</b>
      </div>
      <div className="kv"><span>volume</span>
        <b>${num(v.volume_usd, 0)}</b>
      </div>
      <div className="kv"><span>fee</span>
        <b>{num(v.fee_bps, 1)} bps</b>
      </div>
    </div>
  )
}

function Stat({ label, value, cls }: {
  value: string
  label: string
  cls?: string
}) {
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className={`value ${cls ?? ''}`}>{value}</div>
    </div>
  )
}

function FlattenBanner({ state }: { state: FlattenState }) {
  if (state.running) {
    return (
      <div className="banner warn">
        flatten running — round {state.rounds}…
      </div>
    )
  }
  if (state.result === 'flat') {
    return <div className="banner ok">both legs flat ✓ (engine stays paused)</div>
  }
  if (state.result === 'not_flat') {
    return (
      <div className="banner bad">
        not flat after retries — check positions on each venue's web UI
      </div>
    )
  }
  if (state.result === 'error') {
    return <div className="banner bad">flatten failed: {state.error ?? 'unknown'}</div>
  }
  return null
}

function Controls({ eng, flatten, onMsg }: {
  eng: { paused: boolean; flatten_in_progress: boolean; record_only: boolean }
  flatten: FlattenState | null
  onMsg: (m: string) => void
}) {
  const [confirming, setConfirming] = useState(false)
  const [confirmText, setConfirmText] = useState('')
  const [busy, setBusy] = useState(false)

  const act = async (path: string, msg: string) => {
    setBusy(true)
    const r = await control(path)
    setBusy(false)
    if (!r.ok) onMsg(`✗ ${msg}: ${r.detail}`)
    else onMsg(`✓ ${msg}`)
    if (r.ok && path.includes('flatten')) setConfirming(false)
  }

  const flattening = eng.flatten_in_progress ||
    (flatten?.running ?? false)

  return (
    <section className="card controls">
      <div className="card-title">controls</div>
      <div className="controls-row">
        {eng.paused ? (
          <button disabled={eng.flatten_in_progress} onClick={() => act('/api/engine/resume', 'resumed')}>
            ▶ resume
          </button>
        ) : (
          <button disabled={eng.flatten_in_progress} onClick={() => act('/api/engine/pause', 'paused')}>
            ⏸ pause
          </button>
        )}
        <button className="danger" disabled={flattening || eng.record_only}
          onClick={() => { setConfirming(true); setConfirmText('') }}>
          flatten both legs
        </button>
        {eng.record_only && <span className="muted">record-only — no credentials, nothing to flatten</span>}
      </div>

      {flatten && <FlattenBanner state={flatten} />}

      {confirming && (
        <div className="modal-backdrop" onClick={() => setConfirming(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <div className="card-title">close BOTH legs' positions?</div>
            <p className="muted">
              Sends reduce-only IOC takers on both venues until flat
              (same as <code>entropy-arb flatten</code>). The engine stays
              paused afterwards. Type <b>FLATTEN</b> to confirm.
            </p>
            <input
              autoFocus value={confirmText}
              onChange={e => setConfirmText(e.target.value)}
              placeholder="FLATTEN"
            />
            <div className="controls-row">
              <button disabled={confirmText !== 'FLATTEN' || busy}
                onClick={() => act('/api/positions/flatten', 'flatten started')}>
                confirm flatten
              </button>
              <button onClick={() => setConfirming(false)}>cancel</button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}

export default function App() {
  const [sug, setSug] = useState<Suggestion | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const { snap: live, connected } = useLive()

  useEffect(() => {
    fetch('/api/threshold-suggestion')
      .then(r => r.json())
      .then(setSug)
      .catch(() => {})
  }, [])

  const eng = live?.engine ?? null

  return (
    <div className="app">
      <header>
        <h1>
          entropy-arb <span className="sym">{live?.symbol ?? '…'}</span>
          <span className={`badge ${connected ? 'ok' : 'off'}`}>
            {live ? (eng?.halted ? 'HALTED' : eng?.paused ? 'PAUSED' : live.mode) : 'offline'}
          </span>
        </h1>
        <span className={`badge ${live ? 'ok' : 'off'}`}>
          {live ? (eng?.record_only ? 'record-only' : 'live') : 'offline'}
        </span>
      </header>

      {msg && (
        <div className={`banner ${msg.startsWith('✗') ? 'bad' : 'ok'}`}
          onClick={() => setMsg(null)}>
          {msg}
        </div>
      )}

      {eng === null ? (
        <p className="muted">
          No engine running in this process — showing recorded data only.
          Start the bot (`entropy-arb web`) or check the API endpoints.
          Position controls (pause / flatten) need an embedded engine.
        </p>
      ) : (
        <>
          <section className="stats">
            <Stat label="premium" cls={eng.premium_bps === null ? '' :
              eng.premium_bps > eng.band[1] ? 'up' :
                eng.premium_bps < eng.band[0] ? 'down' : ''}
              value={eng.premium_bps === null ? '—'
                : `${eng.premium_bps >= 0 ? '+' : ''}${num(eng.premium_bps)} bps`} />
            <Stat label="band (mid±upper/lower)"
              value={`${num(eng.band[0], 1)} … ${num(eng.band[1], 1)} bps`} />
            <Stat label="session PnL (MTM)"
              value={eng.pnl === null ? '—' : `$${num(eng.pnl, 4)}`}
              cls={eng.pnl !== null && eng.pnl >= 0 ? 'pos' : 'neg'} />
            <Stat label="trades / hedges"
              value={`${eng.trades} / ${eng.hedges}`} />
            <Stat label="exp edge" value={`$${num(eng.exp_edge_usd, 4)}`} />
            <Stat label="fill edge" value={`$${num(eng.fill_edge_usd, 4)}`}
              cls="ok" />
            <Stat label="uptime"
              value={`${Math.floor(eng.uptime_sec / 3600)}h ${Math.floor(eng.uptime_sec % 3600 / 60)}m`} />
              <Stat label="mode"
              value={eng.record_only ? 'RECORD-ONLY' : 'LIVE TRADING'}
              cls={eng.record_only ? '' : 'warn'} />
          </section>

          {live && (
            <Controls eng={eng} flatten={live.flatten_state} onMsg={setMsg} />
          )}
        </>
      )}

      <section className="venues">
        {(live?.venues ?? []).map(v => <VenueCard key={v.role} v={v} />)}
      </section>

      <section className="card sug">
        <div className="card-title">threshold suggestion (analyzer math)</div>
        {sug?.error ? (
          <p className="muted">{sug.error}</p>
        ) : (
          <table>
            <thead><tr><th></th><th>config</th><th>suggested</th></tr></thead>
            <tbody>
              {['midline_bps', 'upper_bps', 'lower_bps'].map(k => (
                <tr key={k}>
                  <td>{k}</td>
                  <td>{num(sug?.current?.[k])}</td>
                  <td className={
                    sug?.drifted?.some(d => d.key === k) ? 'drift' : 'ok'
                  }>
                    {num(sug?.suggested?.[k])}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {live && live.trades.length > 0 && (
        <section className="card">
          <div className="card-title">recent executions</div>
          <table>
            <thead><tr><th>time</th><th>dir</th><th>qty</th>
              <th>prem</th><th>exp $</th><th>status</th></tr></thead>
            <tbody>
              {live.trades.slice().reverse().map((t, i) => (
                <tr key={i} className={t.ok ? '' : 'bad'}>
                  <td>{new Date(t.ts * 1000).toLocaleTimeString()}</td>
                  <td>{t.direction}</td>
                  <td>{num(t.qty, 4)}</td>
                  <td>${num(t.notional, 0)}</td>
                  <td>{num(t.prem_bps)} bps</td>
                  <td>{t.fill === null ? '—' : `$${num(t.fill, 4)}`}</td>
                  <td>{t.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  )
}

