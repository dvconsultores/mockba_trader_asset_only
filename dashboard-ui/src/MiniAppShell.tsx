import { useState } from 'react'
import { Settings2, LayoutDashboard, ChevronDown, ChevronUp } from 'lucide-react'

const TG = (window as any).TelegramWebApp ?? (window as any).Telegram?.WebApp
const isTelegram = !!TG?.initData

interface Props {
  children: React.ReactNode
  currentView: 'dashboard' | 'settings'
  onNavigate: (view: 'dashboard' | 'settings') => void
}

export default function MiniAppShell({ children, currentView, onNavigate }: Props) {
  const [menuOpen, setMenuOpen] = useState(false)

  if (!isTelegram) return <>{children}</>

  return (
    <div className="flex flex-col min-h-screen bg-gray-950">
      {/* Content */}
      <div className="flex-1 overflow-auto">
        {children}
      </div>

      {/* Bottom Nav */}
      <div className="shrink-0 border-t border-gray-800 bg-gray-900">
        {/* Expanded menu */}
        {menuOpen && (
          <div className="border-b border-gray-800 px-3 py-2 flex gap-2 overflow-x-auto">
            <button
              onClick={() => { onNavigate('dashboard'); setMenuOpen(false) }}
              className={`flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm shrink-0
                ${currentView === 'dashboard'
                  ? 'bg-green-900 text-green-300 border border-green-700'
                  : 'bg-gray-800 text-gray-400 border border-gray-700 hover:bg-gray-700'}`}
            >
              <LayoutDashboard size={14} />
              Dashboard
            </button>
            <button
              onClick={() => { onNavigate('settings'); setMenuOpen(false) }}
              className={`flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm shrink-0
                ${currentView === 'settings'
                  ? 'bg-green-900 text-green-300 border border-green-700'
                  : 'bg-gray-800 text-gray-400 border border-gray-700 hover:bg-gray-700'}`}
            >
              <Settings2 size={14} />
              Settings
            </button>
          </div>
        )}

        {/* Bottom bar */}
        <div className="flex items-center justify-center px-4 py-2">
          <button
            onClick={() => setMenuOpen(!menuOpen)}
            className="flex items-center gap-1.5 px-6 py-2 rounded-full bg-gray-800 text-gray-300
                       border border-gray-700 hover:bg-gray-700 active:bg-gray-600 transition-colors"
          >
            <span className="text-lg leading-none">{menuOpen ? <ChevronDown size={18} /> : <ChevronUp size={18} />}</span>
            <span className="text-sm">Menu</span>
          </button>
        </div>
      </div>
    </div>
  )
}
