# Feature Specification: Multi-Asset Trading with Per-Asset Capital and Independent CEX/DEX Activation

**Feature Branch**: `002-multi-asset-capital`

**Created**: 2026-07-27

**Status**: Draft

**Input**: User description: "Move capital allocation from global settings to per-asset, add independent per-venue (CEX/DEX) activation flags per asset, support multiple assets trading simultaneously, and provide full UI parity for asset management across Telegram and Mini App."

## Clarifications

### Session 2026-07-27

- Q: What should the default concurrency limits be for max concurrent open positions (FR-017) and max active (asset, venue) pairs (FR-018)? → A: 9 positions total across all pairs, 6 max active pairs (3 assets × 2 venues) — matches current `max_slots` default, operator can adjust upward after validating multi-asset behavior.
- Q: Should there be a global portfolio-level kill switch in addition to per-pair daily loss limits (FR-008)? → A: Yes — a single `global_daily_loss_limit` (USD or %) that sums PnL across all pairs; if breached, all trading stops for the day.
- Q: What happens when a trader attempts to remove an asset that has open positions? → A: Block removal with a message indicating the number of open positions. The trader must deactivate the asset first (stop new entries), wait for positions to close, then remove.
- Q: What happens when the exchange balance query fails during add/edit validation (FR-010)? → A: Block save with "Cannot verify balance — save blocked." Offer a "Force save (skip balance check)" option that logs the override prominently. Follows Constitution IV (fail closed) while allowing emergency configuration changes.
- Q: Does asset evaluation order matter when multiple pairs could enter in the same cycle? → A: No — each pair uses its own dedicated capital allocation (absolute USD). The save-time overallocation check ensures total allocation ≤ balance. No capacity competition at cycle time.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Configure a New Asset with Own Capital and Venue Activation (Priority: P1)

A trader wants to add ETH to the bot with $5,000 allocated to Binance spot (CEX) and $3,000 to Orderly futures (DEX), with only CEX trading active initially. The trader opens the asset management interface (Telegram or Mini App), fills in the asset symbol, capital amounts per venue, and toggles the activation flags, then saves. The asset appears in the active list and the bot begins evaluating ETH on Binance in the next cycle using exactly $5,000 as its position-sizing basis, while DEX remains inactive until the trader enables it.

**Why this priority**: This is the core capability — without per-asset capital and venue activation, no other feature has meaning. It replaces the current global-capital model and is the foundation for multi-asset trading.

**Independent Test**: Can be fully tested by adding a single asset via the UI, verifying it appears in the bot cycle logs with the correct capital allocation and only the active venue(s) evaluated. Delivers immediate value: one asset with its own capital, independently controlled.

**Acceptance Scenarios**:

1. **Given** no assets are configured, **When** the trader adds BTC with capital_dex=$2,000, capital_cex=$5,000, active_dex=true, active_cex=true, **Then** the asset is saved and the bot evaluates BTC on both venues in the next cycle, using $2,000 for DEX position sizing and $5,000 for CEX position sizing.
2. **Given** an asset exists with active_dex=true, **When** the trader edits the asset and sets active_dex=false, **Then** the bot stops evaluating that asset on DEX in the next cycle but continues evaluating other active (asset, venue) pairs.
3. **Given** the trader is filling the add-asset form, **When** they omit the symbol field and attempt to save, **Then** the save is rejected with a clear validation message indicating symbol is required.
4. **Given** the trader is filling the add-asset form, **When** they enter capital values that cause total allocated CEX capital across all assets to exceed the Binance account balance, **Then** the save is hard-blocked with a message showing the overage amount and the available balance.

---

### User Story 2 - Run Multiple Assets Simultaneously on Both Venues (Priority: P1)

A trader has BTC, ETH, and SOL configured, each with CEX and DEX capital and both venues active. The bot loop iterates over all six (asset, venue) pairs each cycle, evaluating each independently for mean-reversion entry signals. BTC might enter a DEX short while ETH enters a CEX long in the same cycle. Each position uses only its own asset's allocated capital for sizing. Cooldowns, regime state, and entry blockers are scoped per (asset, venue) pair so a losing streak on BTC/DEX does not prevent ETH/CEX from trading.

