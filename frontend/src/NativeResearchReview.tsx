import { useEffect, useMemo, useState } from 'react'
import { request } from './api'
import { eventName, human, marketName, money, percent, text, type Json, type State } from './types'

type Props = {
  state: State
  mutate: (path: string, body: Json) => Promise<Json>
  notify: (message: string, error?: boolean) => void
  connected: boolean
}

export function NativeResearchReview({ state, mutate, notify, connected }: Props) {
  const markets = state.reference_estimates.filter(row => row.quality_mode === 'trusted_provider' &&
    text(row.market_id).startsWith('research:'))
  const [marketId, setMarketId] = useState('')
  const [margin, setMargin] = useState('')
  const [drafts, setDrafts] = useState<Json[]>([])
  const [busy, setBusy] = useState(false)
  const selected = markets.find(row => row.market_id === marketId)
  const leads = useMemo(() => markets.flatMap(row => (row.native_research_estimates ?? [])
    .filter((offer: Json) => offer.quality === 'provisional_native_research')
    .map((offer: Json) => ({ row, offer }))), [state.reference_estimates])

  const reload = () => request('/api/native/research-review/drafts')
    .then(data => setDrafts(Array.isArray(data.items) ? data.items : []))
    .catch(() => setDrafts([]))
  useEffect(() => { void reload() }, [])
  useEffect(() => {
    const configured = state.pricing?.trusted_research_market_margins?.[marketId]
    setMargin(configured == null ? '' : String(configured))
  }, [marketId, state.pricing?.trusted_research_market_margins?.[marketId]])

  const saveMargin = async () => {
    if (!selected || margin === '' || !Number.isFinite(Number(margin)) || Number(margin) < 0 || Number(margin) >= 1) return
    setBusy(true)
    try {
      await mutate('/api/settings', { trusted_research_market_margins: {
        ...(state.pricing?.trusted_research_market_margins ?? {}), [marketId]: margin,
      } })
      notify('Research-only allowance saved for this exact market. Pilot approval remains off.')
    } catch (error) { notify((error as Error).message, true) } finally { setBusy(false) }
  }
  const saveDraft = async (row: Json, offer: Json) => {
    setBusy(true)
    try {
      await mutate('/api/native/research-review/drafts', {
        source_market_id: row.market_id, venue: offer.venue,
        native_market_id: offer.native_market_id, native_outcome_id: offer.native_outcome_id,
        strategy: row.strategy,
      })
      await reload()
      notify('Unapproved native mapping review saved. No trading permission was changed.')
    } catch (error) { notify((error as Error).message, true) } finally { setBusy(false) }
  }
  const previous = (state.research_history ?? []).find((row: Json) => row.market_id === marketId)
  const change = selected && previous ? Math.max(...Object.keys(selected.probabilities ?? {})
    .map(side => Math.abs(Number(selected.probabilities[side]) - Number(previous.probabilities?.[side])))) : null

  return <>
    <section className="panel">
      <div className="section-head"><div><h2>Source uncertainty scenarios</h2><p>Select an allowance for one exact market. This is a research assumption, not an approved live policy.</p></div></div>
      <div className="form-fields">
        <label className="form-field"><span>Current sportsbook market</span><select value={marketId} onChange={event => setMarketId(event.target.value)}>
          <option value="">Select one current market</option>{markets.map(row => <option key={row.market_id} value={row.market_id}>{eventName(row)} · {marketName(row)}</option>)}
        </select></label>
        <label className="form-field"><span>Probability allowance</span><input aria-label="Research uncertainty allowance" type="number" min="0" max="0.999" step="0.001" value={margin} onChange={event => setMargin(event.target.value)} placeholder="Enter a probability fraction" /></label>
      </div>
      {selected && <p className="quiet-message">Current cross-book probability range: {percent(selected.reference_disagreement_probability, 2)}.
        {change == null || !Number.isFinite(change) ? ' No comparable historical estimate is recorded; this allowance is uncalibrated.' : ` Change from the last expired estimate: ${percent(change, 2)}. This sample does not validate an optimal allowance.`}
      </p>}
      <button className="button primary" disabled={!connected || busy || !selected || margin === ''} onClick={() => void saveMargin()}>Save research scenario</button>
    </section>
    <section className="panel">
      <div className="section-head"><div><h2>Native mapping review</h2><p>Discovery leads and saved drafts are unapproved. Exact identity, rules, fees and payout need owner review before a mapping can be used for execution.</p></div></div>
      {leads.slice(0, 12).map(({ row, offer }) => <div className="setting-row" key={`${row.market_id}:${offer.venue}:${offer.native_market_id}:${offer.native_outcome_id}`}>
        <div><strong>{eventName(row)} · {marketName(row)} · {human(offer.outcome)}</strong>
          <p>{human(offer.venue)} {text(offer.native_market_id)} / {text(offer.native_outcome_id)}{offer.native_strike_id ? ` / strike ${text(offer.native_strike_id)}` : ''} · {text(offer.normal_win_condition)}</p>
          <small>Start {text(offer.native_start)} · {offer.venue === 'prophetx' ? 'USD stake / American odds' : `payout $${text(offer.price_denomination_usd)} per contract`} · rule {text(offer.rules_sha256)} · {text(offer.rules_excerpt)}</small><br />
          <small>Fee evidence {text(offer.fee_evidence?.source)} · observed {text(offer.fee_evidence?.observed_at)} · charge phase {text(offer.fee_evidence?.charged, text(offer.fee_evidence?.event_phase))} · coefficient {text(offer.fee_evidence?.coefficient, text(offer.fee_evidence?.series_fee_multiplier))}</small><br />
          <small>Buy {offer.price == null ? 'depth unavailable' : offer.venue === 'prophetx' ? `${text(offer.american_price)} American · $${text(offer.available_stake_usd)} available stake` : `${text(offer.price)} · ${text(offer.available_depth_contracts)} contracts`} · fee {offer.taker_fee_usd == null ? 'unverified' : money(offer.taker_fee_usd, 4)} · net {offer.net_estimated_profit_usd == null ? 'allowance unset' : money(offer.net_estimated_profit_usd, 4)}</small>
        </div>
        <button className="button" disabled={!connected || busy || state.auto_trade || !state.kill_switch} onClick={() => void saveDraft(row, offer)}>Save unapproved draft</button>
      </div>)}
      {!leads.length && <p className="quiet-message">No exact current native discovery lead has overlapping eligible sportsbook evidence and executable depth.</p>}
      {drafts.slice(0, 10).map(draft => <details key={draft.draft_id} className="setting-row"><summary>{text(draft.source?.event_name)} · {human(draft.venue)} · {text(draft.native_discovery?.outcome)} · UNAPPROVED</summary>
        <p>Captured {text(draft.captured_at)} · market {text(draft.native_market_id)} · outcome {text(draft.native_outcome_id)}</p>
        <p>Condition: {text(draft.native_discovery?.normal_win_condition)} · rules fingerprint {text(draft.native_discovery?.rules_sha256)}</p>
        <p>Fee source: {text(draft.native_discovery?.fee_evidence?.source)} · observed {text(draft.native_discovery?.fee_evidence?.observed_at)} · phase {text(draft.native_discovery?.fee_evidence?.event_phase, text(draft.native_discovery?.fee_evidence?.charged))}</p>
        <p>{(draft.review_required ?? []).join(' · ')}</p></details>)}
    </section>
  </>
}

export function ResearchHistory({ entries }: { entries: Json[] }) {
  return <section className="panel"><div className="section-head"><div><h2>Expired research comparisons</h2>
    <p>Original quote and native calculation times are retained for review. These prices are never executable from history.</p></div></div>
    {entries.slice(0, 30).map((row, index) => <details key={`${row.market_id}:${row.expired_at}:${index}`} className="setting-row">
      <summary>{eventName(row)} · {marketName(row)} · EXPIRED RESEARCH</summary>
      <p>Calculated {text(row.calculated_at)} · expired {text(row.expired_at)} · {text(row.expiration_reason)}</p>
      {(row.native_research_estimates ?? []).slice(0, 4).map((offer: Json, item: number) => <p key={item}>
        {human(offer.venue)} {human(offer.outcome)} · original native calculation {text(offer.calculated_at)} · price {text(offer.price)} · gross {text(offer.gross_estimated_profit_usd)} · fee {text(offer.taker_fee_usd)} · allowance {text(offer.uncertainty_allowance_usd, 'unconfigured')}
      </p>)}
    </details>)}
    {!entries.length && <p className="quiet-message">No research comparison has expired since this process started.</p>}
  </section>
}
