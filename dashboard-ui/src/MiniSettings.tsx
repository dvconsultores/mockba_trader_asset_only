import { useState, useEffect, useCallback, useRef } from 'react'
import { Settings2, Save, Check, AlertCircle } from 'lucide-react'

interface SettingField {
  key: string
  label: string
  hint: string
  type: 'number' | 'select'
  step?: string
  min?: string
  max?: string
  suffix?: string
  options?: string[]
}

interface SettingsData { [key: string]: string }

const TG = (window as any).TelegramWebApp ?? (window as any).Telegram?.WebApp
const isTelegram = !!TG?.initData
const CHAT_ID = 556159355

// ── Field definitions ──────────────────────────────────────────
const SECTIONS: { title: string; fields: SettingField[] }[] = [
  {
    title: '📋 Trading',
    fields: [
      { key: 'cex_capital', label: 'CEX Capital', hint: 'USDT per position', type: 'number', step: '1', min: '5' },
      { key: 'take_profit', label: 'Take Profit', hint: '% above entry', type: 'number', step: '0.1', min: '0.1', suffix: '%' },
      { key: 'ml_threshold', label: 'ML Threshold', hint: 'Gate decision score', type: 'number', step: '0.01', min: '0.5', max: '1.0' },
      { key: 'risk_level', label: 'Risk Level', hint: '% balance risked', type: 'number', step: '0.1', min: '0.1', suffix: '%' },
      { key: 'capital_usage', label: 'Capital Usage', hint: '% buying power', type: 'number', step: '1', min: '1', max: '100', suffix: '%' },
      { key: 'leverage', label: 'Leverage', hint: 'DEX leverage', type: 'number', step: '100', min: '1', suffix: 'x' },
    ]
  },
  {
    title: '📊 Grid Scalper',
    fields: [
      { key: 'grid_obi_buy', label: 'OBI Buy', hint: 'Buy when OBI below', type: 'number', step: '0.01', min: '0.5', max: '1.5' },
      { key: 'grid_obi_sell', label: 'OBI Sell', hint: 'Signal when OBI above', type: 'number', step: '0.01', min: '0.5', max: '2.0' },
      { key: 'grid_tp_pct', label: 'Grid TP', hint: '% above fill price', type: 'number', step: '0.1', min: '0.1', suffix: '%' },
      { key: 'grid_cooldown_sec', label: 'Cooldown', hint: 'Seconds between entries', type: 'number', step: '10', min: '30', suffix: 's' },
      { key: 'grid_price_dip_pct', label: 'Price Dip %', hint: 'Buy when price drops this %', type: 'number', step: '0.05', min: '0.05', suffix: '%' },
      { key: 'grid_max_positions', label: 'Max Positions', hint: 'Concurrent positions', type: 'number', step: '1', min: '1', max: '10' },
    ]
  },
  {
    title: '🤖 Auto Trade',
    fields: [
      { key: 'auto_trade_dex', label: 'DEX Auto', hint: 'Orderly futures mode', type: 'select', options: ['False', 'Signal', 'Automatic'] },
      { key: 'auto_trade_cex', label: 'CEX Auto', hint: 'Binance spot mode', type: 'select', options: ['False', 'Signal', 'Automatic'] },
      { key: 'exchange', label: 'Exchange', hint: 'Default exchange', type: 'select', options: ['dex', 'cex'] },
    ]
  },
]

// ── Recommendations ─────────────────────────────────────────────
function recommend(key: string, capital: number): string | null {
  if (key === 'grid_obi_buy')       return capital > 500 ? '0.94' : capital > 200 ? '0.95' : '0.96'
  if (key === 'grid_obi_sell')      return capital > 500 ? '1.30' : capital > 200 ? '1.22' : '1.18'
  if (key === 'grid_tp_pct')        return capital > 500 ? '0.5'  : capital > 100 ? '0.3'  : '0.2'
  if (key === 'grid_cooldown_sec')  return capital > 500 ? '120'  : capital > 200 ? '90'   : '60'
  if (key === 'grid_price_dip_pct') return capital > 500 ? '0.5'  : capital > 200 ? '0.4'  : '0.3'
  if (key === 'grid_max_positions') return capital > 500 ? '2'    : capital > 200 ? '2'    : '1'
  if (key === 'ml_threshold')       return capital > 500 ? '0.85' : '0.80'
  if (key === 'risk_level')         return capital > 500 ? '0.5'  : '1.0'
  if (key === 'leverage')           return capital > 500 ? '500'  : '1000'
  return null
}

