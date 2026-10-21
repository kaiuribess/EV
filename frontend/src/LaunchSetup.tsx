import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { CheckCircle2, ChevronRight, LockKeyhole, Radio, RefreshCw, ShieldCheck, TriangleAlert, Wallet, X } from 'lucide-react'
import { request } from './api'
import { human, money, type Json } from './types'
import { activationBody, activationGate, parseSetupReport, setupIsCurrent, weightSummary, type SetupReport } from './launchSetupModel'
import './LaunchSetup.css'

type Props = { mutate: (path: string, body: Json) => Promise<Json>; notify: (message: string, error?: boolean) => void; disabled: boolean }
const amount = (value: unknown) => typeof value === 'string' || typeof value === 'number' ? money(value) : 'Not configured'
function Limits({ report, complete = false }: { report: SetupReport; complete?: boolean }) {
  const values = report.risk_policy.values
  const cap = (value: unknown) => value === null && values?.available_cash_only === true ? 'Verified available cash' : amount(value)
  return <div className="setup-limits">
    <div><span>Maximum per bet, including fees</span><strong>{amount(values?.max_order_stake_usd)}</strong></div>
    <div><span>Daily-loss stop</span><strong>{values?.daily_loss_limit_usd === null ? 'Off' : amount(values?.daily_loss_limit_usd)}</strong></div>
    <div><span>Account funding</span><strong>{values?.available_cash_only === true ? 'Verified cash in each account' : 'Not confirmed'}</strong></div>
    <div><span>Overlapping game / player bets</span><strong>{values?.block_overlapping_events === true && values?.block_overlapping_players === true ? 'Blocked across accounts' : 'Not confirmed'}</strong></div>
    {complete && <><div><span>Per-event exposure cap</span><strong>{amount(values?.max_event_exposure_usd)}</strong></div>
      <div><span>Per-player exposure cap</span><strong>{amount(values?.max_player_exposure_usd)}</strong></div>
      <div><span>Correlated-exposure cap</span><strong>{amount(values?.max_correlated_exposure_usd)}</strong></div>
      <div><span>Per-account exposure cap</span><strong>{cap(values?.max_venue_exposure_usd)}</strong></div>
      <div><span>Total exposure cap</span><strong>{cap(values?.max_total_exposure_usd)}</strong></div>
      <div><span>Maximum open orders</span><strong>{typeof values?.max_open_orders === 'number' ? values.max_open_orders : 'Not configured'}</strong></div>
      <div><span>Maximum orders per minute</span><strong>{typeof values?.max_orders_per_minute === 'number' ? values.max_orders_per_minute : 'Not configured'}</strong></div></>}
  </div>
}

