import React, { useState, useEffect, useCallback, type ReactNode } from 'react'
import { Save, Check, AlertCircle, Loader2, Ban } from 'lucide-react'
import { TG, isTelegram } from './TelegramProvider'

interface VenueCapital {
  venue: 'binance' | 'orderly'
  declared_capital: number
  live_equity: number
  equity_age: number | null
  divergence: { declared: number; live: number; pct: number } | null
  slot_pct: number
  slot_size: number
  deployed: number
  free: number
  fee_pct: number
  net_edge_pct: number
  enabled: string
}

interface UniverseRow {
  asset: string
  symbol: string
  rank: number
  scanned_at: number
  quote_volume_24h: number | null
  spread_pct: number | null
  depth_bid_top10: number | null
  depth_ask_top10: number | null
  atr_pct_median: number | null
  signals_count: number | null
  recovery_rate: number | null
  median_minutes_to_tp: number | null
  blacklisted: number
}

interface UniverseData {
  rows: UniverseRow[]
  scan_age_hours: number | null
  stale: boolean
}

const VENUE_LABEL: Record<string, string> = { binance: 'CEX — Binance spot', orderly: 'DEX — Orderly perps' }
const VENUE_SHORT: Record<string, string> = { binance: 'CEX', orderly: 'DEX' }

// settings keys per venue
const DECLARED_KEY: Record<string, string> = { binance: 'capital_cex_usdt', orderly: 'capital_dex_usdc' }
const SLOT_PCT_KEY: Record<string, string> = { binance: 'cex_slot_pct', orderly: 'dex_slot_pct' }

// ── Presentational components — module level (STABLE identity).             ──
// Defining these inside the main component would give them a new function
// reference on every render, which makes React remount the subtree and drop
// input focus after every keystroke. Keep them here.

// Standard-size number input — same style as the Settings view (MiniSettings).
// `onEnter` commits the current value when the Enter / Done key is pressed.
function NumberField({ value, suffix, disabled, step, min, max, error, onChange, onEnter }: {
  value: string; suffix?: string; disabled?: boolean; step?: string; min?: string; max?: string;
  error?: boolean; onChange: (v: string) => void; onEnter?: () => void
}) {
  return (
    <div className="flex items-center gap-2 w-full">
      <input
        type="number"
        value={value}
        onChange={e => onChange(e.target.value)}
        onKeyUp={e => { if (e.key === 'Enter' && onEnter) onEnter() }}
        disabled={disabled}
        step={step ?? 'any'}
        min={min}
        max={max}
        className={`flex-1 min-w-0 px-3 py-2.5 text-sm text-left bg-[#171421] border rounded-lg text-[#D0CFCC] focus:outline-none focus:border-[#D0CFCC] disabled:opacity-50 transition-colors
          ${error ? 'border-red-500' : 'border-[#2a2240]'}`}
      />
      {suffix && <span className="text-xs text-[#7a7090] w-5 text-right shrink-0">{suffix}</span>}
    </div>
  )
}

function StatusIcon({ k, saving, errors }: { k: string; saving: Set<string>; errors: Set<string> }) {
  if (saving.has(k)) return <Save size={14} className="text-yellow-500 animate-pulse" />
  if (errors.has(k)) return <AlertCircle size={14} className="text-red-500" />
  return <Check size={14} className="text-green-600" />
}

function Section({ title, right, children }: { title: string; right?: ReactNode; children: ReactNode }) {
  return (
    <div className="mb-4 rounded-xl border border-[#2a2240] bg-[#1a1528] overflow-hidden">
      <div className="px-3 py-2 border-b border-[#2a2240] bg-[#171421]/40 flex items-center justify-between">
        <h2 className="text-[10px] font-semibold uppercase tracking-wider text-[#7a7090]">{title}</h2>
        {right}
      </div>
      <div className="divide-y divide-[#2a2240]">{children}</div>
    </div>
  )
}

function SettingRow({ label, hint, statusKey, saving, errors, children }: {
  label: string; hint: string; statusKey: string; saving: Set<string>; errors: Set<string>; children: ReactNode
}) {
  return (
    <div className="px-3 py-3 hover:bg-[#171421]/30 transition-colors">
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="min-w-0">
          <div className="text-xs font-medium text-[#D0CFCC]">{label}</div>
          <div className="text-[10px] text-[#7a7090] leading-tight">{hint}</div>
        </div>
        <div className="shrink-0 pt-0.5"><StatusIcon k={statusKey} saving={saving} errors={errors} /></div>
      </div>
      {children}
    </div>
  )
}

