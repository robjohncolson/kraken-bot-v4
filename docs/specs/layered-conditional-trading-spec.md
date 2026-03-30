# Layered Conditional Trading Spec

## Purpose

Define a staged design for conditional capital rotation when the bot turns bearish on `DOGE/USD`.

The intended behavior is:

1. sell DOGE inventory when `DOGE/USD` turns bearish
2. estimate how long that bearish window is likely to last
3. scan Kraken USD pairs for bullish opportunities
4. rotate into the best bullish candidate only if its likely payoff window fits inside the DOGE bear window
5. exit that temporary trade before the DOGE bearish window is expected to close

This must preserve the current architecture:

- pure reducer in `core/state_machine.py`
- runtime/scheduler separation in `runtime_loop.py` and `scheduler.py`
- frozen dataclasses
- fail-closed behavior when market context is missing

## Problem Statement

The current system can already do the first part of the trade tree, but not the rest.

Today:

- `runtime_loop.py` polls beliefs for `settings.allowed_pairs`
- `core/state_machine.py` requires a `reference_price` before any belief-driven entry can size or place an order
- `core/state_machine.py` already contains the DOGE-specific bearish inventory-sell path via `_bearish_inventory_sell(...)`

What is missing is the conditional branch after DOGE turns bearish:

- there is no duration estimate for the DOGE bear window
- there is no rate-limit-aware market scan across Kraken USD pairs
- there is no orchestration layer that chooses a temporary bullish destination for USD
- there is no time-window-aware exit path that closes the temporary trade before the DOGE bearish thesis expires

## Current Relevant State

### Runtime

`runtime_loop.py` already performs the cold-start belief polling loop:

- `_maybe_poll_beliefs()` iterates `settings.allowed_pairs`
- `_ensure_subscriptions()` must subscribe pairs early enough for `reference_prices` to exist before the reducer handles beliefs

This is the dependency described in `docs/specs/reference-price-cold-start-fix.md`.

### Reducer

`core/state_machine.py` already has the core DOGE bearish branch:

- bullish consensus opens a normal position via `_bullish_position_entry(...)`
- bearish `DOGE/USD` consensus routes to `_bearish_inventory_sell(...)`
- missing `reference_prices` causes the reducer to fail closed with `belief_update: no reference price for {pair}`

That means Layer 3 should reuse the existing bullish-entry path where possible, not invent a second order-placement path.

### Belief Generation

The existing quantitative belief path already computes:

- EMA crossover
- RSI above/below 50
- MACD histogram polarity

via `beliefs/technical_ensemble_source.py`.

That makes the proposed duration estimator a natural companion to the existing technical belief logic.

## Goals

- Keep Layer 0 minimal and foundation-only.
- Make Layer 1 a pure function over OHLCV data.
- Make Layer 2 bounded, deterministic, and rate-limit-aware.
- Keep Layer 3 as orchestration, not a second reducer.
- Reuse the current bullish entry path instead of bypassing the reducer.
- Add a time-window exit path so rotated capital does not outlive the DOGE bear thesis.
- Ship the system in layers so each layer is independently testable and useful.

## Non-Goals

- No all-pairs LLM scan with Claude or Codex. Market-wide scanning must stay local and deterministic.
- No replacement of the current consensus/reducer/runtime split.
- No attempt to predict exact tops or bottoms. The first version should use conservative window estimates.
- No unlimited pair universe. Layer 2 is Kraken spot USD pairs only.
- No market-order rotation path beyond the existing capital-preservation exceptions.

## Product Decision

Implement the feature as four explicit layers.

Layers 1 and 2 are reusable building blocks.
Layer 3 composes them into a conditional rotation tree.
Layer 0 is the non-negotiable prerequisite because the reducer cannot trade without `reference_prices`.

## Layer 0: Reference Price Cold-Start Fix

Layer 0 is already specified in [docs/specs/reference-price-cold-start-fix.md](/mnt/c/Users/rober/Downloads/Projects/kraken-bot-v4/docs/specs/reference-price-cold-start-fix.md).

This layer remains the foundation:

- `_ensure_subscriptions()` in `runtime_loop.py` must subscribe `settings.allowed_pairs` from cold start
- price ticks must begin before the first belief-driven reducer path needs a reference price
- the DOGE bearish sell path and any later rotated bullish entry both depend on this

Scope remains the same:

