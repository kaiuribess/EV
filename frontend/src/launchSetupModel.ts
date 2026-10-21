/** Runtime validation and activation decisions: no network calls or execution side effects. */
type RecordValue = Record<string, unknown>
export type SetupBlocker = { code: string; category: string; scope: string; message: string; action: string }
export type SetupAccount = {
  venue: string; enabled: boolean; configured_environment: string; runtime_environment: string | null;
  credential_profile: string; credentials_configured: boolean; authenticated: boolean; streaming: boolean;
  monitor_running: boolean; profile_configured: boolean; verified_native_markets: number; current_native_books: number;
  account_snapshot: { available: boolean; current: boolean; identity_verified: boolean; reconciled: boolean;
    cash_available_verified: boolean; balance_usd: string | null; observed_at: string | null };
  settlement_policy: string | null; supported_settlement_policies?: string[]; blockers: SetupBlocker[];
}
export type SetupReport = {
  schema_version: 1; checked_at: string; environment: string; safe_to_arm: boolean;
  activation: { available: boolean; scope_digest: string | null; requires_restart: boolean; action_label: string; reason: string };
  switches: { live_enabled: boolean; auto_trade: boolean; kill_switch: boolean };
  scope: { profile_count: number; verified_native_markets: number; real_reference_markets: number; native_candidates: number; synthetic_candidates: number };
  risk_policy: { source: string; values: RecordValue | null; applied: boolean; error: string | null };
  accounts: SetupAccount[];
  launch_scope: { venue: string; environment: string; account_fingerprint: string;
    markets: { id: string; name: string }[]; strategy_ids: string[]; provider_ids: string[]; settlement_policy: string }[];
  software_blockers?: SetupBlocker[];
  requirements: { code: string; status: 'passed' | 'pending'; title: string; detail: string; external_evidence_required: boolean }[];
  pricing: { devig_method: string; eligible_books: string[] | null; required_books: string[]; min_references: number;
    weights: Record<string, string | number>; max_quote_age_ms: number; max_book_age_ms: number;
    max_disagreement: string | number; market_overrides: RecordValue };
  inputs: { provider: string; enabled: boolean; authenticated: boolean; mapped_quotes: number; quarantined: number }[];
}
const record = (v: unknown): v is RecordValue => !!v && typeof v === 'object' && !Array.isArray(v)
const strings = (v: unknown): v is string[] => Array.isArray(v) && v.every(x => typeof x === 'string' && x.trim().length > 0)
const count = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0
const bools = (v: RecordValue, keys: string[]) => keys.every(k => typeof v[k] === 'boolean')
const words = (v: RecordValue, keys: string[]) => keys.every(k => typeof v[k] === 'string')
const timestamp = (v: unknown): v is string => typeof v === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(v) && Number.isFinite(Date.parse(v))
const nullableString = (v: unknown) => v === null || typeof v === 'string'
const decimal = (v: unknown) => (typeof v === 'string' && v.trim() !== '' || typeof v === 'number') && Number.isFinite(Number(v)) && Number(v) >= 0

