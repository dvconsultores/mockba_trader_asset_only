interface BotStatus {
  uptime_seconds: number
  dex_mode: string
  cex_mode: string
  ml_threshold: number
  model_loaded: boolean
  current_regime: string | null
}

function fmtUptime(s: number): string {
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  return `${h}h ${m}m ${sec}s`
}

export default function StatusBar({ status }: { status: BotStatus | null }) {
  if (!status) {
    return <div className="p-4 text-[#4a4060] animate-pulse">Loading status...</div>
  }

  return (
    <div className="h-full overflow-auto p-2 sm:p-4 font-mono text-[10px] sm:text-xs">
      <h2 className="text-[#D0CFCC] text-sm font-bold mb-3 sm:mb-4">📡 Bot Status</h2>

      <div className="grid grid-cols-2 gap-2 sm:gap-4">
        {/* Uptime */}
        <div className="bg-[#1a1528] border border-[#2a2240] p-3">
          <div className="text-[#4a4060] text-[10px]">UPTIME</div>
          <div className="text-[#D0CFCC] text-lg">{fmtUptime(status.uptime_seconds)}</div>
        </div>

        {/* ML Threshold */}
        <div className="bg-[#1a1528] border border-[#2a2240] p-3">
          <div className="text-[#4a4060] text-[10px]">ML THRESHOLD</div>
          <div className="text-cyan-500 text-lg">{status.ml_threshold.toFixed(2)}</div>
        </div>

        {/* DEX Mode */}
        <div className={`border p-3 ${status.dex_mode !== 'False' ? 'border-[#D0CFCC]/20 bg-[#D0CFCC]/3' : 'border-[#2a2240] bg-[#1a1528]'}`}>
          <div className="text-[#4a4060] text-[10px]">DEX MODE (Orderly Futures)</div>
          <div className={`text-lg font-bold ${status.dex_mode !== 'False' ? 'text-[#D0CFCC]' : 'text-[#4a4060]'}`}>
            {status.dex_mode.toUpperCase()}
          </div>
        </div>

        {/* CEX Mode */}
        <div className={`border p-3 ${status.cex_mode !== 'False' ? 'border-[#D0CFCC]/20 bg-[#D0CFCC]/3' : 'border-[#2a2240] bg-[#1a1528]'}`}>
          <div className="text-[#4a4060] text-[10px]">CEX MODE (Binance Spot)</div>
          <div className={`text-lg font-bold ${status.cex_mode !== 'False' ? 'text-[#D0CFCC]' : 'text-[#4a4060]'}`}>
            {status.cex_mode.toUpperCase()}
          </div>
        </div>

        {/* Model Status */}
        <div className="bg-[#1a1528] border border-[#2a2240] p-3">
          <div className="text-[#4a4060] text-[10px]">ML MODEL</div>
          <div className={status.model_loaded ? 'text-[#D0CFCC]' : 'text-red-400'}>
            {status.model_loaded ? '✅ LOADED' : '❌ NOT FOUND'}
          </div>
        </div>

        {/* Current Regime */}
        <div className="bg-[#1a1528] border border-[#2a2240] p-3">
          <div className="text-[#4a4060] text-[10px]">REGIME</div>
          <div className={`text-lg font-bold ${
            status.current_regime === 'RANGE'
              ? 'text-yellow-400'
              : status.current_regime === 'TREND_UP'
                ? 'text-green-400'
                : status.current_regime === 'TREND_DOWN'
                  ? 'text-red-400'
                  : 'text-[#4a4060]'
          }`}>
            {(status.current_regime || 'UNKNOWN').toUpperCase()}
          </div>
        </div>

        {/* Active Strategy */}
        <div className="bg-[#1a1528] border border-[#2a2240] p-3">
          <div className="text-[#4a4060] text-[10px]">CEX STRATEGY</div>
          <div className={`text-lg font-bold ${
            status.current_regime === 'RANGE' ? 'text-yellow-400' : 'text-cyan-500'
          }`}>
            {status.current_regime === 'RANGE' ? 'GRID SCALP' : 'REVERSAL'}
          </div>
        </div>
      </div>

      {/* Quick Links */}
      <div className="mt-4 p-3 bg-[#1a1528] border border-[#2a2240]">
        <div className="text-[#4a4060] text-[10px] mb-2">API ENDPOINTS</div>
        <div className="space-y-1 text-[11px]">
          <div><span className="text-cyan-500">GET</span> <span className="text-[#D0CFCC]">/api/status</span></div>
          <div><span className="text-cyan-500">GET</span> <span className="text-[#D0CFCC]">/api/signals?limit=50</span></div>
          <div><span className="text-cyan-500">GET</span> <span className="text-[#D0CFCC]">/api/ml/info</span></div>
          <div><span className="text-cyan-500">GET</span> <span className="text-[#D0CFCC]">/api/logs/recent?lines=200</span></div>
          <div><span className="text-cyan-500">GET</span> <span className="text-[#D0CFCC]">/api/logs/stream</span> <span className="text-[#4a4060]">(SSE)</span></div>
        </div>
      </div>
    </div>
  )
}
