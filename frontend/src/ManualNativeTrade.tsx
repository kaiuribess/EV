import { useEffect, useState } from 'react'
import { ArrowDownLeft, ShieldCheck, TriangleAlert, X } from 'lucide-react'
import { request } from './api'
import { human, money, percent, text, type Json, type State } from './types'
import './ManualNativeTrade.css'

type Review = {
  review_token: string
  terms_digest: string
  max_cost_usd: string
  expires_at: string
  environment: string
  market: { id: string; event_name: string; market_type: string; player?: string | null }
  candidate: Json
}

export function ManualNativeTrade({ state, connected }: { state: State; connected: boolean }) {
  const [review, setReview] = useState<Review | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [maxCost, setMaxCost] = useState('1.00')
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  const native = ['sandbox', 'production'].includes(state.mode)
  const candidates = (Array.isArray(state.native_opportunities) ? state.native_opportunities : [])
    .filter((candidate: Json) => candidate.synthetic === false && candidate.qualified === true && candidate.execution_eligible === true)
  const paused = native && state.auto_trade !== true
  const canReview = connected && paused && state.live_enabled === true && Number(maxCost) > 0 && Number.isFinite(Number(maxCost))
  const validReview = review && now < Date.parse(review.expires_at) && connected && paused && state.kill_switch !== true && state.mode === review.environment

  const preview = async (candidate: Json) => {
    if (!canReview || busy) return
    setBusy(true); setMessage('')
    try {
      const response = await request('/api/native/manual/preview', {
        market_id: candidate.market_id, venue: candidate.venue, outcome: candidate.outcome,
        max_cost_usd: maxCost,
      })
      if (!response.review_token || !response.candidate || !response.expires_at) throw new Error('Manual review response was incomplete.')
      setReview(response as Review)
    } catch (failure) {
      setMessage(failure instanceof Error ? failure.message : 'Manual review failed.')
    } finally { setBusy(false) }
  }

  const place = async () => {
    if (!validReview || !review || busy) return
    setBusy(true); setMessage('')
    const token = review.review_token
    setReview(null) // Never offer a retry of the same submission click.
    try {
      const response = await request('/api/native/manual/submit', {
        review_token: token, terms_digest: review.terms_digest,
        approved_max_cost_usd: review.max_cost_usd,
      })
      const order = response.order as Json
      setMessage(`Manual ${human(order?.intent?.mode)} order ${text(order?.status)}. Check the native ledger for its confirmed fill and reconciliation.`)
    } catch (failure) {
      setMessage(`Submission was not confirmed: ${failure instanceof Error ? failure.message : 'unknown result'}. Check the native order ledger before reviewing another trade.`)
    } finally { setBusy(false) }
  }

  const c = review?.candidate
  const intent = c?.intent
  return <section className="panel manual-trade" aria-label="Place one manual native order">
    <div className="section-head"><div><h2>Place one trade manually</h2><p>Review one qualifying exchange offer, then send only that order. Automation stays paused.</p></div><ShieldCheck size={19} /></div>
    {!native && <p className="quiet-message">This paper workspace cannot place an exchange order. Open a paused native sandbox or production workspace to review one.</p>}
    {native && state.auto_trade && <p className="quiet-message">Pause automation before choosing a manual order, so it cannot race your click.</p>}
    {native && state.kill_switch && <p className="quiet-message">You can review a qualifying order while stopped. Sending it still requires the normal arming control.</p>}
    {native && <label className="manual-trade-cap">Maximum spend for this one order, including fees <span>$<input type="number" min="0.01" step="0.01" value={maxCost} onChange={event => setMaxCost(event.target.value)} aria-label="Maximum spend for one manual order in dollars" /></span><small>Starts at $1 for a test. A venue minimum or quarter-Kelly budget may make that size unavailable.</small></label>}
    {native && !candidates.length && <p className="quiet-message">No verified, qualifying native EV offer is available to review. A connected exchange feed alone does not create an eligible trade.</p>}
    {message && <p className="manual-trade-message" role="status">{message}</p>}
    {native && candidates.length > 0 && <div className="manual-trade-list">{candidates.slice(0, 12).map((candidate: Json) => <div className="manual-trade-row" key={candidate.id}>
      <div><strong>{human(candidate.venue)} · {human(candidate.outcome)}</strong><small>{text(candidate.market_id)} · {human(candidate.intent?.instrument?.quantity_unit)}</small></div>
      <div><strong>{money(candidate.intent?.reserved_cost_usd)} max</strong><small>Limit {text(candidate.intent?.limit_price)} · ROI {percent(candidate.roi, 2)}</small></div>
      <button className="button quiet" disabled={!canReview || busy} onClick={() => void preview(candidate)}>Review trade</button>
    </div>)}</div>}
    <p className="quiet-message">A manual click uses current launch checks, account cash, configured stake sizing, price limits, conflict protection and durable order reconciliation. The order may remain unfilled or settle at a loss.</p>
    {review && <div className="modal-backdrop" onClick={() => !busy && setReview(null)}><section role="dialog" aria-modal="true" aria-label="Review one exchange order" className="modal manual-trade-dialog" onClick={event => event.stopPropagation()}>
      <div className="modal-head"><div><span className="eyebrow">ONE ORDER · {review.environment.toUpperCase()}</span><h2>Review before placing</h2></div><button className="icon-button" aria-label="Close order review" disabled={busy} onClick={() => setReview(null)}><X size={20} /></button></div>
      <p>{review.market.event_name} · {human(review.market.market_type)}{review.market.player ? ` · ${review.market.player}` : ''}</p>
      <dl className="manual-trade-terms">
        <div><dt>Venue / side</dt><dd>{human(c?.venue)} / {human(c?.outcome)}</dd></div>
        <div><dt>Native market</dt><dd>{text(intent?.instrument?.instrument_id)}</dd></div>
        <div><dt>Quantity</dt><dd>{text(intent?.quantity)} {human(intent?.instrument?.quantity_unit)}</dd></div>
        <div><dt>Limit price</dt><dd>{text(intent?.limit_price)}</dd></div>
        <div><dt>Maximum cost including fees</dt><dd>{money(intent?.reserved_cost_usd)}</dd></div>
        <div><dt>Maximum fee</dt><dd>{money(intent?.max_fee_usd)}</dd></div>
        <div><dt>Estimated fair probability / net ROI</dt><dd>{percent(c?.fair_probability, 2)} / {percent(c?.roi, 2)}</dd></div>
        <div><dt>Reference books</dt><dd>{(c?.references ?? []).map((r: Json) => human(r.bookmaker)).join(', ')}</dd></div>
      </dl>
      <p className="manual-trade-warning"><TriangleAlert size={17} />{validReview ? `Review expires in ${Math.max(0, Math.ceil((Date.parse(review.expires_at) - now) / 1000))}s. The server will reprice before sending.` : 'This review expired or the trading state changed. Close it and review again.'}</p>
      <div className="modal-actions"><button className="button quiet" disabled={busy} onClick={() => setReview(null)}>Do not place</button><button className="button primary" disabled={!validReview || busy} onClick={() => void place()}><ArrowDownLeft size={15} />{busy ? 'Checking and sending…' : `Place one ${review.environment === 'production' ? 'real-money' : 'sandbox'} order`}</button></div>
    </section></div>}
  </section>
}
