import React, { useState, useEffect, useCallback, useRef, type ReactNode } from 'react'
import { Settings2, Save, Check, AlertCircle, Star, ChevronDown, X } from 'lucide-react'
import { validateAll, type Verdict } from './validation'

interface SettingsData { [key: string]: string }

const TG = (window as any).TelegramWebApp ?? (window as any).Telegram?.WebApp
const isTelegram = !!TG?.initData
let browserSessionChecked = false
let browserSessionValid = false

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
    key: 'dip_min_pct', label: 'Dip Threshold', hint: 'Buy when price drops % below peak', suffix: '%',
    options: [
      { label: '0.10% — Very sensitive', value: '0.10' },
      { label: '0.15% — Active (default)', value: '0.15' },
      { label: '0.25% — Moderate', value: '0.25' },
      { label: '0.40% — Conservative', value: '0.40' },
    ],
    rec: (c) => c > 500 ? '0.25' : c > 200 ? '0.20' : '0.15',
  },
  {
    key: 'pump_min_pct', label: 'Pump Threshold', hint: 'Sell/short when price pumps % above trough', suffix: '%',
    options: [
      { label: '0.10% — Very sensitive', value: '0.10' },
      { label: '0.15% — Active (default)', value: '0.15' },
      { label: '0.25% — Moderate', value: '0.25' },
      { label: '0.40% — Conservative', value: '0.40' },
    ],
    rec: (c) => c > 500 ? '0.25' : '0.15',
  },
  {
    key: 'tp_min_pct', label: 'Take Profit', hint: '% above fill price', suffix: '%',
    options: [
      { label: '0.3% — Scalp', value: '0.3' },
      { label: '0.5% — Tight', value: '0.5' },
      { label: '0.8% — Balanced (default)', value: '0.8' },
      { label: '1.2% — Swing', value: '1.2' },
    ],
    rec: (c) => c > 1000 ? '0.8' : c > 200 ? '0.5' : '0.3',
  },
  {
    key: 'sl_min_pct', label: 'Stop Loss', hint: '% stop-loss from entry', suffix: '%',
    options: [
      { label: '0.3% — Tight', value: '0.3' },
      { label: '0.5% — Balanced (default)', value: '0.5' },
      { label: '0.8% — Moderate', value: '0.8' },
      { label: '1.2% — Wide', value: '1.2' },
    ],
    rec: (_c) => '0.5',
  },
  {
    key: 'cooldown_sec', label: 'Cooldown', hint: 'Seconds between entries', suffix: 's',
    options: [
      { label: '30s — Rapid', value: '30' },
      { label: '60s — Active (default)', value: '60' },
      { label: '120s — Moderate', value: '120' },
      { label: '300s — Patient', value: '300' },
    ],
    rec: (c) => c > 500 ? '120' : '60',
  },
  {
    key: 'max_slots', label: 'Max Slots', hint: 'Concurrent open positions',
    options: [
      { label: '1 — Single', value: '1' },
      { label: '3 — Grid (default)', value: '3' },
      { label: '5 — Dense grid', value: '5' },
      { label: '9 — Multi-asset', value: '9' },
    ],
    rec: (c) => c > 1000 ? '9' : c > 200 ? '5' : '3',
  },
  {
    key: 'min_entry_spacing_pct', label: 'Entry Spacing', hint: 'Min % between grid levels', suffix: '%',
    options: [
      { label: '0.15% — Tight', value: '0.15' },
      { label: '0.30% — Default', value: '0.30' },
      { label: '0.50% — Moderate', value: '0.50' },
      { label: '1.00% — Wide', value: '1.00' },
    ],
    rec: (_c) => '0.30',
  },
  {
    key: 'leverage', label: 'Leverage', hint: 'DEX futures multiplier', suffix: 'x',
    options: [
      { label: '2x — Safe', value: '2' },
      { label: '3x — Balanced (default)', value: '3' },
      { label: '5x — Aggressive', value: '5' },
    ],
    rec: (c) => c > 500 ? '3' : '2',
  },
  {
    key: 'daily_loss_limit_pct', label: 'Daily Loss Limit', hint: 'Stop trading if daily PnL below % of equity', suffix: '%',
    options: [
      { label: '2% — Tight leash', value: '2' },
      { label: '5% — Default', value: '5' },
      { label: '10% — Loose', value: '10' },
      { label: '0% — Off', value: '0' },
    ],
    rec: (_c) => '5',
  },
  {
    key: 'max_consecutive_losses', label: 'Max Consec. Losses', hint: 'Stop after N losses in a row',
    options: [
      { label: '2 — Quick stop', value: '2' },
      { label: '4 — Default', value: '4' },
      { label: '6 — Tolerant', value: '6' },
      { label: '0 — Off', value: '0' },
    ],
    rec: (_c) => '4',
  },
  {
    key: 'adaptive_enabled', label: 'Adaptive Thresholds', hint: 'Scale dip/TP/SL with volatility (ATR)',
    options: [
      { label: 'On (recommended)', value: 'true' },
      { label: 'Off — fixed floors only', value: 'false' },
    ],
    rec: (_c) => 'true',
  },
  {
    key: 'dip_k', label: 'Dip ATR Multiplier', hint: 'dip = max(k × ATR%, min_pct)',
    options: [
      { label: '0.3 — Sensitive', value: '0.3' },
      { label: '0.5 — Default', value: '0.5' },
      { label: '0.8 — Conservative', value: '0.8' },
      { label: '1.0 — Wide', value: '1.0' },
    ],
    rec: (_c) => '0.5',
  },
  {
    key: 'tp_k', label: 'TP ATR Multiplier', hint: 'tp = max(k × ATR%, min_pct)',
    options: [
      { label: '0.5 — Tight', value: '0.5' },
      { label: '1.0 — Default', value: '1.0' },
      { label: '1.5 — Wide', value: '1.5' },
    ],
    rec: (_c) => '1.0',
  },
]