export function parseSetupReport(value: unknown): SetupReport {
  const fail = () => { throw new Error('Launch checks returned incomplete information. Real trading stays unavailable.') }
  if (!record(value) || value.schema_version !== 1 || !timestamp(value.checked_at)
      || typeof value.environment !== 'string' || typeof value.safe_to_arm !== 'boolean') return fail()
  const { activation, switches, scope, risk_policy: risk, pricing } = value
  if (!record(activation) || !bools(activation, ['available', 'requires_restart']) || !words(activation, ['action_label', 'reason'])
      || !nullableString(activation.scope_digest) || !record(switches) || !bools(switches, ['live_enabled', 'auto_trade', 'kill_switch'])
      || !record(scope) || !['profile_count', 'verified_native_markets', 'real_reference_markets', 'native_candidates', 'synthetic_candidates'].every(k => count(scope[k]))
      || !record(risk) || typeof risk.source !== 'string' || typeof risk.applied !== 'boolean' || !nullableString(risk.error)
      || !(risk.values === null || record(risk.values))) return fail()
  if (record(risk.values)) {
    const values = risk.values
    if (!['max_order_stake_usd', 'max_event_exposure_usd', 'max_player_exposure_usd', 'max_correlated_exposure_usd'].every(k => decimal(values[k]) && Number(values[k]) > 0)
      || !['daily_loss_limit_usd', 'max_venue_exposure_usd', 'max_total_exposure_usd'].every(k => values[k] === null || decimal(values[k]) && Number(values[k]) > 0)
      || !bools(values, ['available_cash_only', 'block_overlapping_events', 'block_overlapping_players'])
      || !['max_open_orders', 'max_orders_per_minute'].every(k => count(values[k]) && Number(values[k]) > 0)) return fail()
  }
  if (!record(pricing) || !words(pricing, ['devig_method']) || !(pricing.eligible_books === null || strings(pricing.eligible_books))
      || !strings(pricing.required_books) || !count(pricing.min_references) || pricing.min_references < 1
      || !record(pricing.weights) || Object.values(pricing.weights).some(v => !decimal(v) || Number(v) <= 0)
      || !count(pricing.max_quote_age_ms) || !count(pricing.max_book_age_ms) || !decimal(pricing.max_disagreement)
      || !record(pricing.market_overrides)) return fail()
  if (!Array.isArray(value.accounts) || value.accounts.some(a => !record(a) || !words(a, ['venue', 'configured_environment', 'credential_profile'])
      || !nullableString(a.runtime_environment) || !nullableString(a.settlement_policy)
      || !bools(a, ['enabled', 'credentials_configured', 'authenticated', 'streaming', 'monitor_running', 'profile_configured'])
      || !count(a.verified_native_markets) || !count(a.current_native_books) || !record(a.account_snapshot)
      || !bools(a.account_snapshot, ['available', 'current', 'identity_verified', 'reconciled', 'cash_available_verified'])
      || !(a.account_snapshot.balance_usd === null || typeof a.account_snapshot.balance_usd === 'string' && decimal(a.account_snapshot.balance_usd))
      || !(a.account_snapshot.observed_at === null || timestamp(a.account_snapshot.observed_at))
      || !Array.isArray(a.blockers) || a.blockers.some(b => !record(b) || !words(b, ['code', 'category', 'scope', 'message', 'action'])))) return fail()
  if (new Set(value.accounts.map(a => a.venue)).size !== value.accounts.length) return fail()
  if (!Array.isArray(value.launch_scope) || value.launch_scope.some(s => !record(s)
      || !words(s, ['venue', 'environment', 'account_fingerprint', 'settlement_policy'])
      || typeof s.account_fingerprint !== 'string' || s.account_fingerprint.trim().length === 0
      || !strings(s.strategy_ids) || !strings(s.provider_ids) || !Array.isArray(s.markets)
      || s.markets.some(m => !record(m) || !words(m, ['id', 'name']) || !m.id || !m.name)
      || new Set(s.markets.map(m => m.id)).size !== s.markets.length)
      || new Set(value.launch_scope.map(s => s.venue)).size !== value.launch_scope.length) return fail()
  if (value.accounts.some(a => a.supported_settlement_policies !== undefined && !strings(a.supported_settlement_policies))) return fail()
  if (value.software_blockers !== undefined && (!Array.isArray(value.software_blockers) || value.software_blockers.some(b => !record(b) || !words(b, ['code', 'category', 'scope', 'message', 'action'])))) return fail()
  if (!Array.isArray(value.requirements) || value.requirements.some(r => !record(r) || !words(r, ['code', 'title', 'detail'])
      || !['passed', 'pending'].includes(String(r.status)) || typeof r.external_evidence_required !== 'boolean')) return fail()
  if (!Array.isArray(value.inputs) || value.inputs.some(p => !record(p) || typeof p.provider !== 'string'
      || !bools(p, ['enabled', 'authenticated']) || !count(p.mapped_quotes) || !count(p.quarantined))) return fail()
  return value as unknown as SetupReport
}

