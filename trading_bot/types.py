"""
MockbaV4 — Shared type contracts.

Every module in the trading path depends on these types.
They are frozen dataclasses: immutable, hashable, no surprises at a distance.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SymbolFilters:
    """Exchange-native symbol metadata, cached per asset per session.

    All numeric fields come from the exchange's own filters endpoint.
    Never hardcode these — fetch them once, validate them, freeze them.
    """
    symbol: str            # venue-native: "NEARUSDT" (Binance) or "PERP_NEAR_USDC" (Orderly)
    base_tick: float       # quantity step size (lot size / step size)
    quote_tick: float      # price tick size
    min_qty: float         # minimum order quantity in base units
    min_notional: float    # minimum order value in quote currency


@dataclass(frozen=True)
class Fill:
    """The result of a filled order, exactly as the exchange reported it.

    Constitution principle V: every PnL number derives from these fields.
    Never substitute the signal price or an assumed fee rate.
    """
    filled_qty: float       # actual quantity filled, from the exchange
    fill_price: float       # actual average fill price, never the signal price
    fee_amount: float       # actual fee charged
    fee_asset: str          # currency the fee was taken in (USDT, USDC, BNB, NEAR…)
    sellable_qty: float     # filled_qty minus base-asset fee, floored to base_tick
    order_id: str           # exchange-assigned order ID
    client_order_id: str    # our idempotency key, derived from position ID
    raw: dict               # full exchange response payload, preserved for logging


@dataclass(frozen=True)
class Position:
    """An open trading position, persisted in the open_positions table.

    Restart-safe: on startup, reconcile these against the exchange's actual
    positions before evaluating any entries.
    """
    id: str                          # UUID, generated at entry time
    asset: str                       # base asset: "NEAR", "ETH", "SOL"
    venue: str                       # "binance" | "orderly"
    side: str                        # "long" | "short"
    qty: float                       # position size in base units
    entry_price: float               # actual fill price from Fill.fill_price
    signal_price: float              # price that triggered the entry — slippage = entry_price - signal_price
    tp_price: float                  # take-profit trigger price
    sl_price: Optional[float]        # stop-loss trigger price; None for spot
    tp_order_id: Optional[str]       # exchange-assigned TP order ID
    sl_order_id: Optional[str]       # exchange-assigned SL order ID; None for spot
    opened_at: float                 # UNIX timestamp of entry fill


# ── Direction helpers (used by scalpers and regime gating) ────────────────────

VALID_VENUES = frozenset({"binance", "orderly"})
VALID_SIDES = frozenset({"long", "short"})


def opposite_side(side: str) -> str:
    """Return the opposing direction."""
    if side == "long":
        return "short"
    if side == "short":
        return "long"
    raise ValueError(f"Invalid side: {side}")


def side_to_buy_sell(side: str) -> str:
    """Map internal side to exchange-native order side."""
    return "BUY" if side == "long" else "SELL"
