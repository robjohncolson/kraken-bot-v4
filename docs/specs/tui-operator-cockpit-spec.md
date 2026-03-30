# TUI Operator Cockpit Spec

## Purpose

Define a keyboard-first terminal UI for `kraken-bot-v4`.

The TUI is intended to be the **live operator cockpit**:

- dense, low-latency situational awareness
- colorful terminal-native monitoring
- keyboard navigation instead of browser clicking
- safe read-only operation by default

This does **not** replace the existing web dashboard.
The web dashboard remains the browser-facing visualization surface for charts and remote access.
The TUI becomes the primary local operations interface.

## Why Now

The current observability stack is web-first:

- FastAPI dashboard in `web/`
- SSE stream at `/sse/updates`
- read-only JSON endpoints under `/api/*`

That is sufficient for browser monitoring, but it is not ideal for:

- fast keyboard-driven triage
- log tailing beside state panels
- compact multi-pane views
- operator workflows on the spare laptop itself

The TUI should fill that gap without introducing a second state authority.

## Goals

- Provide a **read-only v1 operator cockpit** for the live bot.
- Reuse the existing dashboard data model and SSE update flow.
- Support fast keyboard navigation with minimal pointer use.
- Show the most operationally relevant state on one screen:
  - portfolio
  - positions
  - pending orders
  - beliefs and consensus
  - reconciliation issues
  - recent runtime events
  - heartbeat / health
- Render clearly in a terminal over local console, SSH, or Tailscale shell.
- Remain safe if the bot is stopped, disconnected, or partially degraded.

## Non-Goals

- No trade mutation controls in v1.
- No order placement, cancel, pause, or override actions.
- No replacement of FastAPI or the existing dashboard.
- No second event pipeline unique to the TUI.
- No direct reducer/runtime coupling from widgets.
- No ncurses-only implementation that becomes hard to test and maintain.

## Product Decision

Build the TUI as a **read-only operator cockpit** that consumes the same bot state already exposed to the web dashboard.

Recommended stack:

- `textual` for layout, key bindings, widgets, and app lifecycle
- `rich` for tables, logs, styling, and color semantics

Do **not** build this on raw `curses` unless Textual proves impossible on the spare laptop.

## Relationship to the Web Dashboard

The two interfaces have different jobs:

### TUI

Best for:

- live monitoring
- rapid keyboard navigation
- dense operational summaries
- tailing events/logs
- pair-by-pair triage
- running on the bot host in a terminal

### Web Dashboard

Best for:

- D3 charts
- longer historical visualizations
- remote browser access over Tailscale
- presentation-friendly summaries

### Rule

The TUI and web dashboard must consume the **same underlying read model**.
If a new state field is needed, it should be added to the shared read model first, not invented only for one UI.

## Current Data Sources to Reuse

The TUI should reuse the existing local interface surface before adding new endpoints:

- `GET /api/portfolio`
- `GET /api/positions`
- `GET /api/beliefs`
- `GET /api/stats`
- `GET /api/reconciliation`
- `GET /api/grid/{pair}`
- `GET /api/health`
- `GET /sse/updates`

Relevant current types:

- `web.routes.DashboardState`
- `scheduler.DashboardStateUpdate`
- `core.types.BotState`
- `web.sse.publish()`

## Transport Model

### Initial Snapshot

On startup, the TUI should fetch a full initial snapshot from the existing `/api/*` endpoints.

### Live Updates

After the initial snapshot, the TUI should subscribe to `/sse/updates`.

### Reconnect Behavior

If SSE disconnects:

- show a visible degraded banner
- keep the last known state on screen
- retry with backoff
- never crash the app on disconnect

### Fallback

If SSE is unavailable but the API is reachable:

- poll the JSON endpoints on a slow interval
- show that the TUI is in degraded polling mode

## V1 Scope

V1 should be read-only and should include these screens.

### 1. Overview Screen

Single-screen operational summary.

Panels:

- bot health
- portfolio summary
- open positions
- pending orders
- belief summary by pair/source
- reconciliation summary
- latest runtime events

This is the default landing screen.

### 2. Positions Screen

Focused table for:

- pair
- side
- quantity
- entry price
- stop price
- target price
- current price
- unrealized P&L
- grid phase if present

### 3. Beliefs Screen

Matrix view by pair and source.

Per cell:

- direction
- confidence
- regime
- freshness / updated age if available

Also show consensus outcome per pair:

- agreed direction
- agreement count
- whether entry is blocked
- cooldown status if applicable

### 4. Orders Screen

Focused table for:

- open exchange orders
- structured `pending_orders`
- reservation-relevant fields
- fill progress

This is important now that sell-side inventory transitions and pending order accounting exist.

### 5. Reconciliation Screen

Focused display for:

- discrepancy detected
- ghost positions
- foreign orders
- untracked assets
- fee drift
- most recent summary

### 6. Event Log Screen

Tail of recent bot events and operator-relevant logs.

At minimum include:

- reducer logs
- runtime effect handling results
- reconciliation notices
- watchdog / heartbeat issues if available

If log integration is not ready in v1, a lightweight in-memory event ring buffer is acceptable.

### 7. Help Screen

Show all key bindings and status legend.

## Layout Direction

Default layout should feel like an operator terminal, not a toy dashboard.

Recommended overview arrangement:

```text
+----------------------+----------------------+
| Health / Heartbeat   | Portfolio / Risk     |
+----------------------+----------------------+
| Positions            | Pending Orders       |
+---------------------------------------------+
| Beliefs / Consensus                        |
+---------------------------------------------+
| Reconciliation / Alerts                    |
+---------------------------------------------+
| Event Log                                  |
+---------------------------------------------+
```

On narrow terminals, stack panels vertically.

