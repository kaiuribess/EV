export type ProviderFact = { label: string; value: string }

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
  if ('last_received_at' in health || 'last_message_time' in health) {
    facts.push({ label: 'Last message', value: messageAge(health.last_received_at ?? health.last_message_time, now) })
  }
  for (const [field, label] of [['frames', 'Received messages'], ['market_frames', 'Market payloads'],
    ['market_messages', 'Market messages'], ['mapped_quotes', 'Mapped quotes'],
    ['quarantined', 'Quarantined quotes'], ['monitored_books', 'Monitored books'],
    ['orderbook_markets', 'Order-book markets']] as const) {
    if (field in health) facts.push({ label, value: count(health[field]) })
  }
  return facts
}
