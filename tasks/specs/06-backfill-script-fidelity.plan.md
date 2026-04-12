# Plan 06 — Backfill script fidelity fix

## File targets

- `scripts/backfill_shadow.py` — only file to modify

## Step-by-step

### Step 1: Parse the Mode line

In `_parse_report`, after loading the file text, add a check for the
`Mode:` line which is always on line 3 of every brain report:

```python
# Mode: LIVE or Mode: DRY RUN (always on the 3rd non-empty line)
mode_match = re.search(r"Mode:\s+(LIVE|DRY RUN)", text)
mode = mode_match.group(1) if mode_match else "UNKNOWN"
```

Return `mode` alongside analyses and live decision as the 3rd element
of the tuple:

```python
def _parse_report(path: Path) -> tuple[list[dict], dict, str]:
    ...
    return analyses, live, mode
```

### Step 2: Parse Step 6 for real fill status

Add a function to extract the Step 6 action outcome:

```python
_PLACED_LINE = re.compile(r"^\s*PLACED:\s+(\S+)\s+txid=(\S+)", re.MULTILINE)
_FAILED_LINE = re.compile(r"^\s*FAILED:\s+(\S+)\s+[—-]\s+(.*)$", re.MULTILINE)


def _parse_act_outcome(text: str) -> dict:
    """Return {status, pair, txid, error} for the Step 6 Act block.

    status is one of: "placed", "failed", "dry_run", "none"
    """
    step6_m = re.search(r"--- Step 6: Act ---\n(.*?)(?=\n--- Step 7|\Z)",
                         text, re.DOTALL)
    if not step6_m:
        return {"status": "none"}
    block = step6_m.group(1)
    if "DRY RUN" in block or "WOULD:" in block:
        return {"status": "dry_run"}
    pm = _PLACED_LINE.search(block)
    if pm:
        return {"status": "placed", "pair": pm.group(1), "txid": pm.group(2)}
    fm = _FAILED_LINE.search(block)
    if fm:
        return {"status": "failed", "pair": fm.group(1), "error": fm.group(2)}
    return {"status": "none"}
```

### Step 3: Wire fill status into cycle filtering

In `main()`, after parsing each report, capture the mode and act outcome:

```python
filter_counts = Counter()

for path in reports:
    analyses, live, mode = _parse_report(path)
    if not analyses:
        filter_counts["no_analyses"] += 1
        continue

    text = path.read_text(encoding="utf-8", errors="replace")
    act = _parse_act_outcome(text)

    # Filter: only count cycles that actually placed a real order.
    if mode == "DRY RUN":
        filter_counts["dry_run"] += 1
        continue
    if act["status"] == "failed":
        filter_counts["failed_order"] += 1
        continue
    if act["status"] != "placed":
        filter_counts["no_action"] += 1
        continue

    filter_counts["filled"] += 1

    # ... existing per-cycle processing ...
```

### Step 4: Print filter counts in the summary

At the start of the summary output, before the existing sections:

```python
print("=== Report filter ===")
print(f"  Reports scanned:       {len(reports)}")
print(f"  No-analyses cycles:    {filter_counts.get('no_analyses', 0)}")
print(f"  Dry-run cycles:        {filter_counts.get('dry_run', 0)} (dropped)")
print(f"  Failed-order cycles:   {filter_counts.get('failed_order', 0)} (dropped)")
print(f"  No-action cycles:      {filter_counts.get('no_action', 0)} (dropped)")
print(f"  Filled-order cycles:   {filter_counts.get('filled', 0)} (kept)")
print()
```

### Step 5: Guard the forward-return block

The forward-return evaluation section already skips cycles where live
isn't `entry`/`rotation`. After this fix, `cycles` only contains filled
cycles, so the section will only evaluate real trades. No further change
needed there.

## Testing

1. **Syntax check:** `python -c "import ast; ast.parse(open('scripts/backfill_shadow.py').read())"`
2. **Run the fixed script:**
   ```bash
   python scripts/backfill_shadow.py --forward-hours 6 > /tmp/backfill_fixed.txt 2>&1
   head -20 /tmp/backfill_fixed.txt
   ```
3. **Verify the filter report appears** and shows ~10 dry-runs + ~3 failed + ~6 filled.
4. **Verify RAVE/USD is not in the per-cycle table** (it was a failed order
   on 2026-04-12_0132).
5. **Verify the cumulative edge** should now favor shadow (close to the
   +11.59% from the corrected manual analysis).

## Rollback

`git revert` the commit. The script's interface is unchanged; only its
internal filtering changes. Downstream consumers (none currently) will
see a different (correct) cycle count.

## Commit message

```
Fix backfill_shadow.py: only count real filled cycles

The previous version treated `ENTRY from USD: X` in Step 5 as a live
decision regardless of what happened in Step 6. This counted:
- Dry-run cycles (Mode: DRY RUN, WOULD: buy...)
- Failed orders (ordermin, pair_decimals, insufficient funds)
as if they were real trades that captured forward returns.

The RAVE/USD 2026-04-12 01:32 cycle was the biggest victim: Kraken
rejected the order on ordermin, but the backfill credited live with
the +35.25% forward return, flipping the aggregate shadow-vs-live
edge from strongly positive to slightly negative.

New filtering parses `Mode:` and Step 6 `PLACED:`/`FAILED:` lines.
Only cycles with Mode=LIVE and a matching PLACED line are kept for
analysis. Dropped cycles are counted and reported in a new filter
summary at the top of the output.
```
