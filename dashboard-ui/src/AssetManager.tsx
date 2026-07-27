import React, { useState, useEffect, useCallback } from 'react'
import { Plus, Trash2, AlertCircle, Loader2, Power, PowerOff } from 'lucide-react'
import { TG, isTelegram } from './TelegramProvider'

interface AssetItem {
  symbol: string
  capital_dex: number
  capital_cex: number
  active_dex: boolean
  active_cex: boolean
  open_positions: number
}

interface AssetData {
  assets: AssetItem[]
  summary: { venue: string; total_allocated: number; active_pairs: number; remaining: null }[]
}

export default function AssetManager() {
  const [data, setData] = useState<AssetData>({ assets: [], summary: [] })
  const [newAsset, setNewAsset] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [actionLoading, setActionLoading] = useState<string | null>(null)
  const [editable, setEditable] = useState(isTelegram)

  const authHeaders = useCallback((): Record<string, string> => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    if (TG?.initData) headers['X-Telegram-InitData'] = TG.initData
    return headers
  }, [])

  const fetchAssets = useCallback(async () => {
    try {
      const res = await fetch('/api/assets')
      if (!res.ok) throw new Error(await res.text())
      const json = await res.json()
      setData({ assets: json.assets || [], summary: json.summary || [] })
      setError(null)
    } catch (e: any) {
      setError(e.message || 'Failed to load assets')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchAssets()
  }, [fetchAssets])

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

  const addAsset = async () => {
    const symbol = newAsset.trim()
    if (!symbol) return
    setActionLoading(symbol)
    setError(null)
    try {
      const res = await fetch('/api/assets', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ symbol, capital_dex: 0, capital_cex: 0, active_dex: false, active_cex: false }),
      })
      if (!res.ok) throw new Error(await res.text())
      setNewAsset('')
      await fetchAssets()
    } catch (e: any) {
      setError(e.message || 'Failed to add asset')
    } finally {
      setActionLoading(null)
    }
  }

  const removeAsset = async (symbol: string) => {
    setActionLoading(symbol)
    setError(null)
    try {
      const res = await fetch(`/api/assets/${encodeURIComponent(symbol)}`, {
        method: 'DELETE',
        headers: authHeaders(),
      })
      if (!res.ok) throw new Error(await res.text())
      await fetchAssets()
    } catch (e: any) {
      setError(e.message || 'Failed to remove asset')
    } finally {
      setActionLoading(null)
    }
  }

  const toggleVenue = async (symbol: string, venue: 'dex' | 'cex', currentActive: boolean) => {
    setActionLoading(`${symbol}:${venue}`)
    setError(null)
    const asset = data.assets.find(a => a.symbol === symbol)
    if (!asset) return
    try {
      const body: Record<string, any> = {
        capital_dex: asset.capital_dex,
        capital_cex: asset.capital_cex,
        active_dex: asset.active_dex,
        active_cex: asset.active_cex,
      }
      body[`active_${venue}`] = !currentActive
      const res = await fetch(`/api/assets/${encodeURIComponent(symbol)}`, {
        method: 'PUT',
        headers: authHeaders(),
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error(await res.text())
      await fetchAssets()
    } catch (e: any) {
      setError(e.message || 'Failed to toggle')
    } finally {
      setActionLoading(null)
    }
  }

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center text-[#4a4060]">
        <Loader2 className="animate-spin mr-2" size={20} />
        Loading assets…
      </div>
    )
  }

  const totalActive = data.assets.filter(a => a.active_dex || a.active_cex).length

  return (
    <div className="flex-1 overflow-y-auto px-4 sm:px-6 py-4 text-[#D0CFCC]">
      {/* Header */}
      <div className="flex items-center justify-between mb-5">
        <div>
          <h1 className="text-base sm:text-lg font-bold text-[#D0CFCC]">📦 Assets</h1>
          <p className="text-xs text-[#7a7090] mt-0.5">
            {editable ? 'Manage trading assets' : 'Read-only outside Telegram'}
          </p>
        </div>
        <span className={`text-[10px] px-2.5 py-1 rounded-full border ${editable ? 'text-[#D0CFCC] border-[#D0CFCC]/20 bg-[#D0CFCC]/10' : 'text-[#4a4060] border-[#2a2240] bg-[#1a1528]'}`}>
          {data.assets.length} assets · {totalActive} active
        </span>
      </div>

      {/* Allocation summary */}
      {data.summary.length > 0 && (
        <div className="mb-4 grid grid-cols-2 gap-2">
          {data.summary.map(s => (
            <div key={s.venue} className="bg-[#1a1528] border border-[#2a2240] rounded-lg p-2.5">
              <div className="text-[10px] text-[#4a4060] uppercase">{s.venue}</div>
              <div className="text-sm text-[#D0CFCC] font-medium">${s.total_allocated.toFixed(0)}</div>
              <div className="text-[10px] text-[#7a7090]">{s.active_pairs} active pairs</div>
            </div>
          ))}
        </div>
      )}

      {/* Error banner */}
      {error && (
        <div className="mb-4 p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-xs flex items-start gap-2">
          <AlertCircle size={14} className="shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      {/* Add asset */}
      {editable && (
        <div className="mb-4 flex gap-2">
          <input
            type="text"
            value={newAsset}
            onChange={e => setNewAsset(e.target.value)}
            onKeyUp={e => { if (e.key === 'Enter') addAsset() }}
            placeholder="e.g. PERP_NEAR_USDC"
            className="flex-1 px-3 py-2.5 text-sm bg-[#171421] border border-[#2a2240] rounded-lg text-[#D0CFCC] focus:outline-none focus:border-[#D0CFCC] placeholder-[#4a4060]"
          />
          <button
            onClick={addAsset}
            disabled={!newAsset.trim() || actionLoading === newAsset.trim()}
            className="px-4 py-2.5 bg-[#2a2240] hover:bg-[#3a3050] border border-[#3a3050] rounded-lg text-[#D0CFCC] text-sm font-medium disabled:opacity-40 transition-colors flex items-center gap-1.5 shrink-0"
          >
            {actionLoading === newAsset.trim() ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <Plus size={16} />
            )}
            Add
          </button>
        </div>
      )}

      {/* Asset list */}
      {data.assets.length === 0 ? (
        <div className="text-center py-8 text-[#4a4060] text-sm">
          {editable ? 'No assets configured. Add one above.' : 'No assets configured.'}
        </div>
      ) : (
        <div className="rounded-xl border border-[#2a2240] bg-[#1a1528] overflow-hidden">
          <div className="divide-y divide-[#2a2240]">
            {data.assets.map(asset => {
              const isBusy = actionLoading === asset.symbol || actionLoading === `${asset.symbol}:dex` || actionLoading === `${asset.symbol}:cex`
              return (
                <div
                  key={asset.symbol}
                  className="flex items-center justify-between px-3 py-3 hover:bg-[#171421]/30 transition-colors"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <div>
                      <div className="text-sm font-medium text-[#D0CFCC] truncate">{asset.symbol}</div>
                      <div className="text-[10px] text-[#7a7090]">
                        {asset.open_positions > 0 ? `${asset.open_positions} open` : 'No positions'}
                      </div>
                    </div>
                  </div>

                  <div className="flex items-center gap-1.5 shrink-0 ml-2">
                    {/* DEX toggle */}
                    {editable && (
                      <button
                        onClick={() => toggleVenue(asset.symbol, 'dex', asset.active_dex)}
                        disabled={isBusy}
                        className={`p-1.5 rounded-lg transition-colors disabled:opacity-40 ${
                          asset.active_dex
                            ? 'text-green-400 bg-green-500/10 hover:bg-green-500/20'
                            : 'text-[#4a4060] hover:text-[#D0CFCC] hover:bg-[#2a2240]'
                        }`}
                        title={asset.active_dex ? 'DEX active — click to deactivate' : 'DEX inactive — click to activate'}
                      >
                        {isBusy && actionLoading === `${asset.symbol}:dex` ? (
                          <Loader2 size={14} className="animate-spin" />
                        ) : asset.active_dex ? (
                          <Power size={14} />
                        ) : (
                          <PowerOff size={14} />
                        )}
                      </button>
                    )}
                    {/* CEX toggle */}
                    {editable && (
                      <button
                        onClick={() => toggleVenue(asset.symbol, 'cex', asset.active_cex)}
                        disabled={isBusy}
                        className={`p-1.5 rounded-lg transition-colors disabled:opacity-40 ${
                          asset.active_cex
                            ? 'text-green-400 bg-green-500/10 hover:bg-green-500/20'
                            : 'text-[#4a4060] hover:text-[#D0CFCC] hover:bg-[#2a2240]'
                        }`}
                        title={asset.active_cex ? 'CEX active — click to deactivate' : 'CEX inactive — click to activate'}
                      >
                        {isBusy && actionLoading === `${asset.symbol}:cex` ? (
                          <Loader2 size={14} className="animate-spin" />
                        ) : asset.active_cex ? (
                          <Power size={14} />
                        ) : (
                          <PowerOff size={14} />
                        )}
                      </button>
                    )}
                    {/* Remove */}
                    {editable && asset.open_positions === 0 && (
                      <button
                        onClick={() => removeAsset(asset.symbol)}
                        disabled={isBusy}
                        className="p-1.5 rounded-lg text-[#7a7090] hover:text-red-400 hover:bg-red-500/10 transition-colors disabled:opacity-40"
                        title="Remove asset"
                      >
                        {isBusy && actionLoading === asset.symbol ? (
                          <Loader2 size={14} className="animate-spin" />
                        ) : (
                          <Trash2 size={14} />
                        )}
                      </button>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Empty state when read-only */}
      {!editable && data.assets.length > 0 && (
        <p className="mt-3 text-[10px] text-[#4a4060] text-center">
          Open in Telegram to manage assets
        </p>
      )}
    </div>
  )
}