- approximately 3 lines in `runtime_loop.py`
- startup verification only

## Layer 1: Bear Duration Estimation

### Module

- `trading/duration_estimator.py`

### Responsibility

Take recent OHLCV bars for `DOGE/USD` and return a conservative estimate of how long the current bearish phase is likely to remain actionable.

### Contract

Primary API:

```python
def estimate_bear_duration(bars: pd.DataFrame) -> DurationEstimate:
    ...
```

`DurationEstimate` should be a frozen dataclass. If it will be shared across runtime/scheduler/trading boundaries, it belongs in `core/types.py`. If it stays local to Layer 1 only, it may live beside the estimator.

Minimum required field:

```python
@dataclass(frozen=True, slots=True)
class DurationEstimate:
    estimated_bear_hours: int
```

Recommended first version:

```python
@dataclass(frozen=True, slots=True)
class DurationEstimate:
    estimated_bear_hours: int
    confidence: float
    macd_bearish: bool
    rsi_bearish: bool
    ema_bearish: bool
```

### Signal Basis

Layer 1 should reuse the same indicator family already present in `beliefs/technical_ensemble_source.py`:

- MACD
- RSI
- EMA crossover

The goal is not a second trading model. The goal is a duration heuristic that answers:

> "If DOGE is bearish now, how many hours do we expect the bearish window to remain open before the thesis becomes stale?"

### First-Cut Heuristic

The first version should stay bucketed and conservative:

- 0 bearish confirmations: `0h` and abort the conditional tree
- 1 bearish confirmation: `6h`
- 2 bearish confirmations: `12h`
- 3 bearish confirmations: `24h`

Optional refinement:

- extend by one bucket if MACD histogram is still getting more negative
- reduce by one bucket if RSI is already deeply oversold and mean reversion risk is high

This keeps the model explainable and easy to test.

### Fail-Closed Rules

If OHLCV is malformed, insufficient, or non-numeric:

- return no estimate through a typed failure path
- do not guess
- do not proceed to Layer 3

## Layer 2: Multi-Pair Scanner

### Module

- `trading/pair_scanner.py`

### Responsibility

Discover Kraken spot USD pairs, generate deterministic beliefs for each, and rank bullish candidates without violating rate limits.

### Why This Must Be Quantitative

Layer 2 must not use the full Claude/Codex belief pipeline for every pair.

Reasons:

- too slow for all-pairs scanning
- too expensive in cognitive/runtime terms
- too variable for repeatable ranking

Layer 2 should use a local quantitative path:

- `beliefs/technical_ensemble_source.py`
- or the active research artifact path if it is local, deterministic, and cheap enough

The first implementation should default to the fixed technical ensemble.

### Required Responsibilities

1. fetch Kraken USD pairs
2. normalize symbols to the repo's `BASE/USD` form
3. filter out non-spot or otherwise unsuitable pairs
4. fetch OHLCV for each candidate pair
5. generate a belief for each pair
6. keep only bullish candidates
7. rank them by descending confidence
8. expose enough metadata for Layer 3 to decide whether the opportunity fits inside the DOGE bear window

### Scanner Output

Layer 2 should return structured results, not just pair strings.

Recommended contract:

```python
@dataclass(frozen=True, slots=True)
class BullCandidate:
    pair: str
    belief: BeliefSnapshot
    confidence: float
    reference_price_hint: Decimal
    estimated_peak_hours: int
```

```python
def scan_bull_candidates(...) -> tuple[BullCandidate, ...]:
    ...
```

`estimated_peak_hours` is necessary because Layer 3 must select candidates that are likely to mature before the DOGE bear window closes.

The first version may derive `estimated_peak_hours` from the same MACD/RSI/EMA family as Layer 1, but inverted for bullish continuation. Ranking remains confidence-first.

### Rate-Limit Rules

The current codebase already encodes starter-tier Kraken rate-limit assumptions in `exchange/client.py`.

Layer 2 must behave like a good citizen even if it uses Kraken public endpoints:

- cache pair discovery for a TTL instead of refetching every cycle
- bound scan concurrency
- prefer sequential or very small-batch OHLCV fetches
- run only on demand for Layer 3 or on a slow cadence, not every scheduler cycle
- abort the scan cleanly if the rate-limit budget is exhausted

### Fail-Closed Rules

If pair discovery fails, OHLCV is unavailable, or the scan times out:

- return no candidates
- leave capital in USD
- do not open fallback trades

## Layer 3: Conditional Trade Tree

### Responsibility

Compose the first three layers into one conditional path:

1. DOGE turns bearish
2. DOGE inventory is sold to USD
3. DOGE bear duration is estimated
4. the scanner finds bullish USD-pair opportunities
5. the best candidate is entered only if its likely payoff window fits inside the DOGE bear window
6. the temporary position is exited before the DOGE bear thesis is expected to expire

### Recommended Module Boundary

Primary orchestration should live in a dedicated trading-side module, for example:

- `trading/conditional_tree.py`

`runtime_loop.py` should trigger and coordinate it.
`core/state_machine.py` should remain the place where entries and exits become canonical reducer actions.

### Trigger

The conditional tree should start only when all of the following are true:

- DOGE consensus is bearish
- Layer 0 guarantees a reference price exists
- DOGE inventory sell is either filled or enough free USD already exists
- no other conditional rotation is active

The planner may begin on bearish DOGE belief arrival, but trade entry should wait until capital is actually available in USD.

### Candidate Selection Rule

Choose the highest-confidence bullish candidate satisfying:

- pair is not `DOGE/USD`
- confidence is above a configured floor
- `estimated_peak_hours <= estimated_bear_hours`
- risk rules still pass
- position sizing still passes

Tie-breakers:

1. higher confidence
2. shorter `estimated_peak_hours`
3. deterministic symbol sort

If no candidate satisfies the window constraint, stay in USD.

### Reference Price Constraint

Layer 3 must respect a current reducer invariant:

> bullish entry fails closed if `state.reference_prices` lacks the candidate pair

That means the conditional tree cannot simply enqueue a belief for a new pair and hope the reducer can trade it.

The orchestration layer must do one of the following before enqueuing the bullish candidate:

- subscribe the candidate pair and wait for the first price tick
- or seed `current_prices[candidate_pair]` with a provisional `reference_price_hint` from Layer 2, then subscribe immediately for live ticks

The second option is the lower-latency first cut.

### Exit Before Window Close

This is the main new behavior missing from the current core.

The rotated bullish position needs a hard deadline:

- `exit_deadline = opened_at + min(estimated_peak_hours, estimated_bear_hours)`

When that deadline is reached:

- stop treating the rotated position as valid
- emit a time-window exit event
- place the same style of limit exit attempt the system already uses for stop/target exits

The cleanest architectural fit is:

- add `expires_at` or equivalent expiry metadata to the opened position or to scheduler-side conditional state
- extend `Guardian` with a `WINDOW_EXPIRED` action
- map that to a reducer event and close reason

### One-Tree Rule

Only one conditional rotation tree should be active at a time.

The first version should not chain:

- DOGE bearish -> pair A -> pair B -> pair C

It is a single temporary branch:

- DOGE bearish -> best bullish USD pair -> back to cash / normal regime evaluation

## Proposed Runtime Flow

1. Startup uses Layer 0 so `DOGE/USD` receives price ticks from cold start.
2. The runtime polls beliefs for `DOGE/USD`.
3. `core/state_machine.py` handles bearish DOGE belief via `_bearish_inventory_sell(...)`.
4. Once DOGE has sold to USD, Layer 3 launches a conditional planning task.
5. Layer 1 estimates `estimated_bear_hours` from recent `DOGE/USD` OHLCV.
6. Layer 2 scans Kraken USD pairs and returns ranked bullish candidates.
7. Layer 3 filters out candidates whose `estimated_peak_hours` exceed the DOGE bear window.
8. The top valid candidate seeds a provisional reference price, is subscribed for live ticks, and is enqueued through the existing bullish belief-entry path.
9. The opened rotated position receives an exit deadline.
10. Guardian or scheduler closes that position before the deadline window ends.
11. After exit, the system returns to the normal belief-driven regime.

## Suggested Config Surface

All new behavior should be behind explicit flags at first.

Recommended additions:

- `ENABLE_CONDITIONAL_TREE=false`
- `SCANNER_CONFIDENCE_FLOOR`
- `SCANNER_PAIR_DISCOVERY_TTL_SEC`
- `SCANNER_MAX_CONCURRENCY`
- `SCANNER_TIMEOUT_SEC`
- `CONDITIONAL_WINDOW_BUFFER_HOURS`

