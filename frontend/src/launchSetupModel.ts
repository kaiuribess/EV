/** Runtime validation and activation decisions: no network calls or execution side effects. */
type RecordValue = Record<string, unknown>
export type ReferenceSelector = { sport: string; league: string; market_type: string; statistic: string | null }
export type ReferenceProfile = {
  schema_version: 1; selectors: ReferenceSelector[]; phase: 'pregame'; weights_percent: Record<string, string | number>;
  required_any_anchor: string[]; min_distinct_books: number; status: string; live_execution_allowed: boolean;
  evidence_sources: string[]; note: string; primary_devig: 'power'; sensitivity_devig: 'proportional';
}
export type ReferenceFreshnessRule = ReferenceSelector & {
  provider: string; bookmaker: string; phase: 'pregame'; ttl_seconds: string | number; verified: boolean; evidence: string;
}
export type SetupBlocker = { code: string; category: string; scope: string; message: string; action: string }
export type SetupAccount = {
  venue: string; enabled: boolean; configured_environment: string; runtime_environment: string | null;
  credential_profile: string; credentials_configured: boolean; authenticated: boolean; streaming: boolean;
  monitor_running: boolean; profile_configured: boolean; verified_native_markets: number; current_native_books: number;
  account_snapshot: { available: boolean; current: boolean; identity_verified: boolean; reconciled: boolean;
    cash_available_verified: boolean; balance_usd: string | null; observed_at: string | null };
  diagnostic_account?: { venue: string; environment: string; account_fingerprint: string | null;
    identity_verified: boolean; observed_at: string; balance_usd: string | null;
    cash_available_verified: boolean; open_order_count: number | null; position_count: number | null;
    inventory_reconciled: boolean; trading_permission_verified: boolean;
    geographic_attestation_expires_at: string | null; usable_for_execution: false } | null;
  settlement_policy: string | null; supported_settlement_policies?: string[]; blockers: SetupBlocker[];
}
export type SetupReport = {
  schema_version: 1; checked_at: string; environment: string; safe_to_arm: boolean;
  activation: { available: boolean; scope_digest: string | null; requires_restart: boolean; action_label: string; reason: string;
    by_venue?: Record<string, { available: boolean; scope_digest: string }> };
  switches: { live_enabled: boolean; auto_trade: boolean; kill_switch: boolean; native_enabled_venues?: string[] };
  scope: { profile_count: number; verified_native_markets: number; real_reference_markets: number; native_candidates: number; synthetic_candidates: number };
  external_inventory?: { loaded_count: number; owner_tracked_count?: number; unsettled_cost_usd: string;
    pending_order_count: number; pending_venues: string[]; account_reconciled: boolean; note: string };
  risk_policy: { source: string; values: RecordValue | null; applied: boolean; error: string | null };
  accounts: SetupAccount[];
  launch_scope: { venue: string; environment: string; account_fingerprint: string;
    markets: { id: string; name: string }[]; strategy_ids: string[]; provider_ids: string[];
    settlement_policy: string; validation_pilot?: boolean }[];
  software_blockers?: SetupBlocker[];
  requirements: { code: string; status: 'passed' | 'pending'; title: string; detail: string; external_evidence_required: boolean }[];
  pricing: { devig_method: string; eligible_books: string[] | null; required_books: string[]; min_references: number;
    weights: Record<string, string | number>; max_quote_age_ms: number; max_book_age_ms: number;
    max_disagreement: string | number; market_overrides: RecordValue;
    reference_profile_mode?: 'legacy' | 'strict'; reference_profiles?: Record<string, ReferenceProfile>;
    enabled_reference_profiles?: string[]; profile_freshness_rules?: ReferenceFreshnessRule[] };
  inputs: { provider: string; enabled: boolean; authenticated: boolean; mapped_quotes: number; quarantined: number;
    quote_clock_verified_in_review_window?: number; market_status_verified_in_review_window?: number;
    both_evidence_verified_in_review_window?: number;
    review_window_capped?: boolean }[];
}
const record = (v: unknown): v is RecordValue => !!v && typeof v === 'object' && !Array.isArray(v)
const strings = (v: unknown): v is string[] => Array.isArray(v) && v.every(x => typeof x === 'string' && x.trim().length > 0)
const count = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0
const bools = (v: RecordValue, keys: string[]) => keys.every(k => typeof v[k] === 'boolean')
const words = (v: RecordValue, keys: string[]) => keys.every(k => typeof v[k] === 'string')
const timestamp = (v: unknown): v is string => typeof v === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(v) && Number.isFinite(Date.parse(v))
const nullableString = (v: unknown) => v === null || typeof v === 'string'
const decimal = (v: unknown) => (typeof v === 'string' && v.trim() !== '' || typeof v === 'number') && Number.isFinite(Number(v)) && Number(v) >= 0
const identifier = (v: unknown): v is string => typeof v === 'string' && v.trim().length > 0 && v.trim() === v && !v.includes('*')
const uniqueStrings = (v: unknown): v is string[] => strings(v) && new Set(v).size === v.length
const selectorKey = (s: ReferenceSelector) => JSON.stringify([s.sport, s.league, s.market_type, s.statistic])
function validSelector(value: unknown): value is ReferenceSelector & RecordValue {
  return record(value) && ['sport', 'league', 'market_type'].every(k => identifier(value[k]))
    && (value.market_type === 'player_prop' ? identifier(value.statistic) : value.statistic === null)
}
function validPercentWeights(value: unknown): value is Record<string, string | number> {
  if (!record(value) || !Object.keys(value).length || !Object.keys(value).every(identifier)) return false
  const parts = Object.values(value).map(v => typeof v === 'string' || typeof v === 'number' ? String(v) : '')
  if (parts.some(v => v.length > 128 || !/^\d+(?:\.\d+)?$/.test(v) || !Number.isFinite(Number(v)) || Number(v) <= 0)) return false
  // Sum decimal strings exactly: floating point must not accept a nearly-100 total.
  const scale = Math.max(...parts.map(v => v.split('.')[1]?.length ?? 0))
  const total = parts.reduce((sum, v) => {
    const [whole, fraction = ''] = v.split('.')
    return sum + BigInt(whole + fraction.padEnd(scale, '0'))
  }, 0n)
  return total === 100n * 10n ** BigInt(scale)
}
function validReferenceSettings(pricing: RecordValue): boolean {
  const fields = ['reference_profile_mode', 'reference_profiles', 'enabled_reference_profiles', 'profile_freshness_rules']
  if (fields.every(k => pricing[k] === undefined)) return true // Older legacy reports remain readable.
  if (typeof pricing.reference_profile_mode !== 'string' || !['legacy', 'strict'].includes(pricing.reference_profile_mode) || !record(pricing.reference_profiles)
      || !uniqueStrings(pricing.enabled_reference_profiles) || !Array.isArray(pricing.profile_freshness_rules)) return false
  const profiles = pricing.reference_profiles
  for (const [name, p] of Object.entries(profiles)) {
    if (!identifier(name) || !record(p) || p.schema_version !== 1 || p.phase !== 'pregame'
        || !Array.isArray(p.selectors) || !p.selectors.length || !p.selectors.every(validSelector)
        || new Set(p.selectors.map(selectorKey)).size !== p.selectors.length || !validPercentWeights(p.weights_percent)
        || !uniqueStrings(p.required_any_anchor) || !p.required_any_anchor.length
        || p.required_any_anchor.some(book => !Object.hasOwn(p.weights_percent as RecordValue, book))
        || !count(p.min_distinct_books) || p.min_distinct_books < 1 || p.min_distinct_books > Object.keys(p.weights_percent).length
        || !identifier(p.status) || typeof p.live_execution_allowed !== 'boolean' || !strings(p.evidence_sources)
        || typeof p.note !== 'string' || p.primary_devig !== 'power' || p.sensitivity_devig !== 'proportional') return false
  }
  if (pricing.enabled_reference_profiles.some(name => !Object.hasOwn(profiles, name))) return false
  const enabledSelectors = pricing.enabled_reference_profiles.flatMap(name => (profiles[name] as ReferenceProfile).selectors.map(selectorKey))
  if (new Set(enabledSelectors).size !== enabledSelectors.length) return false
  const ruleKeys: string[] = []
  for (const r of pricing.profile_freshness_rules) {
    if (!validSelector(r) || !record(r) || !identifier(r.provider) || !identifier(r.bookmaker) || r.phase !== 'pregame'
        || !decimal(r.ttl_seconds) || !/^\d+(?:\.\d+)?$/.test(String(r.ttl_seconds)) || Number(r.ttl_seconds) <= 0 || typeof r.verified !== 'boolean'
        || typeof r.evidence !== 'string' || r.evidence.trim().length < 8) return false
    ruleKeys.push(JSON.stringify([r.provider, r.bookmaker, selectorKey(r), r.phase]))
  }
  return new Set(ruleKeys).size === ruleKeys.length
}

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
  if (activation.by_venue !== undefined && (!record(activation.by_venue)
      || Object.entries(activation.by_venue).some(([venue, entry]) => !identifier(venue) || !record(entry)
        || typeof entry.available !== 'boolean' || typeof entry.scope_digest !== 'string'
        || !/^[0-9a-f]{64}$/.test(entry.scope_digest)))) return fail()
  if (switches.native_enabled_venues !== undefined && !uniqueStrings(switches.native_enabled_venues)) return fail()
  if (record(risk.values)) {
    const values = risk.values
    if (!['max_order_stake_usd', 'max_event_exposure_usd', 'max_player_exposure_usd', 'max_correlated_exposure_usd'].every(k => values[k] === null && values.available_cash_only === true || decimal(values[k]) && Number(values[k]) > 0)
      || !['daily_loss_limit_usd', 'max_venue_exposure_usd', 'max_total_exposure_usd'].every(k => values[k] === null || decimal(values[k]) && Number(values[k]) > 0)
      || values.max_order_stake_usd === null && values.stake_sizing !== 'quarter_kelly'
      || values.stake_sizing !== undefined && !['capped', 'quarter_kelly'].includes(values.stake_sizing as string)
      || values.unit_size_usd !== undefined && (!decimal(values.unit_size_usd) || Number(values.unit_size_usd) <= 0)
      || values.max_native_order_attempts !== undefined && values.max_native_order_attempts !== null
        && (!count(values.max_native_order_attempts) || Number(values.max_native_order_attempts) < 1)
      || !bools(values, ['available_cash_only', 'block_overlapping_events', 'block_overlapping_players'])
      || !['max_open_orders', 'max_orders_per_minute'].every(k => count(values[k]) && Number(values[k]) > 0)) return fail()
  }
  if (!record(pricing) || !words(pricing, ['devig_method']) || !(pricing.eligible_books === null || strings(pricing.eligible_books))
      || !strings(pricing.required_books) || !count(pricing.min_references) || pricing.min_references < 1
      || !record(pricing.weights) || Object.values(pricing.weights).some(v => !decimal(v) || Number(v) <= 0)
      || !count(pricing.max_quote_age_ms) || !count(pricing.max_book_age_ms) || !decimal(pricing.max_disagreement)
      || !record(pricing.market_overrides) || !validReferenceSettings(pricing)) return fail()
  if (!Array.isArray(value.accounts) || value.accounts.some(a => !record(a) || !words(a, ['venue', 'configured_environment', 'credential_profile'])
      || !nullableString(a.runtime_environment) || !nullableString(a.settlement_policy)
      || !bools(a, ['enabled', 'credentials_configured', 'authenticated', 'streaming', 'monitor_running', 'profile_configured'])
      || !count(a.verified_native_markets) || !count(a.current_native_books) || !record(a.account_snapshot)
      || !bools(a.account_snapshot, ['available', 'current', 'identity_verified', 'reconciled', 'cash_available_verified'])
      || !(a.account_snapshot.balance_usd === null || typeof a.account_snapshot.balance_usd === 'string' && decimal(a.account_snapshot.balance_usd))
      || !(a.account_snapshot.observed_at === null || timestamp(a.account_snapshot.observed_at))
      || !Array.isArray(a.blockers) || a.blockers.some(b => !record(b) || !words(b, ['code', 'category', 'scope', 'message', 'action'])))) return fail()
  if (new Set(value.accounts.map(a => a.venue)).size !== value.accounts.length) return fail()
  if (value.accounts.some(a => a.diagnostic_account !== undefined && a.diagnostic_account !== null && (
    !record(a.diagnostic_account) || !words(a.diagnostic_account, ['venue', 'environment'])
    || !nullableString(a.diagnostic_account.account_fingerprint)
    || !timestamp(a.diagnostic_account.observed_at)
    || !(a.diagnostic_account.balance_usd === null || decimal(a.diagnostic_account.balance_usd))
    || !['open_order_count', 'position_count'].every(k => a.diagnostic_account[k] === null || count(a.diagnostic_account[k]))
    || !nullableString(a.diagnostic_account.geographic_attestation_expires_at)
    || a.diagnostic_account.geographic_attestation_expires_at !== null && !timestamp(a.diagnostic_account.geographic_attestation_expires_at)
    || !bools(a.diagnostic_account, ['identity_verified', 'cash_available_verified', 'inventory_reconciled', 'trading_permission_verified'])
    || a.diagnostic_account.usable_for_execution !== false))) return fail()
  if (!Array.isArray(value.launch_scope) || value.launch_scope.some(s => !record(s)
      || !words(s, ['venue', 'environment', 'account_fingerprint', 'settlement_policy'])
      || typeof s.account_fingerprint !== 'string' || s.account_fingerprint.trim().length === 0
      || s.validation_pilot !== undefined && typeof s.validation_pilot !== 'boolean'
      || !strings(s.strategy_ids) || !strings(s.provider_ids) || !Array.isArray(s.markets)
      || s.markets.some(m => !record(m) || !words(m, ['id', 'name']) || !m.id || !m.name)
      || new Set(s.markets.map(m => m.id)).size !== s.markets.length)
      || new Set(value.launch_scope.map(s => s.venue)).size !== value.launch_scope.length) return fail()
  if (value.accounts.some(a => a.supported_settlement_policies !== undefined && !strings(a.supported_settlement_policies))) return fail()
  if (value.software_blockers !== undefined && (!Array.isArray(value.software_blockers) || value.software_blockers.some(b => !record(b) || !words(b, ['code', 'category', 'scope', 'message', 'action'])))) return fail()
  if (!Array.isArray(value.requirements) || value.requirements.some(r => !record(r) || !words(r, ['code', 'title', 'detail'])
      || !['passed', 'pending'].includes(String(r.status)) || typeof r.external_evidence_required !== 'boolean')) return fail()
  if (!Array.isArray(value.inputs) || value.inputs.some(p => !record(p) || typeof p.provider !== 'string'
      || !bools(p, ['enabled', 'authenticated']) || !count(p.mapped_quotes) || !count(p.quarantined)
      || p.quote_clock_verified_in_review_window !== undefined && !count(p.quote_clock_verified_in_review_window)
      || p.market_status_verified_in_review_window !== undefined && !count(p.market_status_verified_in_review_window)
      || p.both_evidence_verified_in_review_window !== undefined && !count(p.both_evidence_verified_in_review_window)
      || p.review_window_capped !== undefined && typeof p.review_window_capped !== 'boolean')) return fail()
  return value as unknown as SetupReport
}

