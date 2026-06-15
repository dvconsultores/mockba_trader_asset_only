import { useState, useEffect } from 'react'
import { TerminalSquare, BarChart3, Bot, Radio } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import Terminal from './Terminal'
import Signals from './Signals'
import MLMonitor from './MLMonitor'
import StatusBar from './StatusBar'

type Tab = 'live' | 'signals' | 'ml' | 'status'

interface BotStatus {
  uptime_seconds: number
  dex_mode: string
  cex_mode: string
  ml_threshold: number
  model_loaded: boolean
}

export default function App() {
  const [tab, setTab] = useState<Tab>('live')
  const [status, setStatus] = useState<BotStatus | null>(null)

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

  const tabs: { id: Tab; label: string; icon: LucideIcon }[] = [
    { id: 'live', label: 'Live', icon: TerminalSquare },
    { id: 'signals', label: 'Signals', icon: BarChart3 },
    { id: 'ml', label: 'ML', icon: Bot },
    { id: 'status', label: 'Status', icon: Radio },
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
      </div>

      {/* Bottom tab bar (native app style) */}
      <div className="flex items-center justify-around border-t border-[#2a2240] bg-[#1a1528] select-none shrink-0 safe-bottom">
        {tabs.map(t => {
          const Icon = t.icon
          return (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
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
      </div>
    </div>
  )
}