**Why this priority**: Multi-asset simultaneous trading is the primary value proposition — it increases trade frequency (Constitution Principle VIII) and diversifies opportunity capture.

**Independent Test**: Configure three assets, each with both venues active and non-zero capital. Run the bot and observe the logs: each cycle must evaluate all six (asset, venue) pairs. Verify that an entry on one pair does not block entries on other pairs.

**Acceptance Scenarios**:

1. **Given** BTC and ETH are configured with both venues active, **When** the bot cycle runs, **Then** the logs show sequential evaluation of (BTC, CEX), (BTC, DEX), (ETH, CEX), (ETH, DEX) with independent position sizing per pair.
2. **Given** BTC/DEX has hit its daily loss limit, **When** the bot cycle runs, **Then** BTC/DEX is skipped for the cycle but ETH/CEX, ETH/DEX, and BTC/CEX continue to be evaluated normally.
3. **Given** ETH/CEX has an open position, **When** the bot cycle runs, **Then** ETH/CEX is evaluated for exit/management but not for new entry, while ETH/DEX (no open position) is evaluated for new entry independently.

---

### User Story 3 - Manage Assets with Full Parity Across Telegram and Mini App (Priority: P2)

A trader uses both the Telegram bot and the Mini App dashboard interchangeably. They add an asset via the Mini App, then later edit its DEX capital via Telegram. Both interfaces show the same asset list with current capital, venue activation status, open position count, and allocated-vs-available balance summary. Removing an asset from either interface reflects immediately in the other.

**Why this priority**: Interface parity ensures the trader is not forced into one UI. It's P2 because the core trading logic (P1 stories) can function with a single UI if needed.

**Independent Test**: Add an asset via Telegram. Open the Mini App and verify the asset appears with identical data. Edit the asset's capital via the Mini App. Return to Telegram and verify the change is reflected. Remove the asset via Telegram and confirm it disappears from the Mini App.

**Acceptance Scenarios**:

1. **Given** the trader has the Mini App open to the asset list, **When** they add an asset via Telegram, **Then** the Mini App reflects the new asset on next data refresh without requiring a page reload.
2. **Given** an asset exists with CEX capital of $5,000, **When** the trader edits it to $7,000 via Telegram, **Then** the Mini App shows $7,000 on next refresh and the bot uses $7,000 for CEX position sizing in the next cycle.
3. **Given** the Mini App displays the asset management form, **When** the trader attempts to add an asset that would exceed available CEX balance, **Then** the same hard-block validation message appears as in Telegram.

---

### User Story 4 - Migrate from Legacy Global Capital to Per-Asset Model (Priority: P2)

A trader upgrades the bot from the previous version where a single global `dex_slot_pct` and `cex_slot_pct` controlled all position sizing. On first startup after upgrade, the bot detects the legacy settings, migrates the existing comma-separated asset list: the previously "active" asset receives the global capital values converted to absolute USD (percentage × equity), its venue flags are set to the current global booleans, and all other assets become inactive with zero capital. Historical trades and open positions are preserved unchanged. A migration summary is logged and sent via Telegram.

**Why this priority**: Migration is essential for existing users but is a one-time operation. It's P2 because new users (P1 stories) are not affected.

**Independent Test**: Set up the bot with legacy global settings (assets=ETH,BTC, dex_slot_pct=10, cex_slot_pct=20, auto_trade_binance=true, auto_trade_orderly=false, equity=$50,000). Start the bot. Verify ETH receives capital_dex=$5,000, capital_cex=$10,000, active_dex=false, active_cex=true; BTC receives capital_dex=$0, capital_cex=$0, active_dex=false, active_cex=false. Verify existing closed_trades and open_positions rows are untouched.

**Acceptance Scenarios**:

1. **Given** legacy `dex_slot_pct` and `cex_slot_pct` keys exist in settings, **When** the bot starts, **Then** those keys are removed from settings, per-asset capital rows are created, and a migration summary is emitted.
2. **Given** a migration has already been performed (legacy keys absent, per-asset rows present), **When** the bot starts, **Then** no migration runs and the bot proceeds directly to the main loop.
3. **Given** an open position exists for ETH/DEX at migration time, **When** migration completes, **Then** the open position is preserved and associated with the migrated ETH asset; the position continues to be managed normally.

