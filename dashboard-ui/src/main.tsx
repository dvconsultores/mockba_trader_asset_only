import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { TelegramProvider } from './TelegramProvider'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <TelegramProvider>
      <App />
    </TelegramProvider>
  </React.StrictMode>
)
