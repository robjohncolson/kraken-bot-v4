# Post-Mortem: >20% Portfolio Decline — Recovery Plan

**Date**: 2026-04-05
**Author**: CC (Claude Code) post-mortem analysis
**For**: Codex implementation

## Context

The bot's portfolio declined from ~$500 to ~$370 (~26% loss). Investigation reveals a devastating root cause: **the bot never completed a single trade**. Zero fills recorded, zero ledger entries, zero closed rotation nodes. 82 orders were placed on Kraken but none were ever settled in the database.

The entire loss is a **holding loss** — the bot rotated USD into various altcoins (ATOM, BTC, ETH, KSM, LINK, RAVE, SOL, UNITAS, XRP) via orders that may have filled on Kraken but whose fills were never recorded by the bot. Those assets then declined with the market while the bot had no stop losses, no take profits, and no active management.

### Why Fills Were Never Recorded

1. **Order status never transitions in DB**: `persistence/sqlite.py` has `close_order()` (sets status='filled') but **no `cancel_order()` method**. When orders are cancelled via fill timeout (`runtime_loop.py:1225-1247`), the DB is never updated. All 82 orders sit as `status='open'` forever.

2. **Child nodes lost on restart**: `runtime_loop.py:283-331` — `initialize_roots()` only creates root nodes from balances. The merge loop only iterates root nodes. Persisted child nodes (PLANNED, OPEN, CLOSING) are **never rehydrated** from the DB. Any in-flight child is permanently orphaned on restart.

3. **Nonce corruption**: Previous sessions had a separate script calling Kraken API, corrupting the nonce. This prevented all subsequent API calls — fills happened but the bot couldn't detect them.

### Current Portfolio State (from SQLite, 2026-04-05)

| Status | Nodes | Entry Cost | Assets |
|--------|-------|-----------|--------|
| CLOSING | 9 | $214 | ATOM, BTC, ETH, KSM, LINK, RAVE, SOL, UNITAS, XRP — exit orders placed but unfilled |
| OPEN | 6 | $125 | USD ($78), EUR ($22), USDT ($17), TON ($17), GBP ($11), USDC ($10) |

Zero child nodes. Zero closed nodes. Zero ledger entries. 82 orders all `status='open'` in DB.

---

## Phase 0: Emergency Triage (manual, immediate)

**Goal**: Stop bleeding, cancel orphaned orders, establish clean baseline.

### 0.1 Enable safe mode
- **File**: `.env` lines 9-11
- Set `READ_ONLY_EXCHANGE=true` and `DISABLE_ORDER_MUTATIONS=true`
- Restart bot to confirm safe mode active in logs

### 0.2 Cancel all orphaned Kraken orders
- Call Kraken `GET /0/private/OpenOrders` → cancel each via `POST /0/private/CancelOrder`
- One-off script using `exchange/executor.py` (`fetch_open_orders` line ~120, `execute_cancel` line ~184)
- Then: `UPDATE orders SET status = 'cancelled' WHERE status = 'open'` in bot.db

### 0.3 Clean up rotation tree DB
- Mark 9 CLOSING roots back to OPEN (their exit orders are orphaned)
- Verify root quantities match actual Kraken balances
- Remove any ghost child nodes

### 0.4 Consolidate to USD
- Sell all altcoin positions (ATOM, BTC, ETH, KSM, LINK, RAVE, SOL, UNITAS, XRP, TON) to USD
- Can be done manually on Kraken web or via one-off script
- This locks in losses but stops unmanaged exposure
- Keep EUR/GBP/USDT/USDC as-is (stablecoins/fiat, minimal risk)

---

## Phase 1: Fix Execution Layer (the critical fix)

**Goal**: Make orders actually complete: place -> fill -> settle -> close.

### 1.1 Add `cancel_order` to persistence
- **File**: `persistence/sqlite.py` — add method near line 529
- Add `cancel_order(order_id)` that sets `status='cancelled'`
- Call it everywhere orders are cancelled:
  - `runtime_loop.py:1225-1247` (entry timeout cancellation)
  - `runtime_loop.py:1261-1277` (exit timeout escalation — cancel old LIMIT before resubmitting as MARKET)
  - `runtime_loop.py:924-933` (insufficient funds cancellation)
  - `runtime_loop.py:1628-1650` (`_cancel_rotation_entry`)
- **Tests**: Verify that after any order cancellation path, `SELECT status FROM orders WHERE order_id=?` returns `'cancelled'`

### 1.2 Fix startup order reconciliation
- **File**: `runtime_loop.py:146-205` (`build_initial_scheduler_state`)
- After filtering pending orders against Kraken open orders (line 182-190), add reconciliation:
  1. Fetch all DB orders with `status='open'`
  2. Compare against Kraken's actual open orders
  3. For DB orders NOT found on Kraken:
     a. Check Kraken trade history (`executor.fetch_trade_history()`) for fills matching that order
     b. If filled: update DB to `'filled'`, insert ledger entry, queue rotation fill settlement
     c. If not filled (cancelled/expired by Kraken): update DB to `'cancelled'`, cancel associated rotation node
