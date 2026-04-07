# Spec: Win Rate Improvement (Phase 6A/6B/6C)

**Date**: 2026-04-07
**Priority**: 1
**Status**: Spec

## Motivation

The bot has 7 trade outcomes — all root exits, zero child round-trips. The bot has never entered a chosen position because:
1. Small root budgets ($14-25) divided by `max_children=3` fall below `MIN_POSITION_USD` ($10)
2. `MIN_CONFIDENCE=0.70` is hardcoded, rejecting 4/6-signal candidates at 0.67 confidence
3. No volume/spread filters — noisy signals from illiquid pairs
4. Kelly sizing exists (`trading/sizing.py`) but isn't wired into the planner

These three phases are independently deployable. Phase 6A unblocks trading, 6B improves signal quality, 6C scales sizing with performance.

---

## Phase 6A: Unblock Child Spawning

### Change 1: Make MIN_CONFIDENCE env-configurable

**File**: `core/config.py`

Add constant:
```python
DEFAULT_ROTATION_MIN_CONFIDENCE = 0.65
```

Add to `Settings` dataclass (after `rotation_max_children_per_parent`):
```python
rotation_min_confidence: float
```

Add to `load_settings()`:
```python
rotation_min_confidence=_read_float(
    env, "ROTATION_MIN_CONFIDENCE", DEFAULT_ROTATION_MIN_CONFIDENCE,
),
```

Add validation in `validate_settings()`:
```python
if not (0.0 <= settings.rotation_min_confidence <= 1.0):
    warnings.append(
        f"ROTATION_MIN_CONFIDENCE={settings.rotation_min_confidence} must be in [0.0, 1.0]"
    )
```

**File**: `trading/rotation_tree.py`, function `compute_child_allocations()` (lines 95-133)

Add `min_confidence` parameter with default `MIN_CONFIDENCE` (0.70) for backwards compatibility:

```python
def compute_child_allocations(
    parent: RotationNode,
    candidates: tuple[RotationCandidate, ...],
    min_position: Decimal = Decimal("10"),
    max_children: int = 3,
    min_confidence: float = MIN_CONFIDENCE,  # NEW
) -> list[tuple[RotationCandidate, Decimal]]:
```

Change line 111:
```python
raw = max(0.0, c.confidence - min_confidence) ** 2  # was MIN_CONFIDENCE
```

**File**: `trading/rotation_planner.py`, line 133-138

Pass config value through:
```python
allocations = compute_child_allocations(
    leaf,
    candidates,
    min_position=Decimal(str(self._settings.min_position_usd)),
    max_children=remaining_slots,
    min_confidence=self._settings.rotation_min_confidence,  # NEW
)
```

### Change 2: Dynamic max_children based on budget

**File**: `trading/rotation_planner.py`, lines 97-110

Replace the fixed `max_children` with a budget-aware cap:

```python
# Current (line 98):
max_children = self._settings.rotation_max_children_per_parent

# New:
configured_max = self._settings.rotation_max_children_per_parent
min_pos = Decimal(str(self._settings.min_position_usd))
deployable = leaf.quantity_free * PARENT_DEPLOY_RATIO
max_by_budget = max(1, int(deployable / min_pos)) if min_pos > 0 else configured_max
max_children = min(configured_max, max_by_budget)
```

This requires importing `PARENT_DEPLOY_RATIO` from `trading.rotation_tree`. If a root has $14 free, `deployable = $14 * 0.80 = $11.20`, `max_by_budget = int(11.20 / 10) = 1`. One child gets the full budget instead of three undersized ones.

The existing `per_child_budget` check at lines 108-110 becomes a safety net.

### Tests for 6A

1. `test_compute_allocations_accepts_four_of_six_with_lower_threshold` — `min_confidence=0.65`, candidate at 0.67 → allocated
2. `test_compute_allocations_rejects_below_custom_threshold` — `min_confidence=0.65`, candidate at 0.60 → rejected
3. `test_plan_cycle_dynamic_max_children_small_budget` — root with $15 free, `MIN_POSITION_USD=10`, `MAX_CHILDREN=3` → only 1 child planned
4. `test_plan_cycle_uses_rotation_min_confidence` — root with $100, `MIN_CONFIDENCE=0.65`, candidate at 0.67 → child spawned
5. Existing `test_compute_allocations_rejects_four_of_six_confidence` must still pass (uses default 0.70)

---

## Phase 6B: Volume & Spread Filters

### Change 1: Add config fields

**File**: `core/config.py`

```python
DEFAULT_SCANNER_MIN_24H_VOLUME_USD = 50_000.0
DEFAULT_SCANNER_MAX_SPREAD_PCT = 2.0
```

Add to `Settings`:
```python
scanner_min_24h_volume_usd: float
scanner_max_spread_pct: float
```

Add to `load_settings()`:
```python
scanner_min_24h_volume_usd=_read_float(
    env, "SCANNER_MIN_24H_VOLUME_USD", DEFAULT_SCANNER_MIN_24H_VOLUME_USD,
),
scanner_max_spread_pct=_read_float(
    env, "SCANNER_MAX_SPREAD_PCT", DEFAULT_SCANNER_MAX_SPREAD_PCT,
),
```

### Change 2: Filter in _scan_rotation_pair

**File**: `trading/pair_scanner.py`, in `_scan_rotation_pair()` (after OHLCV fetch, before TA analysis)

Use existing OHLCV data — no extra API calls:

