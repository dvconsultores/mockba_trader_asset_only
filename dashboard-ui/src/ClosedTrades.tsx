import { useState, useEffect } from 'react'
import { Loader2, RefreshCw } from 'lucide-react'
import { toCaracasTime } from './timezone'

type VenueFilter = 'all' | 'dex' | 'cex'

interface VenueTotal {
  venue: 'dex' | 'cex'
  label: string
  pnl_net: number
  count: number
}

interface ClosedTrade {
  id: number
  asset: string
  venue: string
  side: string
  pnl_net: number
  reason: string
  reason_label: string
  closed_at: number
}

interface TradesResponse {
  ok: boolean
  month: string
  totals: VenueTotal[]
  trades: ClosedTrade[]
  truncated: boolean
}

const FILTERS: VenueFilter[] = ['all', 'dex', 'cex']

// ── Presentational helpers (module level — stable identity) ─────────

// Up to 4 decimals, at least 2, signed. A real small scalp P&L never shows 0.00.
export function formatPnl(v: number): string {
  const sign = v > 0 ? '+' : v < 0 ? '−' : ''
  const abs = Math.abs(v).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  })
  return `${sign}${abs}`
}

function pnlColor(v: number): string {
  return v > 0 ? 'text-green-400' : v < 0 ? 'text-red-400' : 'text-[#D0CFCC]'
}

function formatSide(side: string): string {
  return side === 'long' ? 'BUY' : side === 'short' ? 'SELL' : side.toUpperCase()
}

function formatMonth(month: string): string {
  const [y, m] = month.split('-')
  const names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
  const idx = Number(m) - 1
  return idx >= 0 && idx < 12 ? `${names[idx]} ${y}` : month
}

// closed_at is a unix epoch (UTC); display in Caracas (UTC-4) like the rest of the app.
function formatClose(ts: number): string {
  const d = toCaracasTime(new Date(ts * 1000))
  const mm = String(d.getUTCMonth() + 1).padStart(2, '0')
  const dd = String(d.getUTCDate()).padStart(2, '0')
  const hh = String(d.getUTCHours()).padStart(2, '0')
  const mi = String(d.getUTCMinutes()).padStart(2, '0')
  return `${mm}-${dd} ${hh}:${mi}`
}

export default function ClosedTrades() {
  const [filter, setFilter] = useState<VenueFilter>('all')
  const [data, setData] = useState<TradesResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)

  useEffect(() => {
    let cancelled = false
    const fetchTrades = () => {
      fetch(`/api/trades/closed?venue=${filter}`)
        .then(r => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`)
          return r.json()
        })
        .then(d => {
          if (!cancelled) {
            setData(d)
            setLoading(false)
            setError(false)
          }
        })
        .catch(() => {
          if (!cancelled) {
            setLoading(false)
            setError(true)
          }
        })
    }
    fetchTrades()
    const interval = setInterval(fetchTrades, 30000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [filter])

  const totals = data?.totals ?? []
  const trades = data?.trades ?? []
  const monthEmpty = data !== null && !error && trades.length === 0 && totals.every(t => t.count === 0)
  const venueEmpty = data !== null && !error && !monthEmpty && trades.length === 0

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-4 sm:px-6 py-2.5 bg-[#1a1528] border-b border-[#2a2240] shrink-0">
        <div className="flex items-baseline justify-between">
          <h2 className="text-sm font-semibold text-[#D0CFCC]">
            Closed Trades <span className="text-[#7a7090] font-normal">{data ? formatMonth(data.month) : ''}</span>
          </h2>
          <span className="text-[10px] text-[#7a7090]">UTC-4</span>
        </div>
        <p className="text-[10px] text-[#7a7090] mt-0.5 leading-tight">
          Net of estimated fees · funding not included
        </p>
      </div>

      {/* Summary cards — full month per venue, unaffected by the filter */}
      <div className="grid grid-cols-2 gap-2 px-4 sm:px-6 pt-3 shrink-0">
        {totals.map(t => (
          <div key={t.venue} className="rounded-xl border border-[#2a2240] bg-[#1a1528] px-3 py-2.5">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-[#7a7090]">{t.label}</div>
            <div className={`text-xl sm:text-2xl font-bold tabular-nums ${pnlColor(t.pnl_net)}`}>
              {loading ? '…' : formatPnl(t.pnl_net)}
            </div>
            <div className="text-[10px] text-[#7a7090] mt-0.5">{t.count} trades</div>
          </div>
        ))}
      </div>

      {/* Filter — narrows the list only */}
      <div className="flex gap-1 px-4 sm:px-6 pt-3 shrink-0">
        <div className="flex flex-1 rounded-xl border border-[#2a2240] bg-[#171421] p-0.5">
          {FILTERS.map(f => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`flex-1 py-2 text-xs font-medium rounded-lg transition-colors ${
                filter === f ? 'bg-[#2a2240] text-[#D0CFCC]' : 'text-[#7a7090] hover:text-[#D0CFCC]'
              }`}
            >
              {f === 'all' ? 'All' : f.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto mt-3">
        {loading && (
          <div className="flex flex-col items-center justify-center py-16 text-[#7a7090]">
            <Loader2 size={28} className="animate-spin mb-3" />
            <span className="text-sm">Loading closed trades…</span>
          </div>
        )}

        {error && (
          <div className="flex flex-col items-center justify-center py-16 text-red-400">
            <span className="text-sm mb-3">Couldn't load closed trades.</span>
            <button
              onClick={() => { setLoading(true); setError(false); }}
              className="flex items-center gap-2 text-xs px-4 py-2 rounded-lg border border-[#2a2240] bg-[#1a1528] text-[#D0CFCC]"
            >
              <RefreshCw size={14} /> Retry
            </button>
          </div>
        )}

        {!loading && !error && monthEmpty && (
          <div className="flex flex-col items-center justify-center py-16 text-[#7a7090]">
            <span className="text-sm">No closed trades this month.</span>
          </div>
        )}

        {!loading && !error && venueEmpty && (
          <div className="flex flex-col items-center justify-center py-16 text-[#7a7090]">
            <span className="text-sm">
              No {filter === 'all' ? '' : filter.toUpperCase() + ' '}trades this month.
            </span>
          </div>
        )}

        {!loading && !error && !monthEmpty && !venueEmpty && (
          <div className="px-4 sm:px-6 pb-4">
            {data?.truncated && (
              <div className="text-[10px] text-[#7a7090] mb-2 px-1">
                Showing the 200 most recent trades.
              </div>
            )}
            <div className="rounded-xl border border-[#2a2240] bg-[#1a1528] divide-y divide-[#2a2240] overflow-hidden">
              {trades.map(t => (
                <div key={t.id} className="px-3 py-2.5 flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-[#D0CFCC] flex items-center gap-2">
                      <span className="truncate">{t.asset}</span>
                      <span className="text-[10px] text-[#7a7090]">{t.venue.toUpperCase()}</span>
                      <span className={`text-[10px] font-semibold ${t.side === 'short' ? 'text-red-400' : 'text-[#D0CFCC]'}`}>
                        {formatSide(t.side)}
                      </span>
                    </div>
                    <div className="text-[10px] text-[#7a7090] mt-0.5">
                      {t.reason_label} · {formatClose(t.closed_at)}
                    </div>
                  </div>
                  <div className={`text-sm font-semibold tabular-nums shrink-0 ${pnlColor(t.pnl_net)}`}>
                    {formatPnl(t.pnl_net)}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
