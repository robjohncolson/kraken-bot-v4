# Kraken Bot V4 — System Specification

## Philosophy

V4 is a from-the-ground-up trading system: a **self-healing DOGE accumulator that harvests volatility, co-piloted by LLM agents**.

It combines:
- **V2's discipline**: frozen pure-reducer state machine, multi-layer safety rails, env-driven config, battle-tested grid trading states (S0/S1a/S1b/S2)
- **V3's intelligence**: multi-source belief formation via CLI-based LLMs, directional trading across Kraken pairs
- **AP Statistics' simplicity**: z-scores, t-tests, chi-square, regression — no HMMs, no Monte Carlo, no VPIN
- **Auto-research's rigor**: backtested quantitative signals as an independent belief source

### Strategic Goal

The bot is **bearish USD, bullish DOGE**. USD-denominated accounting (V3's lesson), but the strategic objective is DOGE accumulation. Money flows toward whichever asset has bullish consensus from the belief sources. When positions close, profits route through USD and into DOGE when the DOGE/USD chart supports it.

### What We're Keeping

| From | Keep | Why |
|------|------|-----|
| V2 | Frozen pure-reducer state machine | Reducer contract held across 112 commits; forces clean separation |
| V2 | Grid trading states (S0/S1a/S1b/S2) | Battle-tested dual-leg mean-reversion lifecycle |
| V2 | Multi-layer safety rails (circuit breakers, cooldowns) | Prevented cascading failures |
| V2 | Env-driven configuration with sensible defaults | Enabled rapid tuning without deploys |
| V2 | SSE-based real-time dashboard | Critical for observability |
| V3 | Frozen dataclasses everywhere | Eliminated an entire class of mutation bugs |
| V3 | Multi-source belief formation (Claude + Codex) | Core alpha generator, now CLI-based |
| V3 | Supabase as durable coordination store | Resilient cross-environment persistence |
| V3 | FastAPI web dashboard | Lightweight, Python-native |
| V3 | Session-based workflow with continuation prompts | Effective AI-assisted development pattern |
| Auto-research | 6-signal backtested ensemble | Independent quantitative vote alongside LLM beliefs |
| AP Stats | z-scores, t-tests, chi-square, CIs, regression | Simpler, interpretable, teachable |

### What We're Leaving Behind

| From | Drop | Why |
|------|------|-----|
| V2 | God-object `main.py` (4,973 lines) | Unmaintainable; single file accumulated all orchestration logic |
| V2 | HTML-in-Python dashboard (`factory_view.py` at 3,769 lines) | Hostile to maintenance |
| V2 | Gaussian HMM, Monte Carlo fill estimation, VPIN | Overengineered; replaced by AP Stats equivalents |
| V2 | 83 markdown spec files in root directory | Spec proliferation; move to `docs/` |
| V2 | Multiple uncoordinated reseed/shed code paths | Root cause of worst operational bugs |
| V3 | R Shiny + Haskell companion processes | Over-engineered, later abandoned for FastAPI |
| V3 | DOGE denomination (portfolio P&L in DOGE) | Created cumulative conversion drag; USD accounting from day 1 |
| V3 | Local-first state with Railway sync | Took many sessions to stabilize; define authority boundaries from day 1 |
| V3 | `guardian.py` at ~100KB, `main_belief.py` at ~134KB | God objects; max 500 lines per module |
| V3 | Broad `except` clauses | Silently hid critical failures (NIGHTUSD margin bug) |
| V3 | Range Harvest Overlay | Massive complexity for limited live validation; V4 makes grid trading first-class instead |
| V3 | LangGraph agent debate system | Replaced by CLI-based beliefs at $0 marginal cost via subscriptions |
| V3 | Vector + BM25 hybrid memory | Replaced by database-as-memory; LLMs query Supabase directly |

---

## Architecture

### Design Constraints

1. **No module exceeds 500 lines.** If it grows past 500, split it.
2. **Explicit authority per domain.** Kraken is truth for live orders/balances. Supabase is the durable coordination store. Local state is a read-through cache plus offline queue.
3. **All order operations flow through one gate.** No scattered reseed/shed paths. One `OrderGate` module controls all Kraken order placement and cancellation.
4. **Typed exceptions only.** No bare `except:`. Every handler names the exception class.
5. **USD accounting, DOGE accumulation.** All P&L, risk calculations, and position sizing in USD. DOGE is the strategic accumulation target, not the accounting unit.
6. **Frozen dataclasses for all state.** No mutable state objects anywhere.
7. **Every statistical decision maps to an AP Stats concept.** Parametric methods first, with a normality gate. Non-parametric methods (Wilcoxon, Spearman, bootstrap) are available but must be earned by demonstrating parametric methods fail.
8. **All limit orders.** The only market order exception is Kraken-native stop-losses for capital preservation.

### Module Map

```
kraken-bot-v4/
├── core/                          # Pure logic, no I/O
│   ├── state_machine.py           # Frozen reducer: (State, Event, Config) -> (State, [Action])
│   ├── types.py                   # All frozen dataclasses, enums, type aliases
│   ├── config.py                  # Env-driven config with defaults
│   └── errors.py                  # Typed exception hierarchy
│
├── stats/                         # AP Statistics toolkit (Units 1-9)
│   ├── descriptive.py             # Mean, SD, percentiles, IQR, z-scores (Units 1-2)
│   ├── regression.py              # LSRL, r², residuals, slope inference (Units 2, 9)
│   ├── probability.py             # Conditional prob, Bayes' rule, independence (Unit 4)
│   ├── distributions.py           # Normal, t, chi-square, binomial, geometric (Units 4-5)
│   ├── intervals.py               # Confidence intervals: z, t, proportion (Units 6-7)
│   ├── hypothesis.py              # z-tests, t-tests, chi-square tests (Units 6-9)
│   ├── sampling.py                # CLT, sampling distributions, sample size calc (Unit 5)
│   └── normality.py               # Normality gate: check assumptions before parametric tests
│
├── grid/                          # Grid trading engine (V2 heritage)
│   ├── engine.py                  # Grid activation, slot management, profit redistribution
│   ├── states.py                  # S0/S1a/S1b/S2 lifecycle (simplified from V2)
│   ├── sizing.py                  # Minimum-sized slot calculation per pair
│   └── accounting.py              # Grid P&L tracking, risk linkage to parent position
│
├── beliefs/                       # CLI-based belief formation
│   ├── orchestrator.py            # Coordinate sources, compute consensus, check agreement
│   ├── claude_source.py           # Claude Code CLI adapter
│   ├── codex_source.py            # Codex CLI adapter
│   ├── autoresearch_source.py     # Auto-research 6-signal ensemble adapter
│   ├── consensus.py               # 2/3 agreement logic, belief strength scoring
│   └── prompts.py                 # Prompt templates with database-as-memory pattern
│
├── exchange/                      # Kraken API layer
│   ├── client.py                  # REST client with rate limiting and retry
│   ├── websocket.py               # WebSocket feed for real-time data
│   ├── symbols.py                 # Symbol normalization (XXRP -> XRP, etc.)
│   └── order_gate.py              # THE single gateway for all order operations
│
├── trading/                       # Position and portfolio management
│   ├── portfolio.py               # Portfolio state, position tracking, P&L
│   ├── position.py                # Individual position lifecycle
│   ├── sizing.py                  # Position sizing via Kelly + CI bounds
│   ├── risk_rules.py              # Stop-loss, take-profit, concentration limits
│   └── reconciler.py              # Kraken <-> Supabase state reconciliation
│
├── persistence/                   # Storage layer
│   ├── supabase.py                # Supabase client with offline queue
│   ├── ledger.py                  # Trade ledger (JSONL local + Supabase)
│   └── snapshots.py               # State snapshots for crash recovery
│
├── web/                           # Read-only dashboard (deployed to Railway)
│   ├── app.py                     # FastAPI app with SSE
│   ├── routes.py                  # GET-only API endpoints
│   ├── static/
│   │   ├── index.html
│   │   ├── app.js                 # Main app shell, SSE connection
│   │   ├── d3-grid.js             # Grid status visualization
│   │   ├── d3-stats.js            # Statistical dashboards
│   │   ├── d3-beliefs.js          # Belief agreement visualization
│   │   ├── d3-equity.js           # Equity curve and P&L charts
│   │   └── styles.css
│   └── sse.py                     # Server-Sent Events broadcaster
│
├── alerts/                        # Notifications
│   ├── telegram.py                # Telegram bot integration
│   └── formatter.py               # Message formatting
│
├── scheduler.py                   # Main loop orchestration (<500 lines)
├── guardian.py                    # Autonomous position monitor (<500 lines)
├── main.py                        # Entry point, wiring, startup (<200 lines)
│
├── tests/
│   ├── core/
│   ├── stats/
│   ├── grid/
│   ├── beliefs/
│   ├── exchange/
│   ├── trading/
│   └── integration/
│
└── docs/
    └── specs/                     # All spec documents live here
```

---

## Core Subsystems

### 1. State Machine (core/state_machine.py)

**Carried forward from V2.** Pure reducer with no side effects:

```python
def reduce(state: BotState, event: Event, config: Config) -> tuple[BotState, list[Action]]:
    """Pure function. No I/O. No exceptions. Deterministic."""
```

**Dual-mode: belief positions + grid slots.**

The state machine handles two coexisting position types:
- **Belief positions**: directional (long/short), with stop/target, opened when 2/3 belief sources agree
- **Grid slots**: direction-agnostic mean-reversion trades, activated within a belief position when beliefs+TA say "ranging"

Events: `PriceTick`, `FillConfirmed`, `StopTriggered`, `TargetHit`, `BeliefUpdate`, `ReconciliationResult`, `GridCycleComplete`.

Actions: `PlaceOrder`, `CancelOrder`, `UpdateStop`, `UpdateTarget`, `ClosePosition`, `ActivateGrid`, `DeactivateGrid`, `RedistributeGridProfits`, `LogEvent`.

State transitions are explicit and exhaustive via match/case.

### 2. Belief Formation (beliefs/)

**Three independent sources. 2/3 agreement required to enter.**

| Source | Type | How It Works | Output |
|--------|------|-------------|--------|
| **Claude Code** | Reasoning-based | CLI invocation with market data + trade history context | Directional belief + confidence + reasoning |
| **Codex** | Independent LLM | CLI invocation, same data, independent analysis | Directional belief + confidence + reasoning |
| **Auto-research** | Quantitative, backtested | 6-signal ensemble (momentum, RSI, MACD, EMA cross, Bollinger compression) on hourly bars | Bull/bear/neutral per symbol |

**Belief lifecycle:**
1. **Formation**: Each source independently analyzes the pair
2. **Consensus**: `consensus.py` checks 2/3 agreement on direction
3. **TA confirmation**: Bollinger Bands, EMA crossovers, etc. confirm the belief's regime call (trending vs ranging)
4. **Grid activation**: If consensus says "ranging" → activate grid trading on that pair
5. **Staleness check**: Beliefs expire after `BELIEF_STALE_HOURS` (default 4h) → refresh
6. **Position closure**: When consensus flips or dissolves → stop new grid entries, let existing orders fill, unwind

**Database-as-memory pattern:**

Instead of a separate vector/BM25 memory system, belief prompts include a directive:

> "Before forming a belief about {pair}, review the last {N} closed positions on this pair from the trades table. Note where predictions diverged from outcomes."

The LLMs running on the laptop have full database access. The "memory" is just Supabase. No embedding model, no retrieval pipeline, no memory subsystem to build and debug.

**Belief cadence:** Daily for full belief cycles. The auto-research ensemble can run more frequently (hourly) since it's pure Python with no LLM cost.

### 3. Grid Trading Engine (grid/)

**Activated when beliefs + TA identify a ranging regime.** Adapted from V2's S0/S1a/S1b/S2 lifecycle, simplified.

#### Grid States (from V2, simplified)

```
S0:  Both entries pending (A = sell high, B = buy low)
S1a: Buy leg filled, awaiting buy exit (sell back higher)
S1b: Sell leg filled, awaiting sell exit (buy back lower)
S2:  Both legs filled, both exits pending
```

**Simplifications from V2:**
- **No per-slot identity.** Whether a slot fills is random. Slots don't have names or persistent state beyond the grid engine's aggregate view.
- **Periodic profit redistribution.** Instead of tracking each slot's individual P&L, profits are redistributed across all active slots periodically → compounding effect → position growth.
- **Minimum-sized slots.** Capital is split into the smallest allowable trade size per pair. This maximizes the number of grid levels and harvests any movement.

#### Grid ↔ Belief Interaction

- Grid trading is **direction-agnostic** — it assumes mean reversion will continue.
- Grid P&L is **linked to the parent belief position's risk accounting**. If grid trading produces losses, the overall trade's risk increases. If it produces profits, it reduces the effective risk of the directional bet.
- When a belief position closes: **no new grid entries** on either leg. Existing limit orders remain and fill naturally.
- **Unwind path**: asset → sell to USD via limit (+0.4% above market) → evaluate DOGE/USD chart → if DOGE bullish, buy DOGE via limit (-0.4% below market).

#### Order Budget

- Belief orders are not numerous (entry, stop, target per position).
- Remaining headroom (within Kraken's limits) is allocated to grid slots.
- Grid uses a configurable percentage of remaining headroom, not all of it.
- Capacity management is a first-class concern — V2 spent 10+ commits on shed/reseed churn when hitting Kraken's per-pair order limits.

### 4. Statistical Intelligence (stats/)

Every analytical decision uses AP Stats methods. Parametric first, with guardrails.

#### Normality Gate

Before trusting any parametric result:
1. Check sample size (n ≥ 30 minimum)
2. If n ≥ 30 and distribution passes basic normality check → use parametric method
3. If normality check fails → log a warning, use the result with reduced confidence, flag for future non-parametric upgrade
4. If stats computation is undefined (zero variance, degenerate case, insufficient data) → **fail closed** (block the decision, don't guess)

Non-parametric methods (Wilcoxon signed-rank, Spearman rank correlation, bootstrap CIs) are available in the codebase but gated behind earned complexity — they activate when parametric methods demonstrably fail for a specific use case.

#### Trading Question → Method Mapping

| Trading Question | AP Stats Method | Module |
|-----------------|----------------|--------|
| "Is this return significantly different from zero?" | 1-sample t-test (H₀: μ = 0) | `hypothesis.py` |
| "Is this strategy better than that one?" | 2-sample t-test or matched-pairs t-test | `hypothesis.py` |
| "Is this price move an outlier?" | z-score: \|z\| > 2 → unusual, \|z\| > 3 → extreme | `descriptive.py` |
| "What return can we expect?" | Confidence interval for mean return | `intervals.py` |
| "Are wins/losses independent of market regime?" | Chi-square test for independence | `hypothesis.py` |
| "Does regime match expected distribution?" | Chi-square goodness-of-fit | `hypothesis.py` |
| "How many trades until first winner?" | Geometric distribution: μ = 1/p | `distributions.py` |
| "What's our win rate confidence?" | 1-proportion z-interval | `intervals.py` |
| "Is pair A correlated with pair B?" | Pearson r + t-test for slope | `regression.py` |
| "How much should we risk?" | Kelly criterion bounded by CI lower bound | `intervals.py` |
| "Has the win rate changed?" | 2-proportion z-test (recent vs. historical) | `hypothesis.py` |
| "Is this belief source calibrated?" | Chi-square GOF: predicted vs. actual outcome bins | `hypothesis.py` |
| "How large a sample do we need?" | Sample size formula: n = (z*/ME)² × p̂(1-p̂) | `sampling.py` |

**No HMMs. No Monte Carlo. No VPIN. No Gaussian mixture models.**

#### Statistical Guardrails on Belief Sources

| Guardrail | Method | Threshold |
|-----------|--------|-----------|
| "Is this belief source adding value?" | 1-sample t-test on source's excess returns vs. 0 | p < 0.10 to keep source active |
| "Is this source calibrated?" | Chi-square GOF: predicted confidence bins vs. actual hit rates | p > 0.05 to keep (fail to reject = calibrated) |
| "Has source accuracy changed?" | 2-proportion z-test: recent hit rate vs. historical | p < 0.05 → flag for review |
| "Is this trade sized correctly?" | Kelly fraction bounded by lower end of 95% CI for win rate | Never bet on point estimate |
| "Are we overexposed to one direction?" | Binomial test: proportion of long positions vs. 0.5 | p < 0.05 → block same-side entries |

### 5. Order Gate (exchange/order_gate.py)

**The single most important lesson from V2.** All order operations flow through one module.

```python
class OrderGate:
    """Single gateway for ALL Kraken order operations.

    V2 lesson: Multiple reseed/shed paths caused the worst operational bugs.
    V3 lesson: Guardian and main loop racing on order operations caused ghosts.

    Rules:
    1. Only OrderGate calls KrakenClient.place_order() or cancel_order()
    2. Every operation is logged before and after
    3. Circuit breaker: 3 failures in 120s -> halt all operations
    4. Rate limiter: respects Kraken's per-endpoint limits
    5. Client order IDs (cl_ord_id) on every order for reconciliation
    6. Post-placement verification via OpenOrders query
    """
```

**Circuit breaker states:** CLOSED (normal) → OPEN (3 failures in 120s, block all ops) → HALF_OPEN (after cooldown, allow 1 test op) → CLOSED.

**Stop-loss policy:** Guardian attempts limit exit first when price approaches stop level. If price blows through, Kraken-native stop-loss order fires as market order. This is the **only** market order in the system. Kraken native stops are server-side and not visible on the order book — no stop hunting exposure.

**Kraken Starter tier constraints:**
- REST API: 15 max bucket, -0.33/s decay
- Matching engine: 60 per pair, +8 cancel penalty if order < 5 seconds old
- Cancel-replace patterns must respect the 5-second age penalty

### 6. Reconciliation (trading/reconciler.py)

**First-class subsystem.** V2 spent ~20 commits on state drift, V3 spent ~29 commits on ghost/phantom positions.

```
Every 5 minutes:
1. Fetch Kraken open orders + balances + trade history
2. Normalize symbols (XXRP → XRP, handle all Kraken naming quirks)
3. Compare Kraken state to Supabase state
4. Detect: ghost positions, untracked assets, fee drift, foreign orders
5. For each discrepancy: log it, categorize severity, auto-fix if LOW, alert if HIGH
6. Reconciliation result feeds back into state machine as ReconciliationResult event
```

**On restart (kill switch recovery):**
1. Connect to Supabase, pull last known state
2. Connect to Kraken, fetch live orders + balances + recent trade history
3. Reconcile: match Supabase positions to Kraken reality
4. Best-effort import for unrecognized Kraken positions + Telegram alert
5. Resolve discrepancies before entering main loop

**Symbol normalization** is centralized in `exchange/symbols.py`. REST returns `XXBTZUSD`, `altname` returns `XBTUSD`, WebSocket v2 returns `BTC/USD`. One module handles all variants.

**Foreign order handling:**
- Every bot order uses `cl_ord_id` prefix for identification
- Orders without the prefix are classified as foreign
- States: `new` → `acked` → `stale` → `resolved`
- Stale foreign orders can be auto-cancelled only through `OrderGate`
- Classification and lifecycle managed by the reconciler, not scattered across modules

**Ghost position prevention:**
- `cl_ord_id` on every order for reliable matching
- Post-placement verification (query `OpenOrders` after every placement)
- WebSocket `executions` feed as primary fill source, REST polling as fallback
- Explicit handling for the "timeout after AddOrder" failure mode: query by `cl_ord_id` to determine if order was placed

### 7. Portfolio (trading/portfolio.py)

**USD-denominated from day 1.** V3's DOGE denomination created conversion drag.

```python
@dataclass(frozen=True)
class Portfolio:
    cash_usd: Decimal           # Available USD
    cash_doge: Decimal          # Available DOGE (accumulation target)
    positions: tuple[Position]  # Open positions (frozen)
    total_value_usd: Decimal    # cash + sum(position values)

    # Risk metrics (updated every cycle)
    concentration: dict[str, Decimal]   # pair -> % of portfolio
    directional_exposure: Decimal       # net long - net short as % of total
    max_drawdown: Decimal               # peak-to-trough since inception
```

**DOGE accumulation logic:** When positions close and profits are in USD:
1. Check DOGE/USD belief consensus
2. If DOGE bullish → place limit buy at -0.4% below market (maker fee avoidance)
3. If DOGE bearish or neutral → hold USD, re-evaluate next cycle

**Pair universe:** 5-10 pairs simultaneously. Spot + margin (margin for shorts only).

### 8. Persistence (persistence/)

**Authority is explicit by domain.**

```
Kraken (live truth)
  ├── open orders
  ├── balances
  └── fills / trade history

SQLite (durable coordination store — local on bot host)
  ├── positions table     # All open/closed positions
  ├── orders table        # All order history with cl_ord_id
  (beliefs, grid_cycles, config, ledger tables added as needed)

Local files (audit + recovery)
  ├── state.json          # Latest snapshot (read-through cache)
  ├── ledger.jsonl        # Append-only local copy
  └── queue.jsonl         # Offline write queue
```

**Startup sequence:**
1. Load config, ensure local state dir
2. Open SQLite, ensure schema
3. Health-check Kraken
4. Fetch Kraken state (balances, open orders, trade history)
5. Fetch recorded state from SQLite
6. Reconcile Kraken state vs. recorded state
7. If discrepancies → log + Telegram alert
8. Start main loop (or exit if STARTUP_RECONCILE_ONLY=true)

### 9. Dashboard (web/)

**Read-only D3.js dashboard, served locally on the bot host.** Access via Tailscale. No action endpoints. No `POST /api/override`.

```
FastAPI backend (Railway):
  GET /api/portfolio     → current portfolio state
  GET /api/positions     → open positions with P&L
  GET /api/grid/{pair}   → grid status, slot distribution, cycle history
  GET /api/beliefs       → latest beliefs per source per pair
  GET /api/stats         → strategy statistics (win rate CI, Sharpe, etc.)
  GET /api/reconciliation → last reconciliation result, discrepancy history
  GET /api/health        → system health status
  GET /sse/updates       → real-time Server-Sent Events stream

Frontend:
  Plain HTML/CSS:
    - Portfolio overview with real-time P&L
    - Position cards with belief summaries
    - Grid status per pair (active slots, phase distribution)
    - Reconciliation status panel
    - Alert log
  D3.js:
    - Return distribution histograms with z-score overlays
    - Grid P&L heatmap (which price levels are profitable)
    - Belief agreement visualization (source × pair matrix)
    - Equity curve chart
    - Confidence interval bands on strategy metrics
```

**Authentication is an open question.** The dashboard is read-only, which reduces risk, but portfolio P&L on a public Railway URL is still exposure. Options to evaluate:
- Basic auth (simple, sufficient for single user)
- Railway private networking (only accessible via Tailscale)
- No auth (acceptable if Railway URL is treated as semi-private)

This decision should be made before Phase 5 (Observability).

**Rendering discipline:** Each D3 visualization lives in its own `.js` module. No monolithic frontend file. This prevents the V2 `factory_view.py` problem from recurring in JavaScript.

### 10. Guardian (guardian.py)

**Under 500 lines.** V3's guardian grew to ~100KB because it accumulated too many responsibilities.

The V4 guardian does exactly three things:
1. **Stop/target monitoring** — every 2 minutes, check if any position's price has reached stop or target. Attempt limit exit first; Kraken native stop is the safety net.
2. **Risk rule enforcement** — check portfolio-level rules (max drawdown, concentration, directional exposure)
3. **Belief staleness** — if a position's belief is older than `BELIEF_STALE_HOURS`, schedule re-evaluation

Everything else (reconciliation, order execution, grid management) lives in its own module.

---

## Operational Safety

### Multi-Layer Defense

```
Layer 1: Statistical Guardrails
  └─ t-tests, chi-square on belief source calibration → disable uncalibrated sources
  └─ Normality gate: check assumptions before trusting parametric results
  └─ Fail closed: undefined stats → block, don't guess

Layer 2: Belief Consensus
  └─ 2/3 source agreement required (Claude Code + Codex + auto-research)
  └─ TA confirmation (Bollinger, EMA) before grid activation
  └─ Belief staleness expiry (4h default)

Layer 3: Position-Level Rules
  └─ Every position has stop-loss (required, no exceptions)
  └─ Position size bounded by Kelly lower CI bound
  └─ Re-entry cooldown after stop-out (24h default)
  └─ Guardian tries limit exit first, Kraken native stop as safety net

Layer 4: Portfolio-Level Rules
  └─ Max positions: configurable (default 8)
  └─ Max same-side concentration: 60%
  └─ Max single-pair allocation: 15% of portfolio
  └─ Max drawdown halt: 10% → stop new entries, 15% → close all

Layer 5: Grid-Level Rules
  └─ Grid only activates when beliefs+TA confirm ranging regime
  └─ Grid losses increase parent position risk score
  └─ Minimum slot sizes prevent overcommitment
  └─ Configurable headroom percentage (don't use all available order capacity)

Layer 6: Order Gate
  └─ Circuit breaker (3 failures → halt)
  └─ Rate limiter (Kraken Starter tier compliant)
  └─ cl_ord_id on every order + post-placement verification
  └─ 5-second cancel penalty awareness

Layer 7: Reconciliation
  └─ Every 5 minutes: Kraken ↔ Supabase consistency check
  └─ Auto-fix LOW severity, alert on HIGH
  └─ Foreign order detection via cl_ord_id prefix
  └─ Ghost position prevention via post-placement verification

Layer 8: Crash Recovery
  └─ Kill switch = Ctrl+C; Kraken native stops persist on exchange
  └─ On restart: Supabase + Kraken reconciliation before trading resumes
  └─ Best-effort import of unrecognized positions + Telegram alert
  └─ No "stale state causes wrong behavior" scenarios
```

### Degraded Modes

| Failure | New Entries | Open Position Mgmt | Persistence | Alert |
|---------|-------------|---------------------|-------------|-------|
| Supabase unavailable | Blocked | Kraken + local cache | Queue writes locally | Telegram |
| Kraken WebSocket down | Allowed (tighter limits) | REST polling fallback | Normal | Telegram |
| Kraken private API down | Blocked | Monitoring only, no order mutations | Normal | Telegram |
| All LLM sources down | Blocked | Stops/targets/reconciliation continue | Normal | Telegram |
| Reconciliation mismatch | Blocked | Reduce-only / close-only mode | Normal | Telegram |

**Default rule:** if belief formation is impaired, the bot manages risk on open positions but does not open new ones.

### Exception Handling Policy

```python
# FORBIDDEN (V3 bug: NIGHTUSD stayed open indefinitely)
try:
    close_position(pos)
except:
    pass

# REQUIRED
try:
    close_position(pos)
except KrakenAPIError as e:
    logger.error(f"Failed to close {pos.pair}: {e}")
    alert_telegram(f"Close failed for {pos.pair}: {e}")
    schedule_retry(pos, delay=120)
except InsufficientFundsError as e:
    logger.error(f"Insufficient funds to close {pos.pair}: {e}")
    escalate_to_manual(pos)
```

### No Paper Trading Phase (Deliberate Decision)

V4 **does not include a paper trading gate**. This is a conscious departure from standard practice. Rationale:
- Minimum position sizes on live Kraken keep per-trade cost of flaws low
- Nothing reveals bugs faster than real exchange interaction (V2 found 11 bugs on day 1 of live deployment)
- Paper trading fill models are systematically optimistic vs. real Kraken execution
- The 8-layer safety system bounds downside even with real orders

This decision means reconciliation, foreign order handling, and ghost position prevention must be correct from Phase 1.

---

## Deployment Architecture

```
┌─────────────────────────────────────────────┐
│  SPARE LAPTOP (always on, Tailscale)         │
│                                              │
│  Python Trading Bot                          │
│    ├── scheduler.py (main loop)              │
│    ├── guardian.py (stop/target monitor)      │
│    ├── reconciler (Kraken ↔ SQLite)          │
│    └── grid engine                           │
│                                              │
│  Belief Formation                            │
│    ├── Claude Code CLI (subscription)        │
│    ├── Codex CLI (subscription)              │
│    └── Auto-research strategy (pure Python)  │
│                                              │
│  SQLite (durable coordination store)         │
│    └── data/bot.db (positions, orders)       │
│                                              │
│  Local Files                                 │
│    ├── state/bot-status.json                 │
│    ├── data/state.json (snapshot cache)      │
│    └── data/queue.jsonl (offline queue)      │
│                                              │
│  Local Dashboard (FastAPI + D3.js + SSE)     │
│    └── localhost:8080, access via Tailscale  │
└─────────────────────────────────────────────┘
```

**Kill switch:** Ctrl+C on the laptop. Kraken native stop-loss orders persist on the exchange after process termination. On next startup, full reconciliation runs before trading resumes.

**Remote access:** Tailscale from school to laptop for monitoring and manual Claude Code/Codex sessions.

---

## Self-Healing Architecture (Implementation Phase 6 — Intended, Not Validated)

> **Important framing:** The patterns below are drawn from the `Agent` and `Fractals` repositories, which provide proven coordination and decomposition patterns but have not been validated as an autonomous repair loop for a live trading system. Implementation Phase 6 builds toward autonomous self-healing; until then, v1 (manual co-pilot) applies.

### v1: Manual Co-Pilot

- Bot writes `state/bot-status.json` with current positions, P&L, errors, health status
- When issues arise: operator opens Claude Code / Codex session on the laptop
- LLMs read bot state, logs, Supabase data to diagnose and fix
- Telegram alerts ensure the operator knows something needs attention

### Future (Phase 6): Autonomous Self-Healing

Based on the `Agent` repo's cross-agent protocol and `Fractals`' recursive task decomposition:

```
Bot detects anomaly → writes to state/bot-incident.json
Claude Code reads incident → classifies: atomic or composite?
  If atomic → CC fixes directly, Codex verifies
  If composite → Fractals-style decomposition into sub-tasks
    Each leaf → dispatched to Codex in isolated worktree
    CC reviews each fix → Codex verifies CC's review
    Both declare satisfaction → merge → bot resumes
```

**Key safety properties from Agent repo:**
- Cross-agent depth limit of 1 (prevents runaway fix loops)
- Graceful degradation (if cross-talk fails, caller continues without subagent)
- File-based IPC via `state/cross-agent/{call_id}.request.json` / `.result.json`
- CC = architect/reviewer, Codex = implementer

**Key decomposition properties from Fractals:**
- Classify-then-decompose at every scale (self-similar)
- Lineage context prevents redundant investigation
- Status propagation for health rollup
- Worktree isolation per fix (no interference between parallel fixes)

---

## Configuration

All config via environment variables with sensible defaults:

```bash
# Exchange (Kraken Starter tier)
KRAKEN_API_KEY=
KRAKEN_API_SECRET=
KRAKEN_TIER=starter                 # starter|intermediate|pro

# Portfolio
MAX_POSITIONS=8
MAX_SAME_SIDE_PCT=60
MAX_SINGLE_PAIR_PCT=15
MAX_DRAWDOWN_SOFT_PCT=10            # Stop new entries
MAX_DRAWDOWN_HARD_PCT=15            # Close all positions

# Sizing
KELLY_CI_LEVEL=0.95                 # Use lower bound of 95% CI
MIN_POSITION_USD=10
MAX_POSITION_USD=100
DEFAULT_STOP_PCT=5
DEFAULT_TARGET_PCT=10

# Grid
GRID_HEADROOM_PCT=70                # Use 70% of remaining order capacity for grid
GRID_PROFIT_REDIST_INTERVAL_SEC=3600
GRID_MAKER_OFFSET_PCT=0.4          # Limit order offset for maker fees

# Beliefs
BELIEF_STALE_HOURS=4
BELIEF_CONSENSUS_THRESHOLD=2        # Out of 3 sources
REENTRY_COOLDOWN_HOURS=24

# Persistence (local-first)
SQLITE_PATH=./data/bot.db
LOCAL_STATE_DIR=./data

# Dashboard (local, access via Tailscale)
WEB_HOST=127.0.0.1
WEB_PORT=8080

# Alerts
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Safety
CIRCUIT_BREAKER_THRESHOLD=3
CIRCUIT_BREAKER_WINDOW_SEC=120
CIRCUIT_BREAKER_COOLDOWN_SEC=300
RECONCILE_INTERVAL_SEC=300

# Statistics
STATS_MIN_SAMPLE_SIZE=30
STATS_NORMALITY_CHECK=true
STATS_FAIL_CLOSED=true
```

---

## Testing Strategy

**Target: 90%+ coverage from the start.**

| Layer | Test Type | Focus |
|-------|-----------|-------|
| `core/` | Unit (pure functions) | State machine transitions exhaustive: every (state, event) pair |
| `stats/` | Unit (math verification) | Compare against scipy/statsmodels for known datasets; normality gate edge cases |
| `grid/` | Unit | S0/S1a/S1b/S2 transitions, profit redistribution math, capacity budget split |
| `beliefs/` | Integration (mock CLI) | Consensus logic, 2/3 agreement, staleness expiry |
| `exchange/` | Unit + integration | Order gate circuit breaker; symbol normalization; rate limiting; cl_ord_id generation |
| `trading/` | Unit | Portfolio math; sizing bounds; reconciler discrepancy detection; DOGE accumulation logic |
| `persistence/` | Integration | Supabase round-trip; offline queue drain; snapshot restore |
| `web/` | Integration | API endpoints return correct data; SSE stream delivers events |
| End-to-end | Simulation | Full pipeline with mock exchange: beliefs → grid activation → fill → profit redistribution |

**Regression tests for every V2/V3 bug:**
- Shed/reseed churn → order gate prevents parallel placement paths
- Foreign order accumulation → reconciler detects and classifies via cl_ord_id
- DOGE denomination drift → all math in USD, test with cross-currency scenarios
- Broad except hiding failures → linting rule + test that typed exceptions propagate
- Ghost positions → cl_ord_id + post-placement verification + reconciler integration test
- Capacity churn → grid headroom percentage prevents full order budget consumption

---

## Implementation Phases

### Phase 1: Foundation (core/ + stats/ + exchange/ + persistence/)
- State machine with dual-mode position lifecycle (belief + grid)
- AP Stats toolkit with normality gate, tests against scipy
- Kraken client with Starter tier rate limiting
- Order gate with circuit breaker and cl_ord_id
- Supabase integration with offline queue
- Symbol normalization (centralized)
- Env-driven config

### Phase 2: Grid Engine (grid/)
- V2 S0/S1a/S1b/S2 adapted (no per-slot identity)
- Minimum slot sizing per pair
- Profit redistribution and compounding
- Grid ↔ belief position accounting linkage
- Capacity headroom management

### Phase 3: Beliefs (beliefs/)
- Claude Code CLI adapter
- Codex CLI adapter
- Auto-research 6-signal ensemble adapter
- Consensus logic (2/3 agreement)
- Database-as-memory prompt pattern
- TA confirmation (Bollinger, EMA)

### Phase 4: Trading (trading/ + guardian.py + scheduler.py)
- Portfolio management (USD accounting, DOGE accumulation)
- Position sizing (Kelly + CI bounds)
- Risk rules enforcement
- Guardian (stops, targets, staleness)
- Reconciler (Kraken ↔ Supabase, foreign orders, ghost prevention)
- Scheduler (main loop orchestration)

### Phase 5: Observability (web/ + alerts/)
- FastAPI read-only dashboard with SSE
- D3.js visualizations (grid, stats, beliefs, equity)
- Telegram alerts
- Railway deployment
- Authentication decision

### Phase 6: Self-Healing (intended architecture)
- Agent repo cross-agent protocol integration
- Fractals task decomposition for incidents
- Bot state file writing for LLM context
- CC ↔ Codex mutual verification loop

---

## Tech Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Language | Python 3.12 | Matches developer environment |
| Belief sources | Claude Code CLI, Codex CLI | Subscription-funded ($0 marginal), proven in V3 |
| Quantitative signals | Auto-research ensemble | Backtested, pure Python, no LLM cost |
| Web framework | FastAPI | Lightweight, async, Railway-friendly |
| Visualization | D3.js + vanilla HTML/CSS/JS | D3 for stats/grid/belief charts; plain DOM for tables/cards |
| Database | Supabase (PostgreSQL) | V3 proven, direct connection for low latency |
| Exchange | Kraken REST + WebSocket | Existing expertise from V2/V3 |
| Statistics | scipy.stats (validation only) | AP Stats formulas from scratch, scipy for test verification |
| Testing | pytest | Standard |
| Deployment | Laptop (trading) + Railway (dashboard) | Laptop-first eliminates container restart issues |
| Remote access | Tailscale | Secure access from school |
| Alerts | python-telegram-bot | Existing infrastructure |

---

## Success Criteria

1. **No module exceeds 500 lines** at any point in development
2. **All statistical methods are AP Stats-explainable** — the developer can teach every analytical decision to high school students
3. **2/3 belief consensus prevents at least one bad trade per week** (measured by tracking trades where one source disagreed and the disagreeing source was right)
4. **Reconciliation catches 100% of state divergences** within 5 minutes
5. **Zero ghost positions** survive more than one reconciliation cycle
6. **Grid trading generates positive P&L** in ranging regimes (measured by grid-specific accounting)
7. **DOGE accumulation is net positive** over rolling 30-day periods
8. **Kill switch recovery is clean** — bot restarts after Ctrl+C with zero manual intervention required beyond reviewing Telegram alerts
