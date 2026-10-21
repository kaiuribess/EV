import { useCallback, useEffect, useRef, useState } from 'react'
import { emptyState, normalizeState, type Json, type State } from './types'

export async function request(path: string, body?: Json, signal?: AbortSignal): Promise<Json> {
  const response = await fetch(path, {
    method: body ? 'POST' : 'GET',
    headers: body ? { 'Content-Type': 'application/json', 'X-EV-Control': 'local' } : {},
    body: body ? JSON.stringify(body) : undefined,
    signal,
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = data.detail
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail || data.error || `Request failed (${response.status})`))
  }
  return data
}

export function useEngine() {
  const [state, setState] = useState<State>(emptyState)
  const [connected, setConnected] = useState(false)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [history, setHistory] = useState<{ time: number; count: number; edge: number }[]>([])
  const live = useRef(false)
  const accept = useCallback((data: Json) => {
    const next = normalizeState(data)
    setState(next)
    setUpdatedAt(Date.now())
    setError(null)
    setHistory(previous => {
      if (previous.length && Date.now() - previous[previous.length - 1].time < 1800) return previous
      const eligible = next.opportunities.filter(o => o.status === 'POSITIVE EV' || (!o.status && Number(o.ev_per_contract) > 0))
      const edge = eligible.length ? eligible.reduce((sum, o) => sum + Number(o.roi || 0), 0) / eligible.length : 0
      return [...previous, { time: Date.now(), count: eligible.length, edge }].slice(-90)
    })
  }, [])
  useEffect(() => {
    let stopped = false
    let socket: WebSocket | null = null
    let reconnect: ReturnType<typeof setTimeout>
    let attempt = 0
    const refresh = () => request('/api/state').then(data => { if (!stopped) accept(data) }).catch(e => { if (!stopped) setError(e.message) })
    void refresh()
    const connect = () => {
      if (stopped) return
      socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`)
      socket.onopen = () => { attempt = 0; live.current = true; setConnected(true) }
      socket.onmessage = event => {
        try {
          const message = JSON.parse(event.data)
          if (message.type === 'state') accept(message.data ?? message.state)
          else if (message.markets || message.plugins) accept(message)
        } catch { setError('An invalid dashboard update was ignored.') }
      }
      socket.onclose = () => {
        live.current = false
        setConnected(false)
        if (!stopped) reconnect = setTimeout(connect, Math.min(1000 * 2 ** attempt++, 15000))
      }
      socket.onerror = () => socket?.close()
    }
    connect()
    const polling = setInterval(() => { if (!live.current) void refresh() }, 5000)
    return () => { stopped = true; clearTimeout(reconnect); clearInterval(polling); socket?.close() }
  }, [accept])
  const mutate = async (path: string, body: Json) => {
    const result = await request(path, body)
    try { accept(await request('/api/state')) } catch { /* websocket will refresh */ }
    return result
  }
  return { state, connected, updatedAt, error, history, mutate }
}
