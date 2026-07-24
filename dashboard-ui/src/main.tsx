import React, { useState, useCallback } from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import SettingsView from './SettingsView'
import MiniAppShell from './MiniAppShell'
import './index.css'

const TG = (window as any).TelegramWebApp ?? (window as any).Telegram?.WebApp
const isTelegram = !!TG?.initData
type View = 'dashboard' | 'settings'

function Router() {
  const path = window.location.pathname
  const [view, setView] = useState<View>(
    path === '/settings' || path.startsWith('/settings') ? 'settings' : 'dashboard'
  )

  const handleNavigate = useCallback((v: View) => {
    setView(v)
    // Update URL without reload
    const url = v === 'settings' ? '/settings' : '/'
    window.history.pushState({}, '', url)
  }, [])

  // In Telegram Mini App: shell with bottom nav, state-based routing
  if (isTelegram) {
    return (
      <MiniAppShell currentView={view} onNavigate={handleNavigate}>
        {view === 'settings' ? <SettingsView /> : <App />}
      </MiniAppShell>
    )
  }

  // Browser: simple path-based routing (settings blocked)
  if (path === '/settings' || path.startsWith('/settings')) {
    return <SettingsView />
  }

  return <App />
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Router />
  </React.StrictMode>
)
