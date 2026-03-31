# Recursive Rotation Tree Spec

## Vision

Transform the bot from a single-pair DOGE/USD trader into a **denomination-agnostic, movement-sensitive recursive trading system**. Whatever assets are in the portfolio form the root nodes. Each held asset scans all its Kraken pairs for bearish exits and bullish entries. Rotations create child nodes with timers, and children can recursively scan for sub-rotations within their windows.

## Example

```
Portfolio: [4651 DOGE, $80 USD]

root:DOGE (4651 DOGE, no deadline)
├── scan DOGE/USD → bearish (conf 0.62) → sell 80% DOGE for USD
│   └── child:USD ($298, deadline 12h)
│       ├── scan ETH/USD → bullish (conf 0.71) → buy ETH with 60%
│       │   └── child:ETH (0.05 ETH, deadline 6h)
│       │       └── timer expires → sell ETH back to USD → returns to parent
│       └── scan SOL/USD → bullish (conf 0.58) → buy SOL with 30%
│           └── child:SOL (0.6 SOL, deadline 8h)
├── scan DOGE/BTC → neutral → hold
└── scan DOGE/ETH → bullish → hold (DOGE gaining vs ETH)

root:USD ($80, no deadline — root nodes have no timer)
├── scan BTC/USD → neutral → skip
└── scan ETH/USD → bullish (conf 0.71) → same candidate as above
```

## Data Model (designed with Codex)

### RotationNodeStatus

```python
class RotationNodeStatus(StrEnum):
    PLANNED = "planned"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
```

### RotationNode (tree node)

```python
@dataclass(frozen=True, slots=True)
class RotationNode:
    node_id: str
    parent_node_id: str | None      # None = root holding
    depth: int                       # 0 = root, 1 = first rotation, 2 = max

    asset: str                       # What this node currently holds
    quantity_total: Decimal           # Total controlled
    quantity_free: Decimal            # Available for child rotations
    quantity_reserved: Decimal        # Committed to pending entries

    entry_pair: Pair | None          # How we got here (e.g., "ETH/USD")
    from_asset: str | None           # What we rotated from
    order_side: OrderSide | None     # BUY or SELL to enter
    entry_price: Price | None

    position_id: str | None          # Link to runtime Position
    opened_at: datetime | None
    deadline_at: datetime | None     # Hard stop — must exit by this time
    window_hours: float | None       # Original estimated window
    confidence: float                # Entry confidence

    status: RotationNodeStatus
```

**Invariants**: `quantity_free + quantity_reserved <= quantity_total`

### RotationCandidate (replaces BullCandidate)

```python
@dataclass(frozen=True, slots=True)
class RotationCandidate:
    pair: Pair
    from_asset: str                  # Source asset (what we're selling)
    to_asset: str                    # Destination asset (what we're buying)
    order_side: OrderSide            # BUY or SELL on this pair
    confidence: float
    reference_price_hint: Price
    estimated_window_hours: float
    depth: int = 0
```

### RotationTreeState (flat, replaces ConditionalTreeState)

```python
@dataclass(frozen=True, slots=True)
class RotationTreeState:
    nodes: tuple[RotationNode, ...]
    root_node_ids: tuple[str, ...]
    pending_entries: tuple[RotationCandidate, ...]
    pending_exit_node_ids: tuple[str, ...]
    max_depth: int = 2
    last_planned_at: datetime | None = None
```

**Flat by design**: easier reducer diffs, persistence, restart rehydration, and reconciliation.

## Confidence-to-Sizing Formula

```python
MIN_CONFIDENCE = 0.55
PARENT_DEPLOY_RATIO = Decimal("0.80")    # keep 20% unallocated
MAX_CHILD_RATIO = Decimal("0.60")        # no child > 60% of parent

raw_score_i = max(0.0, confidence_i - MIN_CONFIDENCE) ** 2
weight_i = raw_score_i / sum(raw_score_j)
allocatable = parent.quantity_free * PARENT_DEPLOY_RATIO
target_i = allocatable * Decimal(str(weight_i))
allocated_i = min(target_i, parent.quantity_free * MAX_CHILD_RATIO)
```

- Confidence < 0.55 → 0 allocation
- Squaring rewards high conviction
- Parent never goes all-in (20% reserve)
- No single child > 60% of parent

## Child Timer Rules

```python
child.deadline_at = min(
    parent.deadline_at,
    child.opened_at + timedelta(hours=candidate.estimated_window_hours),
) if parent.deadline_at else (
    child.opened_at + timedelta(hours=candidate.estimated_window_hours)
)
```

Timers naturally shrink with depth — a 24h bear window creates a 12h bull child, which can only create a 6h sub-child.

## PairScanner Changes

Current: `discover_usd_spot_pairs()` → USD quote only.

New: `discover_spot_pairs(source_asset)` → all tradeable pairs for any asset.

```python
def scan_candidates(
    source_asset: str,
    *,
    max_deadline_hours: float | None = None,
    excluded_assets: set[str] = frozenset(),
) -> tuple[RotationCandidate, ...]
```

## Lifecycle

| Event | Action |
|-------|--------|
| Bot starts | Build root nodes from portfolio balances |
| Planner cycle | For each leaf node with quantity_free > min: scan candidates, size by confidence |
| Entry fill | Create child node, decrement parent.quantity_free |
| Timer expires | Close node position, proceeds return to parent.quantity_free |
| Stop/target hit | Same as timer — close and return to parent |
| Parent expires | Force-close all descendants first (bottom-up) |

## Implementation Phases

### Phase R1: Data model + scanner generalization
- Add `RotationNode`, `RotationCandidate`, `RotationTreeState` to `core/types.py`
- Generalize `PairScanner.discover_spot_pairs(asset)` for any base/quote asset
- Tests for tree helpers (children_of, leaf_nodes, remaining_hours)

### Phase R2: Planner
- Replace `ConditionalTreeCoordinator` with `RotationTreePlanner`
- Root node initialization from portfolio balances
- Candidate scanning per leaf node
- Confidence-weighted sizing
- Tests

### Phase R3: Runtime wiring
- Replace conditional tree wiring in `runtime_loop.py` with rotation tree
- Fill → child node binding
- Timer expiry → bottom-up cascade close
- Proceeds → parent return

### Phase R4: Persistence + dashboard
- Persist `RotationTreeState` to SQLite (flat nodes table)
- Rehydrate on restart
- Dashboard/TUI visualization of active tree

## Constraints

- `max_depth = 2` (v1 safety limit)
- Max 2 children per parent node (v1)
- `min_position` floor per child (from settings)
- Only scan leaf nodes with `remaining_hours >= 2` (don't create children that expire immediately)
- Fail-closed: scanner errors → skip, don't crash
