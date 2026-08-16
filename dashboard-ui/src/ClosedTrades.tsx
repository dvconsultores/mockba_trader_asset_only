import { useState, useEffect } from 'react'
import { Loader2, RefreshCw } from 'lucide-react'
import { toCaracasTime } from './timezone'

type VenueFilter = 'all' | 'dex' | 'cex'

interface VenueTotal {
  venue: 'dex' | 'cex'
  label: string
  pnl_net: number
  count: number
  wins: number
  losses: number
}

interface ClosedTrade {
  id: number
  asset: string
  venue: string
  side: string
  qty: number
  entry_price: number
  exit_price: number
  fee_total: number
  pnl_net: number
  pnl_pct: number
  win: boolean
  balance: number
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

interface OpenPosition {
  asset: string
  venue: string
  side: string
  qty: number
  entry_price: number
  tp_price: number | null
  sl_price: number | null
  live_price: number | null
  unrealized_pnl: number
  pnl_pct: number
  opened_at: number
}

interface OpenPositionsResponse {
  ok: boolean
  positions: OpenPosition[]
  equity: Record<string, number>
  realized_today: Record<string, number>
  fetched_at: number
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

function formatMoney(v: number): string {
  return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function fmtPrice(v: number): string {
  if (v >= 1000) return v.toFixed(0)
  if (v >= 100) return v.toFixed(2)
  if (v >= 1) return v.toFixed(4)
  return v.toPrecision(4)
}

function formatOpenTime(ts: number): string {
  const d = toCaracasTime(new Date(ts * 1000))
  const hh = String(d.getUTCHours()).padStart(2, '0')
  const mi = String(d.getUTCMinutes()).padStart(2, '0')
  return `${hh}:${mi}`
}

export default function ClosedTrades() {
  const [filter, setFilter] = useState<VenueFilter>('all')
  const [todayOnly, setTodayOnly] = useState<boolean>(() => {
    try {
      return localStorage.getItem('mt.trades.today') === '1'
    } catch {
      return false
    }
  })
  const [data, setData] = useState<TradesResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)
  const [openData, setOpenData] = useState<OpenPositionsResponse | null>(null)

  // Persist the "Today only" toggle across visits (localStorage).
  const toggleToday = (v: boolean) => {
    setTodayOnly(v)
    try {
      localStorage.setItem('mt.trades.today', v ? '1' : '0')
    } catch {
      /* storage unavailable — non-fatal */
    }
  }

  useEffect(() => {
    let cancelled = false
    const fetchTrades = () => {
      fetch(`/api/trades/closed?venue=${filter}&today=${todayOnly ? 1 : 0}`)
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
  }, [filter, todayOnly])

  // Open positions — live unrealized PnL, refreshed frequently (gain/balance vary).
  useEffect(() => {
    let cancelled = false
    const fetchOpen = () => {
      fetch('/api/positions/open')
        .then(r => r.json())
        .then(d => {
          if (!cancelled && d?.ok) setOpenData(d)
        })
        .catch(() => {})
    }
    fetchOpen()
    const interval = setInterval(fetchOpen, 15000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [])

  const totals = data?.totals ?? []
  const trades = data?.trades ?? []
  const monthEmpty = data !== null && !error && trades.length === 0 && totals.every(t => t.count === 0)
  const venueEmpty = data !== null && !error && !monthEmpty && trades.length === 0

  return (
    <div className="flex flex-col">
      {/* Header */}
      <div className="px-4 sm:px-6 py-2.5 bg-[#1a1528] border-b border-[#2a2240] shrink-0">
        <div className="flex items-baseline justify-between">
          <h2 className="text-sm font-semibold text-[#D0CFCC]">
            Trades <span className="text-[#7a7090] font-normal">{todayOnly ? 'Today' : (data ? formatMonth(data.month) : '')}</span>
          </h2>
          <span className="text-[10px] text-[#7a7090]">UTC-4</span>
        </div>
        <p className="text-[10px] text-[#7a7090] mt-0.5 leading-tight">
          Net of fees · real Binance fills · funding not included
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
            <div className="text-[10px] text-[#7a7090] mt-0.5">{t.wins}W · {t.losses}L · {t.count} trades · {t.count > 0 ? Math.round((t.wins / t.count) * 100) : 0}%</div>
          </div>
        ))}
      </div>

      {/* Open positions — live unrealized PnL (updates with the market) */}
      {openData && openData.positions.length > 0 && (
        <div className="px-4 sm:px-6 pt-3 shrink-0">
          <div className="flex items-baseline justify-between mb-1.5">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-[#7a7090]">Open positions</span>
            <span className="text-[10px] text-[#7a7090] tabular-nums">
              Equity {formatMoney(openData.equity.cex ?? 0)} · Realized {formatPnl(openData.realized_today.cex ?? 0)}
            </span>
          </div>
          <div className="rounded-xl border border-[#2a2240] bg-[#1a1528] divide-y divide-[#2a2240] overflow-hidden">
            {openData.positions.map(p => (
              <div key={p.asset} className="px-3 py-2 flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-sm font-medium text-[#D0CFCC] flex items-center gap-2">
                    <span className="truncate">{p.asset}</span>
                    <span className="text-[10px] text-[#7a7090]">{p.venue.toUpperCase()}</span>
                    <span className={`text-[10px] font-semibold ${p.side === 'short' ? 'text-red-400' : 'text-[#D0CFCC]'}`}>
                      {formatSide(p.side)}
                    </span>
                  </div>
                  <div className="text-[10px] text-[#7a7090] mt-0.5">
                    {p.qty} @ {fmtPrice(p.entry_price)} → {p.live_price != null ? fmtPrice(p.live_price) : '—'} · {formatOpenTime(p.opened_at)}
                  </div>
                </div>
                <div className="text-right shrink-0">
                  <div className={`text-sm font-semibold tabular-nums ${pnlColor(p.unrealized_pnl)}`}>{formatPnl(p.unrealized_pnl)}</div>
                  <div className={`text-[10px] tabular-nums ${pnlColor(p.unrealized_pnl)}`}>{p.pnl_pct.toFixed(2)}%</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

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

      {/* Today filter */}
      <label className="flex items-center gap-2 px-4 sm:px-6 pt-2.5 shrink-0 cursor-pointer select-none">
        <input
          type="checkbox"
          checked={todayOnly}
          onChange={e => toggleToday(e.target.checked)}
          className="h-3.5 w-3.5 accent-[#8b5cf6]"
        />
        <span className={`text-xs ${todayOnly ? 'text-[#D0CFCC]' : 'text-[#7a7090]'}`}>Today only</span>
      </label>

      {/* Body */}
      <div className="mt-3">
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
            <span className="text-sm">No closed trades {todayOnly ? 'today' : 'this month'}.</span>
          </div>
        )}

        {!loading && !error && venueEmpty && (
          <div className="flex flex-col items-center justify-center py-16 text-[#7a7090]">
            <span className="text-sm">
              No {filter === 'all' ? '' : filter.toUpperCase() + ' '}trades {todayOnly ? 'today' : 'this month'}.
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
                      {t.reason_label} · {formatClose(t.closed_at)} · {t.qty} @ {fmtPrice(t.entry_price)}→{fmtPrice(t.exit_price)}
                    </div>
                  </div>
                  <div className="text-right shrink-0">
                    <div className={`text-sm font-semibold tabular-nums ${pnlColor(t.pnl_net)}`}>
                      {formatPnl(t.pnl_net)} <span className="text-[10px] text-[#7a7090] font-normal">{t.pnl_pct.toFixed(2)}%</span>
                    </div>
                    <div className="text-[10px] tabular-nums text-[#7a7090]">bal {formatPnl(t.balance)}</div>
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
