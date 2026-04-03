# Continuation Prompt — kraken-bot-v4

## Architecture

- **Host**: spare laptop at home, always-on
- **Exchange**: Kraken (Starter tier) — source of truth for balances/orders/fills
- **Persistence**: SQLite (`./data/bot.db`, WAL mode) — positions, orders, ledger, cooldowns, rotation tree, pair_metadata
- **Dashboard**: FastAPI + D3.js at `http://0.0.0.0:58392` (LAN-accessible)
- **TUI**: `python -m tui` — Textual/Rich operator cockpit, 8 screens (key 7 = Rotation Tree)
- **Beliefs**: 3 available models (select via `BELIEF_MODEL` env var)
- **Platform**: Windows 11, Python 3.13, WSL for runtime
- **Repo**: `git@github.com:robjohncolson/kraken-bot-v4.git`, branch `master`

## Current live state (as of 2026-04-03)

Bot running on WSL `work:2.3` pane with **rotation tree LIVE**:

| Field | Value |
|-------|-------|
| Belief model | `llm_council` (CC+Codex via tmux-bridge) |
| Poll interval | 1 hour (`BELIEF_STALE_HOURS=2`) |
| Portfolio | ~$500 fragmented across ~20 assets (see below) |
| Rotation tree | **LIVE** — `ENABLE_ROTATION_TREE=true`, 18 root nodes |
| ALLOWED_PAIRS | Empty (all pairs enabled for rotation) |
| Scanner timeout | 45s (`SCANNER_TIMEOUT_SEC=45`) |
| Dashboard | `http://10.0.0.24:58392` |
| Tests | 560 passing |
| Belief confidence gate | `MIN_BELIEF_CONFIDENCE=0.5` — beliefs below threshold shown dimmed in TUI |
| Price-aware exits | TP=3%, SL=-2%, dynamic entry timeout (25% of window, 30-120min), exit timeout=5min→MARKET |
| Ordermin enforcement | Dynamic from Kraken AssetPairs API, cached 24h in SQLite |
| Anti-churn | Max 3 children per parent (`ROTATION_MAX_CHILDREN_PER_PARENT=3`), top-3 by score |
| OHLCV cache | 5-minute TTL, deduplicates same-pair scans across roots |
| Pre-flight balance check | 2% safety margin, verifies exchange balance before placing rotation entries |
| Rotation events | TP/SL/timeout/fill events in SSE + TUI rotation tree footer |
| Settings validation | Startup warns on out-of-range TP/SL/confidence/timeout values |

### Portfolio (actual Kraken balances as of 2026-04-03)

Previous sessions successfully traded: USD→BABY/BSU/CFG, ADA→AUD, PEPE→CAD. Fills happened during nonce corruption so tree never tracked them. Assets became orphan roots on restart.

| Asset | Amount | ~USD | Notes |
|-------|--------|------|-------|
| USD | 15.86 | $15.86 | Was $79.89, spent on rotations |
| ADA | 19.87 | ~$14 | Was 49.68, sold some for AUD |
| BABY | 1,476 | ~$21 | Bought from USD rotation |
| BSU | 441 | ~$21 | Bought from USD rotation |
| CFG | 134 | ~$21 | Bought from USD rotation |
| CAD | 29.30 | ~$21 | From PEPE→CAD rotation |
| PEPE | 2.55M | ~$20 | Was 6.39M, sold some |
| ALGO | 43.61 | ~$9 | Original holding |
| + EUR, GBP, KSM, LINK, SOL, TON, UNITAS, USDC, USDT, WIF, XRP, ATOM, BTC, ETH | various | small | Most too small to trade further |

**Key problem**: Portfolio is fragmented into ~20 small positions. Most roots are too small to split 3 ways above $10 minimum. Pre-flight correctly blocks orders that can't afford the 2% safety margin.

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
- **Broker hardening**: pane health checks, retry on send failure, stale file cleanup
- Protocol: `state/llm-council/{requests,responses,consensus}/*.json`
- Consensus: 2/2 agree = that direction, split = neutral, 1/2 = that agent's direction at coverage-scaled confidence
- Requires CC+Codex running in tmux panes for council beliefs

### Research model artifact

`artifacts/logistic_regression_20260329_3f73bb8a/` — 7-feature V1 LogReg, threshold 0.55, CryptoCompare-backed 180d dataset.

## Recursive rotation tree

**Spec**: `docs/specs/recursive-rotation-tree-spec.md`

**Vision**: Denomination-agnostic recursive trading. Portfolio holdings become root nodes. Each asset scans all Kraken pairs for bear exits / bull entries. Rotations create timed child nodes. Children recurse within parent windows. Confidence-weighted sizing.

**Complete (R1-R5) — LIVE**:
- `core/types.py`: RotationNode, RotationCandidate, RotationTreeState, RotationEvent
- `trading/rotation_tree.py`: Pure helpers, child cap (max_children), score-sorted allocation
- `trading/rotation_planner.py`: RotationTreePlanner with anti-churn (child count, per-child budget)
- `trading/pair_scanner.py`: Generalized pair discovery, scan_rotation_candidates
- `exchange/pair_metadata.py`: Dynamic ordermin from Kraken API, SQLite-cached
- `exchange/ohlcv.py`: OHLCV fetch with 5-minute TTL cache
- `persistence/sqlite.py`: rotation_nodes + pair_metadata tables
- `runtime_loop.py`: Full execution loop with pre-flight balance check, rotation events, dynamic timeouts
- `web/routes.py`: `/api/rotation-tree` endpoint, beliefs with `filtered` field
- `tui/`: Beliefs display (filtered dimmed), rotation events in footer, foreign orders explained