---

### User Story 5 - Monitor Capital Allocation and Guardrails (Priority: P3)

A trader wants visibility into how their capital is deployed across assets and venues. The asset management interface shows per-venue totals: total CEX capital allocated across all assets, total DEX capital allocated, remaining unallocated balance per venue, and the count of active (asset, venue) pairs. The trader can see at a glance whether they are under-allocated or at the hard cap.

**Why this priority**: Monitoring and guardrails improve operational safety but the bot can trade without them. P3 because it enhances the P1/P2 workflows rather than enabling them.

**Independent Test**: Configure three assets with varying capital. Open the asset list view and verify the summary row displays correct totals and remaining balance. Add capital to an asset that would exceed available balance — the save is blocked, and the remaining balance display updates only on valid saves.

**Acceptance Scenarios**:

1. **Given** Binance balance is $20,000 and total allocated CEX capital is $15,000 across three assets, **When** the trader views the asset list, **Then** the summary shows "CEX: $15,000 allocated / $5,000 remaining (3 assets)".
2. **Given** total allocated DEX capital is $0 with no DEX-active assets, **When** the trader views the asset list, **Then** the summary shows "DEX: $0 allocated / $X remaining (0 active pairs)".

---

### Edge Cases

- **Deactivating an asset with an open position**: New entries are blocked for that (asset, venue) pair immediately. Existing positions continue to be managed (trailing stop, take-profit, stop-loss) until they exit naturally. The UI clearly indicates "position open — deactivation pending exit."
- **Changing capital while a position is open**: The new capital value applies to the next new entry only. The open position continues to use the capital value that was active when it was entered. No position is forcibly closed due to a capital change.
- **Asset with zero capital on a venue**: The (asset, venue) pair is skipped in the bot cycle — no entry evaluation, no position sizing. The asset row remains in the list. This is valid for "CEX only" or "DEX only" configurations.
- **All assets deactivated on a venue**: The bot logs "no active pairs for [venue]" at DEBUG level and skips venue-specific exchange queries, avoiding unnecessary API calls.
- **Maximum concurrent open positions reached**: When the configurable limit is hit, no new entries are opened on any asset/venue. The bot logs the limit-hit reason and continues managing existing positions. As positions close, new entries resume.
- **Rate limit on exchange API**: If market-data calls exceed rate limits during multi-asset iteration, the bot staggers or batches calls within the cycle. Rate-limit errors are logged and trigger exponential backoff per venue.
- **ML signal gate for asset with insufficient training data**: If a strategy requires ML signal confirmation and the asset lacks the minimum signal_history rows, that (asset, venue) pair is skipped with an explicit reason logged. The UI shows "ML gate: insufficient data" for that pair.
- **Restart with multiple assets**: On startup, the bot queries exchange positions for all active (asset, venue) pairs, reconciles with the local `open_positions` table, re-attaches missing stops, and closes orphaned DB records — per Constitution Principle VI, applied across all pairs independently.
- **Duplicate asset symbol**: Adding an asset with a symbol that already exists is rejected with a message indicating the duplicate.
- **Removing an asset with open positions**: Removal is blocked with a message indicating the number of open positions and instructing the trader to deactivate the asset first (which stops new entries), wait for positions to close, then remove.
- **Empty asset list**: The bot starts, logs "no assets configured" at INFO level, and enters a waiting state without error. It re-checks settings each cycle so adding an asset resumes trading without restart.

## Requirements *(mandatory)*

### Functional Requirements

**Asset Data Model**

