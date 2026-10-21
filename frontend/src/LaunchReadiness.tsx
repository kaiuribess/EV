import { useEffect, useId, useState } from 'react'
import { CheckCircle2, CirclePause, LockKeyhole, RefreshCw, ShieldCheck, TriangleAlert } from 'lucide-react'
import { request } from './api'
import './LaunchReadiness.css'

type Category = 'technical' | 'authorization' | 'arming'
type Blocker = { code: string; category: Category; scope: string; message: string; action: string }
type Report = {
  evaluated_at: string; venue: string; environment: string;
  technical_ready: boolean; authorization_ready: boolean; safe_to_arm: boolean;
  live_trading_ready: boolean; sandbox_trading_ready: boolean; blockers: Blocker[];
}
const groups: { key: Category; title: string; description: string }[] = [
  { key: 'technical', title: 'Connections & execution', description: 'Current data, exact markets, fees and order recovery.' },
  { key: 'authorization', title: 'Account & approval', description: 'Verified account access, launch scope and explicit limits.' },
  { key: 'arming', title: 'Trading switches', description: 'Live execution requires a separate, explicit activation.' },
]
const isRecord = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object'
function parseReport(value: unknown): Report {
  if (!isRecord(value) || !Array.isArray(value.blockers) || typeof value.evaluated_at !== 'string'
    || typeof value.venue !== 'string' || typeof value.environment !== 'string'
    || ['technical_ready', 'authorization_ready', 'safe_to_arm', 'live_trading_ready', 'sandbox_trading_ready'].some(key => typeof value[key] !== 'boolean')
    || value.blockers.some(item => !isRecord(item) || !['technical', 'authorization', 'arming'].includes(String(item.category))
      || ['code', 'scope', 'message', 'action'].some(key => typeof item[key] !== 'string'))) {
    throw new Error('The readiness response is incomplete. Trading readiness is unconfirmed.')
  }
  return value as Report
}

export function LaunchReadiness() {
  const headingId = useId()
  const [report, setReport] = useState<Report | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const [lastSuccess, setLastSuccess] = useState<number | null>(null)
  const [clock, setClock] = useState(Date.now())

  useEffect(() => {
    let active = true
    let pending = false
    let controller: AbortController | null = null
    const refresh = async () => {
      if (!active || pending || document.hidden) return
      pending = true
      setLoading(true)
      controller = new AbortController()
      const current = controller
      try {
        const value = parseReport(await request('/api/readiness', undefined, current.signal))
        if (!active || current.signal.aborted) return
        setReport(value)
        setError(null)
        setLastSuccess(Date.now())
      } catch (failure) {
        if (active && !current.signal.aborted) setError(failure instanceof Error ? failure.message : 'Unable to check launch readiness.')
      } finally {
        pending = false
        if (active) setLoading(false)
      }
    }
    const visibility = () => {
      setClock(Date.now())
      if (document.hidden) controller?.abort()
      else void refresh()
    }
    void refresh()
    const interval = setInterval(() => { setClock(Date.now()); void refresh() }, 5000)
    document.addEventListener('visibilitychange', visibility)
    return () => { active = false; controller?.abort(); clearInterval(interval); document.removeEventListener('visibilitychange', visibility) }
  }, [refreshKey])

  // Never present an old passing snapshot as a current approval after a fetch failure.
  const current = !!report && !error && lastSuccess !== null && clock - lastSuccess < 12000
  const live = current && report.live_trading_ready
  const sandbox = current && report.sandbox_trading_ready
  const heading = !current ? 'Readiness unconfirmed' : live ? 'Production gate passed' : sandbox ? 'Sandbox gate passed' : report.safe_to_arm ? 'Checks passed · activation required' : 'Finish the launch checks'
  const blockedCount = report?.blockers.filter(item => item.category !== 'arming').length ?? 0
  return <section className="launch-readiness panel" aria-labelledby={headingId}>
    <div className="launch-head">
      <div className="launch-title"><span className="launch-icon"><ShieldCheck size={22} strokeWidth={1.5} /></span><div><span className="eyebrow">LIVE LAUNCH READINESS</span><h2 id={headingId}>{heading}</h2><p>{report?.venue ? `${report.venue} · ${report.environment}` : 'Choose a reviewed venue and account profile to begin.'}</p></div></div>
      <div className="launch-head-actions"><span className={`launch-state ${live || sandbox ? 'is-ready' : ''}`}><span />{!current ? 'UNCONFIRMED' : live ? 'LIVE GATE PASSED' : sandbox ? 'SANDBOX GATE PASSED' : 'LIVE NOT READY'}</span><button className="icon-button" aria-label="Refresh launch readiness" disabled={loading} onClick={() => setRefreshKey(value => value + 1)}><RefreshCw size={16} className={loading ? 'launch-spin' : ''} /></button></div>
    </div>
    {error && <div className="launch-error" role="alert"><TriangleAlert size={16} /><span>{error} {report ? 'The last snapshot below is for reference only.' : ''}</span></div>}
    <div className="launch-summary" aria-live="polite">
      <div><strong>{current ? report.technical_ready ? 'Passed' : 'Needs work' : 'Unconfirmed'}</strong><span>System checks</span></div>
      <div><strong>{current ? report.authorization_ready ? 'Verified' : 'Pending' : 'Unconfirmed'}</strong><span>Account & authorization</span></div>
      <div><strong>{report ? blockedCount.toLocaleString() : '—'}</strong><span>Checks to resolve</span></div>
      <div><strong>Not established</strong><span>Profitable live track record</span></div>
    </div>
    {!report && <div className="launch-loading">{loading ? 'Checking the local engine…' : 'Current readiness evidence is unavailable.'}</div>}
    {report && <div className="launch-groups">{groups.map(group => {
      const blockers = report.blockers.filter(item => item.category === group.key)
      const passed = current && blockers.length === 0
      const Icon = passed ? CheckCircle2 : group.key === 'arming' ? CirclePause : LockKeyhole
      return <details key={group.key} className="launch-group" open={group.key === 'authorization'}>
        <summary><Icon size={17} className={passed ? 'launch-pass' : ''} /><div><strong>{group.title}</strong><small>{group.description}</small></div><span className={`launch-count ${passed ? 'launch-pass' : ''}`}>{passed ? 'Passed' : blockers.length ? `${blockers.length} pending` : 'Unconfirmed'}</span></summary>
        <div className="launch-checks">{blockers.length ? blockers.map((blocker, index) => <div className="launch-check" key={`${blocker.code}:${blocker.scope}:${index}`}><span className="launch-check-dot" /><div><strong>{blocker.message}</strong><p>{blocker.action}</p><small>{blocker.scope === 'runtime' ? 'System' : blocker.scope}</small></div></div>) : <p className="launch-clear">{passed ? 'Current checks in this group passed.' : 'Refresh to confirm this group.'}</p>}</div>
      </details>
    })}</div>}
    <footer className="launch-footer"><span>Checks refresh every 5 seconds while visible. This panel does not enable trading.</span><span>{lastSuccess ? `Checked ${new Date(lastSuccess).toLocaleTimeString()}` : 'Awaiting current evidence'}</span></footer>
    <p className="launch-performance-note">Technical readiness does not prove a profitable strategy. Actual fills, fees and verified settlements remain separate from simulated results.</p>
  </section>
}
