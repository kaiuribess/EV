export type ProviderFact = { label: string; value: string }

// Source observation is distinct from a reported price-change clock and the
// transport receipt clock. Never turn either of those into quote freshness.
export function verifiedObservationAge(observation: Record<string, unknown>, now = Date.now()): string {
  const value = observation.observation_verified === true ? observation.source_observed_at : null
  if (typeof value !== 'string' || !/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return 'Unverified'
  const observed = Date.parse(value)
  if (!Number.isFinite(observed) || !Number.isFinite(now)) return 'Unverified'
  const elapsed = now - observed
  if (elapsed < -5000) return 'Future clock'
  if (elapsed < 60000) return `${Math.max(0, Math.floor(elapsed / 1000))}s`
  if (elapsed < 3600000) return `${Math.floor(elapsed / 60000)}m`
  return `${Math.floor(elapsed / 3600000)}h`
}

const count = (value: unknown): string =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? value.toLocaleString('en-US') : 'Unavailable'

function messageAge(value: unknown, now: number): string {
  const clock = typeof value === 'number' && value > 0 ? value * 1000
    : typeof value === 'string' && /T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? Date.parse(value) : NaN
  if (!Number.isFinite(clock) || !Number.isFinite(now)) return 'Unavailable'
  const age = now - clock
  if (age < -1000) return 'Clock ahead'
  if (age < 1000) return 'Under 1 s ago'
  if (age < 60000) return `${Math.floor(age / 1000)} s ago`
  if (age < 3600000) return `${Math.floor(age / 60000)} min ago`
  return `${Math.floor(age / 3600000)} h ago`
}

// Transport diagnostics only. Message receipt does not verify a quote's status,
// source observation time, mapping, settlement rules, or executability.
export function providerFacts(health: Record<string, unknown>, now = Date.now()): ProviderFact[] {
  const facts: ProviderFact[] = []
  if ('valid_book_count' in health) {
    facts.push({ label: 'Connection', value: String(health.state ?? health.status ?? 'Unknown') })
    facts.push({ label: 'Last market book', value: messageAge(health.last_market_data_at, now) })
    facts.push({ label: 'Last valid full book', value: messageAge(health.last_valid_full_book_at, now) })
    facts.push({ label: 'Valid books', value: count(health.valid_book_count) })
    facts.push({ label: 'Subscriptions', value: count(health.subscription_count) })
    facts.push({ label: 'Reconnects', value: count(health.reconnect_count) })
    facts.push({ label: 'Genuine book updates', value: count(health.market_data_updates) })
    facts.push({ label: 'Oldest valid book age', value: typeof health.oldest_valid_book_age_seconds === 'number'
      ? `${health.oldest_valid_book_age_seconds.toFixed(1)} s` : 'Unavailable' })
    facts.push({ label: 'Data stale', value: health.data_stale === true ? 'Yes' : health.data_stale === false ? 'No' : 'Unknown' })
    facts.push({ label: 'Last error', value: typeof health.last_reported_error === 'string' ? health.last_reported_error : 'None reported' })
  }
  if ('last_received_at' in health || 'last_message_time' in health) {
    facts.push({ label: 'Last message', value: messageAge(health.last_received_at ?? health.last_message_time, now) })
  }
  for (const [field, label] of [['frames', 'Received messages'], ['market_frames', 'Market payloads'],
    ['market_messages', 'Market messages'], ['mapped_quotes', 'Mapped quotes'],
    ['quarantined', 'Quarantined quotes'], ['monitored_books', 'Monitored books'],
    ['orderbook_markets', 'Order-book markets'], ['out_of_scope_prop_rows', 'Out-of-scope prop rows']] as const) {
    if (field in health) facts.push({ label, value: count(health[field]) })
  }
  for (const [field, label] of [['ready_sports', 'Ready sport streams'],
    ['complete_baseline_sports', 'Complete game-board baselines']] as const) {
    if (field in health) {
      const value = health[field]
      facts.push({ label, value: Array.isArray(value) && value.every(item => typeof item === 'string')
        ? count(value.length) : 'Unavailable' })
    }
  }
  return facts
}
