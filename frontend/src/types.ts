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
  orders: Json[]
  positions: Json[]
  metrics: Json
  risk: Json
  events: Json[]
  signals: Json[]
}
export const emptyState: State = {
  mode: 'disconnected', auto_trade: false, kill_switch: false, plugins: [],
  markets: [], quotes: [], opportunities: [], orders: [], positions: [],
  metrics: {}, risk: {}, events: [], signals: [],
}
export const normalizeState = (data: Json): State => ({
  ...emptyState, ...data,
  plugins: Array.isArray(data.plugins) ? data.plugins : [],
  markets: Array.isArray(data.markets) ? data.markets : [],
  quotes: Array.isArray(data.quotes) ? data.quotes : [],
  opportunities: Array.isArray(data.opportunities) ? data.opportunities : [],
  orders: Array.isArray(data.orders) ? data.orders : [],
  positions: Array.isArray(data.positions) ? data.positions : [],
  events: Array.isArray(data.events) ? data.events : [],
  signals: Array.isArray(data.signals) ? data.signals : [],
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
export const money = (v: any, digits = 2) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: digits, maximumFractionDigits: digits }).format(num(v))
export const duration = (v: any) => v === undefined || v === null ? '—' : num(v) < 1000 ? `${Math.round(num(v))} ms` : `${(num(v) / 1000).toFixed(1)} s`
export const clockTime = (v: any) => !v ? '—' : new Date(v).toLocaleTimeString('en-US', { hour12: false })
export const marketName = (m: Json) => m.player ? `${text(m.player)} · ${human(m.player_stat ?? m.statistic ?? m.market_type)} ${m.line ?? ''}` : `${human(m.market_type)}${m.line != null ? ` ${m.line}` : ''}`
export const eventName = (m: Json) => text(m.event_name ?? m.name ?? m.event_id ?? m.id)
export const isPositive = (s: any) => ['healthy', 'connected', 'ok', 'running', 'ready', 'active', 'valid', 'qualified', 'approved', 'filled', 'positive ev', 'live'].includes(String(s).toLowerCase())
export const isNegative = (s: any) => ['error', 'failed', 'blocked', 'rejected', 'stale', 'suspended', 'disconnected', 'unknown', 'invalid', 'negative ev', 'missing_credentials'].includes(String(s).toLowerCase())
export const qualifies = (o: Json) => o.status === 'POSITIVE EV' || (!o.status && num(o.ev_per_contract) > 0)