- **FR-001**: The system MUST store per-asset capital as absolute USD amounts: `capital_dex` and `capital_cex`, replacing global percentage-based `dex_slot_pct` and `cex_slot_pct`.
- **FR-002**: The system MUST store two independent boolean activation flags per asset: `active_dex` and `active_cex`, replacing the global `auto_trade_binance` and `auto_trade_orderly` booleans.
- **FR-003**: The system MUST remove the legacy `capital`, `dex_slot_pct`, `cex_slot_pct`, `max_slots`, `auto_trade_binance`, and `auto_trade_orderly` keys from the settings model and all interfaces. These are replaced by `asset_configs` fields and the new `max_concurrent_positions` / `max_active_pairs` settings.
- **FR-004**: The system MUST support the concept of a single active asset list where each asset has its own capital and venue flags — no concept of a "primary" or "only" active asset.

**Bot Loop**

- **FR-005**: The bot loop MUST iterate over all (asset, venue) pairs where the asset has `active_<venue>=true` and `capital_<venue> > 0`, evaluating each independently.
- **FR-006**: Each (asset, venue) pair MUST use its own `capital_<venue>` value for position sizing via `compute_slot_size`.
- **FR-007**: Per-asset state (price memory, cooldowns, last entry time, regime) MUST be scoped to the (asset, venue) pair with no cross-asset or cross-venue leakage.
- **FR-008**: Daily loss limits and consecutive loss tracking MUST apply per (asset, venue) pair. Additionally, a global `global_daily_loss_limit` (configurable as absolute USD or percentage of total portfolio equity) MUST sum PnL across all pairs; if breached, all trading stops for the day.

**Asset Management Interfaces**

- **FR-009**: Both Telegram and Mini App MUST provide: add asset (with symbol, capital_dex, capital_cex, active_dex, active_cex fields), edit asset (same fields), list assets (with capital, activation status, open position count), and remove asset.
- **FR-010**: The add/edit asset form MUST validate that total allocated capital per venue across all assets does not exceed the available account balance for that venue, and MUST hard-block the save with a clear message when exceeded. If the exchange balance query fails, the save MUST be blocked with "Cannot verify balance" but MUST offer a force-save override that logs the override prominently.
- **FR-011**: The asset list view MUST display per-venue summary: total allocated capital, active (asset, venue) pair count, and remaining unallocated balance.
- **FR-012**: The system MUST display an asset's open-position state clearly when the asset is deactivated (showing "deactivation pending exit") and MUST block new entries for deactivated (asset, venue) pairs while continuing to manage existing positions.
- **FR-012a**: The system MUST block removal of an asset that has open positions on either venue. The rejection message MUST state the number of open positions and instruct the trader to deactivate the asset first, then remove once positions close.

**Migration**

- **FR-013**: On startup, the system MUST detect legacy global capital settings (`dex_slot_pct`, `cex_slot_pct`) and, if present, migrate them to per-asset capital using the formula: `capital = slot_pct / 100 * equity`, assigning the full values to the first asset in the comma-separated list (the previously "primary" active asset).
- **FR-014**: Migration MUST set venue activation flags per migrated asset to the values of the legacy `auto_trade_binance` and `auto_trade_orderly` booleans; non-migrated assets receive `active_dex=false`, `active_cex=false` and zero capital.
- **FR-015**: Migration MUST remove the legacy keys from settings after successful migration and log a summary (asset count, capital assigned, flag state).
- **FR-016**: Migration MUST NOT alter or delete any rows in `open_positions`, `closed_trades`, or `signals` tables.

**Guardrails**

- **FR-017**: The system MUST support a configurable maximum number of concurrent open positions across all (asset, venue) pairs, defaulting to 9 (matching the current `max_slots` default).
- **FR-018**: The system MUST support a configurable maximum number of concurrently active (asset, venue) pairs, defaulting to 6 (equivalent to 3 assets × 2 venues).
- **FR-019**: DEX capital pool MUST be scoped per-asset. In v1, the operator manually adjusts `capital_dex` to rebalance; automatic compounding from realized DEX PnL is deferred to a future amendment. CEX `capital_cex` is fixed allocation and does not compound.

**Constitution Compliance**

- **FR-020**: All per-asset state queries (position count, equity, order status, exchange connectivity) MUST fail closed independently — a query failure for one (asset, venue) pair MUST NOT block trading on other pairs (Constitution IV).
- **FR-021**: On restart, the system MUST reconcile exchange state for ALL active (asset, venue) pairs independently, re-attaching stops and closing orphaned records per pair (Constitution VI).
- **FR-022**: Multi-asset iteration MUST increase aggregate trade frequency relative to single-asset operation (Constitution VIII).