export const SETUP_MAX_AGE_MS = 12_000
const PILOT_DEFERRED_EVIDENCE = new Set([
  'evidence.native_lifecycle', 'evidence.restart_recovery', 'evidence.settlement_reconciliation',
])
export function deferredPilotRequirement(report: SetupReport, code: string): boolean {
  return report.launch_scope.length > 0
    && report.launch_scope.every(scope => scope.venue === 'kalshi' && scope.validation_pilot === true)
    && PILOT_DEFERRED_EVIDENCE.has(code)
}
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
  if (report.pricing.reference_profile_mode === 'strict'
      && !report.requirements.some(r => r.code === 'pricing.profiles' && r.status === 'passed')) return { allowed: false, reason: 'The reference profiles still need the backend’s live-execution validation.' }
  if (report.pricing.reference_profile_mode === 'strict' && !report.pricing.enabled_reference_profiles?.length)
    return { allowed: false, reason: 'No reference profile is enabled.' }
  if (!report.scope.profile_count || !report.scope.verified_native_markets || !report.risk_policy.applied || report.risk_policy.values === null || report.risk_policy.error
      || report.accounts.filter(a => a.profile_configured).length !== report.scope.profile_count
      || report.launch_scope.length !== report.scope.profile_count
      || report.requirements.length === 0 || report.requirements.some(r => r.status !== 'passed' && !deferredPilotRequirement(report, r.code))) return { allowed: false, reason: 'The account, market and applied-limit checks are incomplete.' }
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
export function venueActivationGate(report: SetupReport | null, receivedAt: number | null, now: number, venue: string, error: string | null = null): { allowed: boolean; reason: string } {
  if (!setupIsCurrent(report, receivedAt, now, error) || !report) return { allowed: false, reason: 'Refresh the current launch checks before continuing.' }
  if (report.environment !== 'production' || !report.switches.live_enabled || report.activation.requires_restart)
    return { allowed: false, reason: 'The reviewed production workspace must be started before real trading can be enabled.' }
  if (report.switches.auto_trade || !report.switches.kill_switch)
    return { allowed: false, reason: 'Pause the current automation and restore the emergency stop before changing venues.' }
  const account = report.accounts.find(item => item.venue === venue)
  if (!account) return { allowed: false, reason: 'This exchange is not configured in the production workspace.' }
  const scope = report.launch_scope.find(item => item.venue === venue)
  if (!scope) return { allowed: false, reason: account.blockers.find(item => item.category !== 'arming')?.message ?? 'This exchange has no reviewed launch profile.' }
  const activation = report.activation.by_venue?.[venue]
  if (!activation) return { allowed: false, reason: 'The backend has not approved a venue-specific activation scope.' }
  if (!activation.available) return { allowed: false, reason: account.blockers.find(item => item.category !== 'arming')?.message ?? 'Current market, account, source or risk checks are incomplete for this exchange.' }
  if (!report.risk_policy.applied || report.risk_policy.values === null || report.risk_policy.error)
    return { allowed: false, reason: 'Reviewed risk limits are not applied.' }
  if (scope.environment !== 'production' || !scope.markets.length || !scope.strategy_ids.length || !scope.provider_ids.length
      || !account.profile_configured || account.settlement_policy !== scope.settlement_policy || !account.verified_native_markets)
    return { allowed: false, reason: 'The exact reviewed account and market scope is incomplete.' }
  const snapshot = account.account_snapshot
  if (!account.enabled || !account.credentials_configured || !account.authenticated || !account.monitor_running
      || account.configured_environment !== 'production' || account.runtime_environment !== 'production'
      || !snapshot.available || !snapshot.current || !snapshot.identity_verified || !snapshot.reconciled
      || !snapshot.cash_available_verified || snapshot.balance_usd === null || !snapshot.observed_at
      || Date.parse(snapshot.observed_at) > Date.parse(report.checked_at)
      || account.blockers.some(blocker => blocker.category !== 'arming'))
    return { allowed: false, reason: 'Selected account facts do not agree with the current launch approval.' }
  return { allowed: true, reason: `Current ${venue} checks passed. Review its exact account, markets and limits before enabling.` }
}
export function activationBody(report: SetupReport, receivedAt: number, reviewedDigest: string, now: number, venue?: string) {
  const gate = venue ? venueActivationGate(report, receivedAt, now, venue) : activationGate(report, receivedAt, now)
  if (!gate.allowed) throw new Error(gate.reason)
  const currentDigest = venue ? report.activation.by_venue?.[venue]?.scope_digest : report.activation.scope_digest
  if (!reviewedDigest || reviewedDigest !== currentDigest) throw new Error('The trading scope changed. Review the new accounts, markets and limits before enabling.')
  return { auto_trade: true, kill_switch: false, expected_launch_digest: reviewedDigest,
    ...(venue ? { native_venues: [venue] } : {}),
    authorization_expires_at: new Date(now + 5 * 60 * 1000).toISOString() }
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
  if (pricing.reference_profile_mode === 'strict') {
    const enabled = pricing.enabled_reference_profiles?.length ?? 0
    return enabled ? `${enabled} active reference profile${enabled === 1 ? '' : 's'} · provisional, not optimized` : 'No active reference profiles · signals disabled'
  }
  const weights = Object.values(pricing.weights).map(Number)
  const eligible = pricing.eligible_books
  const selected = eligible === null ? null : eligible.map(book => Number(pricing.weights[book] ?? 1))
  const equal = selected ? selected.every(w => w === selected[0]) : weights.every(w => w === 1)
  return `${equal ? 'Equal weights' : 'Configured relative weights'} · uncalibrated`
}

