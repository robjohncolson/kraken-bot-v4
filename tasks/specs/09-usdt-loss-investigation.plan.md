# Plan 09 — USDT/USD outlier investigation

## Nature of task

**Diagnostic first, fix second.** Find the root cause, then decide
whether it needs a code fix or just better aggregation.

## File targets (investigation phase)

- `state/cc-reviews/brain_*.md` — READ ONLY
- `data/bot.db` — READ ONLY (SQLite queries)
- `persistence/cc_memory.py` — READ ONLY

## File targets (fix phase, if applicable)

Depends on root cause. Candidates:
- `web/routes.py` — if `/api/trade-outcomes` aggregation is buggy
- `persistence/reconciler.py` or similar — if position/trade reconciliation
  is dropping fills
- `scripts/cc_brain.py` — if the issue is in how cc_brain reads outcomes

## Step-by-step

### Step 1: Find the USDT/USD entry and exit timestamps

Search for USDT/USD decisions in recent memories:

```bash
python << 'EOF'
import urllib.request, json
for cat in ("decision", "postmortem"):
    url = f"http://127.0.0.1:58392/api/memory?category={cat}&hours=168&limit=50"
    with urllib.request.urlopen(url, timeout=10) as r:
        body = json.load(r)
    print(f"=== {cat} ({len(body.get('memories', []))} entries) ===")
    for m in body.get("memories", []):
        pair = m.get("pair", "")
        if "USDT" not in str(pair):
            continue
        c = m.get("content", {})
        if isinstance(c, str):
            c = json.loads(c)
        print(f"  {m.get('created_at', '')}  {pair}")
        print(f"    {c}")
EOF
```

### Step 2: Cross-reference with brain reports

For the entry and exit timestamps found in Step 1, open the
corresponding `brain_YYYY-MM-DD_HHMM.md` reports and capture:
- Step 5 Decide block (what was proposed)
- Step 6 Act block (what was PLACED/FAILED)
- Step 2 Observe block (what positions existed at the time)

### Step 3: Query the raw SQLite tables

```bash
python << 'EOF'
import sqlite3
conn = sqlite3.connect("data/bot.db")
conn.row_factory = sqlite3.Row

# List tables to find the right ones
for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
    print(row["name"])

# Query trades involving USDT
for row in conn.execute("""
    SELECT * FROM trades WHERE pair LIKE '%USDT%'
    ORDER BY created_at DESC LIMIT 20
"""):
    print(dict(row))
EOF
```

Adjust the table name if it isn't `trades`. Typical candidates:
`trades`, `orders`, `fills`, `positions`, `rotation_tree_nodes`.

### Step 4: Reconstruct the trade math

With entry/exit quantities and prices in hand, compute:
- `entry_value = entry_qty × entry_price`
- `exit_value = exit_qty × exit_price`
- `diff = exit_value − entry_value`
- Compare to the reported `net_pnl` = −$15.85

If `diff` matches: the trade really did lose that much (unlikely on a
stablecoin), investigate WHY — probably a partial fill at a weird
price or a data feed glitch.

If `diff` doesn't match: the aggregation layer (`/api/trade-outcomes`)
is computing proceeds or cost incorrectly. Investigate the endpoint.

### Step 5: Identify the root cause

Classify as one of:
- **A) Accounting bug** — the raw fills are correct but the aggregated
  outcome is wrong. Fix in the aggregator.
- **B) Execution bug** — the raw fills are genuinely bad (e.g., panic
  sell at a glitch price). Fix in the decision logic or add a price
  sanity check before exits.
- **C) Data anomaly** — legitimate weird market event (stablecoin
  flash crash). Add an outlier-detection rule that flags but doesn't
  feed back into self-tune.

### Step 6: Write the analysis

Create `tasks/specs/09-usdt-loss-investigation.result.md`:

```markdown
# USDT/USD -$15.85 investigation

## Entry details
- Cycle: <YYYY-MM-DD HH:MM>
- Txid: <...>
- Qty: <...> USDT
- Price: $<...>
- Cost: $<...>

## Exit details
- Cycle: <YYYY-MM-DD HH:MM>
- Txid: <...>
- Qty: <...>
- Price: $<...>
- Proceeds: $<...>
- Exit reason: root_exit_bearish

## Math
- Entry value: $...
- Exit value:  $...
- Computed diff: $...
- Reported net P&L: -$15.85
- <match or mismatch, explanation>

## Root cause
<A / B / C classification + explanation>

## Fix
<Code change OR outlier-detection rule OR "data anomaly, added flag">
```

### Step 7: Implement the fix (if applicable)

Depends entirely on the root cause. Three likely paths:

- **If aggregation bug**: find `/api/trade-outcomes` implementation
  and correct the proc/cost calculation. Add a unit test that
  reproduces the original bad number and verifies the new number.
- **If execution bug**: add a sanity check before exits — if the
  limit sell price is more than 5% off the pair's expected fair value
  (e.g., USDT/USD at $0.9998 not $0.57), refuse the exit and log.
- **If data anomaly**: add an outlier flag:
  ```python
  if pair in ("USDT/USD", "USDC/USD", "DAI/USD"):
      pnl_pct = abs(net_pnl / cost) if cost else 0
      if pnl_pct > 0.10:  # stablecoin moved >10% — data anomaly
          log(f"  ANOMALY: {pair} pnl_pct={pnl_pct:.0%}, flagging")
          # exclude from self-tune feedback
  ```

## Testing

1. If a code fix: verify the specific trade's math now matches
   expected.
2. Add a test case for the outlier detection rule (if taken).
3. Run `scripts/cc_brain.py --dry-run` and confirm self-tune no longer
   fires on the outlier.

## Rollback

Depends on fix; generally `git revert` is safe.

## Commit message (template — adapt to actual finding)

```
Investigate USDT/USD -$15.85 outlier and <fix|flag|document>

The single largest loss in the 7-day window was a USDT/USD trade that
reported -$15.85 net (42.9% loss) despite USDT not depegging. Root
cause: <A/B/C>. See tasks/specs/09-usdt-loss-investigation.result.md
for the full analysis.

Fix: <description of what changed and why>
```
