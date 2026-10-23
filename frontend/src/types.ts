export type Json = Record<string, any>
export type Page = 'opportunities' | 'markets' | 'providers' | 'trading' | 'performance' | 'settings'
export interface State extends Json {
  mode: string
  auto_trade: boolean
  kill_switch: boolean
  plugins: Json[]
  markets: Json[]
  quotes: Json[]
  opportunities: Json[]
  reference_estimates: Json[]
  arbitrage_research: Json[]
  orders: Json[]
  positions: Json[]
  metrics: Json
  risk: Json
  events: Json[]
  signals: Json[]
  quarantine: Json[]
}
export const emptyState: State = {
  mode: 'disconnected', auto_trade: false, kill_switch: false, plugins: [],
  markets: [], quotes: [], opportunities: [], reference_estimates: [], arbitrage_research: [], orders: [], positions: [],
  metrics: {}, risk: {}, events: [], signals: [], quarantine: [],
}
export const normalizeState = (data: Json): State => ({
  ...emptyState, ...data,
  plugins: Array.isArray(data.plugins) ? data.plugins : [],
  markets: Array.isArray(data.markets) ? data.markets : [],
  quotes: Array.isArray(data.quotes) ? data.quotes : [],
  opportunities: Array.isArray(data.opportunities) ? data.opportunities : [],
  reference_estimates: Array.isArray(data.reference_estimates) ? data.reference_estimates : [],
  arbitrage_research: Array.isArray(data.arbitrage_research) ? data.arbitrage_research : [],
  orders: Array.isArray(data.orders) ? data.orders : [],
  positions: Array.isArray(data.positions) ? data.positions : [],
  events: Array.isArray(data.events) ? data.events : [],
  signals: Array.isArray(data.signals) ? data.signals : [],
  quarantine: Array.isArray(data.quarantine) ? data.quarantine : [],
  metrics: {
    ...(data.metrics || {}),
    internal_latency_ms: data.metrics?.latency?.internal_processing_ms ?? data.metrics?.internal_latency_ms,
    opportunities_detected: data.metrics?.counters?.opportunities_detected ?? data.metrics?.opportunities_detected,
  },
  risk: data.risk || {},
  settings: data.pricing ?? data.settings ?? {},
})
export const num = (v: any, fallback = 0) => Number.isFinite(Number(v)) ? Number(v) : fallback
export const text = (v: any, fallback = '—'): string => v === undefined || v === null ? fallback : typeof v === 'object' ? String(v.name ?? v.id ?? v.status ?? JSON.stringify(v)) : String(v)
export const human = (v: any) => text(v).replaceAll('_', ' ').replaceAll('-', ' ')
export const percent = (v: any, digits = 1) => `${(num(v) * 100).toFixed(digits)}%`
// Display-only translation of a $1 binary payout fraction. Fees and contract
// settlement are intentionally not inferred from the quoted percentage.
export const probabilityOdds = (value: unknown): { decimal: string; american: string } | null => {
  if (value === null || value === undefined || value === '') return null
  const probability = Number(value)
  if (!Number.isFinite(probability) || probability <= 0 || probability > 1) return null
  const decimal = 1 / probability
  if (!Number.isFinite(decimal)) return null
  const american = probability === 1 ? '—' : probability <= 0.5
    ? `+${Math.round(100 * (1 - probability) / probability)}`
    : String(Math.round(-100 * probability / (1 - probability)))
  return { decimal: decimal.toFixed(2), american }
}
export const money = (v: any, digits = 2) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: digits, maximumFractionDigits: digits }).format(num(v))
export const duration = (v: any) => v === undefined || v === null ? '—' : num(v) < 1000 ? `${Math.round(num(v))} ms` : `${(num(v) / 1000).toFixed(1)} s`
export const clockTime = (v: any) => !v ? '—' : new Date(v).toLocaleTimeString('en-US', { hour12: false })
export const marketName = (m: Json) => m.player ? `${text(m.player)} · ${human(m.player_stat ?? m.statistic ?? m.market_type)} ${m.line ?? ''}` : `${human(m.market_type)}${m.line != null ? ` ${m.line}` : ''}`
export const eventName = (m: Json) => text(m.event_name ?? m.name ?? m.event_id ?? m.id)
export const isPositive = (s: any) => ['healthy', 'connected', 'streaming', 'ok', 'running', 'ready', 'active', 'valid', 'qualified', 'approved', 'filled', 'positive ev', 'live'].includes(String(s).toLowerCase())
export const isNegative = (s: any) => ['error', 'failed', 'blocked', 'rejected', 'stale', 'suspended', 'disconnected', 'unknown', 'invalid', 'negative ev', 'missing_credentials'].includes(String(s).toLowerCase())
export const qualifies = (o: Json) => o.status === 'POSITIVE EV' || (!o.status && num(o.ev_per_contract) > 0)
