import assert from 'node:assert/strict'
import test from 'node:test'
import { build } from 'esbuild'
const bundle = await build({ entryPoints: ['src/launchSetupModel.ts'], bundle: true, write: false, format: 'esm', platform: 'node' })
const model = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)
const { parseSetupReport, activationGate, activationBody, candidateCounts, weightSummary, collectionEditorValue, collectionField } = model
const NOW = Date.parse('2026-10-21T05:00:00Z')
const DIGEST = 'a'.repeat(64)
function report() {
  return {
    schema_version: 1, checked_at: new Date(NOW - 20).toISOString(), environment: 'production', safe_to_arm: true,
    activation: { available: true, scope_digest: DIGEST, requires_restart: false, action_label: 'Enable live trading', reason: 'Checks passed' },
    switches: { live_enabled: true, auto_trade: false, kill_switch: true },
    scope: { profile_count: 1, verified_native_markets: 1, real_reference_markets: 1, native_candidates: 0, synthetic_candidates: 27 },
    risk_policy: { source: 'launch_profile', applied: true, error: null, values: {
      max_order_stake_usd: '5', max_event_exposure_usd: '5', max_player_exposure_usd: '5', max_correlated_exposure_usd: '5',
      max_venue_exposure_usd: null, max_total_exposure_usd: null, daily_loss_limit_usd: null,
      available_cash_only: true, block_overlapping_events: true, block_overlapping_players: true, max_open_orders: 20, max_orders_per_minute: 30,
    } },
    accounts: [{ venue: 'example_native', enabled: true, configured_environment: 'production', runtime_environment: 'production',
      credential_profile: 'environment', credentials_configured: true, authenticated: true, streaming: true, monitor_running: true,
      profile_configured: true, verified_native_markets: 1, current_native_books: 1, settlement_policy: 'reviewed_policy', supported_settlement_policies: ['reviewed_policy'], blockers: [],
      account_snapshot: { available: true, current: true, identity_verified: true, reconciled: true, cash_available_verified: true,
        balance_usd: '25', observed_at: new Date(NOW - 500).toISOString() } }],
    launch_scope: [{ venue: 'example_native', environment: 'production', account_fingerprint: 'reviewed-account-fingerprint',
      markets: [{ id: 'exact-market-one', name: 'Fixture event · matching contract' }], strategy_ids: ['native_positive_ev'],
      provider_ids: ['example_feed'], settlement_policy: 'reviewed_policy' }],
    requirements: [{ code: 'runtime.gate', status: 'passed', title: 'Current checks', detail: 'Verified fixture only', external_evidence_required: false }],
    pricing: { devig_method: 'proportional', eligible_books: null, required_books: [], min_references: 2, weights: {}, max_quote_age_ms: 5000, max_book_age_ms: 2500, max_disagreement: '.08', market_overrides: {} },
    inputs: [{ provider: 'example_feed', enabled: true, authenticated: true, mapped_quotes: 10, quarantined: 3 }], software_blockers: [],
  }
}
test('well-formed blocked setup preserves all accounts without profiles and no cash claim', () => {
  const raw = report(); raw.safe_to_arm = false; raw.activation.available = false
  raw.accounts[0].profile_configured = false; raw.accounts[0].account_snapshot.current = false
  raw.accounts[0].account_snapshot.balance_usd = null; raw.scope.profile_count = 0
  raw.launch_scope = []
  const parsed = parseSetupReport(raw)
  assert.equal(parsed.accounts.length, 1)
  assert.equal(parsed.accounts[0].account_snapshot.balance_usd, null)
  assert.equal(activationGate(parsed, NOW, NOW).allowed, false)
})
test('valid current production approval can prepare a digest-bound body without executing it', () => {
  const parsed = parseSetupReport(report())
  assert.equal(activationGate(parsed, NOW, NOW).allowed, true)
  assert.deepEqual(activationBody(parsed, NOW, DIGEST, NOW), { auto_trade: true, kill_switch: false, expected_launch_digest: DIGEST })
})
for (const [name, mutate] of [
  ['wrong schema', r => r.schema_version = 2], ['string authorization', r => r.safe_to_arm = 'true'],
  ['naive timestamp', r => r.checked_at = '2026-10-21T05:00:00'], ['invalid timestamp', r => r.checked_at = 'invalid'],
  ['missing account current flag', r => delete r.accounts[0].account_snapshot.current], ['invalid cash', r => r.accounts[0].account_snapshot.balance_usd = 'NaN'],
  ['negative scope count', r => r.scope.native_candidates = -1], ['fractional scope count', r => r.scope.profile_count = 1.5],
  ['missing requirements', r => delete r.requirements], ['malformed blocker', r => r.accounts[0].blockers.push({ message: 'oops' })],
  ['duplicate account identity', r => r.accounts.push(structuredClone(r.accounts[0]))],
  ['unparseable weight', r => r.pricing.weights.pinnacle = 'bad'], ['blank weight', r => r.pricing.weights.pinnacle = ''],
  ['missing eligibility distinction', r => delete r.pricing.eligible_books], ['missing limits', r => delete r.risk_policy.values.max_order_stake_usd],
  ['missing software blocker fields', r => r.software_blockers = [{}]],
  ['missing scope list', r => delete r.launch_scope], ['missing account fingerprint', r => delete r.launch_scope[0].account_fingerprint],
  ['missing market identity', r => delete r.launch_scope[0].markets[0].id],
  ['duplicate scoped market', r => r.launch_scope[0].markets.push(structuredClone(r.launch_scope[0].markets[0]))],
]) test(`malformed report fails closed: ${name}`, () => { const raw = report(); mutate(raw); assert.throws(() => parseSetupReport(raw)) })
for (const [name, mutate] of [
  ['paper environment', r => r.environment = 'paper'], ['sandbox environment', r => r.environment = 'sandbox'],
  ['live disabled', r => r.switches.live_enabled = false], ['restart needed', r => r.activation.requires_restart = true],
  ['failed gate', r => r.safe_to_arm = false], ['activation unavailable', r => r.activation.available = false],
  ['missing digest', r => r.activation.scope_digest = null], ['blank digest', r => r.activation.scope_digest = ' '],
  ['malformed digest', r => r.activation.scope_digest = 'looks-ready'],
  ['unapplied limits draft', r => r.risk_policy.applied = false], ['no reviewed scope', r => r.scope.profile_count = 0],
  ['no verified markets', r => r.scope.verified_native_markets = 0], ['pending requirement', r => r.requirements[0].status = 'pending'],
  ['no requirements', r => r.requirements = []], ['contradictory account scope', r => r.accounts[0].profile_configured = false],
  ['scope profile count mismatch', r => r.launch_scope = []], ['wrong scoped venue', r => r.launch_scope[0].venue = 'different-account'],
  ['wrong scoped environment', r => r.launch_scope[0].environment = 'sandbox'], ['empty market scope', r => r.launch_scope[0].markets = []],
  ['no reference provider in scope', r => r.launch_scope[0].provider_ids = []],
  ['settlement scope mismatch', r => r.launch_scope[0].settlement_policy = 'other_policy'],
  ['account not authenticated', r => r.accounts[0].authenticated = false], ['wrong account environment', r => r.accounts[0].runtime_environment = 'sandbox'],
  ['stale account assessment', r => r.accounts[0].account_snapshot.current = false], ['unreconciled account', r => r.accounts[0].account_snapshot.reconciled = false],
  ['unverified cash', r => r.accounts[0].account_snapshot.cash_available_verified = false], ['missing cash', r => r.accounts[0].account_snapshot.balance_usd = null],
  ['future account clock', r => r.accounts[0].account_snapshot.observed_at = new Date(NOW + 1).toISOString()],
  ['account software blocker', r => r.accounts[0].blockers.push({ category: 'technical', code: 'software.unavailable', scope: 'example_native', message: 'Unavailable', action: 'Implement' })],
  ['already active', r => { r.switches.auto_trade = true; r.switches.kill_switch = false }],
]) test(`activation remains unavailable: ${name}`, () => {
  const raw = report(); mutate(raw)
  assert.equal(activationGate(parseSetupReport(raw), NOW, NOW).allowed, false)
  assert.throws(() => activationBody(parseSetupReport(raw), NOW, DIGEST, NOW))
})
test('both response time and local receipt must be current; fetch failures revoke a prior passing report', () => {
  const parsed = parseSetupReport(report())
  for (const received of [null, NOW - 12000, NOW + 1, NaN]) assert.equal(activationGate(parsed, received, NOW).allowed, false)
  assert.equal(activationGate(parsed, NOW, NOW, 'Refresh failed').allowed, false)
  for (const checked of [NOW - 12000, NOW + 1]) {
    parsed.checked_at = new Date(checked).toISOString()
    assert.equal(activationGate(parsed, NOW, NOW).allowed, false)
  }
})
test('confirmation refuses a changed scope even if every latest check passes', () => {
  const parsed = parseSetupReport(report()); parsed.activation.scope_digest = 'b'.repeat(64)
  assert.throws(() => activationBody(parsed, NOW, DIGEST, NOW), /scope changed/)
})
test('synthetic labels derive only from explicit flags, never mode, truthiness or missing fields', () => {
  assert.deepEqual(candidateCounts([{ synthetic: true }, { synthetic: false }, {}, { synthetic: 'false' }, { synthetic: null }]), { real: 1, synthetic: 1, unknown: 3 })
})
test('weight summary distinguishes default and effective allowlist weights without claiming calibration', () => {
  const p = report().pricing
  assert.equal(weightSummary(p), 'Equal weights · uncalibrated')
  p.weights = { pinnacle: '2' }
  assert.equal(weightSummary(p), 'Configured relative weights · uncalibrated')
  p.eligible_books = ['pinnacle', 'circa']; p.weights.circa = '2'
  assert.equal(weightSummary(p), 'Equal weights · uncalibrated')
})
test('nullable collection editor preserves null, empty allowlist, and specified identities', () => {
  const field = { anyOf: [{ type: 'array', items: { type: 'string' } }, { type: 'null' }], default: null }
  assert.equal(collectionField(field), 'array')
  assert.equal(collectionEditorValue(null, field), 'null')
  assert.equal(collectionEditorValue(undefined, field), 'null')
  assert.equal(collectionEditorValue([], field), '[]')
  assert.deepEqual(JSON.parse(collectionEditorValue(['pinnacle'], field)), ['pinnacle'])
})
