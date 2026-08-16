import { useState, useEffect, useRef } from 'react'
import { TerminalSquare, BarChart3, Radio, Settings2, MoreHorizontal, Banknote, History } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import Terminal from './Terminal'
import Signals from './Signals'
import StatusBar from './StatusBar'
import MiniSettings from './MiniSettings'
import CapitalManager from './CapitalManager'
import ClosedTrades from './ClosedTrades'
import { TG, isTelegram } from './TelegramProvider'

type Tab = 'live' | 'signals' | 'status' | 'settings' | 'capital' | 'closed'

interface BotStatus {
  uptime_seconds: number
  dex_mode: string
  cex_mode: string
}

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

  // Telegram BackButton: shown only on non-home tabs for tab navigation.
  // On Live (home) tab the BackButton is HIDDEN → Telegram displays its native
  // ✕ Close button, and enableClosingConfirmation shows "Close Mini App?" dialog.
  const backHandlerRef = useRef<(() => void) | null>(null)

  useEffect(() => {
    if (!isTelegram || !TG?.BackButton) return

    const backBtn = TG.BackButton
    const mainTabIds: Tab[] = ['live', 'closed', 'signals']
    const isHome = tab === 'live' && !moreOpen

    // Remove previous handler if any
    if (backHandlerRef.current) {
      backBtn.offClick(backHandlerRef.current)
      backHandlerRef.current = null
    }

    if (isHome) {
      // Home: hide ← arrow so the native ✕ Close button appears in the header
      backBtn.hide()
    } else {
      const onClick = () => {
        if (moreOpen) {
          setMoreOpen(false)
          return
        }
        if (tab === 'status' || tab === 'settings' || tab === 'capital') {
          setTab('live')
          return
        }
        const idx = mainTabIds.indexOf(tab)
        if (idx > 0) {
          setTab(mainTabIds[idx - 1])
        }
      }
      backBtn.onClick(onClick)
      backHandlerRef.current = onClick
      backBtn.show()
    }

    return () => {
      if (backHandlerRef.current) {
        backBtn.offClick(backHandlerRef.current)
        backHandlerRef.current = null
      }
    }
  }, [tab, moreOpen])

  const mainTabs: { id: Tab; label: string; icon: LucideIcon }[] = [
    { id: 'live', label: 'Live', icon: TerminalSquare },
    { id: 'closed', label: 'Trades', icon: History },
    { id: 'signals', label: 'Signals', icon: BarChart3 },
  ]

  const moreTabs: { id: Tab; label: string; icon: LucideIcon }[] = [
    { id: 'status', label: 'Status', icon: Radio },
    { id: 'capital', label: 'Capital', icon: Banknote },
    // Settings page kept but hidden from the menu for now (remove for now)
    // { id: 'settings', label: 'Settings', icon: Settings2 },
  ]

  return (
    <div className="h-screen flex flex-col bg-[#171421]">
      {/* Top status bar */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-[#2a2240] bg-[#1a1528] select-none shrink-0">
        <span className="text-base sm:text-lg text-[#D0CFCC] font-bold tracking-tight">Mockba Terminal</span>
        <div className="flex items-center gap-2 sm:gap-3 text-[10px] sm:text-xs">
          {status && (
            <>
              <span className={status.dex_mode !== 'false' && status.dex_mode !== 'False' ? 'text-[#D0CFCC]' : 'text-[#4a4060]'}>
                DEX:{status.dex_mode}
              </span>
              <span className={status.cex_mode !== 'false' && status.cex_mode !== 'False' ? 'text-[#D0CFCC]' : 'text-[#4a4060]'}>
                CEX:{status.cex_mode}
              </span>
            </>
          )}
        </div>
      </div>

      {/* Main Content */}
      <div className="flex-1 overflow-y-auto">
        {tab === 'live' && <Terminal />}
        {tab === 'signals' && <Signals />}
        {tab === 'capital' && <CapitalManager />}
        {tab === 'status' && <StatusBar status={status} />}
        {tab === 'closed' && <ClosedTrades />}
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
