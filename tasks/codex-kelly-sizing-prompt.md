# Codex Prompt: Kelly Sizing Integration (Phase 2.4)

**Repo**: kraken-bot-v4, branch `master`
**Context**: Phases 1-4 of the post-mortem recovery are shipped. The `trade_outcomes` table now captures every completed trade. The Kelly sizing infrastructure exists in `trading/sizing.py` (lines 91-142) but is never called — `MAX_POSITION_USD` was hardcoded to 10 (now 50). The rotation planner uses confidence-weighted allocation but doesn't consider win rate or payoff ratio.

---

## What exists already

**File**: `trading/sizing.py`

```python
def kelly_fraction(win_probability, payoff_ratio):
    """Pure Kelly criterion."""
    loss_probability = 1 - win_probability
    fraction = win_probability - (loss_probability / payoff_ratio)
    return max(fraction, 0)

def bounded_kelly(wins, losses, payoff_ratio, confidence_level=0.90):
    """Kelly with confidence interval — uses lower bound of win prob."""
    point_estimate = wins / (wins + losses)
    lower_bound = point_estimate - z_score * sqrt(variance)
    return min(
        kelly_fraction(point_estimate, payoff_ratio),
        kelly_fraction(lower_bound, payoff_ratio)
    )

def size_position_usd(portfolio_value_usd, kelly_fraction_value,
                       min_position_usd, max_position_usd):
    """Apply Kelly fraction to portfolio value, clamp to min/max."""
    raw_size = portfolio_value_usd * kelly_fraction_value
    if raw_size < min_position_usd:
        return min_position_usd
    return min(raw_size, max_position_usd)
```

**File**: `trading/rotation_tree.py` — `compute_child_allocations` (line ~90)

Currently uses confidence-weighted allocation:
```python
score(c) = max(0, c.confidence - MIN_CONFIDENCE) ** 2
weight(c) = score(c) / sum_scores
allocation(c) = min(allocatable * weight(c), parent.quantity_free * MAX_CHILD_RATIO)
```

This ignores historical performance entirely.

**File**: `persistence/sqlite.py` — `SqliteReader.fetch_trade_outcomes(lookback_days=30)`

Returns all `trade_outcomes` rows from the last N days.

---

## Task: Wire Kelly sizing into the allocation pipeline

### Step 1: Add trade stats function

**File**: New function in `trading/sizing.py` (or new `trading/trade_stats.py` — your call)

```python
def compute_trade_stats(trade_outcomes: Sequence[sqlite3.Row]) -> TradeStats:
    """Compute win/loss stats from trade outcome rows."""
```

Returns a dataclass:
```python
@dataclass(frozen=True)
class TradeStats:
    win_count: int
    loss_count: int
    avg_win: Decimal       # average net_pnl for winning trades
    avg_loss: Decimal      # average abs(net_pnl) for losing trades
    win_rate: float        # win_count / total
    payoff_ratio: float    # avg_win / avg_loss
    expectancy: Decimal    # win_rate * avg_win - loss_rate * avg_loss
    sample_size: int       # total trades
```

A trade is a "win" if `net_pnl > 0`, "loss" if `net_pnl <= 0`.

Handle edge cases:
- 0 trades → return zeroed stats with sample_size=0
- All wins or all losses → payoff_ratio defaults to 1.0

### Step 2: Add Kelly-aware allocation to rotation planner

**File**: `trading/rotation_planner.py` — `plan_cycle` method

Before calling `compute_child_allocations`, compute the Kelly fraction:

```python
# Fetch recent trade stats
outcomes = self._reader.fetch_trade_outcomes(lookback_days=30)
stats = compute_trade_stats(outcomes)

if stats.sample_size >= MIN_KELLY_SAMPLE_SIZE:  # e.g., 10
    kelly_f = bounded_kelly(
        wins=stats.win_count,
        losses=stats.loss_count,
        payoff_ratio=stats.payoff_ratio,
    )
    max_alloc_usd = size_position_usd(
        portfolio_value_usd=portfolio_value,
        kelly_fraction_value=kelly_f,
        min_position_usd=settings.min_position_usd,
        max_position_usd=settings.max_position_usd,
    )
else:
    # Not enough data — use flat allocation
    max_alloc_usd = settings.max_position_usd
```

Then pass `max_alloc_usd` into the allocation as a per-child cap.

**Important**: The rotation planner needs access to `SqliteReader` to fetch trade outcomes. Check how the planner is currently constructed in `runtime_loop.py` — it may already have a reader, or you may need to thread one through.

### Step 3: Integrate per-child Kelly cap into `compute_child_allocations`

**File**: `trading/rotation_tree.py` — `compute_child_allocations`

Add an optional `max_child_usd: Decimal | None = None` parameter. If provided, cap each child allocation at this value (in addition to the existing `MAX_CHILD_RATIO` cap):

```python
capped = min(target, parent.quantity_free * MAX_CHILD_RATIO)
if max_child_usd is not None:
    capped = min(capped, max_child_usd)
```

This requires converting the USD-denominated Kelly cap to the parent's denomination. If the parent is USD, it's direct. If the parent is BTC, you need the USD price of BTC to convert. The planner should handle this conversion before passing to the allocator.

### Step 4: Add MIN_KELLY_SAMPLE_SIZE config

**File**: `core/config.py`

- Default: `DEFAULT_MIN_KELLY_SAMPLE_SIZE = 10`
- Settings field: `min_kelly_sample_size: int`
- Env var: `MIN_KELLY_SAMPLE_SIZE`

Below this threshold, Kelly sizing is bypassed and flat `MAX_POSITION_USD` is used.

---

## Testing requirements

- Unit test `compute_trade_stats` with mock trade outcome rows (all wins, all losses, mixed, empty)
- Unit test Kelly integration: mock 20 trades with 60% win rate, 2:1 payoff → verify Kelly fraction ~0.40, position size = portfolio * 0.40 capped at MAX_POSITION_USD
- Unit test below-threshold fallback: 5 trades → verify flat MAX_POSITION_USD used
- Integration test: verify `plan_cycle` produces different allocations with different trade histories
- Run full suite: `python -m pytest tests/ -x -q` (currently 624 passing)
- `ruff check` and `ruff format` on modified files

## Do NOT change

- The existing `compute_child_allocations` confidence-weighting logic — Kelly caps sit on top of it
- TP/SL settings (Phase 2, already shipped)
- Signal quality (Phase 3, already shipped)
- `.env` (config defaults go in `core/config.py`)
