// MockbaV4 — Client-side settings validation (Amendment 002)
// Pure TypeScript, no network calls. Mirrors trade/settings_rules.py logic.

export interface Verdict {
  level: "ok" | "warn" | "error"
  message: string
  suggested?: string  // suggested value to fix the issue
}

interface Spec {
  key: string
  type: "bool" | "int" | "float" | "str"
  hardMin?: number
  hardMax?: number
  softMin?: number
  softMax?: number
  unit?: string
}

// Mirror of Python SettingSpec entries (dashboard-visible subset)
const SPECS: Record<string, Spec> = {
  tp_min_pct: { key: "tp_min_pct", type: "float", hardMin: 0.1, hardMax: 10, softMin: 0.3, softMax: 3, unit: "%" },
  sl_min_pct: { key: "sl_min_pct", type: "float", hardMin: 0.1, hardMax: 10, softMin: 0.3, softMax: 3, unit: "%" },
  dip_min_pct: { key: "dip_min_pct", type: "float", hardMin: 0.05, hardMax: 5, softMin: 0.1, softMax: 1, unit: "%" },
  pump_min_pct: { key: "pump_min_pct", type: "float", hardMin: 0.05, hardMax: 5, softMin: 0.1, softMax: 1, unit: "%" },
  cooldown_sec: { key: "cooldown_sec", type: "int", hardMin: 10, hardMax: 3600, softMin: 30, softMax: 600, unit: "sec" },
  max_slots: { key: "max_slots", type: "int", hardMin: 1, hardMax: 50, softMin: 1, softMax: 20 },
  min_entry_spacing_pct: { key: "min_entry_spacing_pct", type: "float", hardMin: 0.05, hardMax: 5, softMin: 0.1, softMax: 2, unit: "%" },
  daily_loss_limit_pct: { key: "daily_loss_limit_pct", type: "float", hardMin: 0, hardMax: 100, softMin: 0, softMax: 20, unit: "%" },
  max_consecutive_losses: { key: "max_consecutive_losses", type: "int", hardMin: 0, hardMax: 50, softMin: 0, softMax: 10 },
  leverage: { key: "leverage", type: "int", hardMin: 1, hardMax: 10, softMin: 1, softMax: 5, unit: "x" },
  max_leverage: { key: "max_leverage", type: "int", hardMin: 1, hardMax: 10, softMin: 2, softMax: 5, unit: "x" },
  dex_slot_pct: { key: "dex_slot_pct", type: "float", hardMin: 1, hardMax: 100, softMin: 5, softMax: 50, unit: "%" },
  cex_slot_pct: { key: "cex_slot_pct", type: "float", hardMin: 1, hardMax: 100, softMin: 5, softMax: 50, unit: "%" },
  tp_k: { key: "tp_k", type: "float", hardMin: 0.1, hardMax: 5, softMin: 0.5, softMax: 2 },
  sl_k: { key: "sl_k", type: "float", hardMin: 0.1, hardMax: 5, softMin: 0.3, softMax: 2 },
  dip_k: { key: "dip_k", type: "float", hardMin: 0.1, hardMax: 5, softMin: 0.3, softMax: 2 },
  pump_k: { key: "pump_k", type: "float", hardMin: 0.1, hardMax: 5, softMin: 0.3, softMax: 2 },
  dex_round_trip_fee_pct: { key: "dex_round_trip_fee_pct", type: "float", hardMin: 0, hardMax: 5, softMin: 0.03, softMax: 1, unit: "%" },
  cex_round_trip_fee_pct: { key: "cex_round_trip_fee_pct", type: "float", hardMin: 0, hardMax: 5, softMin: 0.1, softMax: 1, unit: "%" },
  assumed_slippage_pct: { key: "assumed_slippage_pct", type: "float", hardMin: 0, hardMax: 5, softMin: 0.01, softMax: 1, unit: "%" },
  min_net_edge_pct: { key: "min_net_edge_pct", type: "float", hardMin: 0.01, hardMax: 5, softMin: 0.1, softMax: 1, unit: "%" },
  max_hold_minutes_spot: { key: "max_hold_minutes_spot", type: "int", hardMin: 5, hardMax: 1440, softMin: 30, softMax: 480, unit: "min" },
  max_hold_minutes_futures: { key: "max_hold_minutes_futures", type: "int", hardMin: 5, hardMax: 1440, softMin: 60, softMax: 720, unit: "min" },
  atr_period: { key: "atr_period", type: "int", hardMin: 5, hardMax: 50, softMin: 10, softMax: 30, unit: "candles" },
}