// ── Free-input fields ───────────────────────────────────────────
interface FreeDef { key: string; label: string; hint: string; suffix?: string; step?: string; min?: string; max?: string }

const FREE_INPUTS: FreeDef[] = [
  { key: 'atr_period', label: 'ATR Period', hint: 'Candles for ATR calculation', step: '1', min: '5', max: '50' },
  { key: 'max_hold_minutes_spot', label: 'Max Hold (Spot)', hint: 'Minutes before time stop on CEX', step: '5', min: '10' },
  { key: 'max_hold_minutes_futures', label: 'Max Hold (Futures)', hint: 'Minutes before time stop on DEX', step: '5', min: '10' },
  { key: 'max_leverage', label: 'Max Leverage', hint: 'Hard cap on DEX leverage', step: '1', min: '1', max: '10' },
  { key: 'slope_threshold', label: 'Trend Slope', hint: 'Regime slope threshold (0.0012 default)', step: '0.0001', min: '0.0005' },
  { key: 'assumed_slippage_pct', label: 'Assumed Slippage', hint: '% for net-edge validation', step: '0.01', min: '0', suffix: '%' },
  { key: 'min_net_edge_pct', label: 'Min Net Edge', hint: 'Refuse to trade below this', step: '0.01', min: '0', suffix: '%' },
]

// ── Select fields ───────────────────────────────────────────────
interface SelectDef { key: string; label: string; hint: string; options: string[] }

const SELECTS: SelectDef[] = []

type ComboOption = { label: string; value: string; recommended?: boolean }

function NumberField({ value, suffix, disabled, step, min, max, error, verdict, onChange }: { value: string; suffix?: string; disabled?: boolean; step?: string; min?: string; max?: string; error?: boolean; verdict?: Verdict; onChange: (v: string) => void }) {
  const [draft, setDraft] = useState(value)
  const vLevel = verdict?.level

  useEffect(() => {
    setDraft(value)
  }, [value])

  return (
    <div className="flex items-center gap-2 w-full">
      <input
        type="number"
        value={draft}
        onChange={e => setDraft(e.target.value)}
        onBlur={() => { if (draft !== value) onChange(draft) }}
        onKeyUp={e => { if (e.key === 'Enter' && draft !== value) onChange(draft) }}
        disabled={disabled}
        step={step ?? 'any'}
        min={min}
        max={max}
        title={verdict?.message || ''}
        className={`flex-1 min-w-0 px-3 py-2.5 text-sm text-left bg-[#171421] border rounded-lg text-[#D0CFCC] focus:outline-none focus:border-[#D0CFCC] disabled:opacity-50 transition-colors
          ${vLevel === 'error' ? 'border-red-500' : vLevel === 'warn' ? 'border-amber-500' : error ? 'border-red-500' : 'border-[#2a2240]'}`}
      />
      {suffix && <span className="text-xs text-[#7a7090] w-5 text-right shrink-0">{suffix}</span>}
    </div>
  )
}

