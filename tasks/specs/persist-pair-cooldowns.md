# Spec: Persist Pair Cooldowns

**Date**: 2026-04-03
**Priority**: 3
**Status**: Spec

## Motivation

`_rotation_pair_cooldowns` is an in-memory `dict[str, float]` mapping pair → monotonic expiry time. Lost on restart. This means after a restart, the bot immediately retries pairs that just failed, wasting API calls and potentially hitting rate limits.

## Design

### Use existing SQLite `cooldowns` table

The `cooldowns` table already exists with schema:
```sql
CREATE TABLE IF NOT EXISTS cooldowns (
    pair           TEXT PRIMARY KEY,
    cooldown_until TEXT NOT NULL  -- ISO datetime
)
```

And has existing methods:
- `SqliteReader.fetch_cooldowns() -> dict[str, datetime]`
- `SqliteWriter.set_cooldown(pair, until)`

### Changes

1. **On cooldown set**: Write to both in-memory dict AND SQLite via `set_cooldown(pair, until)`
   - Convert monotonic time to absolute datetime for SQLite storage
   - Keep in-memory dict for fast checks during the cycle

2. **On startup**: Load cooldowns from SQLite via `fetch_cooldowns()`, populate `_rotation_pair_cooldowns`
   - Convert stored datetimes back to monotonic expiry times
   - Skip expired cooldowns (don't load stale entries)

3. **Cooldown check**: No change — still checks in-memory dict (fast path)

### Time conversion

Current code uses `time.monotonic()` for cooldown expiry. SQLite stores ISO datetimes. Conversion:
```python
# Setting: monotonic → datetime
abs_until = datetime.now(UTC) + timedelta(seconds=ROTATION_PAIR_COOLDOWN_SEC)
writer.set_cooldown(pair, abs_until)

# Loading: datetime → monotonic
remaining = (stored_until - datetime.now(UTC)).total_seconds()
if remaining > 0:
    _rotation_pair_cooldowns[pair] = time.monotonic() + remaining
```

## Affected Files

| File | Change |
|------|--------|
| `runtime_loop.py` | Load cooldowns on startup; write to SQLite when setting cooldowns (3 places) |

## Edge Cases

1. **Clock skew on restart**: Monotonic clock resets on restart. Converting to absolute datetime for SQLite handles this correctly.
2. **Expired cooldowns in DB**: Filter on load — only populate entries where `cooldown_until > now`.
3. **Concurrent writes**: Only one bot instance runs, so no contention.

## Test Plan

1. Unit: Setting a cooldown writes to SQLite
2. Unit: Loading cooldowns on startup populates in-memory dict
3. Unit: Expired cooldowns in SQLite are not loaded
4. Unit: Cooldown survives simulated restart (write → reload → check)