```python
# Volume filter: 24h USD volume from hourly candles
volume_series = bars["volume"].astype(float)
close_series = bars["close"].astype(float)
recent_24 = min(24, len(bars))
usd_volume_24h = float(
    (volume_series.iloc[-recent_24:] * close_series.iloc[-recent_24:]).sum()
)
if usd_volume_24h < self._settings.scanner_min_24h_volume_usd:
    return None

# Spread proxy: average (high - low) / close over last 6 bars
high_series = bars["high"].astype(float)
low_series = bars["low"].astype(float)
recent_6 = min(6, len(bars))
spread_pct = float(
    ((high_series.iloc[-recent_6:] - low_series.iloc[-recent_6:])
     / close_series.iloc[-recent_6:]).mean() * 100
)
if spread_pct > self._settings.scanner_max_spread_pct:
    return None
```

**Design rationale**: High-low range is a volatility proxy, not true bid-ask spread. But for filtering illiquid garbage pairs, it's effective — a pair with 5%+ hourly HL range isn't suitable for 5% take-profit targets. No extra HTTP calls needed.

### Tests for 6B

6. `test_scan_rotation_pair_rejects_low_volume` — bars with volume=1.0/bar → rejected
7. `test_scan_rotation_pair_rejects_wide_spread` — bars with 10% HL range → rejected
8. `test_scan_rotation_pair_accepts_liquid_tight_pair` — normal bars → passes through

---

## Phase 6C: Kelly Sizing Integration

### Change 1: Add child trade stats query

**File**: `persistence/sqlite.py`

New method on `SqliteWriter`:

```python
def fetch_child_trade_stats(self, lookback_days: int = 90) -> tuple[int, int, Decimal]:
    """Return (wins, losses, avg_payoff_ratio) for child trades (depth > 0)."""
    cursor = self._conn.execute(
        "SELECT net_pnl FROM trade_outcomes "
        "WHERE node_depth > 0 "
        "AND julianday(closed_at) >= julianday('now', ?)",
        (f"-{lookback_days} days",),
    )
    rows = cursor.fetchall()
    wins = losses = 0
    win_sum = loss_sum = Decimal("0")
    for row in rows:
        pnl = Decimal(str(row[0]))
        if pnl > 0:
            wins += 1
            win_sum += pnl
        else:
            losses += 1
            loss_sum += abs(pnl)
    if losses == 0 or wins == 0:
        return (wins, losses, Decimal("1"))
    return (wins, losses, (win_sum / wins) / (loss_sum / losses))
```

### Change 2: Add kelly_cap to compute_child_allocations

**File**: `trading/rotation_tree.py`

Add `kelly_cap: Decimal | None = None` parameter. After the existing capping at line 129:

```python
capped = min(target, parent.quantity_free * MAX_CHILD_RATIO)
if kelly_cap is not None and kelly_cap > Decimal("0"):
    capped = min(capped, parent.quantity_free * kelly_cap)
```

### Change 3: Wire into planner

**File**: `trading/rotation_planner.py`

Add `db_writer` param to `__init__`:
```python
def __init__(self, *, settings, pair_scanner, pair_metadata=None, db_writer=None):
    ...
    self._db_writer = db_writer
```

Add `_kelly_fraction()` method:
```python
def _kelly_fraction(self) -> Decimal | None:
    if self._db_writer is None:
        return None
    wins, losses, payoff = self._db_writer.fetch_child_trade_stats()
    if wins + losses < self._settings.kelly_min_sample_size:
        return None
    return bounded_kelly(wins=wins, losses=losses, payoff_ratio=payoff)
```

In `plan_cycle()`, before the leaf loop:
```python
kelly_frac = self._kelly_fraction()
```

Pass to `compute_child_allocations`:
```python
allocations = compute_child_allocations(
    ...,
    kelly_cap=kelly_frac,
)
```

**File**: `core/config.py`

```python
DEFAULT_KELLY_MIN_SAMPLE_SIZE = 10
```

Add `kelly_min_sample_size: int` to Settings + `load_settings()`.

**File**: `runtime_loop.py`

Pass `db_writer=self._writer` when constructing `RotationTreePlanner`.

### Tests for 6C

9. `test_compute_allocations_with_kelly_cap` — Kelly cap=0.10, parent $200 → allocation ≤ $20
10. `test_compute_allocations_kelly_cap_none_is_noop` — kelly_cap=None → normal sizing
11. `test_fetch_child_trade_stats_filters_by_depth` — mix of depth=0 and depth=1 → only depth>0 counted
12. `test_fetch_child_trade_stats_computes_payoff_ratio` — known wins/losses → correct ratio
13. `test_kelly_fraction_below_sample_gate` — fewer than 10 trades → returns None → flat sizing

---

## Backwards Compatibility

- All new params have defaults matching current behavior (`min_confidence=0.70`, `kelly_cap=None`)
- Existing test `test_compute_allocations_rejects_four_of_six_confidence` still passes (uses default)
- Volume/spread filters only add early returns — can't break existing liquid pairs
- Kelly is a no-op until 10+ child trades accumulate
- Root exit logic is untouched

## Success Criteria

- [ ] Children spawn for roots with $14+ free capital (dynamic max_children)
- [ ] 4/6 TA signal candidates (0.67) pass with ROTATION_MIN_CONFIDENCE=0.65
- [ ] Low-volume (<$50K/24h) and wide-spread (>2%) pairs rejected before TA
- [ ] Kelly sizing activates after 10 child trades, caps position size
- [ ] All 628+ tests pass, ruff clean