function ComboList({ label, value, options, onChange, disabled, onOpenChange }: { label: string; value: string; options: ComboOption[]; onChange: (v: string) => void; disabled?: boolean; onOpenChange?: (open: boolean) => void }) {
  const [open, setOpen] = useState(false)
  const [localValue, setLocalValue] = useState(value)
  const localValueRef = useRef(localValue)
  const selected = options.find(o => o.value === localValue)
  const containerRef = useRef<HTMLDivElement>(null)

  // Keep ref in sync with localValue so commit always reads the latest
  useEffect(() => {
    localValueRef.current = localValue
  }, [localValue])

  // Sync localValue when value prop changes externally
  useEffect(() => {
    setLocalValue(value)
    localValueRef.current = value
  }, [value])

  const updateOpen = useCallback((next: boolean) => {
    setOpen(next)
    onOpenChange?.(next)
  }, [onOpenChange])

  useEffect(() => {
    if (open) document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = '' }
  }, [open])

  // Confirm only on close / explicit confirm to avoid reopen-closing loops.
  // Uses ref to always read the latest localValue, avoiding stale closure issues.
  const commit = useCallback(() => {
    updateOpen(false)
    const current = localValueRef.current
    if (current !== value) onChange(current)
  }, [value, onChange, updateOpen])

  // Close on backdrop tap without firing on the first pointer event that may be part of the open gesture.
  const backdropDown = useRef(false)
  const handleBackdropPointerDown = useCallback((e: React.PointerEvent) => {
    if (e.target === containerRef.current) {
      backdropDown.current = true
    }
  }, [])
  const handleBackdropPointerUp = useCallback((e: React.PointerEvent) => {
    if (backdropDown.current && e.target === containerRef.current) {
      commit()
    }
    backdropDown.current = false
  }, [commit])

  return (
    <>
      <button
        type="button"
        onClick={() => !disabled && updateOpen(true)}
        disabled={disabled}
        className={`flex items-center justify-between w-full px-3 py-2.5 text-sm border rounded-lg transition-colors
          ${selected ? 'text-[#D0CFCC] bg-[#171421] border-[#2a2240]' : 'text-[#4a4060] bg-[#171421]/50 border-[#2a2240]'}
          ${disabled ? 'opacity-50 cursor-not-allowed' : 'hover:border-[#7a7090]'}`}
      >
        <span className="truncate">{selected?.label ?? 'Select…'}</span>
        <ChevronDown size={16} className="text-[#7a7090] shrink-0 ml-1.5" />
      </button>

      {open && (
        <div
          ref={containerRef}
          className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/70"
          onPointerDown={handleBackdropPointerDown}
          onPointerUp={handleBackdropPointerUp}
        >
          <div
            className="w-full sm:w-[360px] max-w-full bg-[#1a1528] rounded-t-2xl sm:rounded-2xl border border-[#2a2240] shadow-2xl overflow-hidden"
            onClick={e => e.stopPropagation()}
          >
            <div className="flex items-center justify-between px-4 py-3 border-b border-[#2a2240] bg-[#171421]">
              <span className="text-sm font-semibold text-[#D0CFCC]">{label}</span>
              <button
                type="button"
                onClick={commit}
                className="p-1 rounded-full text-[#7a7090] hover:bg-[#2a2240] hover:text-[#D0CFCC] transition-colors"
              >
                <X size={18} />
              </button>
            </div>
            <div className="max-h-[55vh] overflow-y-auto">
              {options.map((opt) => {
                const active = localValue === opt.value
                return (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => setLocalValue(opt.value)}
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

function MiniSettingsComponent() {
  const [settings, setSettings] = useState<SettingsData>({})
  const [saving, setSaving] = useState<Set<string>>(new Set())
  const [errors, setErrors] = useState<Set<string>>(new Set())
  const [loaded, setLoaded] = useState(false)
  const [editable, setEditable] = useState(isTelegram)
  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({})
  // Per-asset capital is managed via asset_configs table (Amendment 004).
  // Use a fixed default for preset recommendations (no longer derived from legacy slot_pct).
  const capital = 1000 // reference capital for preset recommendations

  // Inline validation (Amendment 002 — pure TS, no network)
  const verdicts = React.useMemo(() => validateAll(settings), [settings])

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

  useEffect(() => {
    if (isTelegram || browserSessionChecked) return
    browserSessionChecked = true
    fetch('/api/miniapp', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: '__ping__', value: '' }) })
      .then(r => { browserSessionValid = r.ok; setEditable(r.ok) })
      .catch(() => setEditable(false))
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
    if (saving.has(k)) return <Save size={14} className="text-yellow-500 animate-pulse" />
    if (errors.has(k)) return <AlertCircle size={14} className="text-red-500" />
    if (settings[k]) return <Check size={14} className="text-green-600" />
    return <span className="w-3.5 h-3.5" />
  }

  const Section = ({ title, children }: { title: string; children: ReactNode }) => (
    <div className="mb-4 rounded-xl border border-[#2a2240] bg-[#1a1528] overflow-hidden">
      <div className="px-3 py-2 border-b border-[#2a2240] bg-[#171421]/40">
        <h2 className="text-[10px] font-semibold uppercase tracking-wider text-[#7a7090]">{title}</h2>
      </div>
      <div className="divide-y divide-[#2a2240]">{children}</div>
    </div>
  )

  const SettingRow = ({ label, hint, statusKey, recommended, children }: { label: string; hint: string; statusKey: string; recommended?: string; children: ReactNode }) => (
    <div className="px-3 py-3 hover:bg-[#171421]/30 transition-colors">
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="min-w-0">
          <div className="text-xs font-medium text-[#D0CFCC]">{label}</div>
          <div className="text-[10px] text-[#7a7090] leading-tight">{hint}</div>
        </div>
        <div className="shrink-0 pt-0.5"><StatusIcon k={statusKey} /></div>
      </div>
      {children}
      {recommended && (
        <div className="mt-1.5 text-[10px] text-[#4a4060]">
          💡 Recommended: <span className="text-yellow-500">{recommended}</span>
        </div>
      )}
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
            {editable ? 'Tap a value to change it' : 'Read-only outside Telegram'}
          </p>
        </div>
        <span className={`text-[10px] px-2.5 py-1 rounded-full border ${editable ? 'text-[#D0CFCC] border-[#D0CFCC]/20 bg-[#D0CFCC]/10' : 'text-[#4a4060] border-[#2a2240] bg-[#1a1528]'}`}>
          {editable ? '⚡ auto-save' : 'read-only'}
        </span>
      </div>

      {/* ── Grid Scalper & Risk ── */}
      <Section title="Grid Scalper & Risk">
        {PRESETS.map(p => {
          const val = settings[p.key] ?? ''
          const rec = p.rec(capital)
          return (
            <SettingRow key={p.key} label={p.label} hint={p.hint} statusKey={p.key} recommended={rec + (p.suffix || '')}>
              <ComboList
                label={p.label}
                value={val}
                options={p.options.map(o => ({ ...o, recommended: o.value === rec }))}
                onChange={v => selectAndSave(p.key, v)}
                disabled={!editable}
              />
            </SettingRow>
          )
        })}
      </Section>

      {/* ── Capital & Targets ── */}
      <Section title="Capital & Targets">
        {FREE_INPUTS.map(f => {
          const val = settings[f.key] ?? ''
          const rec = f.key === 'grid_position_capital'
            ? String(Math.max(15, Math.round(capital / 3)))
            : f.key === 'ml_threshold' ? '0.80'
            : undefined
          return (
            <SettingRow key={f.key} label={f.label} hint={f.hint} statusKey={f.key} recommended={rec}>
              <NumberField
                value={val}
                suffix={f.suffix}
                step={f.step}
                verdict={verdicts[f.key]}
                min={f.min}
                max={f.max}
                disabled={!editable}
                error={errors.has(f.key)}
                onChange={v => debouncedSave(f.key, v)}
              />
            </SettingRow>
          )
        })}
      </Section>


      <div className="text-center text-[10px] text-[#4a4060] pb-6">
        {editable ? 'Changes save automatically on selection or after typing' : 'Open via Telegram bot to edit'}
      </div>
    </div>
  )
}

const MiniSettings = React.memo(MiniSettingsComponent)
export default MiniSettings