### Key Entities

- **Asset Configuration**: Represents a tradable symbol with per-venue parameters. Key attributes: symbol, capital_dex (USD), capital_cex (USD), active_dex (boolean), active_cex (boolean). Replaces the legacy comma-separated `assets` string and global capital/activation keys.
- **Venue Pair**: The combination of an asset and a trading venue (CEX or DEX). This is the unit of iteration in the bot loop. Each pair has its own state (price memory, cooldowns, regime, PnL tracking) derived from its parent Asset Configuration.
- **Allocation Summary**: A computed view showing per-venue totals: sum of allocated capital, count of active pairs, remaining unallocated balance. Not persisted — derived from Asset Configurations and exchange balance queries.
- **Migration Record**: A one-time transformation that converts legacy global settings to per-asset rows. Not a persisted entity — exists as a startup procedure with logged output.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A trader can configure a new asset with per-venue capital and activation flags in under 30 seconds from either Telegram or the Mini App.
- **SC-002**: The bot evaluates all active (asset, venue) pairs in a single cycle without cross-pair state interference — verified by a test case with three assets × two venues (six pairs) where a losing streak on one pair does not affect entry decisions on the other five.
- **SC-003**: Total trade frequency (entries per day across all pairs) is at least 2× the frequency of single-asset operation with the same strategy parameters, assuming at least three assets with non-zero capital and both venues active.
- **SC-004**: Migration from legacy global settings to per-asset model completes in a single startup with zero data loss — all historical trades, open positions, and signals are preserved and remain queryable.
- **SC-005**: A capital overallocation attempt is blocked at save time with a message that identifies the specific venue, the overage amount, and the available balance — the trader does not need to consult logs or docs to understand the rejection.
- **SC-006**: Deactivating an asset with an open position does not cause forced liquidation; the position exits naturally via its existing take-profit or stop-loss, and the UI reflects the "pending exit" state within one cycle.

## Assumptions

1. **Account balance per venue is queryable**: The bot can obtain the available balance (equity) for Binance and Orderly at any point in the cycle. If balance queries fail, the fail-closed principle (Constitution IV) applies and new entries on that venue are skipped for the cycle.
2. **Asset symbols are mechanically derivable**: The existing symbol-derivation rules (PERP_{ASSET}_USDC for Orderly, {ASSET}USDT for Binance) remain valid. No asset requires a custom symbol mapping.
3. **Settings storage model supports structured data**: The current key-value settings table will accommodate per-asset structured rows. The exact schema mechanism (separate table, JSON column, or key convention) is an implementation detail.
4. **Existing per-asset state dicts scale**: The module-level dictionaries in spot_scalper and futures_scalper (keyed by asset) will handle multiple assets without performance degradation under the target of ~10 assets.
5. **Rate limits accommodate multi-asset iteration**: Exchange API rate limits are sufficient for iterating ~10 assets × 2 venues with the existing per-cycle call pattern. If limits are hit, batching/staggering is acceptable per the rate-limit edge case.
6. **Telegram and Mini App share a common backend**: Both UIs read/write the same settings store, so changes from one are immediately visible to the other on refresh. No real-time push synchronization is required.
7. **The ML signal gate threshold is configurable or has a documented minimum**: The minimum number of signal_history rows required for ML-gated strategies is known and configurable, defaulting to a value that ensures statistical relevance (assumed: ≥50 rows).
8. **Migration runs exactly once**: The presence of any legacy key (`dex_slot_pct`, `cex_slot_pct`) triggers migration. After migration, those keys are deleted. Subsequent restarts find no legacy keys and skip migration.
9. **Asset evaluation order is irrelevant**: Each (asset, venue) pair uses its own dedicated absolute capital allocation for position sizing — not a shared pool. There is no capacity competition at cycle time because the save-time overallocation check guarantees the sum of all allocations fits within available balance. The bot may iterate pairs in any order.
