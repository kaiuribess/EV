import { useEffect, useState } from 'react'
import { request } from './api'

type NativeOrder = {
  status: string; filled_quantity: string; filled_cost_usd: string; fees: string;
  gross_payout_usd: string; reconciliation_complete: boolean; reason: string;
  intent: { id: string; quantity: string; limit_price: string; mode: string;
    instrument: { venue: string; canonical_market_id: string; outcome: string; quantity_unit: string } }
}

export function NativeOrders() {
  const [orders, setOrders] = useState<NativeOrder[]>([])
  const [error, setError] = useState(false)
  useEffect(() => {
    let closed = false
    let controller: AbortController | undefined
    const refresh = async () => {
      if (document.hidden) return
      controller?.abort()
      controller = new AbortController()
      try {
        const result = await request('/api/native/orders', undefined, controller.signal)
        if (!closed) { setOrders(Array.isArray(result.orders) ? result.orders : []); setError(false) }
      } catch (e) { if (!closed && (e as Error).name !== 'AbortError') setError(true) }
    }
    void refresh()
    const timer = setInterval(() => void refresh(), 5000)
    return () => { closed = true; clearInterval(timer); controller?.abort() }
  }, [])
  return <section className="panel" aria-label="Native order ledger">
    <div className="section-head"><div><h2>Native order ledger</h2><p>Actual venue units, costs and reconciliation status.</p></div></div>
    {error && <p className="quiet-message">Ledger refresh unavailable. Displayed records may be out of date.</p>}
    {orders.length ? <div className="table-scroll"><table><thead><tr><th>VENUE / MARKET</th><th>MODE</th><th>STATE</th><th>FILLED / REQUESTED</th><th>COST / FEES</th><th>RECONCILIATION</th></tr></thead><tbody>{orders.map(o => <tr key={o.intent.id}>
      <td>{o.intent.instrument.venue}<small>{o.intent.instrument.canonical_market_id} · {o.intent.instrument.outcome}</small></td>
      <td>{o.intent.mode}</td><td>{o.status}</td>
      <td>{o.filled_quantity} / {o.intent.quantity}<small>{o.intent.instrument.quantity_unit.replaceAll('_', ' ')}</small></td>
      <td>${o.filled_cost_usd}<small>Fees ${o.fees}</small></td>
      <td>{o.reconciliation_complete ? 'Confirmed' : 'Pending'}<small>{o.reason}</small></td>
    </tr>)}</tbody></table></div> : <p className="quiet-message">No native orders recorded. Launch checks must pass before native execution can be enabled.</p>}
  </section>
}