Layers 1 and 2 can ship dark before Layer 3 is enabled live.

## Acceptance Criteria

### Layer 0

- Startup subscribes `allowed_pairs` before the first belief-driven reducer entry attempt.
- `belief_update: no reference price for DOGE/USD` no longer blocks cold-start DOGE bearish logic.

### Layer 1

- A pure OHLCV input returns a deterministic `DurationEstimate`.
- Synthetic bearish, mixed, and bullish OHLCV fixtures produce expected buckets.
- Invalid or short input fails closed.

### Layer 2

- Scanner returns only normalized Kraken USD spot pairs.
- Scanner ranks bullish candidates by confidence deterministically.
- Scanner respects bounded concurrency and does not run every runtime cycle.
- Scan failure results in no trade, not a fallback guess.

### Layer 3

- DOGE bearish sell still goes through the existing reducer path.
- Rotation only occurs after capital is available in USD.
- Candidate entry is blocked if the pair lacks a usable reference price.
- No candidate is entered when `estimated_peak_hours > estimated_bear_hours`.
- The rotated position is exited before the conditional window closes.

## Fractal Task Sizing

Classification rule:

- `COMPOSITE`: still needs decomposition into smaller implementation tasks
- `ATOMIC`: can be implemented and verified in one bounded change without hidden substeps

| ID | Parent | Task | Class | Files |
|----|--------|------|-------|-------|
| `L0` | — | Establish cold-start reference-price foundation | `COMPOSITE` | `runtime_loop.py`, docs |
| `L0.1` | `L0` | Union `settings.allowed_pairs` into `_ensure_subscriptions()` | `ATOMIC` | `runtime_loop.py` |
| `L0.2` | `L0` | Verify startup ticker subscription and first reference price arrival | `ATOMIC` | smoke test |
| `L1` | — | Add bear duration estimation layer | `COMPOSITE` | `trading/duration_estimator.py`, `core/types.py`, tests |
| `L1.1` | `L1` | Add `DurationEstimate` frozen dataclass contract | `ATOMIC` | `core/types.py` or `trading/duration_estimator.py` |
| `L1.2` | `L1` | Validate/coerce OHLCV input columns and minimum bar count | `ATOMIC` | `trading/duration_estimator.py` |
| `L1.3` | `L1` | Compute EMA, RSI, and MACD bearish signal flags | `ATOMIC` | `trading/duration_estimator.py` |
| `L1.4` | `L1` | Map signal strength into conservative `estimated_bear_hours` buckets | `ATOMIC` | `trading/duration_estimator.py` |
| `L1.5` | `L1` | Add unit tests for bearish, mixed, oversold, and invalid-input cases | `ATOMIC` | `tests/trading/` |
| `L2` | — | Add multi-pair bull scanner | `COMPOSITE` | `trading/pair_scanner.py`, exchange helpers, tests |
| `L2.1` | `L2` | Discover and normalize Kraken USD spot pairs | `ATOMIC` | `trading/pair_scanner.py`, optional `exchange/` helper |
| `L2.2` | `L2` | Add pair-discovery caching and scan pacing knobs | `ATOMIC` | `trading/pair_scanner.py`, config |
| `L2.3` | `L2` | Fetch OHLCV per pair under bounded concurrency | `ATOMIC` | `trading/pair_scanner.py`, `exchange/ohlcv.py` |
| `L2.4` | `L2` | Generate deterministic beliefs for each scanned pair | `ATOMIC` | `trading/pair_scanner.py`, `beliefs/technical_ensemble_source.py` |
| `L2.5` | `L2` | Derive `estimated_peak_hours` metadata for bullish candidates | `ATOMIC` | `trading/pair_scanner.py` |
| `L2.6` | `L2` | Rank, filter, and return bullish candidates deterministically | `ATOMIC` | `trading/pair_scanner.py` |
| `L2.7` | `L2` | Add scanner tests for filtering, ranking, and timeout/failure behavior | `ATOMIC` | `tests/trading/` |
| `L3` | — | Add conditional trade tree orchestration | `COMPOSITE` | `trading/conditional_tree.py`, `runtime_loop.py`, `scheduler.py`, `guardian.py`, `core/state_machine.py`, tests |
| `L3.1` | `L3` | Add conditional-tree coordinator state and feature flag gating | `ATOMIC` | `runtime_loop.py`, config, optional scheduler state |
| `L3.2` | `L3` | Trigger planning when DOGE turns bearish and USD becomes available | `ATOMIC` | `runtime_loop.py`, `scheduler.py` |
| `L3.3` | `L3` | Call Layer 1 to estimate DOGE bear duration | `ATOMIC` | `trading/conditional_tree.py`, `trading/duration_estimator.py` |
| `L3.4` | `L3` | Call Layer 2 and filter candidates to those fitting the bear window | `ATOMIC` | `trading/conditional_tree.py`, `trading/pair_scanner.py` |
| `L3.5` | `L3` | Seed candidate reference price and subscribe pair before belief enqueue | `ATOMIC` | `runtime_loop.py`, `trading/conditional_tree.py` |
| `L3.6` | `L3` | Reuse existing bullish belief-entry path to open the chosen candidate | `ATOMIC` | `runtime_loop.py`, `core/state_machine.py` |
| `L3.7` | `L3` | Add time-window expiry handling for rotated positions | `COMPOSITE` | `core/types.py`, `guardian.py`, `scheduler.py`, `core/state_machine.py` |
| `L3.7.1` | `L3.7` | Add expiry metadata to position or scheduler-side conditional state | `ATOMIC` | `core/types.py` or `scheduler.py` |
| `L3.7.2` | `L3.7` | Extend guardian/scheduler with `WINDOW_EXPIRED` detection | `ATOMIC` | `guardian.py`, `scheduler.py` |
| `L3.7.3` | `L3.7` | Add reducer close path for time-window expiry | `ATOMIC` | `core/state_machine.py`, `trading/position.py` |
| `L3.8` | `L3` | Add end-to-end tests for DOGE bearish rotation, no-fit fallback, and expiry exit | `ATOMIC` | `tests/integration/`, `tests/trading/` |