export default function MiniSettings() {
  const [settings, setSettings] = useState<SettingsData>({})
  const [saving, setSaving] = useState<Set<string>>(new Set())
  const [errors, setErrors] = useState<Set<string>>(new Set())
  const [loaded, setLoaded] = useState(false)
  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({})
  const capital = parseFloat(settings.cex_capital || settings.capital_usage || '10')

  // ── Load settings ───────────────────────────────────────────
  useEffect(() => {
    fetch('/api/miniapp')
      .then(r => r.json())
      .then(data => {
        if (data.settings) setSettings(data.settings)
        setLoaded(true)
      })
      .catch(() => setLoaded(true))
    if (TG) { TG.ready(); TG.expand() }
  }, [])

  // ── Save single setting ─────────────────────────────────────
  const saveSetting = useCallback(async (key: string, value: string) => {
    setSaving(prev => new Set(prev).add(key))
    try {
      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      if (TG?.initData) headers['X-Telegram-InitData'] = TG.initData

      const res = await fetch('/api/miniapp', {
        method: 'POST',
        headers,
        body: JSON.stringify({ key, value }),
      })
      if (!res.ok) throw new Error(await res.text())

      setSettings(prev => ({ ...prev, [key]: value }))
      setSaving(prev => { const n = new Set(prev); n.delete(key); return n })
      setErrors(prev => { const n = new Set(prev); n.delete(key); return n })
    } catch {
      setSaving(prev => { const n = new Set(prev); n.delete(key); return n })
      setErrors(prev => new Set(prev).add(key))
      setTimeout(() => setErrors(prev => { const n = new Set(prev); n.delete(key); return n }), 1500)
    }
  }, [])

  // ── Debounced input handler ─────────────────────────────────
  const onInput = useCallback((key: string, value: string) => {
    setSettings(prev => ({ ...prev, [key]: value }))
    if (timers.current[key]) clearTimeout(timers.current[key])
    timers.current[key] = setTimeout(() => saveSetting(key, value), 500)
  }, [saveSetting])

  // ── Select handler (save immediately) ───────────────────────
  const onSelect = useCallback((key: string, value: string) => {
    setSettings(prev => ({ ...prev, [key]: value }))
    saveSetting(key, value)
  }, [saveSetting])

  if (!loaded) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-950 text-gray-400">
        <Settings2 className="animate-spin mr-2" size={20} />
        Loading settings…
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-gray-950 text-green-400 p-4 max-w-lg mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-4 pb-3 border-b border-gray-800">
        <div>
          <h1 className="text-lg font-bold">⚙️ Mockba Settings</h1>
          {!isTelegram && <p className="text-xs text-gray-500 mt-1">Read-only — open in Telegram to edit</p>}
        </div>
        {isTelegram && (
          <span className="text-xs text-gray-500 bg-gray-900 px-2 py-1 rounded">⚡ auto-save</span>
        )}
      </div>

      {/* Sections */}
      {SECTIONS.map(section => (
        <div key={section.title} className="mb-6">
          <h2 className="text-sm font-semibold text-green-300 mb-2 uppercase tracking-wide">
            {section.title}
          </h2>
          <div className="space-y-2">
            {section.fields.map(f => {
              const val = settings[f.key] ?? ''
              const isSaving = saving.has(f.key)
              const isError = errors.has(f.key)
              const rec = recommend(f.key, capital)

              return (
                <div key={f.key} className="bg-gray-900 rounded-lg p-3">
                  <div className="flex items-center justify-between">
                    <div className="flex-1">
                      <label className="text-sm text-gray-300">{f.label}</label>
                      <p className="text-xs text-gray-600">{f.hint}</p>
                    </div>
                    <div className="flex items-center gap-2">
                      {f.type === 'select' ? (
                        <select
                          value={val}
                          onChange={e => onSelect(f.key, e.target.value)}
                          disabled={!isTelegram}
                          className="w-28 px-2 py-1.5 text-sm bg-gray-800 border border-gray-700 rounded text-green-400
                                     focus:border-green-500 outline-none disabled:opacity-50"
                        >
                          {f.options?.map(o => <option key={o} value={o}>{o}</option>)}
                        </select>
                      ) : (
                        <div className="flex items-center">
                          <input
                            type="number"
                            value={val}
                            onChange={e => onInput(f.key, e.target.value)}
                            disabled={!isTelegram}
                            step={f.step ?? 'any'}
                            min={f.min}
                            max={f.max}
                            className={`w-24 px-2 py-1.5 text-sm bg-gray-800 border rounded text-right text-green-400
                                        focus:border-green-500 outline-none disabled:opacity-50
                                        ${isError ? 'border-red-500' : isSaving ? 'border-yellow-500' : 'border-gray-700'}`}
                          />
                          {f.suffix && <span className="text-xs text-gray-500 ml-1 w-5">{f.suffix}</span>}
                        </div>
                      )}
                      {isSaving && <Save size={14} className="text-yellow-500 animate-pulse shrink-0" />}
                      {!isSaving && !isError && val !== '' && <Check size={14} className="text-green-600 shrink-0" />}
                      {isError && <AlertCircle size={14} className="text-red-500 shrink-0" />}
                    </div>
                  </div>
                  {rec && (
                    <p className="text-xs text-blue-400 mt-1 text-right">⭐ {rec}</p>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      ))}

      <div className="text-center text-xs text-gray-600 pb-8">
        {isTelegram ? 'Tap any value to edit — saved automatically' : 'Open via Telegram bot to edit settings'}
      </div>
    </div>
  )
}
