// Live data hook: WebSocket /ws/live with auto-reconnect.
import { useEffect, useState } from 'react'
import type { Snapshot } from './types'

export function useLive(): { snap: Snapshot | null; connected: boolean } {
  const [snap, setSnap] = useState<Snapshot | null>(null)
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    let sock: WebSocket | null = null
    let retry: ReturnType<typeof setTimeout> | null = null
    let closed = false

    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      sock = new WebSocket(`${proto}://${location.host}/ws/live`)
      sock.onopen = () => setConnected(true)
      sock.onmessage = (ev) => {
        try {
          setSnap(JSON.parse(ev.data))
        } catch {
          /* ignore malformed frame */
        }
      }
      sock.onclose = () => {
        setConnected(false)
        if (!closed) retry = setTimeout(connect, 2000)
      }
      sock.onerror = () => sock?.close()
    }
    connect()
    return () => {
      closed = true
      if (retry) clearTimeout(retry)
      sock?.close()
    }
  }, [])

  return { snap, connected }
}
