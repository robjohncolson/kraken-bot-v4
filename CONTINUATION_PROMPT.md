# Continuation Prompt — kraken-bot-v4

## Architecture

- **Host**: spare laptop at home, always-on
- **Exchange**: Kraken (Starter tier) — source of truth for balances/orders/fills
- **Persistence**: SQLite (`./data/bot.db`, WAL mode) — positions, orders, ledger, cooldowns, rotation tree
- **Dashboard**: FastAPI + D3.js at `http://0.0.0.0:58392` (LAN-accessible)
- **TUI**: `python -m tui` — Textual/Rich operator cockpit, 8 screens (key 7 = Rotation Tree)
- **Beliefs**: 3 available models (select via `BELIEF_MODEL` env var)
- **Platform**: Windows 11, Python 3.13, WSL for runtime
- **Repo**: `git@github.com:robjohncolson/kraken-bot-v4.git`, branch `master`

## Current live state (as of 2026-04-02)

Bot running on WSL Athena pane with **rotation tree LIVE**:

| Field | Value |
|-------|-------|
| Belief model | `llm_council` (CC+Codex via tmux-bridge) |
| Poll interval | 1 hour (`BELIEF_STALE_HOURS=2`) |
| Portfolio | ~$500 across DOGE, AUD, ADA, ETH, BTC, USD |
| Rotation tree | **LIVE** — `ENABLE_ROTATION_TREE=true` |
| ALLOWED_PAIRS | Empty (all pairs enabled for rotation) |
| Scanner timeout | 45s (`SCANNER_TIMEOUT_SEC=45`) |
| Dashboard | `http://10.0.0.24:58392` |
| Tests | 560 passing |
| Belief confidence gate | `MIN_BELIEF_CONFIDENCE=0.5` — beliefs below threshold shown dimmed in TUI |
| Price-aware exits | TP=3%, SL=-2%, dynamic entry timeout (25% of window, 30-120min), exit timeout=5min→MARKET |
| Ordermin enforcement | Dynamic from Kraken AssetPairs API, cached 24h in SQLite |
| Anti-churn | Max 3 children per parent (`ROTATION_MAX_CHILDREN_PER_PARENT`), top-3 by score |
| OHLCV cache | 5-minute TTL, deduplicates same-pair scans across roots |
| Pre-flight balance check | Verifies exchange balance before placing rotation entries (no more InsufficientFundsError) |
| Rotation events | TP/SL/timeout/fill events in SSE + TUI rotation tree footer |
| Settings validation | Startup warns on out-of-range TP/SL/confidence/timeout values |

### Active rotation tree (observed 2026-04-01)

```
root-aud    AUD  245.57   49.13 free   OPEN
  aud-ada   ADA  65.48    PLANNED  (BUY ADA/AUD)
  aud-eth   ETH  65.48    PLANNED  (BUY ETH/AUD)
  aud-btc   BTC  65.48    PLANNED  (BUY BTC/AUD)
root-doge   DOGE 2790.59  free     OPEN
root-usd    USD  79.89    free     OPEN
```

DOGE sold for AUD, AUD immediately deployed into ADA/ETH/BTC children.
USD found 148 candidates but timed out; partial results now returned (will retry next cycle).

## Belief models

| Model | Env value | How it works |
|-------|-----------|-------------|
| `technical_ensemble` | Default | 6-signal TA (EMA, RSI, MACD, Bollinger, momentum) |
| `research_model` | Requires `ACTIVE_ARTIFACT_ID` | V1 LogReg (+5,531 bps on 180d backtest, all rollout gates pass) |
| `llm_council` | Requires broker sidecar | CC+Codex analyze structured market context via file-based messaging |

### LLM Council (with fallback chain)

- Handler: `beliefs/llm_council_handler.py` — builds market context, writes request files
- **Fallback**: `make_fallback_council_handler()` wraps council + `technical_ensemble`. If no fresh consensus, falls back to TA instantly. Bot is never belief-less.
- Broker: `python scripts/llm_council_broker.py` — dispatches to CC+Codex panes, collects votes
- **Broker hardening**: pane health checks (`_pane_exists`, `_pane_is_ready`), retry on send failure, stale file cleanup on startup, malformed request deletion, valid-vote tracking
- Protocol: `state/llm-council/{requests,responses,consensus}/*.json`
- Consensus: 2/2 agree = that direction, split = neutral, 1/2 = that agent's direction at coverage-scaled confidence (`conf * valid/expected`)
- **Per-pair request backoff**: handler won't pile up duplicate requests for the same pair
- Requires CC+Codex running in tmux panes (`work:2.0` Codex, `work:2.1` Claude) for council beliefs

### Research model artifact

`artifacts/logistic_regression_20260329_3f73bb8a/` — 7-feature V1 LogReg, threshold 0.55, CryptoCompare-backed 180d dataset.

Backfill validation: +4,862 bps, 55.1% accuracy, 100% coverage, all rollout gates pass.

## Recursive rotation tree

**Spec**: `docs/specs/recursive-rotation-tree-spec.md`

