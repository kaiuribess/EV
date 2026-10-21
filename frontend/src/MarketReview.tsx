import { useEffect, useState } from 'react'
import { RefreshCw, ScanSearch } from 'lucide-react'
import { request } from './api'
import type { Json } from './types'

export function MarketReview() {
  const [data, setData] = useState<Json | null>(null)
  const [error, setError] = useState('')
  const [page, setPage] = useState(0)
  const [refresh, setRefresh] = useState(0)
  const [loading, setLoading] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    void request(`/api/market-review?limit=25&offset=${page * 25}`, undefined, controller.signal).then(value => {
      if (!Array.isArray(value.items) || typeof value.total !== 'number') throw new Error('Market review response is incomplete.')
      setData(value); setError('')
    }).catch(failure => { if (!controller.signal.aborted) { setData(null); setError(failure.message) } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [page, refresh])
  const age = (clock: unknown) => {
    if (typeof clock !== 'string' || !data) return 'Unknown source time'
    const seconds = (Date.parse(data.observed_at) - Date.parse(clock)) / 1000
    if (!Number.isFinite(seconds) || seconds < 0) return 'Invalid source time'
    return seconds < 60 ? `${seconds.toFixed(1)}s at capture` : `${(seconds / 60).toFixed(1)}m at capture`
  }
  return <details className="panel launch-readiness market-review">
    <summary className="launch-head"><div className="launch-title"><ScanSearch size={21} /><div><span className="eyebrow">REAL MARKET REVIEW</span><h2>{data ? `${data.total.toLocaleString()} observations awaiting review` : 'Unreviewed market observations'}</h2><p>Exact events, players, lines and settlement rules must agree before trading.</p></div></div></summary>
    <div className="launch-footer"><span>Bounded source window · These are not approved signals.</span><button className="button quiet" disabled={loading} onClick={() => setRefresh(value => value + 1)}><RefreshCw size={14} />Refresh</button></div>
    {error && <p className="launch-error" role="alert">{error}</p>}
    {data && <><div className="table-scroll"><table><thead><tr><th>SOURCE / BOOK</th><th>EVENT / PLAYER</th><th>MARKET / LINE / PERIOD</th><th>SOURCE AGE</th><th>REVIEW STATUS</th></tr></thead><tbody>{data.items.map((row: Json) => {
      const identity = row.identity || {}, context = identity.context || {}
      const participants = Array.isArray(context.participants) ? context.participants.filter((value: unknown) => typeof value === 'string' && value).join(' / ') : ''
      return <tr key={`${row.provider}:${row.signature}`}><td>{row.provider}<small>{identity.bookmaker || 'Unknown book'}</small></td><td>{participants || identity.event_id || 'Unknown event'}<small>{identity.player_key || 'Game market'}</small></td><td>{identity.market_key || 'Unknown market'}<small>{identity.line ?? 'No line supplied'} · {identity.period || 'Unknown period'}</small></td><td>{age(row.observation?.source_time)}</td><td>{row.reason}</td></tr>
    })}</tbody></table></div><div className="launch-footer"><span>Captured {new Date(data.observed_at).toLocaleTimeString()} · Source age is not refreshed by receipt time.</span><div className="launch-head-actions"><button className="button quiet" disabled={page === 0 || loading} onClick={() => setPage(value => value - 1)}>Previous</button><span>Page {page + 1}</span><button className="button quiet" disabled={(page + 1) * 25 >= data.total || loading} onClick={() => setPage(value => value + 1)}>Next</button></div></div></>}
  </details>
}
