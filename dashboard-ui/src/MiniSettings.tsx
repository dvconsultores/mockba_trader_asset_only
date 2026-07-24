import { useState, useEffect, useCallback, useRef } from 'react'
import { Settings2, Save, Check, AlertCircle, Star } from 'lucide-react'

interface SettingsData { [key: string]: string }

const TG = (window as any).TelegramWebApp ?? (window as any).Telegram?.WebApp
const isTelegram = !!TG?.initData

// ── Preset options (mirrors telegram bot grid setters) ──────────
interface PresetDef {
  key: string
  label: string
  hint: string
  suffix?: string
  options: { label: string; value: string }[]
  rec: (cap: number) => string
}

const PRESETS: PresetDef[] = [
  {
    key: 'grid_obi_buy', label: 'OBI Buy', hint: 'Enter LONG when OBI below',
    options: [
      { label: '0.92 — Aggressive', value: '0.92' },
      { label: '0.94 — Balanced', value: '0.94' },
      { label: '0.96 — Moderate', value: '0.96' },
      { label: '0.98 — Conservative', value: '0.98' },
    ],
    rec: (c) => c > 500 ? '0.94' : c > 200 ? '0.95' : '0.96',
  },
  {
    key: 'grid_obi_sell', label: 'OBI Sell', hint: 'Signal when OBI above',
    options: [
      { label: '1.10 — Slight pump', value: '1.10' },
      { label: '1.18 — Moderate', value: '1.18' },
      { label: '1.22 — Strong', value: '1.22' },
      { label: '1.30 — Euphoria', value: '1.30' },
    ],
    rec: (c) => c > 500 ? '1.30' : c > 200 ? '1.22' : '1.18',
  },
  {
    key: 'grid_tp_pct', label: 'Grid TP', hint: '% above fill price', suffix: '%',
    options: [
      { label: '0.2% — Micro', value: '0.2' },
      { label: '0.3% — Scalp', value: '0.3' },
      { label: '0.5% — Balanced', value: '0.5' },
      { label: '0.75% — Swing', value: '0.75' },
    ],
    rec: (c) => c > 500 ? '0.5' : c > 100 ? '0.3' : '0.2',
  },
  {
    key: 'grid_sl_pct', label: 'Grid SL (DEX)', hint: '% stop-loss for futures', suffix: '%',
    options: [
      { label: '0.5% — Tight', value: '0.5' },
      { label: '0.8% — Default', value: '0.8' },
      { label: '1.2% — Moderate', value: '1.2' },
      { label: '2.0% — Wide', value: '2.0' },
    ],
    rec: (_c) => '0.8',
  },
  {
    key: 'grid_cooldown_sec', label: 'Cooldown', hint: 'Seconds between entries', suffix: 's',
    options: [
      { label: '60s — Aggressive', value: '60' },
      { label: '90s — Fast', value: '90' },
      { label: '120s — Moderate', value: '120' },
      { label: '300s — Patient', value: '300' },
    ],
    rec: (c) => c > 500 ? '120' : c > 200 ? '90' : '60',
  },
  {
    key: 'grid_price_dip_pct', label: 'Price Dip', hint: 'Buy when price drops % below peak', suffix: '%',
    options: [
      { label: '0.2% — Aggressive', value: '0.2' },
      { label: '0.3% — Active', value: '0.3' },
      { label: '0.4% — Moderate', value: '0.4' },
      { label: '0.6% — Conservative', value: '0.6' },
    ],
    rec: (c) => c > 500 ? '0.5' : c > 200 ? '0.4' : '0.3',
  },
  {
    key: 'grid_max_positions', label: 'Max Positions', hint: 'Concurrent grid entries',
    options: [
      { label: '1 — Single (safe)', value: '1' },
      { label: '2 — Double stack', value: '2' },
      { label: '3 — Triple stack', value: '3' },
      { label: '5 — Aggressive', value: '5' },
    ],
    rec: (c) => c > 500 ? '2' : c > 200 ? '2' : '1',
  },
  {
    key: 'risk_level', label: 'Risk Level', hint: '% balance risked per DEX trade', suffix: '%',
    options: [
      { label: '1% — Safe', value: '1.0' },
      { label: '2.5% — Balanced', value: '2.5' },
      { label: '5% — Aggressive', value: '5.0' },
      { label: '6.5% — Max', value: '6.5' },
    ],
    rec: (_c) => '2.5',
  },
  {
    key: 'capital_usage', label: 'Capital Usage', hint: '% buying power deployed', suffix: '%',
    options: [
      { label: '25% — Conservative', value: '25' },
      { label: '50% — Balanced', value: '50' },
      { label: '75% — Aggressive', value: '75' },
      { label: '100% — Max', value: '100' },
    ],
    rec: (_c) => '50',
  },
  {
    key: 'leverage', label: 'Leverage', hint: 'DEX futures multiplier', suffix: 'x',
    options: [
      { label: '2x — Safe', value: '2' },
      { label: '3x — Balanced', value: '3' },
      { label: '5x — Aggressive', value: '5' },
      { label: '10x — Max', value: '10' },
    ],
    rec: (c) => c > 500 ? '3' : '5',
  },
]