export function LaunchSetup({ mutate, notify, disabled }: Props) {
  const id = useId()
  const [report, setReport] = useState<SetupReport | null>(null)
  const [receivedAt, setReceivedAt] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [working, setWorking] = useState(false)
  const [clock, setClock] = useState(Date.now())
  const [review, setReview] = useState<SetupReport | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const mounted = useRef(true)
  const activeRequest = useRef<AbortController | null>(null)
  const busy = useRef(false)
  const dialog = useRef<HTMLDialogElement>(null)
  const fetchReport = useCallback(async () => {
    activeRequest.current?.abort()
    const controller = new AbortController()
    activeRequest.current = controller
    const timeout = setTimeout(() => controller.abort(), 8000)
    setLoading(true)
    try {
      const result = parseSetupReport(await request('/api/launch/setup', undefined, controller.signal))
      const received = Date.now()
      if (controller.signal.aborted || !mounted.current) throw new Error('Launch check interrupted. Refresh before continuing.')
      if (!setupIsCurrent(result, received, received, null)) throw new Error('The launch report is stale or has an invalid clock. Refresh before continuing.')
      setReport(result); setReceivedAt(received); setClock(received); setError(null)
      return { report: result, receivedAt: received }
    } catch (failure) {
      if (mounted.current && activeRequest.current === controller) {
        setError(controller.signal.aborted ? 'Launch checks timed out or were interrupted. Real trading is unavailable.' : failure instanceof Error ? failure.message : 'Launch checks are unavailable.')
        setReceivedAt(null); setReview(null); setConfirmed(false)
      }
      throw failure
    } finally {
      clearTimeout(timeout)
      if (mounted.current && activeRequest.current === controller) { activeRequest.current = null; setLoading(false) }
    }
  }, [])
  useEffect(() => {
    mounted.current = true
    const refresh = () => { if (!document.hidden && !busy.current && !activeRequest.current) void fetchReport().catch(() => {}) }
    const visibility = () => {
      setClock(Date.now())
      if (document.hidden) { activeRequest.current?.abort(); setReceivedAt(null); setReview(null); setConfirmed(false) }
      else refresh()
    }
    refresh()
    const polling = setInterval(refresh, 5000)
    const ticking = setInterval(() => setClock(Date.now()), 1000)
    document.addEventListener('visibilitychange', visibility)
    return () => { mounted.current = false; activeRequest.current?.abort(); clearInterval(polling); clearInterval(ticking); document.removeEventListener('visibilitychange', visibility) }
  }, [fetchReport])
  useEffect(() => {
    if (review && !dialog.current?.open) dialog.current?.showModal()
    else if (!review && dialog.current?.open) dialog.current?.close()
  }, [review])
  const current = setupIsCurrent(report, receivedAt, clock, error)
  const gate = activationGate(report, receivedAt, clock, error)
  const prepareReview = async () => {
    if (disabled || busy.current || !gate.allowed) return
    busy.current = true; setWorking(true)
    try {
      const fresh = await fetchReport()
      const checked = activationGate(fresh.report, fresh.receivedAt, Date.now())
      if (!checked.allowed) throw new Error(checked.reason)
      setConfirmed(false); setReview(fresh.report)
    } catch (failure) { notify(failure instanceof Error ? failure.message : 'Launch checks are unavailable.', true) }
    finally { busy.current = false; if (mounted.current) setWorking(false) }
  }
  const enable = async () => {
    if (!review || !confirmed || disabled || busy.current) return
    busy.current = true; setWorking(true)
    try {
      const fresh = await fetchReport()
      const body = activationBody(fresh.report, fresh.receivedAt, review.activation.scope_digest ?? '', Date.now())
      await mutate('/api/control', body)
      setReview(null); setConfirmed(false)
      notify('Activation request accepted for the reviewed trading scope. Checking the current engine state.')
      await fetchReport()
    } catch (failure) {
      setReview(null); setConfirmed(false)
      notify(failure instanceof Error ? failure.message : 'Activation could not be confirmed. Check the current engine state.', true)
      void fetchReport().catch(() => {})
    } finally { busy.current = false; if (mounted.current) setWorking(false) }
  }
  const dialogCurrent = gate.allowed && review?.activation.scope_digest === report?.activation.scope_digest
  const pending = report?.requirements.filter(r => r.status !== 'passed') ?? []
  return <section className="panel launch-setup" aria-labelledby={id}>
    <header className="setup-header"><div><span className="eyebrow">REAL TRADING SETUP</span><h2 id={id}>{current ? gate.allowed ? 'Ready for your final review' : report?.switches.auto_trade && !report.switches.kill_switch && report.environment === 'production' ? 'Real trading is enabled' : 'Finish setup before real trading' : 'Checking launch readiness'}</h2><p>Connected feeds, verified markets and trading permission are checked separately.</p></div><button className="button quiet" onClick={() => void fetchReport().catch(() => {})} disabled={loading || working}><RefreshCw size={14} className={loading ? 'setup-spin' : ''} />Refresh checks</button></header>
    {error && <div className="setup-alert" role="alert"><TriangleAlert size={18} /><span>{error} Previous facts below are not current approval.</span></div>}
    {!report ? <p className="setup-placeholder">{loading ? 'Reading the local engine’s current accounts and launch requirements…' : 'No current launch information is available.'}</p> : <>
      {!current && !error && <div className="setup-alert"><TriangleAlert size={18} /><span>This snapshot has expired. Refresh before relying on account or readiness facts.</span></div>}
      <div className="setup-counts" aria-label="Real and synthetic signal counts">
        <div><strong>{report.scope.native_candidates}</strong><span>Real native candidates</span></div><div><strong>{report.scope.synthetic_candidates}</strong><span>Synthetic demo candidates</span></div>
        <div><strong>{report.scope.verified_native_markets}</strong><span>Verified exchange markets</span></div><div><strong>{report.scope.real_reference_markets}</strong><span>Markets with real references</span></div>
      </div>
      <div className="setup-body"><div className="setup-section-title"><h3>Your exchange connections</h3><span>Each wallet is checked independently</span></div>
        <div className="setup-accounts">{report.accounts.map(account => {
          const snapshot = account.account_snapshot
          const cash = current && snapshot.current && snapshot.cash_available_verified && snapshot.balance_usd !== null
          const blocker = account.blockers.find(b => b.code.startsWith('software.')) ?? account.blockers.find(b => b.category !== 'arming')
          return <article className="setup-account" key={account.venue}>
            <div className="setup-account-top"><span><Wallet size={17} />{human(account.venue)}</span><span className={`setup-pill ${current && account.authenticated ? 'good' : ''}`}>{!current ? 'Unconfirmed' : account.authenticated ? 'Authenticated' : account.credentials_configured ? 'Not authenticated' : 'Key missing'}</span></div>
            <div className="setup-account-balance"><strong>{cash ? money(snapshot.balance_usd) : 'Not verified'}</strong><span>Current spendable cash</span></div>
            <dl><div><dt>Environment</dt><dd>{human(account.runtime_environment ?? account.configured_environment)}</dd></div><div><dt>Credential selection</dt><dd>{human(account.credential_profile)}</dd></div><div><dt>Market feed</dt><dd>{!current ? 'Unconfirmed' : account.streaming ? 'Streaming' : 'Not streaming'}</dd></div><div><dt>Reviewed trading scope</dt><dd>{account.profile_configured ? 'Profile loaded' : 'Not configured'}</dd></div><div><dt>Verified markets / fresh books</dt><dd>{account.verified_native_markets} / {current ? account.current_native_books : '—'}</dd></div></dl>
            {blocker && <p className="setup-account-blocker"><LockKeyhole size={13} /><span>{blocker.message}</span></p>}
          </article>
        })}</div>
        {!report.accounts.length && <p className="setup-placeholder">No exchange monitor plugins are registered.</p>}
        <div className="setup-two-columns"><section className="setup-subpanel"><div className="setup-section-title"><h3>Trading limits</h3><span className={`setup-pill ${current && report.risk_policy.applied ? 'good' : ''}`}>{!current ? 'Unconfirmed' : report.risk_policy.applied ? 'Applied to live scope' : report.risk_policy.values ? 'Saved draft · not applied' : 'Not configured'}</span></div><Limits report={report} /><details className="setup-all-limits"><summary>Review all exposure and order limits <ChevronRight size={12} /></summary><Limits report={report} complete /></details><p className="setup-note">{report.risk_policy.applied ? 'These limits bind the reviewed scope.' : 'Saved limits do not authorize real orders.'} Wallet balances are never pooled. Pending orders and unsettled fills retain their game and player reservations.</p>{report.risk_policy.error && <p className="setup-inline-error">{report.risk_policy.error}</p>}</section>
          <section className="setup-subpanel"><div className="setup-section-title"><h3>How books are weighted</h3><span className="setup-pill">Model estimate</span></div><strong className="setup-model-title">{weightSummary(report.pricing)}</strong><p>{human(report.pricing.devig_method)} no-vig probabilities, combined using the configured weights. Multiple feeds for the same book count once.</p><dl className="setup-pricing"><div><dt>Eligible books</dt><dd>{report.pricing.eligible_books === null ? 'All mapped books (no allowlist)' : report.pricing.eligible_books.length ? report.pricing.eligible_books.join(', ') : 'None · signals disabled'}</dd></div><div><dt>Required books</dt><dd>{report.pricing.required_books.length ? report.pricing.required_books.join(', ') : 'None configured'}</dd></div><div><dt>Minimum distinct books</dt><dd>{report.pricing.min_references}</dd></div><div><dt>Maximum source age</dt><dd>{report.pricing.max_quote_age_ms / 1000}s references · {report.pricing.max_book_age_ms / 1000}s exchange</dd></div><div><dt>Maximum disagreement</dt><dd>{Number(report.pricing.max_disagreement) * 100} probability points</dd></div></dl><details><summary>View weights and market overrides <ChevronRight size={12} /></summary><pre>{JSON.stringify({ weights: report.pricing.weights, market_overrides: report.pricing.market_overrides }, null, 2)}</pre><p>Unspecified eligible books have weight 1. Market-specific overrides can change the defaults above.</p></details><p className="setup-note">These weights have not established a profitable live track record.</p></section></div>
        <details className="setup-inputs"><summary><Radio size={16} />Sportsbook mapping coverage <ChevronRight size={13} /></summary><div className="table-scroll"><table><thead><tr><th>PROVIDER</th><th>ACCESS</th><th>MAPPED UPDATES</th><th>AWAITING REVIEW</th></tr></thead><tbody>{report.inputs.map(input => <tr key={input.provider}><td>{human(input.provider)}</td><td>{current && input.authenticated ? 'Authenticated' : 'Unconfirmed'}</td><td>{input.mapped_quotes.toLocaleString()}</td><td>{input.quarantined.toLocaleString()}</td></tr>)}</tbody></table></div><p className="setup-note">Mapped updates are cumulative observations, not unique tradable markets. Quarantined observations cannot produce approved native orders.</p></details>
        <div className="setup-section-title"><h3>What still needs to pass</h3><span>{current ? `${pending.length} pending` : 'Current status unconfirmed'}</span></div><div className="setup-checklist">{report.requirements.map(item => <details key={item.code}><summary>{current && item.status === 'passed' ? <CheckCircle2 size={17} className="good" /> : <LockKeyhole size={16} />}<strong>{item.title}</strong><span>{!current ? 'Unconfirmed' : item.status === 'passed' ? 'Passed' : 'Pending'}</span></summary><p>{item.detail}</p></details>)}</div>
      </div></>}
    <footer className="setup-activation"><div><strong>{gate.allowed ? 'Your final action' : 'Real trading stays unavailable until checks pass'}</strong><p>{gate.reason}</p><small>{current && report ? `Checked ${new Date(report.checked_at).toLocaleTimeString()}` : 'Awaiting a fresh report'} · No order is placed by this setup report.</small></div><button className="button primary" disabled={disabled || working || loading || !gate.allowed} onClick={() => void prepareReview()}><ShieldCheck size={16} />{working ? 'Checking…' : 'Enable real trading'}</button></footer>
    <dialog className="setup-dialog" ref={dialog} aria-labelledby={`${id}-review`} onCancel={event => { if (working) event.preventDefault(); else { setReview(null); setConfirmed(false) } }}>
      {review && <><header><div><span className="eyebrow">FINAL OWNER REVIEW</span><h2 id={`${id}-review`}>Enable real-money trading</h2></div><button className="icon-button" aria-label="Close activation review" disabled={working} onClick={() => setReview(null)}><X size={19} /></button></header><p>Automation may place real orders only within this reviewed scope. Review the accounts, markets and limits before continuing.</p><div className="setup-review-accounts">{review.launch_scope.map(scope => <section key={scope.venue}><h3>{human(scope.venue)} · {human(scope.environment)}</h3><p>Account fingerprint <code>{scope.account_fingerprint}</code></p><ul>{scope.markets.map(market => <li key={market.id}><strong>{market.name}</strong><small>{market.id}</small></li>)}</ul><dl><div><dt>Strategies</dt><dd>{scope.strategy_ids.join(', ')}</dd></div><div><dt>Reference providers</dt><dd>{scope.provider_ids.join(', ')}</dd></div><div><dt>Settlement policy</dt><dd>{scope.settlement_policy}</dd></div></dl></section>)}</div><Limits report={review} complete /><p className="setup-note">Positive estimated EV does not guarantee profit. This activates ongoing order submission subject to the central risk engine.</p><label className="setup-confirmation"><input type="checkbox" checked={confirmed} disabled={working} onChange={e => setConfirmed(e.target.checked)} /><span>I confirm these accounts, markets and limits for real-money automation.</span></label>{!dialogCurrent && <p role="alert" className="setup-inline-error">The current checks or reviewed scope changed. Close this review and refresh.</p>}<div className="setup-dialog-actions"><button className="button quiet" disabled={working} onClick={() => setReview(null)}>Keep paused</button><button className="button primary" disabled={!confirmed || !dialogCurrent || disabled || working} onClick={() => void enable()}>{working ? 'Checking current scope…' : 'Confirm and enable real trading'}</button></div></>}
    </dialog>
  </section>
}