**Vision**: Denomination-agnostic recursive trading. Portfolio holdings become root nodes. Each asset scans all Kraken pairs for bear exits / bull entries. Rotations create timed child nodes. Children recurse within parent windows. Confidence-weighted sizing.

**Complete (R1-R5) — LIVE**:
- `core/types.py`: RotationNode, RotationCandidate, RotationTreeState, PendingOrder.rotation_node_id
- `trading/rotation_tree.py`: Pure helpers + denomination conversion (entry_base_quantity, destination_quantity, exit_base_quantity, exit_proceeds)
- `trading/rotation_planner.py`: RotationTreePlanner (initialize_roots, plan_cycle)
- `trading/pair_scanner.py`: Generalized pair discovery, scan_rotation_candidates, partial results on timeout
- `persistence/sqlite.py`: rotation_nodes table + rotation_node_id on orders table
- `runtime_loop.py`: Full execution loop:
  - `_execute_rotation_entries()`: PLANNED → order (place first, track on success)
  - `_settle_rotation_fills()`: WS fill → denomination conversion → OPEN
  - `_handle_rotation_expiry()`: OPEN → exit order, PLANNED → cancel + return
  - `_collect_root_prices()`: REST OHLCV fallback for non-USD root assets
- `core/state_machine.py`: Reducer handles rotation_entry/rotation_exit fills (PendingOrder cleanup only)
- `exchange/symbols.py`: Broadened normalizer (AUD, CAD, CHF, JPY, ETH quote currencies)
- `web/routes.py`: `/api/rotation-tree` endpoint with RotationTreeSnapshot
- `tui/screens/rotation_tree.py`: TUI screen (key 7) with hierarchical tree table

**Architecture**: Rotation tree is a shadow ledger separate from Portfolio. Orders placed directly via executor (not reducer). Fill settlement updates tree, not portfolio. Reconciliation re-aligns on restart.

**Price-aware exits**: On entry fill, fee-aware TP/SL prices are computed (TP includes round-trip fees). Every 30s cycle `_monitor_rotation_prices()` checks OPEN nodes: TP hit → LIMIT exit, SL hit → MARKET exit. `_check_rotation_fill_timeouts()` cancels stale entries (30min) and escalates stale exit limits to MARKET (5min). Window estimation uses volatility: `hours_to_tp = tp_pct / hourly_vol`, clamped 2-48h.

**P&L tracking**: RotationNode records `entry_cost` (parent-denomination allocation), `fill_price`, `exit_price`, `closed_at`, `exit_proceeds` on settlement. CLOSED nodes persisted to SQLite. API `/api/rotation-tree` returns per-node `realized_pnl` + tree-level `total_deployed`, `total_realized_pnl`, `open_count`, `closed_count`, `rotation_tree_value_usd`, `total_portfolio_value_usd`. TUI rotation tree screen (key 7) shows P&L column with green/red coloring + summary footer.

**Portfolio valuation**: TUI overview shows total portfolio value by pricing all rotation tree root assets in USD (WebSocket prices → cached REST fallback → N/A). The rotation tree IS the portfolio now — the old Portfolio.total_value_usd only tracks cash/positions from the pre-rotation era. `~` prefix on value means some asset prices are missing.

## Key infrastructure

| Feature | Status |
|---------|--------|
| Position persistence | Done — survives restart via SQLite |
| Exit price | Uses trigger/reference + 0.1% marketable offset |
| WebSocket | Re-enabled for home laptop |
| Reconciliation | Uses exchange open orders (not bot_state) |
| D3 dashboard | Wired — portfolio cards, positions table, belief heatmap, reconciliation |
| TUI | 8 screens (incl. rotation tree), live SSE, keyboard navigation |
| Rotation tree | **LIVE** — scanning, ordering, settling across all Kraken pairs |
| Rotation resilience | Retry budget (3x), rate limit bypass, matching engine decay, auto-cancel on insufficient funds |
| Ordermin enforcement | **NEW** — dynamic from Kraken API, cached in SQLite `pair_metadata` table |
| Rotation events | **NEW** — structured TP/SL/timeout/fill events in SSE + TUI |
| Beliefs display | **FIXED** — all beliefs shown in TUI (filtered ones dimmed) |
| Settings validation | **NEW** — startup warnings for out-of-range TP/SL/confidence/timeout |
| Conditional tree (v1) | Built, disabled by default (`ENABLE_CONDITIONAL_TREE`) |
| Backfill shadow eval | `python -m research.backfill_shadow` |

## Running the bot

```bash
# Launch bot (from WSL)
cd /mnt/c/Users/rober/Downloads/Projects/kraken-bot-v4
/mnt/c/Python313/python.exe main.py

# Launch TUI (separate terminal)
/mnt/c/Python313/python.exe -m tui

# Launch LLM Council broker (MUST use WSL python3, not Windows Python — needs tmux access)
python3 scripts/llm_council_broker.py
```

### Key env vars (.env)

