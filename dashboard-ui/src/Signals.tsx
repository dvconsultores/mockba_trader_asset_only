import { useState, useEffect } from 'react'
import { Search } from 'lucide-react'
import { convertTimestampToCaracas } from './timezone'

interface Signal {
  id: number
  ts: number
  asset: string
  venue: string
  regime: string
  direction: string | null
  price: number
  extreme_pct: number | null
  threshold_pct: number | null
  atr_pct: number | null
  obi: number | null
  action: string
  reason: string
  tp_price: number | null
  sl_price: number | null
}

function formatVenue(venue: string): string {
  return venue === 'binance' ? 'CEX' : venue === 'orderly' ? 'DEX' : venue.toUpperCase()
}

function formatSide(direction: string | null): string {
  if (!direction) return '—'
  return direction === 'long' ? 'BUY' : direction === 'short' ? 'SELL' : direction.toUpperCase()
}

function sideColor(direction: string | null): string {
  if (!direction) return 'text-[#4a4060]'
  return direction === 'long' ? 'text-[#D0CFCC]' : 'text-red-400'
}

function actionBadge(action: string): { text: string; color: string } {
  switch (action) {
    case 'entered': return { text: 'ENTERED', color: 'text-[#D0CFCC]' }
    case 'signaled': return { text: 'SIGNAL', color: 'text-cyan-400' }
    case 'skipped': return { text: 'SKIPPED', color: 'text-[#6a6070]' }
    default: return { text: action.toUpperCase(), color: 'text-[#4a4060]' }
  }
}

export default function Signals() {
  const [signals, setSignals] = useState<Signal[]>([])
  const [filter, setFilter] = useState('')
  const [exchangeFilter, setExchangeFilter] = useState('')

  useEffect(() => {
    const fetchSignals = () => {
      const params = new URLSearchParams({ limit: '100' })
      if (exchangeFilter) params.set('exchange', exchangeFilter)
      fetch(`/api/signals?${params}`)
        .then(r => r.json())
        .then(data => setSignals(data.signals || []))
        .catch(() => {})
    }
    fetchSignals()
    const interval = setInterval(fetchSignals, 10000)
    return () => clearInterval(interval)
  }, [exchangeFilter])

  const filtered = (filter
    ? signals.filter(s =>
        s.asset?.toLowerCase().includes(filter.toLowerCase()) ||
        s.action?.toLowerCase().includes(filter.toLowerCase()) ||
        String(s.id).includes(filter)
      )
    : signals
  ).slice().sort((a, b) => b.id - a.id)

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center gap-2 px-4 sm:px-6 py-2.5 bg-[#1a1528] border-b border-[#2a2240]">
        <div className="relative flex-1">
          <Search size={18} className="absolute left-3 top-1/2 -translate-y-1/2 text-[#4a4060] pointer-events-none" />
          <input
            type="text"
            value={filter}
            onChange={e => setFilter(e.target.value)}
            placeholder="filter signals..."
            className="w-full bg-[#171421] border border-[#2a2240] rounded-xl text-[#D0CFCC] pl-10 pr-3 py-3 text-base focus:outline-none focus:border-[#D0CFCC] font-mono placeholder-[#4a4060]"
          />
        </div>
        <select
          value={exchangeFilter}
          onChange={e => setExchangeFilter(e.target.value)}
          className="bg-[#171421] border border-[#2a2240] rounded-xl text-[#D0CFCC] px-4 py-3 text-base focus:outline-none focus:border-[#D0CFCC]"
        >
          <option value="">ALL</option>
          <option value="dex">DEX</option>
          <option value="cex">CEX</option>
        </select>
      </div>

      <div className="flex-1 overflow-auto">
        <div className="min-w-[650px] sm:min-w-0">
        <table className="w-full text-[10px] sm:text-xs font-mono border-collapse">
          <thead className="sticky top-0 bg-[#1a1528] text-[#4a4060]">
            <tr>
              <th className="px-2 py-1 text-left">TIME</th>
              <th className="px-2 py-1 text-left">EX</th>
              <th className="px-2 py-1 text-left">ASSET</th>
              <th className="px-2 py-1 text-left">SIDE</th>
              <th className="px-2 py-1 text-left">REGIME</th>
              <th className="px-2 py-1 text-left">PATTERN</th>
              <th className="px-2 py-1 text-right">PRICE</th>
              <th className="px-2 py-1 text-right">TP</th>
              <th className="px-2 py-1 text-right">SL</th>
              <th className="px-2 py-1 text-right">OBI</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map(s => {
              const ab = actionBadge(s.action)
              const entered = s.action === 'entered'
              return (
              <tr
                key={s.id}
                className={`border-b border-[#1e1830] hover:bg-[#1e1830] ${
                  entered ? 'text-[#D0CFCC]' : 'text-[#6a6070]'
                }`}
              >
                <td className="px-2 py-0.5 whitespace-nowrap text-[#4a4060]">
                  {s.ts ? convertTimestampToCaracas(new Date(s.ts * 1000).toISOString().replace('T', ' ').slice(0, 19)) : '—'}
                </td>
                <td className="px-2 py-0.5 font-bold">
                  {formatVenue(s.venue)}
                </td>
                <td className="px-2 py-0.5">{s.asset}</td>
                <td className={`px-2 py-0.5 ${sideColor(s.direction)}`}>
                  {formatSide(s.direction)}
                </td>
                <td className="px-2 py-0.5 text-cyan-500">{s.regime}</td>
                <td className={`px-2 py-0.5 max-w-xs truncate ${ab.color}`}>
                  {ab.text}
                </td>
                <td className="px-2 py-0.5 text-right text-[#6a6070]">
                  {s.price?.toFixed(4)}
                </td>
                <td className="px-2 py-0.5 text-right text-[#D0CFCC]">
                  {s.tp_price != null ? s.tp_price.toFixed(4) : '—'}
                </td>
                <td className="px-2 py-0.5 text-right text-red-400">
                  {s.sl_price != null ? s.sl_price.toFixed(4) : '—'}
                </td>
                <td className="px-2 py-0.5 text-right text-[#6a6070]">
                  {s.obi != null ? s.obi.toFixed(2) : '—'}
                </td>
              </tr>
            )})}
          </tbody>
        </table>
        </div>
      </div>
    </div>
  )
}