export const SETUP_MAX_AGE_MS = 12_000
export function setupIsCurrent(report: SetupReport | null, receivedAt: number | null, now: number, error: string | null): boolean {
  if (!report || error || receivedAt === null || !Number.isFinite(receivedAt) || !Number.isFinite(now)) return false
  const age = now - Date.parse(report.checked_at), localAge = now - receivedAt
  return Number.isFinite(age) && age >= 0 && age < SETUP_MAX_AGE_MS && localAge >= 0 && localAge < SETUP_MAX_AGE_MS
}
export function activationGate(report: SetupReport | null, receivedAt: number | null, now: number, error: string | null = null): { allowed: boolean; reason: string } {
  if (!setupIsCurrent(report, receivedAt, now, error) || !report) return { allowed: false, reason: 'Refresh the current launch checks before continuing.' }
  if (report.environment !== 'production' || !report.switches.live_enabled || report.activation.requires_restart) return { allowed: false, reason: 'The reviewed production workspace must be started before real trading can be enabled.' }
  if (!report.safe_to_arm || !report.activation.available) return { allowed: false, reason: report.activation.reason }
  if (!report.activation.scope_digest || !/^[0-9a-f]{64}$/.test(report.activation.scope_digest)) return { allowed: false, reason: 'The reviewed trading scope is unavailable.' }
  if (!report.scope.profile_count || !report.scope.verified_native_markets || !report.risk_policy.applied || report.risk_policy.values === null || report.risk_policy.error
      || report.accounts.filter(a => a.profile_configured).length !== report.scope.profile_count
      || report.launch_scope.length !== report.scope.profile_count
      || report.requirements.length === 0 || report.requirements.some(r => r.status !== 'passed')) return { allowed: false, reason: 'The account, market and applied-limit checks are incomplete.' }
  if (report.launch_scope.some(scope => scope.environment !== 'production' || scope.markets.length === 0
      || scope.strategy_ids.length === 0 || scope.provider_ids.length === 0
      || !report.accounts.some(a => a.venue === scope.venue && a.profile_configured && a.settlement_policy === scope.settlement_policy))) return { allowed: false, reason: 'The exact reviewed account and market scope is incomplete.' }
  if (report.accounts.filter(a => a.profile_configured).some(a => {
    const snapshot = a.account_snapshot
    return !a.enabled || !a.credentials_configured || !a.authenticated || !a.monitor_running
      || a.configured_environment !== 'production' || a.runtime_environment !== 'production'
      || !snapshot.available || !snapshot.current || !snapshot.identity_verified || !snapshot.reconciled
      || !snapshot.cash_available_verified || snapshot.balance_usd === null || !snapshot.observed_at
      || Date.parse(snapshot.observed_at) > Date.parse(report.checked_at)
      || a.blockers.some(blocker => blocker.category !== 'arming')
  })) return { allowed: false, reason: 'Selected account facts do not agree with the current launch approval.' }
  if (report.switches.auto_trade && !report.switches.kill_switch) return { allowed: false, reason: 'Real trading is already enabled for the reviewed scope.' }
  return { allowed: true, reason: 'Current checks passed. Review the exact accounts and limits before enabling.' }
}
export function activationBody(report: SetupReport, receivedAt: number, reviewedDigest: string, now: number) {
  const gate = activationGate(report, receivedAt, now)
  if (!gate.allowed) throw new Error(gate.reason)
  if (!reviewedDigest || reviewedDigest !== report.activation.scope_digest) throw new Error('The trading scope changed. Review the new accounts, markets and limits before enabling.')
  return { auto_trade: true, kill_switch: false, expected_launch_digest: reviewedDigest }
}
export function candidateCounts(values: { synthetic?: unknown }[]) {
  return values.reduce<{ real: number; synthetic: number; unknown: number }>((counts, item) => {
    if (item.synthetic === true) counts.synthetic++
    else if (item.synthetic === false) counts.real++
    else counts.unknown++
    return counts
  }, { real: 0, synthetic: 0, unknown: 0 })
}
export function weightSummary(pricing: SetupReport['pricing']) {
  const weights = Object.values(pricing.weights).map(Number)
  const eligible = pricing.eligible_books
  const selected = eligible === null ? null : eligible.map(book => Number(pricing.weights[book] ?? 1))
  const equal = selected ? selected.every(w => w === selected[0]) : weights.every(w => w === 1)
  return `${equal ? 'Equal weights' : 'Configured relative weights'} · uncalibrated`
}

/** JSON editors must preserve nullable collections; null and [] have different policies. */
export function collectionField(field: RecordValue): 'array' | 'object' | null {
  if (field.type === 'array' || field.type === 'object') return field.type
  const variant = Array.isArray(field.anyOf) ? field.anyOf.find(v => record(v) && (v.type === 'array' || v.type === 'object')) : null
  return record(variant) ? variant.type as 'array' | 'object' : null
}
export function collectionEditorValue(value: unknown, field: RecordValue): string {
  const effective = value === undefined ? field.default === undefined ? collectionField(field) === 'array' ? [] : {} : field.default : value
  return JSON.stringify(effective, null, 2)
}
