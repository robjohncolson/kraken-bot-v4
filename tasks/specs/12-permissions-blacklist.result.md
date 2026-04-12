Implemented the permissions-aware pair blacklist in `scripts/cc_brain.py`.

- Added `load_permission_blocked()` to read persisted `permission_blocked` memories and return blocked pair strings.
- Added a single chokepoint filter so any blocked pair is removed from `orders_to_place` before shadow logging, placement, and decision-memory writes.
- Added persistence on order failures that contain `EAccount:Invalid permissions`, storing `category=permission_blocked`, the pair, raw error text, and an ISO `first_blocked_ts`.
- Added focused tests in `tests/test_cc_brain_permission_blacklist.py` for blacklist filtering and permission-failure memory persistence.

Validation was not run in this subagent because the task wrapper explicitly prohibited tests/lint/verification commands.