function ReadRow({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="px-3 py-3 flex items-center justify-between gap-2">
      <span className="text-xs text-[#7a7090]">{label}</span>
      <span className={`text-sm font-medium text-right ${tone ?? 'text-[#D0CFCC]'}`}>{value}</span>
    </div>
  )
}

export default function CapitalManager() {
  const [capitals, setCapitals] = useState<VenueCapital[]>([])
  const [universes, setUniverses] = useState<Record<string, UniverseData>>({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState<Set<string>>(new Set())
  const [errors, setErrors] = useState<Set<string>>(new Set())
  const [editable, setEditable] = useState(isTelegram)
  // Staged edits per settings key — nothing is saved until Save/Enter.
  const [drafts, setDrafts] = useState<Record<string, string>>({})

  const authHeaders = useCallback((): Record<string, string> => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    if (TG?.initData) headers['X-Telegram-InitData'] = TG.initData
    return headers
  }, [])

  const fetchAll = useCallback(async () => {
    try {
      const [capRes, uniCex, uniDex] = await Promise.all([
        fetch('/api/capital'),
        fetch('/api/universe/binance'),
        fetch('/api/universe/orderly'),
      ])
      const cap = await capRes.json()
      const cex = await uniCex.json()
      const dex = await uniDex.json()
      setCapitals(cap.venues || [])
      setUniverses({ binance: cex, orderly: dex })
    } catch {
      /* keep last data */
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchAll()
    const iv = setInterval(fetchAll, 30000)
    return () => clearInterval(iv)
  }, [fetchAll])

  // Browser session probe for editability (same as MiniSettings)
  useEffect(() => {
    if (isTelegram) return
    fetch('/api/miniapp', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: '__ping__', value: '' }),
    })
      .then(r => setEditable(r.ok))
      .catch(() => setEditable(false))
  }, [])

  const saveSetting = useCallback(async (key: string, value: string) => {
    setSaving(prev => new Set(prev).add(key))
    try {
      const res = await fetch('/api/miniapp', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ key, value }),
      })
      if (!res.ok) throw new Error(await res.text())
      await fetchAll()
      setSaving(prev => { const n = new Set(prev); n.delete(key); return n })
      setErrors(prev => { const n = new Set(prev); n.delete(key); return n })
      setDrafts(prev => { const n = { ...prev }; delete n[key]; return n })
    } catch {
      setSaving(prev => { const n = new Set(prev); n.delete(key); return n })
      setErrors(prev => new Set(prev).add(key))
      setTimeout(() => setErrors(prev => { const n = new Set(prev); n.delete(key); return n }), 1500)
    }
  }, [authHeaders, fetchAll])

  const toggleVenue = async (venue: string, current: string) => {
    const sk = `${venue}:toggle`
    setSaving(prev => new Set(prev).add(sk))
    const exchange = venue === 'binance' ? 'cex' : 'dex'
    const mode = current === 'False' ? 'Automatic' : 'False'
    try {
      const res = await fetch('/api/bot/control', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ exchange, mode }),
      })
      if (!res.ok) throw new Error(await res.text())
      await fetchAll()
    } catch {
      setErrors(prev => new Set(prev).add(`${venue}:toggle`))
      setTimeout(() => setErrors(prev => { const n = new Set(prev); n.delete(`${venue}:toggle`); return n }), 1500)
    } finally {
      setSaving(prev => { const n = new Set(prev); n.delete(sk); return n })
    }
  }

  const toggleBlacklist = async (venue: string, asset: string, current: number) => {
    const sk = `${venue}:${asset}`
    setSaving(prev => new Set(prev).add(sk))
    try {
      const res = await fetch(`/api/universe/${venue}/${encodeURIComponent(asset)}/blacklist`, {
        method: 'PUT',
        headers: authHeaders(),
        body: JSON.stringify({ blacklisted: !current }),
      })
      if (!res.ok) throw new Error(await res.text())
      await fetchAll()
    } catch {
      setErrors(prev => new Set(prev).add(sk))
      setTimeout(() => setErrors(prev => { const n = new Set(prev); n.delete(sk); return n }), 1500)
    } finally {
      setSaving(prev => { const n = new Set(prev); n.delete(sk); return n })
    }
  }

  // Editable row rendered inline (NOT a component) so the input keeps focus
  // across keystrokes. Empty/NaN values are refused: a cleared field can never
  // store "" and silently turn the setting into its 0 default.
  const renderEditable = (
    label: string, hint: string, settingsKey: string, value: string,
    suffix?: string, step?: string, min?: string, max?: string,
  ) => {
    const commit = () => {
      const raw = (drafts[settingsKey] ?? value).trim()
      if (raw === '' || Number.isNaN(parseFloat(raw))) {
        setDrafts(prev => { const n = { ...prev }; delete n[settingsKey]; return n })
        return
      }
      saveSetting(settingsKey, raw)
    }
    return (
      <SettingRow label={label} hint={hint} statusKey={settingsKey} saving={saving} errors={errors}>
        <div className="flex items-center gap-2">
          <NumberField
            value={drafts[settingsKey] ?? value}
            suffix={suffix}
            step={step}
            min={min}
            max={max}
            disabled={!editable}
            error={errors.has(settingsKey)}
            onChange={v => setDrafts(prev => ({ ...prev, [settingsKey]: v }))}
            onEnter={commit}
          />
          {editable && (
            <button
              onClick={commit}
              disabled={saving.has(settingsKey)}
              className="shrink-0 px-3 py-2.5 text-xs font-medium rounded-lg border border-[#2a2240] bg-[#2a2240] text-[#D0CFCC] hover:bg-[#3a3050] transition-colors disabled:opacity-40"
            >
              {saving.has(settingsKey) ? <Loader2 size={14} className="animate-spin" /> : 'Save'}
            </button>
          )}
        </div>
      </SettingRow>
    )
  }

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center text-[#4a4060]">
        <Loader2 className="animate-spin mr-2" size={20} />
        Loading capital…
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-y-auto px-4 sm:px-6 py-4 text-[#D0CFCC]">
      {/* Header */}
      <div className="flex items-center justify-between mb-5">
        <div>
          <h1 className="text-base sm:text-lg font-bold text-[#D0CFCC]">💰 Capital</h1>
          <p className="text-xs text-[#7a7090] mt-0.5">
            {editable ? 'Edit a value and tap Save — live equity always wins' : 'Read-only outside Telegram'}
          </p>
        </div>
        {!editable && (
          <span className="text-[10px] px-2.5 py-1 rounded-full border text-[#4a4060] border-[#2a2240] bg-[#1a1528]">
            read-only
          </span>
        )}
      </div>

      {/* Per-venue capital panels */}
      {capitals.map(cap => {
        const v = cap.venue
        const noRoom = cap.slot_size > 0 && cap.free < cap.slot_size
        const on = cap.enabled !== 'False'
        return (
          <Section
            key={v}
            title={`${VENUE_SHORT[v]} · ${VENUE_LABEL[v]}`}
            right={
              editable && (
                <button
                  onClick={() => toggleVenue(v, cap.enabled)}
                  disabled={saving.has(`${v}:toggle`)}
                  className={`text-[10px] px-3 py-1 rounded-full border transition-colors disabled:opacity-40 ${on ? 'text-green-400 border-green-500/30 bg-green-500/10' : 'text-[#7a7090] border-[#2a2240] bg-[#171421]'}`}
                >
                  {on ? 'ON' : 'OFF'}
                </button>
              )
            }
          >
            {renderEditable(
              'Declared Capital',
              'Operator-declared pool — live equity wins if they diverge',
              DECLARED_KEY[v],
              String(cap.declared_capital),
              '$',
            )}

            <ReadRow label="Live equity" value={`$${cap.live_equity.toFixed(0)}`} />

            {cap.divergence && (
              <div className="px-3 py-2.5 bg-amber-500/10 border-t border-amber-500/30 text-amber-400 text-xs">
                ⚠️ Declared ${cap.divergence.declared.toFixed(0)} diverges from live ${cap.divergence.live.toFixed(0)} by {cap.divergence.pct}% — exchange wins, sizing unchanged
              </div>
            )}

            {renderEditable(
              'Slot %',
              'Size of a single position = this % × live equity',
              SLOT_PCT_KEY[v],
              String(cap.slot_pct),
              '%',
              '0.5', '0.1', '100',
            )}

            <ReadRow label="Slot size" value={`$${cap.slot_size.toFixed(0)}`} />

            <ReadRow label="Deployed / Free" value={`$${cap.deployed.toFixed(0)} / $${cap.free.toFixed(0)}`} />
            <ReadRow label="Fee (round trip)" value={`${cap.fee_pct.toFixed(2)}%`} />
            <ReadRow
              label="Net edge"
              value={`${cap.net_edge_pct.toFixed(2)}%`}
              tone={cap.net_edge_pct > 0 ? 'text-green-400' : 'text-red-400'}
            />

            {noRoom && (
              <div className="px-3 py-2.5 bg-red-500/10 border-t border-red-500/30 text-red-400 text-xs">
                <div>✗ Free ${cap.free.toFixed(0)} below one slot ${cap.slot_size.toFixed(0)} — insufficient equity</div>
              </div>
            )}
          </Section>
        )
      })}

      {/* Universe panels (read-only except blacklist) */}
      {(['binance', 'orderly'] as const).map(venue => {
        const u = universes[venue]
        if (!u) return null
        return (
          <Section
            key={venue}
            title={`🛰️ ${VENUE_SHORT[venue]} Universe`}
            right={
              u.scan_age_hours != null ? (
                <span className={`text-[10px] px-2 py-0.5 rounded-full border ${u.stale ? 'text-red-400 border-red-500/40 bg-red-500/10' : 'text-[#7a7090] border-[#2a2240] bg-[#171421]'}`}>
                  {u.stale ? `⚠️ STALE · ${u.scan_age_hours.toFixed(0)}h` : `${u.scan_age_hours.toFixed(1)}h ago`}
                </span>
              ) : undefined
            }
          >
            {u.rows.length === 0 ? (
              <div className="px-3 py-4 text-center text-[#4a4060] text-xs">No scan stored yet.</div>
            ) : (
              u.rows.map(r => (
                <div key={r.asset} className={`px-3 py-2.5 flex items-center justify-between gap-2 ${r.blacklisted ? 'opacity-60' : ''}`}>
                  <div className="min-w-0">
                    <div className="flex items-center gap-1.5">
                      <span className="text-[10px] text-[#4a4060]">#{r.rank}</span>
                      <span className="text-sm font-medium truncate">{r.asset}</span>
                      {r.blacklisted && <Ban size={12} className="text-red-400 shrink-0" />}
                    </div>
                    <div className="text-[10px] text-[#7a7090] mt-0.5 leading-tight">
                      rec={r.recovery_rate != null ? `${Math.round(r.recovery_rate * 100)}%` : '—'}
                      {' · '}sig={r.signals_count ?? '—'}
                      {' · '}spread={r.spread_pct != null ? `${r.spread_pct.toFixed(3)}%` : '—'}
                      {' · '}vol=${((r.quote_volume_24h || 0) / 1e6).toFixed(1)}M
                    </div>
                  </div>
                  {editable && (
                    <button
                      onClick={() => toggleBlacklist(venue, r.asset, r.blacklisted)}
                      disabled={saving.has(`${venue}:${r.asset}`)}
                      className={`shrink-0 px-3 py-1.5 text-xs rounded-lg border transition-colors disabled:opacity-40 ${
                        r.blacklisted
                          ? 'text-red-400 border-red-500/30 bg-red-500/10 hover:bg-red-500/20'
                          : 'text-[#7a7090] border-[#2a2240] hover:text-[#D0CFCC] hover:bg-[#2a2240]'
                      }`}
                      title={r.blacklisted ? 'Blacklisted — click to allow' : 'Blacklist (operator override)'}
                    >
                      {saving.has(`${venue}:${r.asset}`) ? <Loader2 size={12} className="animate-spin" /> : r.blacklisted ? 'Unblock' : 'Block'}
                    </button>
                  )}
                </div>
              ))
            )}
          </Section>
        )
      })}

      <div className="text-center text-[10px] text-[#4a4060] pb-6">
        Recovery rate is a relative ranking signal from candle replay — not a predicted win rate.
      </div>
    </div>
  )
}
