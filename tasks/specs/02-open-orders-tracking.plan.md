# Plan 02 — Open-orders tracking

## File targets

- `web/routes.py` — add the new endpoint
- `main.py` — confirm the bot keeps `state.open_orders` fresh; wire it
  into the route handler's context if needed
- `scripts/cc_brain.py` — add helper + update decision path

## Investigation first

Before writing code, verify:
- What does `main.py` line 188 (`state.open_orders`) contain?
  Does it auto-refresh, or only at startup?
- Does `web/routes.py` already have access to `state` for other
  endpoints? Look at the balance endpoint pattern.
- If `open_orders` is only refreshed at startup, add a fresh-fetch
  path in the endpoint handler (don't just return stale state).

## Step-by-step

### Step 1: Add `/api/open-orders` route

In `web/routes.py`, following the pattern of the existing
`/api/exchange-balances` route, add:

```python
@router.get("/open-orders")
async def get_open_orders(...):
    """Return current open orders on Kraken.

    Fetches fresh state from the Kraken adapter rather than relying
    on cached startup state.
    """
    try:
        orders = state.kraken_adapter.fetch_open_orders()  # or equivalent
    except Exception as e:
        return {"error": str(e), "orders": []}
    return {
        "orders": [
            {
                "txid": o.txid,
                "pair": normalized_pair(o.pair),
                "side": o.side,
                "volume": str(o.volume),
                "volume_executed": str(o.volume_executed),
                "price": str(o.price),
                "status": o.status,
                "opentm": o.opentm,
            }
            for o in orders
        ],
        "count": len(orders),
    }
```

The field names must match whatever the Kraken adapter actually
exposes. Inspect first; adapt exactly.

Ensure the returned `pair` is normalized to wsname format (e.g.
`BTC/USD`, not `XXBTZUSD`) so downstream code can use it directly.

### Step 2: Add helper in cc_brain

In `scripts/cc_brain.py`, right after `get_pairs_with_pending_orders`
(around line 885), add:

```python
def get_pairs_with_open_orders() -> set[str]:
    """Return pair names with current open orders on the exchange.

    Authoritative (queries Kraken via the bot API) vs. the memory-based
    pending_order path which only tracks orders the brain remembers.
    """
    resp = fetch("/api/open-orders")
    if "error" in resp:
        return set()
    pairs: set[str] = set()
    for o in resp.get("orders", []):
        p = o.get("pair")
        if p:
            pairs.add(p)
    return pairs
```

### Step 3: Union both sources in step 5

In the Step 5 decide block (around line 1180), change:

```python
pending_pairs = get_pairs_with_pending_orders()
```

to:

```python
pending_pairs = (
    get_pairs_with_pending_orders()
    | get_pairs_with_open_orders()
)
```

The union gives the authoritative exchange view PLUS any brain-
placed orders not yet confirmed by the exchange (transient race).

### Step 4: Extend stale-order cancellation

In `check_pending_orders` (around line 844), add a second pass that
queries `/api/open-orders` directly and cancels any order whose
`opentm` is older than `STALE_ORDER_HOURS`:

```python
def check_pending_orders(log_fn, dry_run: bool) -> None:
    # Existing memory-based pass (unchanged)
    ...
    # New authoritative pass: cancel stale orders even if the brain
    # has no memory of them.
    open_resp = fetch("/api/open-orders")
    if "error" not in open_resp:
        now_ts = time.time()
        cutoff_s = STALE_ORDER_HOURS * 3600
        for o in open_resp.get("orders", []):
            opentm = float(o.get("opentm", 0))
            if opentm == 0 or (now_ts - opentm) < cutoff_s:
                continue
            txid = o.get("txid")
            if not txid:
                continue
            if dry_run:
                log_fn(f"  WOULD cancel ghost order {txid} ({o.get('pair')}, "
                       f"{(now_ts - opentm)/3600:.1f}h old)")
            else:
                result = fetch(f"/api/orders/{txid}", method="DELETE")
                if "error" in result:
                    log_fn(f"  Ghost order {txid}: cancel failed "
                           f"({result.get('error')})")
                else:
                    log_fn(f"  Cancelled ghost order {txid} "
                           f"({(now_ts - opentm)/3600:.1f}h old)")
```

This is what would have caught the AUD/USD ghost that the memory-
based pass couldn't see.

## Testing

1. **Route smoke test:**
   ```bash
   python -c "import urllib.request, json; \
     print(json.load(urllib.request.urlopen('http://127.0.0.1:58392/api/open-orders', timeout=5)))"
   ```
   Should return `{"orders": [...], "count": N}`.
2. **Verify ghost cancellation in dry run:**
   `python scripts/cc_brain.py --dry-run` — look for
   `WOULD cancel ghost order` lines. The current AUD/USD ghost
   order should be identified and flagged for cancellation.
3. **Live run:** `python scripts/cc_brain.py` — verify the ghost
   orders are actually cancelled, and that subsequent cycles no
   longer repeat the AUD/USD failure.

## Rollback

`git revert` the commit. The new endpoint is additive (new route),
so removal is clean. The `check_pending_orders` extension falls
back to previous behavior if the endpoint is unavailable.

## Commit message

```
Add /api/open-orders + authoritative stale-order cancellation

The pending_order memory system only tracked orders the brain
successfully placed AND remembered. Orders from earlier runs
(before the tracking existed, or from crashed runs) were invisible
and the exchange kept their balance reserved, causing insufficient-
funds loops on pairs like AUD/USD.

New GET /api/open-orders returns live exchange order state.
cc_brain.get_pairs_with_open_orders() consults this authoritative
source and unions it with the memory-based proxy. check_pending_orders
now also cancels any open order older than STALE_ORDER_HOURS, even
if the brain has no memory entry for it.
```
