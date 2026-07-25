import React, { useState, useEffect, useCallback } from 'react'
import { Plus, Trash2, Check, AlertCircle, Loader2, Radio, Star } from 'lucide-react'
import { TG, isTelegram } from './TelegramProvider'

interface AssetData {
  assets: string[]
  current_asset: string
}

export default function AssetManager() {
  const [data, setData] = useState<AssetData>({ assets: [], current_asset: '' })
  const [newAsset, setNewAsset] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [actionLoading, setActionLoading] = useState<string | null>(null) // asset name being acted on
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
      setData({ assets: json.assets || [], current_asset: json.current_asset || '' })
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
    // Already handled by MiniSettings shared logic — just inherit
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
    const asset = newAsset.trim()
    if (!asset) return
    setActionLoading(asset)
    setError(null)
    try {
      const res = await fetch('/api/assets', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ asset }),
      })
      if (!res.ok) throw new Error(await res.text())
      const json = await res.json()
      setData({ assets: json.assets || [], current_asset: json.current_asset || data.current_asset })
      setNewAsset('')
    } catch (e: any) {
      setError(e.message || 'Failed to add asset')
    } finally {
      setActionLoading(null)
    }
  }

  const removeAsset = async (asset: string) => {
    setActionLoading(asset)
    setError(null)
    try {
      const res = await fetch(`/api/assets/${encodeURIComponent(asset)}`, {
        method: 'DELETE',
        headers: authHeaders(),
      })
      if (!res.ok) throw new Error(await res.text())
      const json = await res.json()
      setData({ assets: json.assets || [], current_asset: db_get_setting_fallback(json) })
    } catch (e: any) {
      setError(e.message || 'Failed to remove asset')
    } finally {
      setActionLoading(null)
    }
  }

  const selectAsset = async (asset: string) => {
    if (asset === data.current_asset) return
    setActionLoading(asset)
    setError(null)
    try {
      const res = await fetch('/api/assets/select', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ asset }),
      })
      if (!res.ok) throw new Error(await res.text())
      const json = await res.json()
      setData(prev => ({ ...prev, current_asset: json.current_asset || asset }))
    } catch (e: any) {
      setError(e.message || 'Failed to select asset')
    } finally {
      setActionLoading(null)
    }
  }

  function db_get_setting_fallback(json: any): string {
    // After removal, the API may update current_asset; reflect it
    return json?.current_asset ?? data.current_asset
  }

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center text-[#4a4060]">
        <Loader2 className="animate-spin mr-2" size={20} />
        Loading assets…
      </div>
    )
  }

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
          {editable ? `${data.assets.length} assets` : `${data.assets.length} assets`}
        </span>
      </div>

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
              const isActive = asset === data.current_asset
              const isBusy = actionLoading === asset
              return (
                <div
                  key={asset}
                  className={`flex items-center justify-between px-3 py-3 transition-colors ${
                    isActive ? 'bg-[#D0CFCC]/5' : 'hover:bg-[#171421]/30'
                  }`}
                >
                  <div className="flex items-center gap-2 min-w-0">
                    {isActive && <Star size={14} className="text-yellow-500 shrink-0" />}
                    <div>
                      <div className={`text-sm font-medium truncate ${isActive ? 'text-[#D0CFCC]' : 'text-[#D0CFCC]'}`}>
                        {asset}
                      </div>
                      {isActive && (
                        <div className="text-[10px] text-yellow-500/80">Active</div>
                      )}
                    </div>
                  </div>

                  <div className="flex items-center gap-1.5 shrink-0 ml-2">
                    {editable && !isActive && (
                      <button
                        onClick={() => selectAsset(asset)}
                        disabled={isBusy}
                        className="p-1.5 rounded-lg text-[#7a7090] hover:text-[#D0CFCC] hover:bg-[#2a2240] transition-colors disabled:opacity-40"
                        title="Set as active"
                      >
                        {isBusy ? <Loader2 size={14} className="animate-spin" /> : <Radio size={14} />}
                      </button>
                    )}
                    {isActive && (
                      <span className="p-1.5 text-green-600">
                        <Check size={14} />
                      </span>
                    )}
                    {editable && (
                      <button
                        onClick={() => removeAsset(asset)}
                        disabled={isBusy}
                        className="p-1.5 rounded-lg text-[#7a7090] hover:text-red-400 hover:bg-red-500/10 transition-colors disabled:opacity-40"
                        title="Remove asset"
                      >
                        {isBusy ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}
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