/** Coverage is descriptive, not authorization: each quote still needs its own exact provider rule. */
export function profileFreshnessCoverage(profile: ReferenceProfile, rules: ReferenceFreshnessRule[]) {
  const matching = rules.filter(r => r.phase === profile.phase && Object.hasOwn(profile.weights_percent, r.bookmaker)
    && profile.selectors.some(s => selectorKey(s) === selectorKey(r)))
  const verified = matching.filter(r => r.verified)
  const covered = new Set(verified.map(r => JSON.stringify([r.bookmaker, selectorKey(r)]))).size
  return { covered, total: Object.keys(profile.weights_percent).length * profile.selectors.length,
    verifiedRules: verified.length, unverifiedRules: matching.length - verified.length, rules: matching }
}

export function candidateIsResearch(candidate: RecordValue): boolean {
  return candidate.reference_inputs_eligible === false || candidate.status === 'RESEARCH ONLY'
}
export function candidateCanQualify(candidate: RecordValue, thresholdsPass: boolean): boolean {
  return !candidateIsResearch(candidate) && candidate.execution_eligible !== false && thresholdsPass
}
export function candidateDisplayStatus(candidate: RecordValue): string {
  if (candidateIsResearch(candidate)) return 'RESEARCH ONLY'
  if (candidate.execution_eligible === false) return 'NEGATIVE EV'
  return typeof candidate.status === 'string' && candidate.status ? candidate.status : 'MODEL ESTIMATE'
}
export function referenceObservationAge(quote: RecordValue): number | null {
  // Only the explicit observation clock is suitable for this column. Never relabel price-change age.
  const age = quote.observation_age_ms
  return typeof age === 'number' && Number.isFinite(age) && age >= 0 ? age : null
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
