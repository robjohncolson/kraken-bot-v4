# Plan 04 — Extended shadow veto (Option C: hybrid)

## File targets

- `scripts/cc_brain.py` — only file to modify

## Step-by-step

### Step 1: Add the helper

After the existing `compute_unified_holds` function (around line 540),
add:

```python
SHADOW_STRONG_THRESHOLD = 0.65  # top3_mean floor for "strong" shadow conviction


def shadow_preferred_entry(
    unified: dict[str, dict],
    eligible: list[tuple[str, dict]],
    analyses: list[dict],
    cash_usd: float,
    max_position_value: float,
    live_pick_pair: str | None,
    log_fn,
) -> dict | None:
    """Decide what to do with shadow's verdict for this entry cycle.

    Returns:
        {"action": "order", ...}  - place this order instead of live's pick
        {"action": "veto"}        - block the entry, hold cash
        None                      - shadow has no strong preference, defer to live
    """
    if not eligible:
        return None  # shadow has nothing to say
    best_asset, best_info = eligible[0]
    best_top3 = best_info["top3_mean"]
    if best_top3 < SHADOW_STRONG_THRESHOLD:
        log_fn(f"  [SHADOW] Best is {best_asset} top3m={best_top3:.3f} "
               f"(below strong threshold {SHADOW_STRONG_THRESHOLD:.2f}), "
               f"deferring to live")
        return None

    # Shadow has a strong pick. Check if we're already about to act on it.
    live_pick_asset = (live_pick_pair.split("/")[0]
                       if live_pick_pair and "/" in live_pick_pair
                       else None)
    if live_pick_asset == best_asset:
        log_fn(f"  [SHADOW] Agrees with live pick ({best_asset}), no change")
        return None

    # Try to redirect: find a tradeable */USD pair for shadow's pick.
    if best_asset == "USD":
        log_fn(f"  [SHADOW VETO] Best is USD (top3m={best_top3:.3f}), "
               f"blocking entry. Live wanted: {live_pick_pair}")
        return {"action": "veto"}

    redirect_pair = f"{best_asset}/USD"
    redirect_analysis = next(
        (a for a in analyses if a["pair"] == redirect_pair), None
    )
    if redirect_analysis is None:
        log_fn(f"  [SHADOW VETO] Best is {best_asset} (top3m={best_top3:.3f}) "
               f"but no {redirect_pair} analysis this cycle, blocking entry")
        return {"action": "veto"}

    if redirect_analysis.get("trade_gate", 0) < MIN_REGIME_GATE:
        log_fn(f"  [SHADOW VETO] Best is {best_asset} but {redirect_pair} "
               f"regime-gated (gate={redirect_analysis['trade_gate']:.2f}), "
               f"blocking entry")
        return {"action": "veto"}

    if cash_usd < max_position_value:
        log_fn(f"  [SHADOW VETO] Best is {best_asset} but insufficient cash "
               f"(${cash_usd:.2f} < ${max_position_value:.2f}), blocking")
        return {"action": "veto"}

    # Redirect: build an order for shadow's preferred entry.
    price = redirect_analysis["price"]
    qty = round(max_position_value / price, 6)
    limit_price = round(price * 1.002, _price_decimals(price, redirect_pair))
    log_fn(f"  [SHADOW REDIRECT] From live {live_pick_pair or '(none)'} "
           f"to shadow pick {redirect_pair} (top3m={best_top3:.3f})")
    return {
        "action": "order",
        "order": {
            "pair": redirect_pair, "side": "buy", "order_type": "limit",
            "quantity": str(qty), "limit_price": str(limit_price),
        },
    }
```

### Step 2: Rewire Step 5b

Find the Step 5b block (around line 1220). Current structure:
```python
if not orders_to_place:
    scored = [...]
    scored.sort(key=lambda x: -x[1])
    if scored and scored[0][1] > ENTRY_THRESHOLD and cash_usd >= max_position_value:
        if shadow_wants_cash:
            # old narrow veto
            log(f"  [SHADOW VETO] ...")
        else:
            best, score, bd = scored[0]
            # place order
            orders_to_place.append({...})
    else:
        # no entry
```

Replace the `if shadow_wants_cash` branch with:

```python
if not orders_to_place:
    scored = [(a, a["_score"], a["_breakdown"]) for a in analyses
              if a["pair"] not in pending_pairs]
    scored.sort(key=lambda x: -x[1])
    live_pick = scored[0] if scored else None
    live_pick_pair = live_pick[0]["pair"] if live_pick else None

    # Consult shadow first — may redirect, veto, or defer.
    shadow_decision = shadow_preferred_entry(
        unified, eligible, analyses, cash_usd, max_position_value,
        live_pick_pair, log,
    )
    if shadow_decision and shadow_decision["action"] == "order":
        orders_to_place.append(shadow_decision["order"])
    elif shadow_decision and shadow_decision["action"] == "veto":
        pass  # intentional: hold cash
    elif live_pick and live_pick[1] > ENTRY_THRESHOLD and cash_usd >= max_position_value:
        best, score, bd = live_pick
        bd_str = " ".join(f"{k}={v:+.2f}" for k, v in bd.items()
                          if isinstance(v, (int, float)))
        log(f"ENTRY from USD: {best['pair']} score={score:.2f} [{bd_str}]")
        qty = round(max_position_value / best["price"], 6)
        limit_price = round(best["price"] * 1.002,
                            _price_decimals(best["price"], best["pair"]))
        orders_to_place.append({
            "pair": best["pair"], "side": "buy", "order_type": "limit",
            "quantity": str(qty), "limit_price": str(limit_price),
        })
    else:
        top_reason = "no USD" if cash_usd < max_position_value else (
            f"best score={scored[0][1]:.2f}" if scored else "no data")
        log(f"  No entry: {top_reason}. Sitting out.")
```

### Step 3: Remove the old narrow-veto variable

The `shadow_wants_cash` local variable (set at the top of Step 5)
is no longer used. Remove its assignment. Keep `unified` and `eligible`
because the helper uses them.

## Testing

1. **Syntax check:** `python -c "import ast; ast.parse(open('scripts/cc_brain.py').read())"`
2. **Dry-run scenarios:**
   - **Redirect case:** when shadow best = BTC at top3m ≥ 0.65 and
     BTC/USD is in the analyses with trade_gate above floor, the log
     should show `[SHADOW REDIRECT] From live X/USD to shadow pick BTC/USD`.
   - **Veto case:** when shadow best = USD OR no redirect pair
     available, log should show `[SHADOW VETO] ...`.
   - **Defer case:** when shadow top3m < 0.65, log should show
     "deferring to live" and the live pick proceeds.
3. **Live run:** confirm no regressions on rotations/exits.

## Rollback

`git revert` the commit. The new helper is self-contained; removing
it restores the old narrow-veto behavior.

## Commit message

```
Extend shadow veto: redirect entries to shadow's preferred asset

Previous veto only fired on shadow=USD. 24h of observation showed
shadow consistently picked BTC/ETH (never USD) while live deployed
into losing alts, so the narrow veto never triggered despite shadow
being correct on 11/11 cycles.

New shadow_preferred_entry() helper implements a hybrid: if shadow
has strong conviction (top3_mean >= 0.65) on an asset different from
live's pick, first try to redirect the entry to that asset's */USD
pair; if no viable redirect exists, hard-veto and hold cash. Below
the strong-confidence threshold, defer to live.

All three outcomes (REDIRECT / VETO / defer) logged explicitly.
Scoped to Step 5b entries only; rotations and exits untouched.
```
