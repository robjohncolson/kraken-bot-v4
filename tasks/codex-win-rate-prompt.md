# Codex Prompt: Win Rate Improvement (Phase 6)

**Repo**: kraken-bot-v4, branch `master`
**Context**: Bot is live, 628 tests. Zero child trades have occurred — structural blockers prevent children from spawning. This spec unblocks trading, adds signal quality filters, and wires Kelly sizing.
Full spec at `tasks/specs/win-rate-improvement.md`.

---

## Phase 6A: Unblock Child Spawning

### Task 1: Make MIN_CONFIDENCE env-configurable

**File**: `core/config.py` — search for `DEFAULT_ROTATION_MAX_CHILDREN`

Add near the other rotation defaults:
```python
DEFAULT_ROTATION_MIN_CONFIDENCE = 0.65
```

Add to `Settings` dataclass (after `rotation_max_children_per_parent`):
```python
rotation_min_confidence: float
```

Add to `load_settings()` (after the `rotation_max_children_per_parent` read):
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

**File**: `trading/rotation_tree.py` — search for `def compute_child_allocations`

Add `min_confidence: float = MIN_CONFIDENCE` parameter to the function signature. Change the scoring line from:
```python
raw = max(0.0, c.confidence - MIN_CONFIDENCE) ** 2
```
to:
```python
raw = max(0.0, c.confidence - min_confidence) ** 2
```

**File**: `trading/rotation_planner.py` — search for `compute_child_allocations(`

Add `min_confidence=self._settings.rotation_min_confidence` to the call.

**Tests**: Add to `tests/trading/test_rotation_tree.py`:
- `test_compute_allocations_accepts_with_lower_threshold`: `min_confidence=0.65`, candidate conf=0.67 → 1 allocation
- `test_compute_allocations_rejects_below_custom_threshold`: `min_confidence=0.65`, candidate conf=0.60 → 0 allocations
- IMPORTANT: existing test `test_compute_allocations_rejects_four_of_six_confidence` must still pass (uses default min_confidence=0.70)

---

### Task 2: Dynamic max_children based on budget

**File**: `trading/rotation_planner.py` — search for `max_children = self._settings.rotation_max_children_per_parent`

Replace that single line with:
```python
configured_max = self._settings.rotation_max_children_per_parent
min_pos = Decimal(str(self._settings.min_position_usd))
deployable = leaf.quantity_free * PARENT_DEPLOY_RATIO
max_by_budget = max(1, int(deployable / min_pos)) if min_pos > 0 else configured_max
max_children = min(configured_max, max_by_budget)
```

This requires importing `PARENT_DEPLOY_RATIO` from `trading.rotation_tree` at the top of the file. Check if it's already imported — if not, add it to the existing import line.

**Logic**: EUR root with $14.62 free → `deployable = 14.62 * 0.80 = 11.70` → `max_by_budget = int(11.70 / 10) = 1` → one child gets the full budget.

**Tests**: Add to `tests/trading/test_rotation_planner.py`:
- `test_plan_cycle_dynamic_max_children_small_budget`: root with $15 free, `MIN_POSITION_USD=10`, `MAX_CHILDREN=3` → verify only 1 child planned
- `test_plan_cycle_dynamic_max_children_large_budget`: root with $100 free → verify up to 3 children planned

---

## Phase 6B: Volume & Spread Filters

### Task 3: Add scanner config fields

**File**: `core/config.py` — search for `DEFAULT_SCANNER_TIMEOUT_SEC`

Add nearby:
```python
DEFAULT_SCANNER_MIN_24H_VOLUME_USD = 50_000.0
DEFAULT_SCANNER_MAX_SPREAD_PCT = 2.0
```

Add to `Settings` (after `scanner_timeout_sec`):
```python
scanner_min_24h_volume_usd: float
scanner_max_spread_pct: float
```

Add to `load_settings()` (after the `scanner_timeout_sec` read):
```python
scanner_min_24h_volume_usd=_read_float(
    env, "SCANNER_MIN_24H_VOLUME_USD", DEFAULT_SCANNER_MIN_24H_VOLUME_USD,
),
scanner_max_spread_pct=_read_float(
    env, "SCANNER_MAX_SPREAD_PCT", DEFAULT_SCANNER_MAX_SPREAD_PCT,
),
```

### Task 4: Add volume/spread filters to pair scanner

**File**: `trading/pair_scanner.py` — search for `def _scan_rotation_pair`

After the OHLCV bars are fetched and validated (length check), BEFORE the TA analysis call, add:

```python
# Volume filter: 24h USD volume from hourly candles
close_f = bars["close"].astype(float)
vol_f = bars["volume"].astype(float)
recent_24 = min(24, len(bars))
usd_volume_24h = float((vol_f.iloc[-recent_24:] * close_f.iloc[-recent_24:]).sum())
if usd_volume_24h < self._settings.scanner_min_24h_volume_usd:
    logger.debug("Skipped %s: 24h vol $%.0f < $%.0f", pair, usd_volume_24h, self._settings.scanner_min_24h_volume_usd)
    return None

# Spread proxy: avg (high-low)/close over last 6 bars
high_f = bars["high"].astype(float)
low_f = bars["low"].astype(float)
recent_6 = min(6, len(bars))
spread_pct = float(((high_f.iloc[-recent_6:] - low_f.iloc[-recent_6:]) / close_f.iloc[-recent_6:]).mean() * 100)
if spread_pct > self._settings.scanner_max_spread_pct:
    logger.debug("Skipped %s: spread %.2f%% > %.2f%%", pair, spread_pct, self._settings.scanner_max_spread_pct)
    return None
```