## Dependency Edges

| From | To | Why |
|------|----|-----|
| `L0.1` | `L0.2` | Verify the exact cold-start subscription change before building on it |
| `L0` | `L3.5` | Candidate entries still depend on reducer-visible reference prices |
| `L1.1` | `L1.2` | Input validation needs the duration contract to exist |
| `L1.2` | `L1.3` | Indicator logic assumes validated OHLCV input |
| `L1.3` | `L1.4` | Duration buckets depend on computed signal flags |
| `L1.4` | `L1.5` | Tests should lock the finished heuristic, not a partial one |
| `L2.1` | `L2.2` | Caching and pacing attach to the discovered pair universe |
| `L2.2` | `L2.3` | Scan execution policy must exist before OHLCV fan-out |
| `L2.3` | `L2.4` | Beliefs require fetched bars |
| `L2.4` | `L2.5` | Peak-window metadata depends on computed signal state |
| `L2.5` | `L2.6` | Ranking needs the full candidate metadata set |
| `L2.6` | `L2.7` | Tests should cover the final scanner output contract |
| `L1.4` | `L3.3` | Layer 3 consumes the bear-duration heuristic |
| `L2.6` | `L3.4` | Layer 3 consumes ranked bull candidates |
| `L3.1` | `L3.2` | Planner cannot trigger before coordinator state and flags exist |
| `L3.2` | `L3.3` | The DOGE bearish trigger initiates duration estimation |
| `L3.3` | `L3.4` | Candidate filtering needs the bear-window estimate first |
| `L3.4` | `L3.5` | Only the chosen candidate should receive provisional pricing/subscription |
| `L3.5` | `L3.6` | Reducer entry requires the candidate pair to have a reference price |
| `L3.7.1` | `L3.7.2` | Expiry cannot be detected until it is stored somewhere canonical |
| `L3.7.2` | `L3.7.3` | Detection must exist before reducer close handling can consume it |
| `L3.6` | `L3.8` | End-to-end tests require a complete entry path |
| `L3.7.3` | `L3.8` | End-to-end tests also require the expiry exit path |

## Rollout Recommendation

Ship this in order:

1. Layer 0 if not already merged
2. Layer 1 with unit tests
3. Layer 2 with scanner tests, but dark
4. Layer 3 behind `ENABLE_CONDITIONAL_TREE=false`
5. shadow/paper validation before any live mutation mode is enabled

The key constraint is architectural discipline:

- Layer 1 and Layer 2 compute
- Layer 3 orchestrates
- the reducer remains the canonical place where trades become state transitions
