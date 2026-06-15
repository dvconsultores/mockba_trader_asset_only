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

/** Convert a single timestamp string (ISO or log format) to Caracas time string. */
export function convertTimestampToCaracas(ts: string): string {
  // Handle ISO format: "2026-06-15T03:49:54"
  if (ts.includes('T')) {
    const d = toCaracasTime(ts)
    const mm = String(d.getMonth() + 1).padStart(2, '0')
    const dd = String(d.getDate()).padStart(2, '0')
    const hh = String(d.getHours()).padStart(2, '0')
    const min = String(d.getMinutes()).padStart(2, '0')
    const ss = String(d.getSeconds()).padStart(2, '0')
    return `${mm}-${dd} ${hh}:${min}:${ss}`
  }
  return ts
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
