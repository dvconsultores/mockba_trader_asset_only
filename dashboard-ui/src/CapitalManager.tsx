import React, { useState, useEffect, useCallback } from 'react'
import { AlertCircle, Loader2, Ban, CheckCircle2 } from 'lucide-react'
import { TG, isTelegram } from './TelegramProvider'

interface VenueCapital {
  venue: 'binance' | 'orderly'
  declared_capital: number
  live_equity: number
  equity_age: number | null
  divergence: { declared: number; live: number; pct: number } | null
  slot_pct: number
  slot_size: number
  max_slots: number
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

export default function CapitalManager() {
  const [capitals, setCapitals] = useState<VenueCapital[]>([])
  const [universes, setUniverses] = useState<Record<string, UniverseData>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [actionLoading, setActionLoading] = useState<string | null>(null)
  const [editable, setEditable] = useState(isTelegram)
  const [edit, setEdit] = useState<Record<string, Record<string, string>>>({})

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
      setError(null)
    } catch (e: any) {
      setError(e.message || 'Failed to load capital')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchAll()
    const iv = setInterval(fetchAll, 30000)
    return () => clearInterval(iv)
  }, [fetchAll])

  // Check browser session for editability (same pattern as MiniSettings)
  useEffect(() => {
    if (isTelegram) return
    const check = async () => {
      try {
        const res = await fetch('/api/miniapp', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: '__ping__', value: '' }),
        })
        setEditable(res.ok)
      } catch {
        setEditable(false)
      }
    }
    check()
  }, [])

  const saveSetting = async (venue: string, key: string, value: string) => {
    setActionLoading(`${venue}:${key}`)
    setError(null)
    try {
      const res = await fetch('/api/miniapp', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ key, value }),
      })
      if (!res.ok) throw new Error(await res.text())
      await fetchAll()
    } catch (e: any) {
      setError(e.message || 'Failed to save')
    } finally {
      setActionLoading(null)
    }
  }

  const toggleVenue = async (venue: string, current: string) => {
    setActionLoading(`${venue}:toggle`)
    setError(null)
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
    } catch (e: any) {
      setError(e.message || 'Failed to toggle venue')
    } finally {
      setActionLoading(null)
    }
  }

  const toggleBlacklist = async (venue: string, asset: string, current: number) => {
    setActionLoading(`${venue}:${asset}`)
    setError(null)
    try {
      const res = await fetch(`/api/universe/${venue}/${encodeURIComponent(asset)}/blacklist`, {
        method: 'PUT',
        headers: authHeaders(),
        body: JSON.stringify({ blacklisted: !current }),
      })
      if (!res.ok) throw new Error(await res.text())
      await fetchAll()
    } catch (e: any) {
      setError(e.message || 'Failed to update blacklist')
    } finally {
      setActionLoading(null)
    }
  }

  const capByVenue = (v: string) => capitals.find(c => c.venue === v)
  const uniByVenue = (v: string) => universes[v]

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
            {editable ? 'Per-venue pools — live equity always wins' : 'Read-only outside Telegram'}
          </p>
        </div>
      </div>

      {error && (
        <div className="mb-4 p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-xs flex items-start gap-2">
          <AlertCircle size={14} className="shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      {/* Venue panels */}
      <div className="space-y-4 mb-6">
        {capitals.map(cap => {
          const v = cap.venue
          const slotExceeds = cap.max_slots * cap.slot_pct > 100
          const noRoom = cap.slot_size > 0 && cap.free < cap.slot_size
          const busy = actionLoading === `${v}:toggle` || actionLoading?.startsWith(`${v}:`)
          const e = edit[v] || {}
          return (
            <div key={v} className="rounded-xl border border-[#2a2240] bg-[#1a1528] overflow-hidden">
              <div className="px-3 py-2.5 flex items-center justify-between border-b border-[#2a2240]">
                <span className="text-sm font-medium">{VENUE_LABEL[v]}</span>
                {editable && (
                  <button
                    onClick={() => toggleVenue(v, cap.enabled)}
                    disabled={busy}
                    className={`text-[10px] px-2.5 py-1 rounded-full border transition-colors disabled:opacity-40 ${
                      cap.enabled !== 'False'
                        ? 'text-green-400 border-green-500/30 bg-green-500/10'
                        : 'text-[#4a4060] border-[#2a2240] bg-[#171421]'
                    }`}
                  >
                    {cap.enabled !== 'False' ? 'ON' : 'OFF'}
                  </button>
                )}
              </div>

              <div className="p-3 space-y-2 text-[11px]">
                {/* Declared vs live equity */}
                <div className="flex items-center justify-between">
                  <span className="text-[#7a7090]">Declared</span>
                  {editable ? (
                    <div className="flex items-center gap-1.5">
                      <input
                        type="number"
                        value={e.declared ?? String(cap.declared_capital)}
                        onChange={ev => setEdit({ ...edit, [v]: { ...e, declared: ev.target.value } })}
                        min="0"
                        step="100"
                        className="w-24 px-2 py-1 text-right text-xs bg-[#171421] border border-[#2a2240] rounded-md text-[#D0CFCC] focus:outline-none focus:border-[#D0CFCC]"
                      />
                      <button
                        onClick={() => {
                          const key = v === 'binance' ? 'capital_cex_usdt' : 'capital_dex_usdc'
                          saveSetting(v, key, e.declared ?? String(cap.declared_capital))
                        }}
                        disabled={actionLoading === `${v}:capital_${key(v)}`}
                        className="px-2 py-1 text-[10px] bg-green-500/10 border border-green-500/30 rounded-md text-green-400 hover:bg-green-500/20 disabled:opacity-40"
                      >
                        {actionLoading === `${v}:capital_${key(v)}` ? <Loader2 size={10} className="animate-spin" /> : 'Save'}
                      </button>
                    </div>
                  ) : (
                    <span>${cap.declared_capital.toFixed(0)}</span>
                  )}
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[#7a7090]">Live equity</span>
                  <span className="font-medium">${cap.live_equity.toFixed(0)}</span>
                </div>

                {cap.divergence && (
                  <div className="p-2 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-400">
                    ⚠️ Declared ${cap.divergence.declared.toFixed(0)} diverges from live
                    ${cap.divergence.live.toFixed(0)} by {cap.divergence.pct}% — exchange wins, sizing unchanged
                  </div>
                )}

                {/* Slot config */}
                <div className="flex items-center justify-between">
                  <span className="text-[#7a7090]">Slot %</span>
                  {editable ? (
                    <div className="flex items-center gap-1.5">
                      <input
                        type="number"
                        value={e.slotPct ?? String(cap.slot_pct)}
                        onChange={ev => setEdit({ ...edit, [v]: { ...e, slotPct: ev.target.value } })}
                        min="0.1"
                        max="100"
                        step="0.5"
                        className="w-20 px-2 py-1 text-right text-xs bg-[#171421] border border-[#2a2240] rounded-md text-[#D0CFCC] focus:outline-none focus:border-[#D0CFCC]"
                      />
                      <button
                        onClick={() => saveSetting(v, `${v === 'binance' ? 'cex' : 'dex'}_slot_pct`, e.slotPct ?? String(cap.slot_pct))}
                        disabled={actionLoading === `${v}:${v === 'binance' ? 'cex' : 'dex'}_slot_pct`}
                        className="px-2 py-1 text-[10px] bg-green-500/10 border border-green-500/30 rounded-md text-green-400 hover:bg-green-500/20 disabled:opacity-40"
                      >
                        {actionLoading === `${v}:${v === 'binance' ? 'cex' : 'dex'}_slot_pct` ? <Loader2 size={10} className="animate-spin" /> : 'Save'}
                      </button>
                    </div>
                  ) : (
                    <span>{cap.slot_pct}%</span>
                  )}
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[#7a7090]">Slot size</span>
                  <span className="font-medium">${cap.slot_size.toFixed(0)}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[#7a7090]">Max slots</span>
                  {editable ? (
                    <div className="flex items-center gap-1.5">
                      <input
                        type="number"
                        value={e.maxSlots ?? String(cap.max_slots)}
                        onChange={ev => setEdit({ ...edit, [v]: { ...e, maxSlots: ev.target.value } })}
                        min="1"
                        step="1"
                        className="w-16 px-2 py-1 text-right text-xs bg-[#171421] border border-[#2a2240] rounded-md text-[#D0CFCC] focus:outline-none focus:border-[#D0CFCC]"
                      />
                      <button
                        onClick={() => saveSetting(v, `max_slots_${v === 'binance' ? 'cex' : 'dex'}`, e.maxSlots ?? String(cap.max_slots))}
                        disabled={actionLoading === `${v}:max_slots_${v === 'binance' ? 'cex' : 'dex'}`}
                        className="px-2 py-1 text-[10px] bg-green-500/10 border border-green-500/30 rounded-md text-green-400 hover:bg-green-500/20 disabled:opacity-40"
                      >
                        {actionLoading === `${v}:max_slots_${v === 'binance' ? 'cex' : 'dex'}` ? <Loader2 size={10} className="animate-spin" /> : 'Save'}
                      </button>
                    </div>
                  ) : (
                    <span>{cap.max_slots}</span>
                  )}
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[#7a7090]">Deployed / Free</span>
                  <span>${cap.deployed.toFixed(0)} / ${cap.free.toFixed(0)}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[#7a7090]">Fee (round trip)</span>
                  <span>{cap.fee_pct.toFixed(2)}%</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[#7a7090]">Net edge</span>
                  <span className={cap.net_edge_pct > 0 ? 'text-green-400' : 'text-red-400'}>{cap.net_edge_pct.toFixed(2)}%</span>
                </div>

                {/* Deterministic inline validation */}
                {(slotExceeds || noRoom) && (
                  <div className="p-2 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400">
                    {slotExceeds && <div>✗ {cap.max_slots} slots × {cap.slot_pct}% = {Math.round(cap.max_slots * cap.slot_pct)}% of equity — exceeds 100%</div>}
                    {noRoom && <div>✗ Free ${cap.free.toFixed(0)} below one slot ${cap.slot_size.toFixed(0)} — insufficient equity</div>}
                  </div>
                )}
              </div>
            </div>
          )
        })}
      </div>

      {/* Universe panels */}
      {(['binance', 'orderly'] as const).map(venue => {
        const u = uniByVenue(venue)
        if (!u) return null
        return (
          <div key={venue} className={`rounded-xl border overflow-hidden mb-4 ${u.stale ? 'border-red-500/40 bg-red-500/5' : 'border-[#2a2240] bg-[#1a1528]'}`}>
            <div className="px-3 py-2.5 flex items-center justify-between border-b border-[#2a2240]">
              <span className="text-sm font-medium">🛰️ {VENUE_SHORT[venue]} Universe</span>
              {u.scan_age_hours != null && (
                <span className={`text-[10px] px-2 py-0.5 rounded-full border ${
                  u.stale ? 'text-red-400 border-red-500/40 bg-red-500/10' : 'text-[#7a7090] border-[#2a2240] bg-[#171421]'
                }`}>
                  {u.stale ? `⚠️ STALE · ${u.scan_age_hours.toFixed(0)}h` : `${u.scan_age_hours.toFixed(1)}h ago`}
                </span>
              )}
            </div>
            {u.rows.length === 0 ? (
              <div className="p-3 text-center text-[#4a4060] text-xs">No scan stored yet.</div>
            ) : (
              <div className="divide-y divide-[#2a2240]">
                {u.rows.map(r => (
                  <div key={r.asset} className={`px-3 py-2 flex items-center justify-between text-[11px] ${r.blacklisted ? 'opacity-60' : ''}`}>
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5">
                        <span className="text-[10px] text-[#4a4060]">#{r.rank}</span>
                        <span className="text-sm font-medium truncate">{r.asset}</span>
                        {r.blacklisted && <Ban size={12} className="text-red-400 shrink-0" />}
                      </div>
                      <div className="text-[10px] text-[#7a7090] mt-0.5">
                        rec={r.recovery_rate != null ? `${Math.round(r.recovery_rate * 100)}%` : '—'}
                        {' · '}sig={r.signals_count ?? '—'}
                        {' · '}spread={r.spread_pct != null ? `${r.spread_pct.toFixed(3)}%` : '—'}
                        {' · '}vol=${((r.quote_volume_24h || 0) / 1e6).toFixed(1)}M
                        {' · '}depth=${((r.depth_bid_top10 || 0) / 1e3).toFixed(0)}k
                      </div>
                    </div>
                    {editable && (
                      <button
                        onClick={() => toggleBlacklist(venue, r.asset, r.blacklisted)}
                        disabled={actionLoading === `${venue}:${r.asset}`}
                        className={`ml-2 px-2 py-1 text-[10px] rounded-md border transition-colors shrink-0 disabled:opacity-40 ${
                          r.blacklisted
                            ? 'text-red-400 border-red-500/30 bg-red-500/10 hover:bg-red-500/20'
                            : 'text-[#4a4060] border-[#2a2240] hover:text-[#D0CFCC] hover:bg-[#2a2240]'
                        }`}
                        title={r.blacklisted ? 'Blacklisted — click to allow' : 'Blacklist (operator override)'}
                      >
                        {actionLoading === `${venue}:${r.asset}` ? <Loader2 size={10} className="animate-spin" /> : r.blacklisted ? 'Unblock' : 'Block'}
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )
      })}

      <p className="mt-3 text-[10px] text-[#4a4060] text-center">
        Recovery rate is a relative ranking signal from candle replay — not a predicted win rate.
      </p>
    </div>
  )
}

function key(v: string): string {
  return v === 'binance' ? 'cex_usdt' : 'dex_usdc'
}
