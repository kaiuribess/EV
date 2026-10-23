import { useEffect, useState } from 'react'
import { request } from './api'
import { human, text, type Json } from './types'

export function PolymarketUSMarkets() {
  const [data, setData] = useState<Json | null>(null)
  useEffect(() => {
    let alive = true
    const refresh = () => request('/api/native/market-data/polymarket-us')
      .then(value => { if (alive) setData(value) })
      .catch(() => { if (alive) setData(null) })
    void refresh()
    const timer = setInterval(refresh, 10000)
    return () => { alive = false; clearInterval(timer) }
  }, [])
  if (!data?.available) return null
  return <section className="panel">
    <div className="section-head"><div><h2>Polymarket US market data</h2><p>Current public or authenticated full books. Discovery alone does not approve a trade mapping.</p></div></div>
    <p className="quiet-message">{text(data.discovered_count)} discovered · {text(data.current_market_count)} current full books · {data.market_streaming ? 'authenticated stream connected' : 'public REST only or stream disconnected'} · catalog {text(data.catalog_at)}</p>
    {(data.markets ?? []).map((market: Json) => <details key={market.slug} className="setting-row">
      <summary>{human(market.league)} · {text(market.event)} · {text(market.slug)} · {market.current ? 'CURRENT DEPTH' : `NOT EXECUTABLE: ${text(market.rejection)}`}</summary>
      <p>{text(market.question)} · {text(market.market_status)} / {text(market.book_state)} · {text(market.book_source)} · source {text(market.source_time)} ({text(market.source_age_seconds)}s)</p>
      <p>YES bid levels {JSON.stringify(market.bid_levels ?? [])} · YES offer levels {JSON.stringify(market.offer_levels ?? [])} (USD price, fractional contracts)</p>
      {(market.directions ?? []).map((direction: Json) => <p key={direction.direction}>{direction.direction === 'long' ? 'Buy LONG / YES' : 'Sell SHORT / NO'} {text(direction.outcome)} · effective cost ${text(direction.effective_win_cost_usd)} · available {text(direction.available_contracts)} contracts · one-contract fee ${text(direction.one_contract_taker_fee_usd)}{direction.direction === 'short' ? ` · $1 margin, sale proceeds $${text(direction.sale_proceeds_per_contract_usd)}` : ''}</p>)}
      <small>Unreviewed native identity · no account or order permission is inferred.</small>
    </details>)}
  </section>
}