```
KRAKEN_API_KEY=...
KRAKEN_API_SECRET=...
BELIEF_MODEL=llm_council
BELIEF_STALE_HOURS=2
ALLOWED_PAIRS=                    # empty = all pairs (required for rotation tree)
ENABLE_ROTATION_TREE=true
SCANNER_TIMEOUT_SEC=45            # 15s default too short for USD (hundreds of pairs)
WEB_HOST=0.0.0.0
WEB_PORT=58392
READ_ONLY_EXCHANGE=false
DISABLE_ORDER_MUTATIONS=false
MIN_POSITION_USD=10
MAX_POSITION_USD=10
EXIT_LIMIT_OFFSET_PCT=0.1
```

## Research results summary

| Model | P&L (bps) | Sharpe | Accuracy | Trades |
|-------|-----------|--------|----------|--------|
| V1 LogReg (180d) | **+5,531** | **11.3** | 47.2% | 214 |
| TA Ensemble (180d) | +257 | 0.27 | 48.9% | 380 |
| Backfill shadow | +4,862 | — | 55.1% | 1,548 |
| LLM Qwen3 (30d) | -167 | -9.3 | 62.5% | 8 |

## Goal for next session

1. **Observe ordermin enforcement**: verify planner logs "Skipping X: base_qty below ordermin" for undersized allocations
2. **Observe rotation events in TUI**: check rotation tree footer shows last TP/SL/timeout event
3. **Observe beliefs panel**: verify filtered (low-confidence) beliefs appear dimmed in TUI
4. **Monitor P&L**: check `/api/rotation-tree` for `total_realized_pnl` after closed nodes
5. **Tune parameters**: adjust TP/SL/confidence based on observed TP hit rate vs SL hit rate
6. **Consider trailing stop**: once enough TP/SL data, evaluate tightening SL or implementing trailing stop activation
7. **Broker sidecar**: must use `python3 scripts/llm_council_broker.py` (WSL python3, not Windows — needs tmux socket)

## What shipped 2026-04-02

- **Anti-churn**: Max 3 children per parent (`ROTATION_MAX_CHILDREN_PER_PARENT=3`), `compute_child_allocations` sorts by score descending and takes top-N. Planner counts existing live children and passes remaining slots. Dynamic entry timeout: 25% of estimated window (floor=config, cap=4x config=120min). Stops capital dilution across 10+ thin children.
- **Scan efficiency**: OHLCV cache with 5-min TTL deduplicates same-pair scans across multiple roots. Per-child budget check skips underfunded leaves before scanning (saves 100s of HTTP calls).
- **Pre-flight balance check**: Before placing rotation entries, verifies `order_cost <= exchange_balance - committed_orders`. Cancels planned node silently (no pair cooldown) if insufficient. Eliminates InsufficientFundsError from exchange.
- **Ordermin enforcement**: `exchange/pair_metadata.py` fetches `ordermin` from Kraken AssetPairs API, caches in SQLite `pair_metadata` table (24h TTL). Enforced in rotation planner (filters undersized allocations), order gate (defensive `OrderBelowMinimumError`), grid sizing (dynamic fallback)
- **Beliefs display fix**: All beliefs (including low-confidence/neutral) now shown in TUI. Filtered beliefs rendered with dim styling + "filtered" label. Trading logic unchanged (still gates on `MIN_BELIEF_CONFIDENCE`)
- **Rotation event tracking**: Structured `RotationEvent` dataclass emitted for TP hits, SL hits, entry timeouts, exit escalations, entry/exit fills. Events included in SSE payload. TUI rotation tree footer shows last event
- **Settings validation**: `validate_settings()` logs warnings at startup for out-of-range TP/SL/confidence/timeout values (never blocks startup)
- **TUI reconciliation**: Foreign orders now explained with contextual label
- **Spec-and-ship skill**: Saved as `.claude/skills/spec-and-ship/SKILL.md` for future use

## What shipped 2026-04-01 (13 commits)

- LLM Council fallback chain + broker hardening (bulletproof pane health checks, retry, cleanup)
- Rotation entry resilience (retry budget, rate limit decay, breaker bypass, 30min pair cooldown)
- P&L tracking (entry_cost, fill_price, exit_price, exit_proceeds on nodes, SQLite persistence)
- Confidence-weighted trading (MIN_BELIEF_CONFIDENCE=0.5 gate, staleness fix)
- Portfolio total value (rotation tree root pricing in USD, TUI shows ~$440 not $79)
- TUI polish (orders + health in SSE, P&L column on rotation tree, summary footer)
- **Price-aware exits**: TP=3% (LIMIT), SL=-2% (MARKET), fill timeouts (30min entry, 5min exit→MARKET), volatility-based window estimation, fee-aware TP calculation, MARKET order support
- Broker: WSL python3 for tmux access, single-line prompt delivery, sys.path fix

## Validation

```bash
python -m pytest                    # 560 tests
python -m ruff check .              # clean
curl http://127.0.0.1:58392/api/health         # dashboard up
curl http://127.0.0.1:58392/api/rotation-tree  # rotation tree state
```
