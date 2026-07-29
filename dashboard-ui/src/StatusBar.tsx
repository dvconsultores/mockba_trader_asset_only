import { useState, useEffect, useCallback } from 'react'
import { Power, Loader2 } from 'lucide-react'
import { TG, isTelegram } from './TelegramProvider'

interface BotStatus {
  uptime_seconds: number
  dex_mode: string
  cex_mode: string
}

function fmtUptime(s: number): string {
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  return `${h}h ${m}m ${sec}s`
}

function normalizeMode(mode: string): string {
  // Normalize legacy "true"/"false" and case variants to canonical names
  const v = (mode || '').trim().toLowerCase()
  if (v === 'true' || v === 'automatic') return 'Automatic'
  if (v === 'signal') return 'Signal'
  return 'False'
}

function modeLabel(mode: string): string {
  const m = normalizeMode(mode)
  if (m === 'Automatic') return 'AUTO'
  if (m === 'Signal') return 'SIGNAL'
  return 'OFF'
}

function modeClass(mode: string): string {
  const m = normalizeMode(mode)
  if (m === 'Automatic') return 'text-green-400'
  if (m === 'Signal') return 'text-yellow-400'
  return 'text-[#4a4060]'
}

function nextMode(current: string): string {
  const m = normalizeMode(current)
  if (m === 'False') return 'Signal'
  if (m === 'Signal') return 'Automatic'
  return 'False'
}

function buttonStyle(mode: string): string {
  const m = normalizeMode(mode)
  if (m === 'Automatic')
    return 'bg-red-500/10 border border-red-500/30 text-red-400 hover:bg-red-500/20'
  if (m === 'Signal')
    return 'bg-yellow-500/10 border border-yellow-500/30 text-yellow-400 hover:bg-yellow-500/20'
  return 'bg-green-500/10 border border-green-500/30 text-green-400 hover:bg-green-500/20'
}

function buttonLabel(mode: string): string {
  const m = normalizeMode(mode)
  if (m === 'Automatic') return '⏹ Stop'
  if (m === 'Signal') return '⏩ Auto'
  return '▶ Start (Signal)'
}

export default function StatusBar({ status: initialStatus }: { status: BotStatus | null }) {
  const [status, setStatus] = useState<BotStatus | null>(initialStatus)
  const [transitioning, setTransitioning] = useState<Set<string>>(new Set())
  const [editable, setEditable] = useState(isTelegram)
  const [error, setError] = useState<string | null>(null)

  // Sync with parent when status prop changes
  useEffect(() => {
    setStatus(initialStatus)
  }, [initialStatus])

  // Check browser session for editability
  useEffect(() => {
    if (isTelegram) return
    const check = async () => {
      try {
        const res = await fetch('/api/miniapp', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: '__ping__', value: '' }),
        })
        setEditable(res.ok)
      } catch {
        setEditable(false)
      }
    }
    check()
  }, [])

  const authHeaders = useCallback((): Record<string, string> => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    if (TG?.initData) headers['X-Telegram-InitData'] = TG.initData
    return headers
  }, [])

  const toggleExchange = useCallback(async (exchange: 'dex' | 'cex') => {
    if (!status) return
    const currentMode = exchange === 'dex' ? status.dex_mode : status.cex_mode
    const newMode = nextMode(currentMode)
    const key = exchange

    setTransitioning(prev => new Set(prev).add(key))
    setError(null)

    try {
      const res = await fetch('/api/bot/control', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ exchange, mode: newMode }),
      })
      if (!res.ok) throw new Error(await res.text())
      const json = await res.json()
      setStatus(prev => prev ? {
        ...prev,
        dex_mode: json.dex_mode ?? prev.dex_mode,
        cex_mode: json.cex_mode ?? prev.cex_mode,
      } : prev)
    } catch (e: any) {
      setError(e.message || 'Failed to toggle')
    } finally {
      setTransitioning(prev => { const n = new Set(prev); n.delete(key); return n })
    }
  }, [status, authHeaders])

  if (!status) {
    return <div className="p-4 text-[#4a4060] animate-pulse">Loading status...</div>
  }

  const isTransitioning = (exchange: string) => transitioning.has(exchange)

  return (
    <div className="h-full overflow-auto px-4 sm:px-6 py-4 font-mono text-[10px] sm:text-xs">
      <div className="flex items-center justify-between mb-3 sm:mb-4">
        <h2 className="text-[#D0CFCC] text-sm font-bold">📡 Bot Status</h2>
        {!editable && (
          <span className="text-[10px] text-[#4a4060] px-2 py-0.5 rounded border border-[#2a2240]">read-only</span>
        )}
      </div>

      {/* Error banner */}
      {error && (
        <div className="mb-3 p-2 rounded bg-red-500/10 border border-red-500/30 text-red-400 text-[10px]">
          {error}
        </div>
      )}

      <div className="grid grid-cols-2 gap-2 sm:gap-4">
        {/* Uptime */}
        <div className="bg-[#1a1528] border border-[#2a2240] p-3">
          <div className="text-[#4a4060] text-[10px]">UPTIME</div>
          <div className="text-[#D0CFCC] text-lg">{fmtUptime(status.uptime_seconds)}</div>
        </div>

        {/* DEX Mode */}
        <div className="flex flex-col border p-3 border-[#2a2240] bg-[#1a1528]">
          <div className="text-[#4a4060] text-[10px]">DEX (Orderly Futures)</div>
          <div className={`text-lg font-bold ${modeClass(status.dex_mode)}`}>
            {modeLabel(status.dex_mode)}
          </div>
          {editable && (
            <button
              onClick={() => toggleExchange('dex')}
              disabled={isTransitioning('dex')}
              className={`mt-auto w-full flex items-center justify-center gap-1.5 px-2 py-1.5 rounded text-[10px] font-medium transition-colors disabled:opacity-50 ${buttonStyle(status.dex_mode)}`}
            >
              {isTransitioning('dex') ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <Power size={12} />
              )}
              {buttonLabel(status.dex_mode)}
            </button>
          )}
        </div>

        {/* CEX Mode */}
        <div className="flex flex-col border p-3 border-[#2a2240] bg-[#1a1528]">
          <div className="text-[#4a4060] text-[10px]">CEX (Binance Spot)</div>
          <div className={`text-lg font-bold ${modeClass(status.cex_mode)}`}>
            {modeLabel(status.cex_mode)}
          </div>
          {editable && (
            <button
              onClick={() => toggleExchange('cex')}
              disabled={isTransitioning('cex')}
              className={`mt-auto w-full flex items-center justify-center gap-1.5 px-2 py-1.5 rounded text-[10px] font-medium transition-colors disabled:opacity-50 ${buttonStyle(status.cex_mode)}`}
            >
              {isTransitioning('cex') ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <Power size={12} />
              )}
              {buttonLabel(status.cex_mode)}
            </button>
          )}
        </div>
      </div>

      {/* Quick Links */}
      <div className="mt-4 p-3 bg-[#1a1528] border border-[#2a2240]">
        <div className="text-[#4a4060] text-[10px] mb-2">API ENDPOINTS</div>
        <div className="space-y-1 text-[11px]">
          <div><span className="text-cyan-500">GET</span> <span className="text-[#D0CFCC]">/api/status</span></div>
          <div><span className="text-cyan-500">GET</span> <span className="text-[#D0CFCC]">/api/logs/recent?lines=200</span></div>
          <div><span className="text-cyan-500">GET</span> <span className="text-[#D0CFCC]">/api/logs/stream</span> <span className="text-[#4a4060]">(SSE)</span></div>
        </div>
      </div>
    </div>
  )
}
