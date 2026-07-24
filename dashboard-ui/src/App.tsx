import { useState, useEffect, useRef } from 'react'
import { TerminalSquare, BarChart3, Bot, Radio, Settings2, MoreHorizontal } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import Terminal from './Terminal'
import Signals from './Signals'
import MLMonitor from './MLMonitor'
import StatusBar from './StatusBar'
import MiniSettings from './MiniSettings'

type Tab = 'live' | 'signals' | 'ml' | 'status' | 'settings'

interface BotStatus {
  uptime_seconds: number
  dex_mode: string
  cex_mode: string
  ml_threshold: number
  model_loaded: boolean
  current_regime: string | null
}

const TG = (window as any).TelegramWebApp ?? (window as any).Telegram?.WebApp

export default function App() {
  const [tab, setTab] = useState<Tab>('live')
  const [status, setStatus] = useState<BotStatus | null>(null)
  const [moreOpen, setMoreOpen] = useState(false)
  const moreRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const fetchStatus = () => {
      fetch('/api/status')
        .then(r => r.json())
        .then(setStatus)
        .catch(() => {})
    }
    fetchStatus()
    const interval = setInterval(fetchStatus, 5000)
    return () => clearInterval(interval)
  }, [])

  // Close "more" menu on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (moreRef.current && !moreRef.current.contains(e.target as Node)) {
        setMoreOpen(false)
      }
    }
    if (moreOpen) document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [moreOpen])

  const mainTabs: { id: Tab; label: string; icon: LucideIcon }[] = [
    { id: 'live', label: 'Live', icon: TerminalSquare },
    { id: 'signals', label: 'Signals', icon: BarChart3 },
    { id: 'ml', label: 'ML', icon: Bot },
  ]

  const moreTabs: { id: Tab; label: string; icon: LucideIcon }[] = [
    { id: 'status', label: 'Status', icon: Radio },
    { id: 'settings', label: 'Settings', icon: Settings2 },
  ]

  return (
    <div className="h-screen flex flex-col bg-[#171421]">
      {/* Top status bar */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-[#2a2240] bg-[#1a1528] select-none shrink-0">
        <span className="text-base sm:text-lg text-[#D0CFCC] font-bold tracking-tight">Mockba Terminal</span>
        <div className="flex items-center gap-2 sm:gap-3 text-[10px] sm:text-xs">
          {status && (
            <>
              <span className={status.dex_mode !== 'False' ? 'text-[#D0CFCC]' : 'text-[#4a4060]'}>
                DEX:{status.dex_mode}
              </span>
              <span className={status.cex_mode !== 'False' ? 'text-[#D0CFCC]' : 'text-[#4a4060]'}>
                CEX:{status.cex_mode}
              </span>
              {status.model_loaded && (
                <span className="text-cyan-500">ML:on</span>
              )}
              {status.current_regime && (
                <span className={
                  status.current_regime === 'RANGE' ? 'text-yellow-400' :
                  status.current_regime === 'TREND_UP' ? 'text-green-400' :
                  'text-red-400'
                }>
                  {status.current_regime === 'RANGE' ? '⚡Grid' : status.current_regime}
                </span>
              )}
            </>
          )}
        </div>
      </div>

      {/* Main Content */}
      <div className="flex-1 overflow-hidden">
        {tab === 'live' && <Terminal />}
        {tab === 'signals' && <Signals />}
        {tab === 'ml' && <MLMonitor />}
        {tab === 'status' && <StatusBar status={status} />}
        {tab === 'settings' && <MiniSettings />}
      </div>

      {/* Bottom tab bar */}
      <div className="flex items-center justify-around border-t border-[#2a2240] bg-[#1a1528] select-none shrink-0 safe-bottom">
        {mainTabs.map(t => {
          const Icon = t.icon
          return (
            <button
              key={t.id}
              onClick={() => { setTab(t.id); setMoreOpen(false) }}
              className={`flex flex-col items-center gap-1 py-3 px-3 min-w-[64px] leading-none transition-colors ${
                tab === t.id
                  ? 'text-[#D0CFCC] border-t-2 border-[#D0CFCC] -mt-[1px]'
                  : 'text-[#4a4060] border-t-2 border-transparent -mt-[1px]'
              }`}
            >
              <Icon size={28} strokeWidth={tab === t.id ? 2.4 : 1.8} />
              <span className="text-xs sm:text-sm font-medium mt-1">{t.label}</span>
            </button>
          )
        })}

        {/* ⋯ More menu */}
        <div className="relative" ref={moreRef}>
          <button
            onClick={() => setMoreOpen(!moreOpen)}
            className={`flex flex-col items-center gap-1 py-3 px-3 min-w-[64px] leading-none transition-colors ${
              moreTabs.some(t => t.id === tab)
                ? 'text-[#D0CFCC] border-t-2 border-[#D0CFCC] -mt-[1px]'
                : 'text-[#4a4060] border-t-2 border-transparent -mt-[1px]'
            }`}
          >
            <MoreHorizontal size={28} strokeWidth={moreTabs.some(t => t.id === tab) ? 2.4 : 1.8} />
            <span className="text-xs sm:text-sm font-medium mt-1">More</span>
          </button>

          {moreOpen && (
            <div className="absolute bottom-full right-0 mb-2 bg-[#1a1528] border border-[#2a2240] rounded-lg shadow-xl py-1 min-w-[140px] z-50">
              {moreTabs.map(t => {
                const Icon = t.icon
                return (
                  <button
                    key={t.id}
                    onClick={() => { setTab(t.id); setMoreOpen(false) }}
                    className={`flex items-center gap-2 w-full px-4 py-2.5 text-sm transition-colors ${
                      tab === t.id
                        ? 'text-[#D0CFCC] bg-[#2a2240]'
                        : 'text-[#7a7090] hover:text-[#D0CFCC] hover:bg-[#2a2240]'
                    }`}
                  >
                    <Icon size={18} />
                    <span>{t.label}</span>
                  </button>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
