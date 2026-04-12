# Plan — Spec 12: permissions-aware blacklist

## Context for the implementer

Read first: `tasks/specs/12-permissions-blacklist.spec.md`

## Where the fix goes

Two places in `scripts/cc_brain.py`:

### 1. Capture the failure (write the memory)

Around line 1467:

```python
for order in orders_to_place:
    result = fetch("/api/orders", method="POST", data=order)
    if "error" in result:
        log(f"  FAILED: {order['pair']} — {result['error']}")
        # NEW: detect EAccount:Invalid permissions and persist
        if "EAccount:Invalid permissions" in str(result.get("error", "")):
            try:
                fetch("/api/memory", method="POST", data={
                    "category": "permission_blocked",
                    "pair": order["pair"],
                    "content": {
                        "pair": order["pair"],
                        "error_text": str(result["error"]),
                        "first_blocked_ts": time.time(),
                    },
                    "importance": 0.9,
                })
                log(f"  -> persisted permission_blocked for {order['pair']}")
            except Exception as e:
                log(f"  -> failed to persist permission block: {e}")
    else:
        ...
```

### 2. Read the cache and filter candidates

Find where `orders_to_place` is built (Step 5: Decide). Before
appending to `orders_to_place`, check the blacklist. The simplest
way is to load the blocked set once at the top of the cycle:

```python
def load_permission_blocked() -> set[str]:
    """Read all permission_blocked memories, return set of blocked pairs."""
    try:
        memories = fetch("/api/memory?category=permission_blocked&hours=999999", method="GET").get("memories", [])
        blocked = set()
        for m in memories:
            content = m.get("content")
            if isinstance(content, dict):
                p = content.get("pair")
                if p:
                    blocked.add(p)
            elif isinstance(content, str):
                # Best-effort parse if content was stored as JSON string
                import json as _j
                try:
                    parsed = _j.loads(content)
                    if isinstance(parsed, dict) and parsed.get("pair"):
                        blocked.add(parsed["pair"])
                except Exception:
                    pass
        return blocked
    except Exception:
        return set()
```

Call this near the top of `run_brain_cycle()` (after the recall step
and before pair discovery). Then filter:

- **Exit candidates**: when computing exits in Step 5, skip any
  candidate whose `entry_pair`-equivalent (the actual order pair
  the brain would place) is in the blocked set.
- **Entry candidates**: when iterating `analyses` to build new
  entries, skip pairs in the blocked set entirely.
- **Rotation candidates**: same — skip rotations whose source or
  destination pair is in the blocked set.

The brain already has `pairs_to_scan` and similar collections. The
filter should apply at every place where a pair could enter
`orders_to_place`. Easiest path: filter at the very end, just
before the `for order in orders_to_place:` loop:

```python
blocked_pairs = load_permission_blocked()
if blocked_pairs:
    pre_count = len(orders_to_place)
    orders_to_place = [o for o in orders_to_place if o.get("pair") not in blocked_pairs]
    if len(orders_to_place) < pre_count:
        log(f"  Filtered {pre_count - len(orders_to_place)} order(s) by permission_blocked: {sorted(blocked_pairs)}")
```

This is the **single chokepoint** approach — clean, hard to bypass,
covers all proposal paths regardless of how the order got into
`orders_to_place`.

### 3. Test

Add `tests/test_cc_brain_permission_blacklist.py`:

```python
def test_permission_blocked_pair_filtered(monkeypatch):
    # Mock fetch() to return one permission_blocked memory
    # Mock orders_to_place containing AUD/USD and BTC/USD
    # Run the filter
    # Assert AUD/USD is gone, BTC/USD remains

def test_permission_failure_persists_memory(monkeypatch):
    # Mock fetch("/api/orders", ...) to return error with EAccount:Invalid permissions
    # Run the order placement loop
    # Assert fetch("/api/memory", method="POST", category="permission_blocked", ...) was called
```

## Files to modify

- `scripts/cc_brain.py` — capture failure + filter candidates
- `tests/test_cc_brain_permission_blacklist.py` — new file

## Validation

1. `python -m pytest tests/test_cc_brain_permission_blacklist.py -x`
2. `python -m pytest tests/ -x` — full suite still passes
3. `python scripts/cc_brain.py --dry-run` — works without errors
4. After running once with a real fail and once with a real success,
   verify SQLite: `SELECT * FROM cc_memory WHERE category='permission_blocked'`

## Dependencies

None (independent of specs 11 and 13).

## Risk

LOW. The change is purely additive. If the memory write fails, the
bot keeps running as before (just keeps re-trying the failed pair).
The filter only blocks pairs that were definitively rejected by
Kraken with a specific error code.