function coerce(value: string, spec: Spec): number | string | boolean | null {
  if (spec.type === "bool") {
    const v = value.toLowerCase().trim()
    if (v === "true" || v === "1" || v === "yes") return true
    if (v === "false" || v === "0" || v === "no") return false
    return null
  }
  if (spec.type === "int") {
    const n = parseInt(value, 10)
    return isNaN(n) ? null : n
  }
  if (spec.type === "float") {
    const n = parseFloat(value)
    return isNaN(n) ? null : n
  }
  return value
}

export function validateSetting(key: string, value: string, all: Record<string, string>): Verdict {
  const spec = SPECS[key]
  if (!spec) return { level: "ok", message: "" }

  const v = coerce(value, spec)
  if (v === null) return { level: "error", message: `Invalid type: expected ${spec.type}` }

  if (typeof v === "number") {
    if (spec.hardMin !== undefined && v < spec.hardMin)
      return { level: "error", message: `${key} = ${v} below minimum ${spec.hardMin}` }
    if (spec.hardMax !== undefined && v > spec.hardMax)
      return { level: "error", message: `${key} = ${v} above maximum ${spec.hardMax}` }
    if (spec.softMin !== undefined && v < spec.softMin)
      return { level: "warn", message: `${key} = ${v} below recommended ${spec.softMin}` }
    if (spec.softMax !== undefined && v > spec.softMax)
      return { level: "warn", message: `${key} = ${v} above recommended ${spec.softMax}` }
  }

  // Cross-setting checks
  const tp = parseFloat(all.tp_min_pct || "0.8")
  const sl = parseFloat(all.sl_min_pct || "0.5")
  const lev = parseInt(all.leverage || "3")
  const maxLev = parseInt(all.max_leverage || "3")
  const fee = parseFloat(all.dex_round_trip_fee_pct || "0.06")
  const slip = parseFloat(all.assumed_slippage_pct || "0.03")
  const minEdge = parseFloat(all.min_net_edge_pct || "0.3")
  const slots = parseInt(all.max_slots || "9")
  const slotPct = parseFloat(all.dex_slot_pct || "15")

  if (key === "tp_min_pct" && typeof v === "number" && v <= sl)
    return { level: "error", message: `TP (${v}%) must exceed SL (${sl}%) — risk > reward`, suggested: (sl + 0.3).toFixed(1) }
  if (key === "sl_min_pct" && typeof v === "number" && tp <= v)
    return { level: "error", message: `SL (${v}%) must be below TP (${tp}%)`, suggested: (tp - 0.3).toFixed(1) }
  if (key === "leverage" && typeof v === "number" && v > maxLev)
    return { level: "error", message: `Leverage (${v}x) exceeds max (${maxLev}x)`, suggested: String(maxLev) }
  if (key === "max_slots" && typeof v === "number" && v * slotPct > 100)
    return { level: "error", message: `${v} slots × ${slotPct}% = ${v * slotPct}% of equity` }
  if ((key === "tp_min_pct" || key === "assumed_slippage_pct" || key === "min_net_edge_pct") && tp - fee - slip < minEdge) {
    const net = tp - fee - slip
    const sugTP = (minEdge + fee + slip + 0.05).toFixed(1)
    return { level: "error", message: `Net edge ${net.toFixed(2)}% below min ${minEdge}% (TP ${tp}% − fee ${fee}% − slip ${slip}%)`, suggested: sugTP }
  }

  return { level: "ok", message: "" }
}

export function validateAll(settings: Record<string, string>): Record<string, Verdict> {
  const result: Record<string, Verdict> = {}
  for (const key of Object.keys(settings)) {
    if (SPECS[key]) {
      result[key] = validateSetting(key, settings[key] || "", settings)
    }
  }
  return result
}
