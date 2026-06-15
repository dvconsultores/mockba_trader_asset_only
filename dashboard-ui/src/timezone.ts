/**
 * Convert a timestamp string to Caracas time (UTC-4).
 *
 * Accepts:
 *   - Log format:  "2026-06-15 03:49:54,514"  (YYYY-MM-DD HH:MM:SS,mmm)
 *   - DB/ISO format: "2026-06-15T03:49:54"     (ISO 8601)
 *   - Already in Caracas time (passes through if it starts with "VE" prefix)
 *
 * Returns the same format but shifted -4h from UTC.
 */

const UTC_OFFSET_MS = -4 * 60 * 60 * 1000 // UTC-4

/** Regex matches log timestamps: "2026-06-15 03:49:54,514" */
const LOG_TS_RE = /\b(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})(,\d{3})?\b/g

export function toCaracasTime(dateOrIso: string | Date): Date {
  const d = typeof dateOrIso === 'string' ? new Date(dateOrIso + (dateOrIso.includes('T') ? '' : 'Z')) : dateOrIso
  return new Date(d.getTime() + UTC_OFFSET_MS)
}

/** Regex matches DB timestamp format: "2026-06-14 17:12:22" (space, no ms, UTC) */
const DB_TS_RE = /^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}$/

/** Convert a single timestamp string (ISO, DB, or log format) to Caracas time string. */
export function convertTimestampToCaracas(ts: string): string {
  if (!ts) return '—'
  // Handle DB format: "2026-06-14 17:12:22" (space, no T, UTC)
  if (DB_TS_RE.test(ts)) {
    return _shiftAndFormat(ts.replace(' ', 'T') + 'Z')
  }
  // Handle ISO format: "2026-06-15T03:49:54"
  if (ts.includes('T')) {
    return _shiftAndFormat(ts + (ts.endsWith('Z') ? '' : 'Z'))
  }
  return ts
}

function _shiftAndFormat(utcStr: string): string {
  const d = new Date(utcStr)
  if (isNaN(d.getTime())) return utcStr
  const caracas = new Date(d.getTime() + UTC_OFFSET_MS)
  // Use UTC getters because we already shifted the epoch — the result is
  // the Caracas wall-clock time expressed as a UTC timestamp
  const mm = String(caracas.getUTCMonth() + 1).padStart(2, '0')
  const dd = String(caracas.getUTCDate()).padStart(2, '0')
  const hh = String(caracas.getUTCHours()).padStart(2, '0')
  const min = String(caracas.getUTCMinutes()).padStart(2, '0')
  const ss = String(caracas.getUTCSeconds()).padStart(2, '0')
  return `${mm}-${dd} ${hh}:${min}:${ss}`
}

/** Replace every UTC timestamp in a log line with Caracas time. */
export function convertLogLineToCaracas(line: string): string {
  return line.replace(LOG_TS_RE, (_match, datePart, msPart) => {
    const utcStr = datePart.replace(' ', 'T') + 'Z'
    const d = new Date(utcStr)
    if (isNaN(d.getTime())) return _match // unparseable, leave as-is
    const caracas = new Date(d.getTime() + UTC_OFFSET_MS)
    const iso = caracas.toISOString() // "2026-06-15T00:49:54.514Z"
    const date = iso.substring(0, 10)                  // "2026-06-15"
    const time = iso.substring(11, 23)                 // "00:49:54.514"
    const timeNoMs = time.replace('.', ',')            // "00:49:54,514"
    return `${date} ${timeNoMs}`
  })
}
