// entropy-arb dashboard — live state via /ws/live, artifact data via REST.
import { useEffect, useState } from 'react'
import { useLive } from './useLive'
import type { Suggestion, VenueLeg } from './types'

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

export default function App() {
  const [sug, setSug] = useState<Suggestion | null>(null)
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
            {live ? (eng?.halted ? 'HALTED' : live.mode) : 'offline'}
          </span>
        </h1>
        <span className={`badge ${live ? 'ok' : 'off'}`}>
          {live ? (eng?.record_only ? 'record-only' : 'live') : 'offline'}
        </span>
      </header>

      {eng === null ? (
        <p className="muted">
          No engine running in this process — showing recorded data only.
          Start the bot (`entropy-arb web`) or check the API endpoints.
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