**Architecture**: Rotation tree is a shadow ledger. Orders placed directly via executor. Fill settlement updates tree. Pre-flight verifies exchange balance (2% safety margin) before each order.

**Known limitation**: Root nodes have NO deadlines — they are permanent portfolio anchors. Child nodes get deadlines from scanner's estimated window. This means orphaned assets (from missed fill tracking) become permanent roots with no exit plan.

## Key infrastructure

| Feature | Status |
|---------|--------|
| Position persistence | Done — survives restart via SQLite |
| Rotation tree | **LIVE** — scanning, ordering, settling across all Kraken pairs |
| Anti-churn | Max 3 children, top-3 by score, per-child budget gate |
| Ordermin enforcement | Dynamic from Kraken API, cached in SQLite |
| OHLCV cache | 5-min TTL dedup across roots |
| Pre-flight balance | 2% safety margin, committed order tracking |
| Rotation events | Structured TP/SL/timeout/fill events in SSE + TUI |
| Beliefs display | All beliefs shown (filtered dimmed) |
| Settings validation | Startup warnings for out-of-range params |
| Conditional tree (v1) | Built, disabled by default |

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
ROTATION_MAX_CHILDREN_PER_PARENT=3
```

## Goal for next session

### Priority 1: Root Exit Windows (Decision: Option B)

Root nodes currently have no deadlines — they hold forever. This must change. The design:

1. **On startup and every plan_cycle**: run TA on each root asset (against its best quote pair)
2. **Estimate window**: `hours_to_tp = tp_pct / hourly_volatility` (same formula used for child windows, clamped 2-48h)
3. **Set deadline on root**: `root.deadline_at = now + timedelta(hours=hours_to_tp)`
4. **On deadline expiry: RE-EVALUATE, not hard sell**:
   - Re-run TA on the root asset
   - If BEARISH or NEUTRAL → sell to USD (or best available quote currency)
   - If still BULLISH → extend deadline with new estimate
5. **Root exit mechanics**: same as child exit — place LIMIT sell order, escalate to MARKET after 5min timeout
6. **Proceeds**: root sells → USD/quote received → new root node created for the proceeds
7. **Key constraint**: this turns all holdings into actively-managed positions. No permanent "buy and hold."

This solves:
- Orphaned assets from missed fills sitting as permanent roots
- Portfolio fragmentation — bearish small holdings get consolidated back to USD
- The "no exits ever" problem — roots currently never sell

### Priority 2: One Order Per Cycle

Currently places up to 3 orders simultaneously against stale balance data. Change to place **one rotation entry per 30-second cycle**. Each order settles on Kraken between cycles, so the next pre-flight sees accurate balances. Simpler and eliminates the stale-balance edge case entirely.

### Priority 3: Persist Pair Cooldowns

`_rotation_pair_cooldowns` is in-memory dict, lost on restart. Persist to SQLite `cooldowns` table (already exists). Load on startup. Prevents retrying same failing pairs immediately after restart.

### Priority 4: Prune Cancelled Nodes from TUI

Cancelled nodes accumulate in the tree forever. Either:
- Don't display CANCELLED nodes in TUI (filter in widget)
- Prune CANCELLED nodes from tree state after 1 hour

### Lessons learned 2026-04-02/03

- **Nonce safety**: NEVER use a separate script to call authenticated Kraken API while the bot is running. Nonce conflict breaks all subsequent API calls. Cancel orders through the bot's own interface.
- **Shadow ledger divergence**: The rotation tree's `quantity_free` can diverge from actual Kraken balances. Pre-flight check is essential but only as good as the balance staleness allows. One-order-per-cycle is the real fix.
- **Portfolio fragmentation**: Rotation tree creates many small positions. Without root exit windows, these accumulate and become individually untradeable.

## What shipped 2026-04-02/03 (6 commits)

- **Anti-churn**: Max 3 children per parent, top-3 by confidence score, dynamic entry timeout (25% of window, 30-120min), per-child budget gate
- **Scan efficiency**: OHLCV cache (5-min TTL), underfunded leaf skip
- **Pre-flight balance check**: 2% safety margin on order cost + committed tracking, silent cancel (no cooldown) on insufficient funds
- **Ordermin enforcement**: Dynamic from Kraken AssetPairs API, SQLite-cached 24h, enforced in planner + order gate + grid sizing
- **Beliefs display fix**: All beliefs shown in TUI (filtered ones dimmed)
- **Rotation events**: Structured TP/SL/timeout/fill events in SSE + TUI footer
- **Settings validation**: Startup warnings for out-of-range parameters
- **TUI polish**: Foreign orders explained, rotation event in footer
- **Spec-and-ship skill**: `.claude/skills/spec-and-ship/SKILL.md`

## What shipped 2026-04-01 (13 commits)

- LLM Council fallback chain + broker hardening
- Rotation entry resilience (retry budget, rate limit decay, breaker bypass)
- P&L tracking (entry_cost, fill_price, exit_price, exit_proceeds)
- Confidence-weighted trading (MIN_BELIEF_CONFIDENCE=0.5 gate)
- Portfolio total value (rotation tree root pricing in USD)
- TUI polish (orders + health in SSE, P&L column)
- **Price-aware exits**: TP=3%, SL=-2%, fill timeouts, MARKET order support
- Broker: WSL python3 for tmux access

## Validation

```bash
python -m pytest                    # 560 tests
python -m ruff check .              # clean
curl http://127.0.0.1:58392/api/health         # dashboard up
curl http://127.0.0.1:58392/api/rotation-tree  # rotation tree state
```
