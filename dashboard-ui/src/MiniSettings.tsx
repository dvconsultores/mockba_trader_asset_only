import { useState, useEffect, useCallback, useRef, type ReactNode } from 'react'
import { Settings2, Save, Check, AlertCircle, Star, ChevronDown, X } from 'lucide-react'

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
interface FreeDef { key: string; label: string; hint: string; suffix?: string; step?: string; min?: string; max?: string }

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

type ComboOption = { label: string; value: string; recommended?: boolean }

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

  const StatusIcon = ({ k }: { k: string }) => {
    if (saving.has(k)) return <Save size={12} className="text-yellow-500 animate-pulse" />
    if (errors.has(k)) return <AlertCircle size={12} className="text-red-500" />
    if (settings[k]) return <Check size={12} className="text-green-600" />
    return <span className="w-3 h-3" />
  }

  const Section = ({ title, children }: { title: string; children: ReactNode }) => (
    <div className="mb-4 rounded-xl border border-[#2a2240] bg-[#1a1528] overflow-hidden">
      <div className="px-3 py-2 border-b border-[#2a2240] bg-[#171421]/40">
        <h2 className="text-[10px] font-semibold uppercase tracking-wider text-[#7a7090]">{title}</h2>
      </div>
      <div className="divide-y divide-[#2a2240]">{children}</div>
    </div>
  )

  const SettingRow = ({ label, hint, statusKey, children }: { label: string; hint: string; statusKey: string; children: ReactNode }) => (
    <div className="grid grid-cols-[1fr_140px_20px] sm:grid-cols-[1fr_168px_20px] items-center gap-2 px-3 py-3 hover:bg-[#171421]/30 transition-colors">
      <div className="min-w-0">
        <div className="text-xs font-medium text-[#D0CFCC] truncate">{label}</div>
        <div className="text-[10px] text-[#7a7090] truncate">{hint}</div>
      </div>
      <div className="flex justify-end">{children}</div>
      <div className="flex justify-center"><StatusIcon k={statusKey} /></div>
    </div>
  )

  const ComboList = ({ label, value, options, onChange, disabled }: { label: string; value: string; options: ComboOption[]; onChange: (v: string) => void; disabled?: boolean }) => {
    const [open, setOpen] = useState(false)
    const selected = options.find(o => o.value === value)

    useEffect(() => {
      if (open) document.body.style.overflow = 'hidden'
      return () => { document.body.style.overflow = '' }
    }, [open])

    return (
      <>
        <button
          type="button"
          onClick={() => !disabled && setOpen(true)}
          disabled={disabled}
          className={`flex items-center justify-between w-full px-2.5 py-1.5 text-sm border rounded-lg transition-colors
            ${selected ? 'text-[#D0CFCC] bg-[#171421] border-[#2a2240]' : 'text-[#4a4060] bg-[#171421]/50 border-[#2a2240]'}
            ${disabled ? 'opacity-50 cursor-not-allowed' : 'hover:border-[#7a7090]'}`}
        >
          <span className="truncate">{selected?.label ?? 'Select…'}</span>
          <ChevronDown size={16} className="text-[#7a7090] shrink-0 ml-1.5" />
        </button>

        {open && (
          <div
            className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/70"
            onClick={() => setOpen(false)}
          >
            <div
              className="w-full sm:w-[360px] max-w-full bg-[#1a1528] rounded-t-2xl sm:rounded-2xl border border-[#2a2240] shadow-2xl overflow-hidden"
              onClick={e => e.stopPropagation()}
            >
              <div className="flex items-center justify-between px-4 py-3 border-b border-[#2a2240] bg-[#171421]">
                <span className="text-sm font-semibold text-[#D0CFCC]">{label}</span>
                <button
                  type="button"
                  onClick={() => setOpen(false)}
                  className="p-1 rounded-full text-[#7a7090] hover:bg-[#2a2240] hover:text-[#D0CFCC] transition-colors"
                >
                  <X size={18} />
                </button>
              </div>
              <div className="max-h-[55vh] overflow-y-auto">
                {options.map((opt) => {
                  const active = value === opt.value
                  return (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() => { onChange(opt.value); setOpen(false) }}
                      className={`w-full flex items-center justify-between px-4 py-3 text-left text-sm border-b border-[#2a2240]/50 last:border-0 transition-colors
                        ${active ? 'bg-[#2a2240] text-[#D0CFCC]' : 'text-[#D0CFCC] hover:bg-[#2a2240]/60'}`}
                    >
                      <span className="flex items-center gap-2">
                        {opt.recommended && <Star size={12} className="text-yellow-500 shrink-0" />}
                        {opt.label}
                      </span>
                      {active && <Check size={18} className="text-green-600 shrink-0" />}
                    </button>
                  )
                })}
              </div>
            </div>
          </div>
        )}
      </>
    )
  }

  const NumberField = ({ value, suffix, disabled, step, min, max, error, onChange }: { value: string; suffix?: string; disabled?: boolean; step?: string; min?: string; max?: string; error?: boolean; onChange: (v: string) => void }) => (
    <div className="flex items-center gap-1.5 w-full">
      <input
        type="number"
        value={value}
        onChange={e => onChange(e.target.value)}
        disabled={disabled}
        step={step ?? 'any'}
        min={min}
        max={max}
        className={`flex-1 min-w-0 px-2 py-1.5 text-sm text-right bg-[#171421] border rounded-lg text-[#D0CFCC] focus:outline-none focus:border-[#D0CFCC] disabled:opacity-50 transition-colors
          ${error ? 'border-red-500' : 'border-[#2a2240]'}`}
      />
      {suffix && <span className="text-[10px] text-[#7a7090] w-4 text-right shrink-0">{suffix}</span>}
    </div>
  )

  if (!loaded) {
    return (
      <div className="flex-1 flex items-center justify-center text-gray-500">
        <Settings2 className="animate-spin mr-2" size={20} />
        Loading…
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-y-auto px-4 sm:px-6 py-4 text-[#D0CFCC]">
      {/* Header */}
      <div className="flex items-center justify-between mb-5">
        <div>
          <h1 className="text-base sm:text-lg font-bold text-[#D0CFCC]">⚙️ Settings</h1>
          <p className="text-xs text-[#7a7090] mt-0.5">
            {isTelegram ? 'Tap a value to change it' : 'Read-only outside Telegram'}
          </p>
        </div>
        <span className={`text-[10px] px-2.5 py-1 rounded-full border ${isTelegram ? 'text-[#D0CFCC] border-[#D0CFCC]/20 bg-[#D0CFCC]/10' : 'text-[#4a4060] border-[#2a2240] bg-[#1a1528]'}`}>
          {isTelegram ? '⚡ auto-save' : 'read-only'}
        </span>
      </div>

      {/* ── Grid Scalper & Risk ── */}
      <Section title="Grid Scalper & Risk">
        {PRESETS.map(p => {
          const val = settings[p.key] ?? ''
          const rec = p.rec(capital)
          return (
            <SettingRow key={p.key} label={p.label} hint={p.hint} statusKey={p.key}>
              <ComboList
                label={p.label}
                value={val}
                options={p.options.map(o => ({ ...o, recommended: o.value === rec }))}
                onChange={v => selectAndSave(p.key, v)}
                disabled={!isTelegram}
              />
            </SettingRow>
          )
        })}
      </Section>

      {/* ── Capital & Targets ── */}
      <Section title="Capital & Targets">
        {FREE_INPUTS.map(f => (
          <SettingRow key={f.key} label={f.label} hint={f.hint} statusKey={f.key}>
            <NumberField
              value={settings[f.key] ?? ''}
              suffix={f.suffix}
              step={f.step}
              min={f.min}
              max={f.max}
              disabled={!isTelegram}
              error={errors.has(f.key)}
              onChange={v => debouncedSave(f.key, v)}
            />
          </SettingRow>
        ))}
      </Section>

      {/* ── Auto Trade Modes ── */}
      <Section title="Auto Trade Modes">
        {SELECTS.map(s => (
          <SettingRow key={s.key} label={s.label} hint={s.hint} statusKey={s.key}>
            <ComboList
              label={s.label}
              value={settings[s.key] ?? ''}
              options={s.options.map(o => ({ label: o, value: o }))}
              onChange={v => selectAndSave(s.key, v)}
              disabled={!isTelegram}
            />
          </SettingRow>
        ))}
      </Section>

      <div className="text-center text-[10px] text-[#4a4060] pb-6">
        {isTelegram ? 'Changes save automatically on selection or after typing' : 'Open via Telegram bot to edit'}
      </div>
    </div>
  )
}
