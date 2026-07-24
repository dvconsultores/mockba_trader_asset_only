import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import SettingsView from './SettingsView'
import './index.css'

function Router() {
  const path = window.location.pathname

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