- **Key function**: `executor.fetch_trade_history()` returns `KrakenTrade` objects — cross-reference by `order_id`
- **Tests**: Start bot → place order → restart before fill → verify order reconciled correctly on startup

### 1.3 Fix child node rehydration on restart
- **File**: `runtime_loop.py:283-331`
- Currently `initialize_roots()` (line 289) only creates root nodes from balances
- The merge loop (lines 298-323) only iterates root nodes
- **Fix**: After root merge, also rehydrate persisted child nodes from DB:
  ```python
  for persisted_node in persisted.nodes:
      if persisted_node.depth > 0 and persisted_node.status in (OPEN, CLOSING, PLANNED):
          if node_by_id(self._rotation_tree, persisted_node.parent_node_id) is not None:
              self._rotation_tree = add_node(self._rotation_tree, persisted_node)
  ```
- Restore: TP/SL prices, entry_cost, fill_price, trailing_stop_high, exit_reason, etc.
- **Tests**: Start bot → let child reach OPEN → restart → verify child still present and being monitored

### 1.4 Add periodic order reconciliation
- **File**: `runtime_loop.py` — add to `run_once()` cycle
- Every `RECONCILE_INTERVAL_SEC` (300s), run the same reconciliation logic as 1.2:
  - Compare in-memory `pending_orders` against Kraken `fetch_open_orders()`
  - For any pending order whose `exchange_order_id` is NOT on Kraken:
    1. Check trade history for fills
    2. If filled: emit fill event, settle rotation
    3. If not filled: cancel rotation node, clean up pending order
- This is the safety net that catches fills missed by WebSocket AND REST poller
- **Tests**: Manually cancel an order on Kraken web → within one reconcile interval, bot detects and cleans up

### 1.5 REST fallback fill detection fix
- **File**: `exchange/websocket.py:128-221` (`FallbackPoller`)
- When poller detects order disappeared from Kraken open orders:
  - Currently emits fill with snapshot values (original order price/qty)
  - **Fix**: Cross-reference trade history for exact fill price/qty before emitting
  - Fall back to snapshot values only if trade history is unavailable
- **Tests**: Place limit order → disconnect WebSocket → let fill → verify poller reports exact fill price

---

## Phase 2: Fix Risk Management

**Goal**: Fix the math so trades can actually be profitable when execution works.

### 2.1 Fix TP/SL ratio and fee accounting
- **Files**: `runtime_loop.py:1014-1023`, `core/config.py:28-29`
- **Current math**:
  - TP trigger: `fill_price * (1 + (3.0 + 0.52) / 100)` = needs 3.52% move
  - SL trigger: `fill_price * (1 - 2.0 / 100)` = 2.0% trigger, but real loss = 2.0% + 0.40% taker fee = 2.4%
  - Effective R:R after fees: ~2.48% profit : ~2.40% loss = ~1.03:1
- **Fix**:
  - Change defaults in `core/config.py`:
    ```python
    DEFAULT_ROTATION_TAKE_PROFIT_PCT = 5.0  # was 3.0
    DEFAULT_ROTATION_STOP_LOSS_PCT = 2.5    # was 2.0
    ```
  - In `runtime_loop.py:1020`, adjust SL to account for exit fee:
    ```python
    # SL should represent NET loss tolerance, so tighten trigger to absorb exit fee
    exit_fee = self._settings.kraken_taker_fee_pct  # 0.40% for market SL
    sl_price = fill_price * (1 - Decimal(str((sl_pct - exit_fee) / 100)))
    ```
  - New effective R:R: ~4.48% net profit : ~2.50% net loss = ~1.8:1
- **Tests**: Unit test computing net P&L for TP and SL scenarios including fees

### 2.2 Activate trailing stops
- **File**: `runtime_loop.py:1122-1134`
- **Current**: `trailing_stop_high` tracked but `stop_loss_price` is fixed at entry fill value
- **Fix**: After updating `trailing_stop_high`, ratchet `stop_loss_price` upward:
  ```python
  # Only activate trailing after price exceeds activation threshold
  activation_price = node.fill_price * (1 + Decimal(str(activation_pct / 100)))
  if node.trailing_stop_high >= activation_price:
      trail_pct = Decimal(str(self._settings.rotation_stop_loss_pct / 100))
      new_sl = node.trailing_stop_high * (Decimal("1") - trail_pct)
      if new_sl > node.stop_loss_price:
          self._rotation_tree = update_node(
              self._rotation_tree, node.node_id,
              stop_loss_price=new_sl,
          )
  ```