## Key Bindings

V1 should be fully usable from the keyboard.

Minimum bindings:

- `1` overview
- `2` positions
- `3` beliefs
- `4` orders
- `5` reconciliation
- `6` event log
- `?` help
- `r` manual refresh
- `p` pause/resume auto-refresh rendering only
- `[` previous pair
- `]` next pair
- `/` filter or jump
- `q` quit

Optional:

- `g` jump to grid detail for selected pair
- `b` jump to belief detail for selected pair

## Color Semantics

Color should convey state, not decoration.

- green: healthy, bullish, profitable, connected
- red: unhealthy, bearish, blocked, loss, discrepancy
- yellow: warning, stale, reconnecting, cooldown
- blue/cyan: neutral informational values
- magenta only if a specific category needs it; do not make the whole UI purple-biased

ASCII-first operation is required.
Unicode box drawing is acceptable if the terminal supports it, but the UI must remain legible in plain text environments.

## Proposed Package Layout

Create a new package:

```text
tui/
  __init__.py
  __main__.py
  app.py
  client.py
  state.py
  events.py
  theme.py
  widgets/
    health.py
    portfolio.py
    positions.py
    beliefs.py
    orders.py
    reconciliation.py
    event_log.py
  screens/
    overview.py
    positions.py
    beliefs.py
    orders.py
    reconciliation.py
    logs.py
    help.py
```

This keeps the TUI isolated from `web/` and `runtime_loop.py`.

## Shared Read Model Rule

Do not duplicate bot semantics inside the TUI.

The TUI should maintain a local presentation state shaped from the existing dashboard/API payloads.
If a field is missing, prefer extending the shared read model rather than embedding trading logic in the UI.

Examples:

- acceptable: derive row colors from `belief.direction`
- not acceptable: recompute consensus in the TUI from scratch if the backend can provide it

## New Read Model Additions Likely Needed

The current web state is close, but not enough for a strong TUI operator view.

Expected backend additions for TUI usefulness:

- `pending_orders` in the dashboard read model
- `bot_state.last_event`
- `entry_blocked`
- cooldown summary by pair
- heartbeat summary
- a recent event/log ring buffer
- SSE event types that are more structured than generic dashboard refreshes

These should be added to shared dashboard state, not TUI-only internals.

## SSE Event Contract Direction

The current SSE layer supports arbitrary event names and payloads.

The TUI should target these event types over time:

- `dashboard.portfolio`
- `dashboard.positions`
- `dashboard.beliefs`
- `dashboard.orders`
- `dashboard.reconciliation`
- `dashboard.health`
- `dashboard.log`

V1 may start by handling the current coarse `dashboard.update` style payloads if that is what exists today.
But the implementation should be structured so granular events can replace coarse updates later without rewriting the whole app.

## Safety Rules

- TUI is read-only in v1.
- No mutation endpoints are added for the TUI.
- No direct executor calls from the TUI.
- No hotkeys that change exchange state.
- No “hidden” debug action paths.
- The TUI must tolerate missing data, stale data, and disconnected SSE.

## Testing Strategy

The TUI should be testable without a live Kraken connection.

### Unit Tests

- payload parsing
- state merge/update behavior
- reconnect/backoff logic
- key binding routing
- color/status mapping helpers

### Textual App Tests

Use Textual test support for:

- screen navigation
- widget rendering with fake snapshots
- SSE event application
- degraded/disconnected banners

### Fixtures

Reuse or adapt existing dashboard fixtures from `tests/web/` where possible.

## Implementation Phases

### Phase T0: Spec + Contract Review

- confirm v1 is read-only
- identify missing shared read-model fields
- decide whether to extend `/api/*` or add one consolidated snapshot endpoint

### Phase T1: TUI Skeleton

- app shell
- theme
- key bindings
- overview screen with static fake data

Acceptance:

- `python -m tui` launches
- screen switching works
- no network required for local fake-data mode

### Phase T2: API Client + Snapshot Hydration

- startup fetch from existing `/api/*`
- normalize payloads into local presentation state

Acceptance:

- TUI renders from real local dashboard endpoints
- graceful error if dashboard is down

### Phase T3: SSE Live Updates

- subscribe to `/sse/updates`
- merge live payloads into TUI state
- visible reconnect banner

Acceptance:

- state updates live without restart
- disconnect does not crash the app

### Phase T4: Operator Screens

- positions
- beliefs
- orders
- reconciliation
- log view

Acceptance:

- all major operator screens navigable by keyboard
- layout remains usable on laptop terminal sizes

### Phase T5: Shared Read Model Enhancements

Implement only the missing backend fields required for the TUI to be truly useful:

- pending order view
- cooldowns
- entry blocked
- recent logs/events
- heartbeat summary

Acceptance:

- no trading logic duplicated in the TUI
- backend remains read-only

## Acceptance Criteria

The TUI is considered complete for v1 when:

- it launches locally with a single command
- it works against the existing local dashboard service
- it is fully keyboard navigable
- it remains read-only
- it survives SSE disconnects
- it clearly shows beliefs, positions, pending orders, reconciliation, and health
- it adds operator value beyond the web dashboard

## Suggested Launch Command

```powershell
python -m tui
```

Optional config:

- `TUI_BASE_URL=http://127.0.0.1:58392`
- `TUI_REFRESH_SEC=5`

## Recommendation for Claude Worktree

The next Claude session should implement this in a separate worktree with this order:

1. build `tui/` skeleton with fake local state
2. connect to existing `/api/*`
3. wire `/sse/updates`
4. add operator screens
5. only then extend backend read-model fields if still necessary

That sequencing keeps the first cuts cheap and avoids coupling TUI implementation to backend refactors too early.