// ── Free-input fields ───────────────────────────────────────────
interface FreeDef { key: string; label: string; hint: string; suffix?: string; step?: string; min?: string }

const FREE_INPUTS: FreeDef[] = [
  { key: 'cex_capital', label: 'CEX Capital', hint: 'USDT per spot position', step: '1', min: '5' },
  { key: 'dex_capital', label: 'DEX Capital', hint: 'USDC per futures position', step: '1', min: '5' },
  { key: 'take_profit', label: 'Take Profit', hint: '% above entry (reversal scalper)', step: '0.1', min: '0.1', suffix: '%' },
  { key: 'ml_threshold', label: 'ML Threshold', hint: 'Gate score (0.01–1.00)', step: '0.01', min: '0.5', max: '1.0' },
]

// ── Select fields ───────────────────────────────────────────────
interface SelectDef { key: string; label: string; hint: string; options: string[] }

const SELECTS: SelectDef[] = [
  { key: 'auto_trade_dex', label: 'DEX Mode', hint: 'Orderly futures', options: ['False', 'Signal', 'Automatic'] },
  { key: 'auto_trade_cex', label: 'CEX Mode', hint: 'Binance spot', options: ['False', 'Signal', 'Automatic'] },
  { key: 'exchange', label: 'Exchange', hint: 'Default for manual signal', options: ['dex', 'cex'] },
]

