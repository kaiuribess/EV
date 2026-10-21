import { useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { request } from './api'
import { human, money, type Json } from './types'

export function AccountCash() {
  const [accounts, setAccounts] = useState<Record<string, Json>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [clock, setClock] = useState(Date.now())
  useEffect(() => {
    let closed = false
    const controller = new AbortController()
    const load = async () => {
      try {
        const result = await request('/api/accounts/cash', undefined, controller.signal)
        if (!closed && result.read_only === true && result.available_for_kelly === false) setAccounts(result.accounts ?? {})
      } catch { if (!closed) setError('Reported balances are unavailable.') }
    }
    void load()
    const timer = setInterval(() => { setClock(Date.now()); if (!document.hidden) void load() }, 10000)
    return () => { closed = true; controller.abort(); clearInterval(timer) }
  }, [])
  const refresh = async () => {
    setBusy(true); setError('')
    try {
      const result = await request('/api/accounts/cash/refresh', {})
      if (result.read_only !== true || result.available_for_kelly !== false || typeof result.accounts !== 'object') throw new Error('Invalid balance response')
      setAccounts(result.accounts); setClock(Date.now())
    } catch { setError('Balance refresh failed. Previous amounts are historical observations.') }
    finally { setBusy(false) }
  }
  return <section className="setup-subpanel" aria-label="Reported account balances">
    <div className="setup-section-title"><h3>Reported account balances</h3><button className="button quiet" disabled={busy} onClick={() => void refresh()}><RefreshCw size={14} />{busy ? 'Reading accounts…' : 'Refresh balances'}</button></div>
    <p className="setup-note">A balance read shows what each account reports. Live Kelly sizing separately requires current spendable cash, reconciled positions and pending reservations.</p>
    {error && <p className="setup-inline-error" role="alert">{error}</p>}
    <div className="setup-limits">{Object.entries(accounts).map(([venue, row]) => {
      const value = row.reported_balance_usd
      const valid = typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value) && Number.isFinite(Number(value))
      const time = Date.parse(row.observed_at ?? '')
      const age = Number.isFinite(time) && time <= clock ? Math.floor((clock-time)/1000) : null
      return <div key={venue}><span>{human(venue)} · {human(row.environment ?? 'unknown')}<small>{age !== null ? `Observed ${age}s ago` : 'Not observed'} · {human(row.money_type ?? 'unverified balance')}</small></span><strong>{valid ? money(value) : 'Unavailable'}</strong></div>
    })}</div>
    {!Object.keys(accounts).length && <p className="setup-note">Refresh to read enabled accounts. No deposits or orders are made.</p>}
  </section>
}