Also apply the same filters to `_scan_pair` if it exists and follows the same pattern (search for `def _scan_pair`).

**Tests**: Add to `tests/trading/test_pair_scanner.py`:
- `test_scan_rotation_pair_rejects_low_volume`: OHLCV bars with volume=1.0/bar, close=4.0 → 24h vol ~$96 → rejected
- `test_scan_rotation_pair_rejects_wide_spread`: bars where high=close*1.05, low=close*0.95 → 10% spread → rejected
- `test_scan_rotation_pair_accepts_liquid_pair`: bars with volume=5000/bar, tight HL → passes through

---

## Phase 6C: Kelly Sizing Integration

### Task 5: Add child trade stats query

**File**: `persistence/sqlite.py` — search for `def insert_trade_outcome`

Add a new method on `SqliteWriter` (after `insert_trade_outcome`):

```python
def fetch_child_trade_stats(self, lookback_days: int = 90) -> tuple[int, int, Decimal]:
    """Return (wins, losses, avg_payoff_ratio) for child trades (depth > 0)."""
    try:
        cursor = self._conn.execute(
            "SELECT net_pnl FROM trade_outcomes "
            "WHERE node_depth > 0 "
            "AND julianday(closed_at) >= julianday('now', ?)",
            (f"-{lookback_days} days",),
        )
        rows = cursor.fetchall()
    except sqlite3.Error:
        return (0, 0, Decimal("1"))
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

### Task 6: Add kelly_cap to compute_child_allocations

**File**: `trading/rotation_tree.py` — search for `def compute_child_allocations`

Add `kelly_cap: Decimal | None = None` parameter. After the existing cap line:
```python
capped = min(target, parent.quantity_free * MAX_CHILD_RATIO)
```
Add:
```python
if kelly_cap is not None and kelly_cap > Decimal("0"):
    capped = min(capped, parent.quantity_free * kelly_cap)
```

### Task 7: Wire Kelly into planner

**File**: `core/config.py`

Add:
```python
DEFAULT_KELLY_MIN_SAMPLE_SIZE = 10
```

Add to `Settings`:
```python
kelly_min_sample_size: int
```

Add to `load_settings()`:
```python
kelly_min_sample_size=_read_int(env, "KELLY_MIN_SAMPLE_SIZE", DEFAULT_KELLY_MIN_SAMPLE_SIZE),
```

**File**: `trading/rotation_planner.py`

Add `db_writer=None` param to `__init__`:
```python
def __init__(self, *, settings, pair_scanner, pair_metadata=None, db_writer=None):
    ...
    self._db_writer = db_writer
```

Add import at top:
```python
from trading.sizing import bounded_kelly
```

Add method:
```python
def _kelly_fraction(self) -> Decimal | None:
    if self._db_writer is None:
        return None
    try:
        wins, losses, payoff = self._db_writer.fetch_child_trade_stats()
    except Exception:
        return None
    if wins + losses < self._settings.kelly_min_sample_size:
        return None
    return bounded_kelly(wins=wins, losses=losses, payoff_ratio=payoff)
```

In `plan_cycle()`, before the leaf loop, add:
```python
kelly_frac = self._kelly_fraction()
```

Pass to `compute_child_allocations`:
```python
allocations = compute_child_allocations(
    leaf,
    candidates,
    min_position=Decimal(str(self._settings.min_position_usd)),
    max_children=remaining_slots,
    min_confidence=self._settings.rotation_min_confidence,
    kelly_cap=kelly_frac,
)
```

**File**: `runtime_loop.py` — search for `RotationTreePlanner(`

Add `db_writer=self._writer` to the constructor call.

**Tests**:
- `test_compute_allocations_with_kelly_cap`: kelly_cap=Decimal("0.10"), parent $200, conf 0.83 → allocation ≤ $20
- `test_compute_allocations_kelly_cap_none_is_noop`: kelly_cap=None → normal sizing
- `test_fetch_child_trade_stats_filters_by_depth`: insert depth=0 and depth=1 outcomes → only depth>0 counted
- `test_fetch_child_trade_stats_computes_payoff_ratio`: known data → correct (wins, losses, ratio)
- `test_kelly_fraction_below_sample_gate`: 5 trades → returns None

---

## What exists already

- `trading/sizing.py:104-119` — `bounded_kelly()` fully implemented and tested
- `trading/sizing.py:122-142` — `size_position_usd()` converts fraction to USD amount
- `trading/rotation_tree.py:18-21` — `MIN_CONFIDENCE`, `PARENT_DEPLOY_RATIO`, `MAX_CHILD_RATIO` constants
- `exchange/ohlcv.py` — OHLCV bars already contain `volume`, `high`, `low` columns
- `persistence/sqlite.py` — `trade_outcomes` table already has `node_depth` column (added in Phase 5)

## Testing requirements

```bash
python -m pytest                    # all tests pass (currently 628)
python -m ruff check .              # clean (ignore pre-existing failures in beliefs/, research/, scripts/)
```

Minimum 13 new test functions across 3-4 test files.

## Do NOT change

- Root exit logic (`_handle_root_expiry`, `_close_rotation_node`) — working correctly
- `evaluate_root_ta()` in pair_scanner — root TA is separate from candidate TA
- The TA ensemble itself (`beliefs/technical_ensemble_source.py`) — signals stay the same
- `trade_outcomes` schema — already has `node_depth` from Phase 5
- LLM council integration — separate future work