- **New config** in `core/config.py` and `.env`:
  - `ROTATION_TRAILING_STOP_ENABLE=true` (default: true)
  - `ROTATION_TRAILING_STOP_ACTIVATION_PCT=1.5` (trail engages after 1.5% above entry)
- **Tests**: Simulate price: entry $100 → up to $105 → trail activates → SL ratchets to $105*(1-0.025)=$102.375

### 2.3 Add root-level stop loss
- **File**: `runtime_loop.py` — extend `_monitor_rotation_prices` (currently skips depth==0 at line 1107)
- **Current**: Root nodes have NO stop loss — only deadline-based exit on TA re-evaluation
- **Fix**: Add check for root nodes with `entry_cost`:
  ```python
  if node.depth == 0 and node.entry_cost is not None:
      usd_price = self._root_usd_prices.get(node.asset)
      if usd_price:
          current_value = node.quantity_total * usd_price
          drawdown_pct = (Decimal("1") - current_value / node.entry_cost) * 100
          if drawdown_pct >= Decimal(str(ROOT_STOP_LOSS_PCT)):
              # Emergency root exit
              await self._close_rotation_node(node, order_type=OrderType.MARKET)
  ```
- **New config**: `ROOT_STOP_LOSS_PCT=10` (default: 10%)
- **Tests**: Mock root with $100 entry_cost, current value $89 → verify emergency exit triggers

### 2.4 Enable variable position sizing
- **File**: `.env` lines 22-23, `trading/rotation_tree.py:90-128`
- Change `.env`: `MAX_POSITION_USD=50` (from 10)
- In `compute_child_allocations`, integrate Kelly from `trading/sizing.py:91-142`:
  - Use `belief.confidence` as proxy for win probability
  - Use TP/SL ratio as payoff ratio
  - Apply `bounded_kelly()` then `size_position_usd()` to cap allocation
- **Depends on**: Phase 4.2 for empirical win/loss data (initially use confidence proxy)
- **Tests**: Verify position sizes vary with confidence scores

---

## Phase 3: Improve Signal Quality

**Goal**: Better entry/exit decisions, fewer false signals.

### 3.1 Fix peak window estimation
- **File**: `trading/pair_scanner.py:443-454`
- **Current**: `hours_to_tp = (tp_pct / 100) / hourly_vol`, clamped to [2, 48]
  - Problem: High-vol pairs get 2-hour windows → entered and stopped on noise
- **Fix**:
  - Raise minimum floor: `max(6.0, min(48.0, hours_to_tp))` (was 2.0)
  - Use drift-adjusted estimator: `hours = (tp_pct / 100)^2 / (2 * hourly_vol^2)` (random walk time)
  - Add trend persistence check: if autocorrelation of returns > 0.1, allow shorter windows
- **Tests**: Backtest on historical data for several pairs; verify estimated windows correlate with actual time-to-TP

### 3.2 LLM council disagreement handling
- **File**: `beliefs/llm_council_handler.py:213-237`
- **Current**: Any disagreement → `direction="neutral"`, `confidence=0.0` → always gates out
- **Fix**: Implement weighted majority:
  - 2-of-3 bullish → bullish at confidence = avg_confidence * 0.6 (scaled down for split)
  - Unanimous → keep current behavior (full confidence)
  - Council agrees with TA → boost TA confidence by 20%
  - Council contradicts TA → use min(council_conf, ta_conf) * 0.5
- **Tests**: Unit tests for all consensus scenarios

### 3.3 Raise minimum confidence threshold
- **File**: `trading/rotation_tree.py:18`
- **Current**: `MIN_CONFIDENCE = 0.55` (4/6 TA signals = 0.67 confidence → passes)
- **Fix**: Raise to `MIN_CONFIDENCE = 0.70` (requires 5/6 signals → 0.83 confidence to pass)
- **Tradeoff**: Fewer entries but higher quality. Monitor entry frequency after change.
- **Tests**: Verify with mock beliefs that 4/6 signals (0.67) no longer triggers entry

---

## Phase 4: Observability

**Goal**: Track outcomes, compute stats, alert on problems.

### 4.1 Add trade outcomes table
- **File**: `persistence/sqlite.py`
- New table schema:
  ```sql
  CREATE TABLE IF NOT EXISTS trade_outcomes (
      id              INTEGER PRIMARY KEY,
      node_id         TEXT NOT NULL,
      pair            TEXT NOT NULL,
      direction       TEXT NOT NULL,
      entry_price     TEXT NOT NULL,
      exit_price      TEXT NOT NULL,
      entry_cost      TEXT NOT NULL,
      exit_proceeds   TEXT NOT NULL,
      net_pnl         TEXT NOT NULL,
      fee_total       TEXT,
      exit_reason     TEXT NOT NULL,
      hold_hours      REAL,
      confidence      REAL,
      opened_at       TEXT NOT NULL,
      closed_at       TEXT NOT NULL
  )
  ```
