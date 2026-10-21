import assert from 'node:assert/strict'
import test from 'node:test'
import { build } from 'esbuild'

const bundle = await build({ entryPoints: ['src/providerTelemetry.ts'], bundle: true, write: false, format: 'esm', platform: 'node' })
const { providerFacts, verifiedObservationAge } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)
const NOW = Date.parse('2026-10-21T17:00:00Z')
const facts = (health) => Object.fromEntries(providerFacts(health, NOW).map(row => [row.label, row.value]))

test('recent receipt never creates a quote-verification assertion', () => {
  assert.deepEqual(facts({ last_received_at: '2026-10-21T16:59:58Z', mapped_quotes: 0, quarantined: 58 }), {
    'Last message': '2 s ago', 'Mapped quotes': '0', 'Quarantined quotes': '58',
  })
})
test('unverified source clock never masquerades as a fresh observation', () => {
  const unverified = { source_time: '2026-10-21T16:59:59Z', source_price_changed_at: '2026-10-21T16:59:59Z',
    source_observed_at: '2026-10-21T16:59:59Z', observation_verified: false }
  assert.equal(verifiedObservationAge(unverified, NOW), 'Unverified')
  assert.equal(verifiedObservationAge({ ...unverified, observation_verified: true, source_observed_at: null }, NOW), 'Unverified')
  assert.equal(verifiedObservationAge({ ...unverified, observation_verified: true }, NOW), '1s')
  assert.equal(verifiedObservationAge({ ...unverified, observation_verified: true,
    source_observed_at: '2026-10-21T17:01:00Z' }, NOW), 'Future clock')
})
test('native transport time uses seconds and exposes zero market payloads', () => {
  assert.deepEqual(facts({ last_message_time: (NOW - 500) / 1000, frames: 100, market_frames: 0 }), {
    'Last message': 'Under 1 s ago', 'Received messages': '100', 'Market payloads': '0',
  })
})
test('quiet sources display elapsed time without arbitrary freshness approval', () => {
  assert.equal(facts({ last_message_time: (NOW - 24 * 60000) / 1000 })['Last message'], '24 min ago')
  assert.equal(facts({ last_message_time: (NOW - 2 * 3600000) / 1000 })['Last message'], '2 h ago')
})
test('invalid and missing source clocks do not appear current', () => {
  for (const value of [null, '', false, 'invalid', '2026-10-21T17:00:00', -1, 0, Infinity]) {
    assert.equal(facts({ last_message_time: value })['Last message'], 'Unavailable')
  }
  assert.equal(facts({ last_message_time: (NOW + 60000) / 1000 })['Last message'], 'Clock ahead')
})
test('unknown counters never become fabricated zeros', () => {
  for (const value of [null, false, '0', -1, 1.5, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(facts({ frames: value })['Received messages'], 'Unavailable')
  }
  assert.deepEqual(facts({ authenticated: true, secret_key: 'OFFLINE test value' }), {})
})
test('market and book diagnostics remain separate from private traffic', () => {
  assert.deepEqual(facts({ market_messages: 0, private_messages: 5, monitored_books: 1, orderbook_markets: 0 }), {
    'Market messages': '0', 'Monitored books': '1', 'Order-book markets': '0',
  })
})
test('a connected sport stream is distinct from a provider-confirmed complete game board', () => {
  assert.deepEqual(facts({ ready_sports: ['nba', 'nfl'], complete_baseline_sports: ['nba'],
    quarantined: 20, out_of_scope_prop_rows: 4 }), {
    'Quarantined quotes': '20', 'Out-of-scope prop rows': '4',
    'Ready sport streams': '2', 'Complete game-board baselines': '1',
  })
})
