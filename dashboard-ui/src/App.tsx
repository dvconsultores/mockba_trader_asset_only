import { useState, useEffect } from 'react'
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

  const tabs: { id: Tab; label: string; icon: string }[] = [
    { id: 'live', label: 'Live', icon: '📟' },
    { id: 'signals', label: 'Signals', icon: '📊' },
    { id: 'ml', label: 'ML', icon: '🤖' },
    { id: 'status', label: 'Status', icon: '📡' },
  ]

  return (
    <div className="h-screen flex flex-col bg-[#171421]">
      {/* Top status bar */}
      <div className="flex items-center justify-between px-3 py-1.5 border-b border-[#2a2240] bg-[#1a1528] select-none shrink-0">
        <span className="text-[11px] sm:text-xs text-[#D0CFCC] font-bold">Mockba Terminal</span>
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
      <div className="flex items-center justify-around border-t border-[#2a2240] bg-[#1a1528] select-none shrink-0 safe-bottom pb-[env(safe-area-inset-bottom,0px)]">
        {tabs.map(t => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`flex flex-col items-center gap-0.5 py-2 px-3 min-w-[60px] transition-colors ${
              tab === t.id
                ? 'text-[#D0CFCC] border-t-2 border-[#D0CFCC] -mt-[1px]'
                : 'text-[#4a4060] border-t-2 border-transparent -mt-[1px]'
            }`}
          >
            <span className="text-base sm:text-lg leading-none">{t.icon}</span>
            <span className="text-[9px] sm:text-[10px] font-medium">{t.label}</span>
          </button>
        ))}
      </div>
    </div>
  )
}