export default function MiniSettings() {
  const [settings, setSettings] = useState<SettingsData>({})
  const [saving, setSaving] = useState<Set<string>>(new Set())
  const [errors, setErrors] = useState<Set<string>>(new Set())
  const [loaded, setLoaded] = useState(false)
  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({})
  const capital = parseFloat(settings.cex_capital || settings.capital_usage || '10')

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

  const saveSetting = useCallback(async (key: string, value: string) => {
    setSaving(prev => new Set(prev).add(key))
    try {
      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      if (TG?.initData) headers['X-Telegram-InitData'] = TG.initData
      const res = await fetch('/api/miniapp', { method: 'POST', headers, body: JSON.stringify({ key, value }) })
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

  const debouncedSave = useCallback((key: string, value: string) => {
    setSettings(prev => ({ ...prev, [key]: value }))
    if (timers.current[key]) clearTimeout(timers.current[key])
    timers.current[key] = setTimeout(() => saveSetting(key, value), 500)
  }, [saveSetting])

  const selectAndSave = useCallback((key: string, value: string) => {
    setSettings(prev => ({ ...prev, [key]: value }))
    saveSetting(key, value)
  }, [saveSetting])

  if (!loaded) {
    return (
      <div className="flex-1 flex items-center justify-center text-gray-500">
        <Settings2 className="animate-spin mr-2" size={20} />
        Loading…
      </div>
    )
  }

  const StatusIcon = ({ k }: { k: string }) => {
    if (saving.has(k)) return <Save size={12} className="text-yellow-500 animate-pulse" />
    if (errors.has(k)) return <AlertCircle size={12} className="text-red-500" />
    if (settings[k]) return <Check size={12} className="text-green-600" />
    return null
  }

  return (
    <div className="flex-1 overflow-y-auto px-4 sm:px-6 py-4 text-[#D0CFCC]">
      {/* Header */}
      <div className="flex items-center justify-between mb-4 pb-2 border-b border-[#2a2240]">
        <div>
          <h1 className="text-base sm:text-lg font-bold">⚙️ Mockba Settings</h1>
          {!isTelegram && <p className="text-xs text-[#4a4060] mt-0.5">Read-only — open in Telegram to edit</p>}
        </div>
        {isTelegram && <span className="text-[10px] text-[#4a4060] bg-[#1a1528] px-2 py-1 rounded">⚡ auto-save</span>}
      </div>

      {/* ── Grid Scalper + Risk presets (button grid, like Telegram bot) ── */}
      <h2 className="text-xs font-semibold text-[#7a7090] uppercase tracking-wide mb-3">📊 Grid Scalper & Risk</h2>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 mb-6">
        {PRESETS.map(p => {
          const val = settings[p.key] ?? ''
          const rec = p.rec(capital)
          return (
            <div key={p.key} className="bg-[#1a1528] border border-[#2a2240] rounded-lg p-3">
              <div className="flex items-center justify-between mb-2">
                <div>
                  <span className="text-xs font-medium text-[#D0CFCC]">{p.label}</span>
                  <span className="text-[10px] text-[#4a4060] ml-1.5">{p.hint}</span>
                </div>
                <StatusIcon k={p.key} />
              </div>
              <div className="flex flex-wrap gap-1.5">
                {p.options.map(opt => {
                  const active = val === opt.value
                  const recommended = opt.value === rec
                  return (
                    <button
                      key={opt.value}
                      onClick={() => selectAndSave(p.key, opt.value)}
                      disabled={!isTelegram}
                      className={`text-[11px] sm:text-xs px-2 py-1 rounded border transition-colors
                        ${active
                          ? 'bg-[#2a2240] border-[#D0CFCC] text-[#D0CFCC]'
                          : 'bg-[#171421] border-[#2a2240] text-[#7a7090] hover:border-[#4a4060]'
                        }
                        disabled:opacity-50 disabled:cursor-not-allowed`}
                    >
                      {recommended && <Star size={10} className="inline mr-0.5 text-yellow-500" />}
                      {opt.label}
                    </button>
                  )
                })}
              </div>
              {val && (
                <div className="text-[10px] text-[#4a4060] mt-1.5">
                  Current: {val}{p.suffix ?? ''}
                  {rec === val && <span className="text-yellow-500 ml-1">⭐ recommended</span>}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* ── Free input fields ── */}
      <h2 className="text-xs font-semibold text-[#7a7090] uppercase tracking-wide mb-3">💰 Capital & Targets</h2>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 mb-6">
        {FREE_INPUTS.map(f => {
          const val = settings[f.key] ?? ''
          return (
            <div key={f.key} className="bg-[#1a1528] border border-[#2a2240] rounded-lg p-3">
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-xs font-medium text-[#D0CFCC]">{f.label}</span>
                <StatusIcon k={f.key} />
              </div>
              <p className="text-[10px] text-[#4a4060] mb-1.5">{f.hint}</p>
              <div className="flex items-center">
                <input
                  type="number"
                  value={val}
                  onChange={e => debouncedSave(f.key, e.target.value)}
                  disabled={!isTelegram}
                  step={f.step ?? 'any'}
                  min={f.min}
                  max={(f as any).max}
                  className={`w-full px-2.5 py-1.5 text-sm bg-[#171421] border rounded text-right text-[#D0CFCC]
                              focus:border-[#7a7090] outline-none disabled:opacity-50
                              ${errors.has(f.key) ? 'border-red-500' : 'border-[#2a2240]'}`}
                />
                {f.suffix && <span className="text-xs text-[#4a4060] ml-1.5 w-5">{f.suffix}</span>}
              </div>
            </div>
          )
        })}
      </div>

      {/* ── Mode selectors ── */}
      <h2 className="text-xs font-semibold text-[#7a7090] uppercase tracking-wide mb-3">🤖 Auto Trade Modes</h2>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-6">
        {SELECTS.map(s => {
          const val = settings[s.key] ?? ''
          return (
            <div key={s.key} className="bg-[#1a1528] border border-[#2a2240] rounded-lg p-3">
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-xs font-medium text-[#D0CFCC]">{s.label}</span>
                <StatusIcon k={s.key} />
              </div>
              <p className="text-[10px] text-[#4a4060] mb-1.5">{s.hint}</p>
              <select
                value={val}
                onChange={e => selectAndSave(s.key, e.target.value)}
                disabled={!isTelegram}
                className="w-full px-2.5 py-1.5 text-sm bg-[#171421] border border-[#2a2240] rounded text-[#D0CFCC]
                           focus:border-[#7a7090] outline-none disabled:opacity-50"
              >
                {s.options.map(o => <option key={o} value={o}>{o}</option>)}
              </select>
            </div>
          )
        })}
      </div>

      <div className="text-center text-[10px] text-[#4a4060] pb-6">
        {isTelegram ? 'Changes save automatically on selection or after typing' : 'Open via Telegram bot to edit'}
      </div>
    </div>
  )
}
