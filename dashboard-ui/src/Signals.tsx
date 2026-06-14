import { useState, useEffect } from 'react'

interface Signal {
  id: number
  timestamp: string
  exchange: string
  asset: string
  regime: string
  obi: number
  pattern_type: string
  approved: number
  side: string
  entry_price: number
  ml_score: number | null
  ml_decision: string | null
  trade_outcome: string | null
  rejection_reasons: string[] | null
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

  const filtered = filter
    ? signals.filter(s =>
        s.asset?.toLowerCase().includes(filter.toLowerCase()) ||
        s.pattern_type?.toLowerCase().includes(filter.toLowerCase()) ||
        String(s.id).includes(filter)
      )
    : signals

  function mlColor(score: number | null, decision: string | null): string {
    if (score === null) return 'text-[#4a4060]'
    if (decision === 'approved') return 'text-[#D0CFCC]'
    return 'text-red-400'
  }

  function outcomeBadge(outcome: string | null) {
    if (!outcome) return <span className="text-[#4a4060]">—</span>
    const color = outcome === 'win' ? 'text-[#D0CFCC]' : 'text-red-400'
    return <span className={color}>{outcome.toUpperCase()}</span>
  }

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center gap-1 sm:gap-2 px-1.5 sm:px-2 py-1 bg-[#1a1528] border-b border-[#2a2240] text-[10px] sm:text-xs">
        <span className="text-[#4a4060] hidden sm:inline">signal_history</span>
        <input
          type="text"
          value={filter}
          onChange={e => setFilter(e.target.value)}
          placeholder="filter..."
          className="flex-1 bg-[#171421] border border-[#2a2240] text-[#D0CFCC] px-1.5 sm:px-2 py-0.5 text-[10px] sm:text-xs focus:outline-none focus:border-[#D0CFCC] font-mono placeholder-[#4a4060]"
        />
        <select
          value={exchangeFilter}
          onChange={e => setExchangeFilter(e.target.value)}
          className="bg-[#171421] border border-[#2a2240] text-[#D0CFCC] px-1 sm:px-2 py-0.5 text-[10px] sm:text-xs"
        >
          <option value="">ALL</option>
          <option value="dex">DEX</option>
          <option value="cex">CEX</option>
        </select>
        <span className="text-[#4a4060] text-[9px] sm:text-xs hidden sm:inline">{filtered.length} rows</span>
      </div>

      <div className="flex-1 overflow-auto">
        <div className="min-w-[600px] sm:min-w-0">
        <table className="w-full text-[10px] sm:text-xs font-mono border-collapse">
          <thead className="sticky top-0 bg-[#1a1528] text-[#4a4060]">
            <tr>
              <th className="px-2 py-1 text-left">TIME</th>
              <th className="px-2 py-1 text-left">EX</th>
              <th className="px-2 py-1 text-left">SIDE</th>
              <th className="px-2 py-1 text-left">REGIME</th>
              <th className="px-2 py-1 text-left">PATTERN</th>
              <th className="px-2 py-1 text-right">OBI</th>
              <th className="px-2 py-1 text-right">ML SCORE</th>
              <th className="px-2 py-1 text-center">ML</th>
              <th className="px-2 py-1 text-center">OK?</th>
              <th className="px-2 py-1 text-center">OUTCOME</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map(s => (
              <tr
                key={s.id}
                className={`border-b border-[#1e1830] hover:bg-[#1e1830] ${
                  s.approved ? 'text-[#D0CFCC]' : 'text-[#6a6070]'
                }`}
              >
                <td className="px-2 py-0.5 whitespace-nowrap text-[#4a4060]">
                  {s.timestamp?.replace('T', ' ').substring(5, 19) || '—'}
                </td>
                <td className="px-2 py-0.5">{s.exchange?.toUpperCase()}</td>
                <td className={`px-2 py-0.5 ${s.side === 'BUY' ? 'text-[#D0CFCC]' : 'text-red-400'}`}>
                  {s.side || '—'}
                </td>
                <td className="px-2 py-0.5 text-cyan-500">{s.regime}</td>
                <td className="px-2 py-0.5 max-w-xs truncate text-[#6a6070]">
                  {s.pattern_type || '—'}
                </td>
                <td className="px-2 py-0.5 text-right text-[#6a6070]">
                  {s.obi?.toFixed(2)}
                </td>
                <td className={`px-2 py-0.5 text-right font-bold ${mlColor(s.ml_score, s.ml_decision)}`}>
                  {s.ml_score != null ? s.ml_score.toFixed(3) : '—'}
                </td>
                <td className="px-2 py-0.5 text-center">
                  {s.ml_decision === 'approved' ? '✅' : s.ml_decision === 'rejected' ? '❌' : '—'}
                </td>
                <td className="px-2 py-0.5 text-center">
                  {s.approved ? '✅' : '❌'}
                </td>
                <td className="px-2 py-0.5 text-center font-bold">
                  {outcomeBadge(s.trade_outcome)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      </div>
    </div>
  )
}
