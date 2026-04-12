# Spec 16 Result

## Changes
- `web/routes.py`: `create_cc_router()` now creates or reuses a CC-safe SQLite writer, and `place_order()` persists successful `/api/orders` placements with `kind="cc_api"`, `client_order_id="kbv4-cc-<txid>"`, zero filled/quote quantities, and `exchange_order_id=txid`.
- `web/routes.py`: if SQLite persistence fails after Kraken accepts the order, the handler logs a warning and still returns success with a `warning` field in the JSON response.
- `tests/web/test_routes.py`: added regression coverage for successful CC-order persistence and the non-fatal warning path when the writer raises.

## Kind handling
- Searched for an `orders.kind` whitelist. `persistence/sqlite.py` stores `kind` as plain `TEXT`, so no storage whitelist change was required for `cc_api`.
- The remaining explicit kind list is the comment on `core.types.PendingOrder.kind`, which was outside this task's owned paths and was not modified.

## Impact note
- GitNexus `impact` / `context` calls for `place_order` were cancelled twice in this subagent session, so I used a manual blast-radius fallback.
- Manual fallback: `create_cc_router()` is mounted from `runtime_loop.build_runtime_app()`, and the behavioral change is isolated to the `/api/orders` POST success path.

## Tests
- Not run. Subagent mode instructed: `Do not run verification commands, tests, or lint checks.`
- Requested but not executed:
  - `python -m pytest tests/web/test_routes.py -x`
  - `python -m pytest tests/ -x`