- Populate on every exit fill settlement (`runtime_loop.py:1060-1091`)
- **Tests**: Complete a rotation cycle → verify trade_outcomes row inserted with correct values

### 4.2 Win/loss rate tracking
- **File**: New `trading/trade_stats.py` or extend `trading/sizing.py`
- Functions:
  - `fetch_trade_stats(conn, lookback_days=30)` → returns win_count, loss_count, avg_win, avg_loss, win_rate, payoff_ratio, expectancy
  - Feed into `bounded_kelly()` for position sizing
- **Tests**: Insert mock trade_outcomes rows → verify stats computation

### 4.3 Fix child node P&L display
- **File**: `runtime_loop.py:2082-2091`
- **Current**: Only root nodes (depth==0) get unrealized P&L computed
- **Fix**: Add block for depth>0 OPEN/CLOSING child nodes:
  ```python
  elif n.depth > 0 and n.status in (RotationNodeStatus.OPEN, RotationNodeStatus.CLOSING):
      if n.entry_pair and n.fill_price and n.entry_cost:
          snap = current_prices.get(n.entry_pair)
          if snap:
              current_price = snap.price if hasattr(snap, 'price') else snap
              if n.order_side == OrderSide.BUY:
                  unrealized = (current_price - n.fill_price) * n.quantity_total
              else:
                  unrealized = (n.fill_price - current_price) * n.quantity_total
              realized_pnl = str(unrealized)
  ```
- **Tests**: Create OPEN child with known fill_price → mock current price → verify unrealized P&L shown

### 4.4 Telegram alerts (stretch goal)
- **File**: `alerts/telegram.py`, `.env` lines 49-50
- Configure bot token and chat ID
- Alert on: SL hit, TP hit, fill timeout, WS disconnect >5min, drawdown >5%, consecutive failures
- **Tests**: Mock alert trigger → verify message sent

---

## Dependency Graph

```
Phase 0 (manual, immediate — do FIRST)
    │
    ├── Phase 1.1 (cancel_order DB method)
    │       │
    │       ├── Phase 1.2 (startup reconciliation) — depends on 1.1
    │       │       │
    │       │       └── Phase 1.3 (child rehydration) — depends on 1.2
    │       │
    │       └── Phase 1.4 (periodic reconciliation) — depends on 1.1
    │
    ├── Phase 1.5 (REST fallback fix) — independent
    │
    ├── Phase 2.1 (TP/SL ratio) — independent, can parallel with Phase 1
    │       │
    │       └── Phase 2.2 (trailing stops) — depends on 2.1
    │
    ├── Phase 2.3 (root stops) — independent
    │
    ├── Phase 3.1-3.3 (signal quality) — independent, can parallel
    │
    ├── Phase 4.1 (trade outcomes table) — best after Phase 1 complete
    │       │
    │       └── Phase 4.2 (win/loss stats) — depends on 4.1
    │               │
    │               └── Phase 2.4 (Kelly sizing) — depends on 4.2
    │
    └── Phase 4.3 (child P&L display) — independent
```

**Minimum viable fix**: Phase 0 + 1.1 + 1.2 + 1.4 — makes the bot actually complete trades.

---

## Verification Plan

1. **Phase 0**: Kraken dashboard shows 0 open orders; bot.db orders all `status='cancelled'`; balances match actual exchange
2. **Phase 1**: Place limit order → restart → order reconciled correctly; place order → let fill → settlement recorded in DB; place order → let timeout → DB shows `'cancelled'`
3. **Phase 2**: Unit tests for TP/SL math showing net P&L after fees; simulate price trajectory testing trailing stop ratchet; mock root drawdown to test emergency exit
4. **Phase 3**: Backtest signal changes on historical data; compare entry frequency and quality metrics
5. **Phase 4**: Complete a full trade cycle → verify `trade_outcomes` row; check dashboard shows child P&L; verify stats computation
6. **Integration**: Run with `READ_ONLY_EXCHANGE=true` for 24h → verify no state drift → then live with minimal capital ($50)

---

## Implementation Notes for Codex

- Run `ruff check` and `ruff format` after all changes
- Run existing test suite: `python -m pytest tests/ -x -q`
- All new code needs tests — existing patterns in `tests/` show the conventions
- Do NOT modify `.env` directly — config changes go in `core/config.py` as new defaults
- The `.env` changes (safe mode, MAX_POSITION_USD) are manual operator actions
- Phase 0 is manual/scripted triage — not a code change to the bot itself
- For Phase 0.2, write a standalone script at `scripts/triage_cancel_orders.py`
