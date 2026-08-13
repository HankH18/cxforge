// Small display-formatting helpers shared by the feed, draft detail, and
// metrics panel. No business logic lives here — just presentation.

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(0)}%`
}

export function formatSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value.toFixed(1)}s`
}

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

/** Human-readable message for anything thrown by the API client — used so
 * the reviewer sees a real explanation instead of "[object Object]". */
export function describeError(error: unknown): string {
  if (error instanceof Error) return error.message
  return String(error)
}
