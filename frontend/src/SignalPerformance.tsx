import { useEffect, useState } from 'react'
import { ChevronLeft, ChevronRight, ClipboardList, RefreshCw, ShieldCheck } from 'lucide-react'
import { request } from './api'
import { human, text, type Json } from './types'

const PAGE_SIZE = 25
const availableNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const count = (value: unknown) => availableNumber(value) ? value.toLocaleString() : '—'
const amount = (value: unknown, digits = 2) => availableNumber(value) ? new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value) : '—'
const ratio = (value: unknown) => availableNumber(value) ? `${(value * 100).toFixed(2)}%` : '—'
const firstSeen = (value: unknown) => {
  if (typeof value !== 'string' && typeof value !== 'number') return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' })
}
const profitClass = (value: unknown) => availableNumber(value) ? value > 0 ? 'green' : value < 0 ? 'signal-negative' : '' : ''

export function SignalPerformance({ mode }: { mode: string }) {
  const [synthetic, setSynthetic] = useState(mode.toLowerCase() !== 'production')
  const [offset, setOffset] = useState(0)
  const [refreshKey, setRefreshKey] = useState(0)
  const [ledgerData, setLedger] = useState<Json | null>(null)
  const [summaryData, setSummary] = useState<Json | null>(null)
  const [loadedScope, setLoadedScope] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)

  useEffect(() => {
    let active = true
    let pending = false
    let controller: AbortController | null = null
    setLedger(null)
    setSummary(null)
    setUpdatedAt(null)
    setError(null)
    const refresh = async () => {
      if (!active || document.hidden || pending) return
      pending = true
      controller = new AbortController()
      const thisController = controller
      setLoading(true)
      try {
        const query = `synthetic=${synthetic}`
        const [page, totals] = await Promise.all([
          request(`/api/signals?limit=${PAGE_SIZE}&offset=${offset}&${query}`, undefined, thisController.signal),
          request(`/api/signals/summary?${query}`, undefined, thisController.signal),
        ])
        if (!Array.isArray(page.items)) throw new Error('The signal ledger returned an invalid response.')
        if (!active || thisController.signal.aborted) return
        setLedger(page)
        setSummary(totals)
        setLoadedScope(`${synthetic}:${offset}`)
        setUpdatedAt(Date.now())
        setError(null)
      } catch (failure) {
        if (active && !thisController.signal.aborted) setError(failure instanceof Error ? failure.message : 'Unable to refresh the signal ledger.')
      } finally {
        pending = false
        if (active && controller === thisController) setLoading(false)
      }
    }
    const onVisibility = () => {
      if (document.hidden) {
        controller?.abort()
        setLoading(false)
      } else {
        // Allow the cancelled request to finish unwinding before the visible refresh.
        void Promise.resolve().then(() => { if (!pending) void refresh() })
      }
    }
    void refresh()
    const interval = setInterval(() => void refresh(), 5000)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      active = false
      controller?.abort()
      clearInterval(interval)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [synthetic, offset, refreshKey])

  const chooseSource = (next: boolean) => { setSynthetic(next); setOffset(0) }
  // Never relabel a previous source's snapshot while the next request loads.
  const currentScope = loadedScope === `${synthetic}:${offset}`
  const ledger = currentScope ? ledgerData : null
  const summary = currentScope ? summaryData : null
  const records: Json[] = ledger?.items ?? []
  const total = availableNumber(ledger?.total) ? ledger.total : null
  const groups: Json[] = Array.isArray(summary?.groups) ? summary.groups : []
  const lastPage = total === null || offset + PAGE_SIZE >= total
  const showPnl = availableNumber(summary?.graded) && summary.graded > 0
  return <section className="signal-performance" aria-labelledby="signal-performance-title">
    <div className="signal-performance-head">
      <div><span className="eyebrow">DURABLE SIGNAL HISTORY</span><h2 id="signal-performance-title">How the signals perform</h2><p>Every tracked episode keeps its first price and fair estimate, even when no order is placed.</p></div>
      <div className="signal-source-filter" role="group" aria-label="Signal data source">
        <button className={synthetic ? 'selected' : ''} onClick={() => chooseSource(true)} aria-pressed={synthetic}>Synthetic</button>
        <button className={!synthetic ? 'selected' : ''} onClick={() => chooseSource(false)} aria-pressed={!synthetic}>Live observations</button>
      </div>
    </div>
    <div className="notice subtle-notice signal-method-note"><ShieldCheck size={17} /><span><strong>{synthetic ? 'Synthetic signals and hypothetical results.' : 'Live signals and hypothetical results.'}</strong> Results assume one contract at the first qualifying snapshot, with the recorded fee estimate. These are separate from actual or paper fills. Only qualified episodes enter profit metrics; ungraded signals are pending, not losses.</span></div>
    {error && <div className="notice connection-notice signal-fetch-error" role="alert"><span><strong>Signal history could not refresh.</strong> {error}{updatedAt ? ' The last successful snapshot is shown.' : ''}</span><button className="button quiet" onClick={() => setRefreshKey(key => key + 1)}>Retry</button></div>}
    <div className="stats-grid signal-stats" aria-busy={loading && !summary}>
      <div className="stat"><div className="stat-label">QUALIFIED EPISODES</div><div className="stat-value">{count(summary?.eligible_episodes)}</div><div className="stat-detail">{count(summary?.episodes)} total episodes retained</div></div>
      <div className="stat"><div className="stat-label">GRADED SAMPLE</div><div className="stat-value">{count(summary?.graded)}</div><div className="stat-detail">{count(summary?.pending)} pending verification</div></div>
      <div className="stat"><div className="stat-label">HYPOTHETICAL PROFIT</div><div className={`stat-value ${showPnl ? profitClass(summary?.hypothetical_pnl) : ''}`}>{showPnl ? amount(summary?.hypothetical_pnl) : '—'}</div><div className="stat-detail">First snapshot · one contract per qualified episode</div></div>
      <div className="stat"><div className="stat-label">HYPOTHETICAL ROI</div><div className={`stat-value ${showPnl ? profitClass(summary?.hypothetical_roi) : ''}`}>{showPnl ? ratio(summary?.hypothetical_roi) : '—'}</div><div className="stat-detail">{showPnl ? `${amount(summary?.hypothetical_cost)} estimated entry cost` : 'Appears after qualified signals are graded'}</div></div>
    </div>
    <div className="panel signal-ledger-panel">
      <div className="section-head"><div><h2>Signal ledger</h2><p>Results require a verified result or import. An automatic grading feed is not connected.</p></div><button className="icon-button" aria-label="Refresh signal history" disabled={loading} onClick={() => setRefreshKey(key => key + 1)}><RefreshCw size={16} className={loading ? 'signal-refreshing' : ''} /></button></div>
      <div className="signal-summary-strip">
        {([['Open', summary?.open], ['Wins', summary?.wins], ['Losses', summary?.losses], ['Refunds', summary?.refunds], ['Partial results', summary?.partials]] as const).map(([label, value]) => <span key={label}>{label}<b>{count(value)}</b></span>)}
        <span title="Mean squared error of graded fair probabilities; lower is better. No score is shown before results are available.">Brier score<b>{availableNumber(summary?.brier_score) ? summary.brier_score.toFixed(4) : '—'}</b></span>
      </div>
      <div className="table-scroll" tabIndex={0} role="region" aria-label="Signal ledger, scroll horizontally for all columns" aria-busy={loading}>
        <table className="signal-ledger-table"><thead><tr><th scope="col">FIRST SEEN</th><th scope="col">EVENT / MARKET</th><th scope="col">VENUE / STRATEGY</th><th scope="col">OUTCOME</th><th scope="col">FIRST SNAPSHOT</th><th scope="col">ELIGIBILITY</th><th scope="col">EPISODE STATUS</th><th scope="col">RESULT / HYP. P&amp;L</th><th scope="col">UPDATES / CLOSE REASON</th></tr></thead>
          <tbody>{records.map((record, index) => <tr key={record.id ?? record.signal_id ?? `${record.first_seen}-${record.market_id}-${record.venue}-${record.outcome}-${index}`}>
            <td className="tabular" title={text(record.first_seen)}>{firstSeen(record.first_seen)}</td>
            <td><strong className="signal-event">{text(record.event_name ?? record.event_id)}</strong><small>{human(record.market_type)}{record.player ? ` · ${text(record.player)}` : ''}{record.line !== null && record.line !== undefined ? ` · ${text(record.line)}` : ''}</small></td>
            <td>{human(record.venue)}<small>{human(record.strategy)}</small></td>
            <td>{human(record.outcome)}</td>
            <td className="tabular">{amount(record.first_price, 4)}<small>Fair {ratio(record.first_fair_probability)}</small><small className={profitClass(record.first_ev_per_contract)}>EV {amount(record.first_ev_per_contract, 4)}</small></td>
            <td><span className={`badge ${record.qualified === true ? '' : 'neutral'}`}>{record.qualified === true ? 'QUALIFIED' : record.qualified === false ? 'NOT QUALIFIED' : 'UNKNOWN'}</span></td>
            <td>{human(record.status)}</td>
            <td><span className={`signal-grade ${text(record.grade).toLowerCase() === 'win' ? 'green' : text(record.grade).toLowerCase() === 'loss' ? 'signal-negative' : ''}`}>{record.grade ? human(record.grade) : 'Pending'}</span><small className={record.qualified === true ? profitClass(record.hypothetical_pnl) : ''}>{record.qualified === true ? amount(record.hypothetical_pnl) : 'Excluded from profit metrics'}</small></td>
            <td className="signal-close-reason">{count(record.update_count)}<small>{human(record.close_reason)}</small></td>
          </tr>)}</tbody>
        </table>
      </div>
      {!records.length && <div className="empty"><ClipboardList size={28} strokeWidth={1.3} /><strong>{loading ? 'Loading signal history' : error ? 'Signal history unavailable' : 'No tracked episodes in this view'}</strong><p>{error ? 'Retry when the local engine is available.' : synthetic ? 'Synthetic episodes appear as the demo evaluates markets.' : 'Live episodes appear once verified mappings and trustworthy references produce signal observations.'}</p></div>}
      <div className="table-footer signal-pagination"><div><span>{total === null ? '— episodes' : total === 0 ? '0 episodes' : `${offset + 1}–${Math.min(offset + records.length, total)} of ${total.toLocaleString()} episodes`}</span><small>{updatedAt ? `Updated ${new Date(updatedAt).toLocaleTimeString()} · refreshes every 5 s while visible` : 'Waiting for the first saved snapshot'}</small></div><div className="signal-page-controls"><button className="button quiet" aria-label="Previous signal page" disabled={offset === 0 || loading} onClick={() => setOffset(value => Math.max(0, value - PAGE_SIZE))}><ChevronLeft size={14} />Previous</button><button className="button quiet" aria-label="Next signal page" disabled={lastPage || loading} onClick={() => setOffset(value => value + PAGE_SIZE)}>Next<ChevronRight size={14} /></button></div></div>
    </div>
    <div className="signal-methodology"><p>{text(summary?.note, 'No results are inferred from missing data. A signal is an observation, not a confirmed execution or a promise of profit.')}</p><span>{count(summary?.decision_count)} decisions audited <span className="dot-separator">·</span> {count(summary?.no_signal_count)} no-signal decisions</span></div>
    {groups.length > 0 && <section className="panel signal-breakdowns" aria-labelledby="signal-breakdown-title"><div className="section-head"><div><h2 id="signal-breakdown-title">Qualified signal breakdown</h2><p>Graded sample sizes matter. Profit and ROI use the same hypothetical entry assumptions.</p></div></div><div className="table-scroll"><table><thead><tr><th scope="col">GROUP</th><th scope="col">VALUE</th><th scope="col">EPISODES</th><th scope="col">GRADED</th><th scope="col">HYP. PROFIT</th><th scope="col">HYP. ROI</th></tr></thead><tbody>{groups.map((group, index) => <tr key={`${group.dimension}-${group.value}-${index}`}><td>{human(group.dimension)}</td><td>{human(group.value)}</td><td className="tabular">{count(group.count)}</td><td className="tabular">{count(group.graded)}</td><td className={`tabular ${group.graded > 0 ? profitClass(group.hypothetical_pnl) : ''}`}>{group.graded > 0 ? amount(group.hypothetical_pnl) : '—'}</td><td className="tabular">{group.graded > 0 ? ratio(group.hypothetical_roi) : '—'}</td></tr>)}</tbody></table></div></section>}
  </section>
}
