"use strict";

var handlers = {
  portfolio: updatePortfolio,
  positions: updatePositions,
  grid: updateGrid,
  beliefs: updateBeliefs,
  stats: updateStats,
  reconciliation: updateReconciliation,
  alert: updateAlerts,
  alerts: updateAlerts,
  rotation_tree: updateRotationTree,
  rotation_events: updateRotationEvents,
};

/* Global so D3 modules can install placeholder bridges */
window.renderPlaceholder = function renderPlaceholder(targetId, payload, label) {
  var target = document.getElementById(targetId);
  if (!target) return;
  target.textContent = label + ": " + stringifyPayload(payload);
};

initializeDashboard();

function initializeDashboard() {
  setConnectionStatus("Connecting...");
  fetchInitialState();
  connectSse();
}

/* ── Initial state from REST ─────────────────────────────── */

function fetchInitialState() {
  var endpoints = [
    { url: "/api/health", handler: updateHealth },
    { url: "/api/portfolio", handler: updatePortfolio },
    { url: "/api/positions", handler: function(d) { updatePositions(d && d.positions ? d.positions : d); } },
    { url: "/api/beliefs", handler: updateBeliefs },
    { url: "/api/reconciliation", handler: updateReconciliation },
    { url: "/api/stats", handler: updateStats },
    { url: "/api/rotation-tree", handler: updateRotationTree },
  ];
  endpoints.forEach(function fetchEndpoint(ep) {
    fetch(ep.url)
      .then(function(resp) { return resp.ok ? resp.json() : null; })
      .then(function(data) { if (data) ep.handler(data); })
      .catch(function() { /* silent on initial load */ });
  });
}

/* ── SSE connection ──────────────────────────────────────── */

function connectSse() {
  if (typeof EventSource === "undefined") {
    setConnectionStatus("EventSource unsupported");
    appendAlert("Browser does not support server-sent events.");
    return;
  }

  var eventSource = new EventSource("/sse/updates");
  var eventTypes = [
    "portfolio", "positions", "grid", "beliefs",
    "stats", "reconciliation", "alert", "alerts",
    "dashboard.update",
  ];

  eventSource.addEventListener("open", function() { setConnectionStatus("Live"); });
  eventSource.addEventListener("error", function() {
    setConnectionStatus("Reconnecting...");
    appendAlert("Connection lost. Waiting for the next SSE retry.");
  });

  eventTypes.forEach(function(type) {
    eventSource.addEventListener(type, function(event) {
      dispatchUpdate(type, parseEventPayload(event.data), event.lastEventId);
    });
  });

  eventSource.onmessage = function(event) {
    dispatchUpdate("dashboard.update", parseEventPayload(event.data), event.lastEventId);
  };
}

function dispatchUpdate(type, payload, eventId) {
  setLastEvent(type, eventId);
  if (type === "dashboard.update" && isRecord(payload)) {
    Object.entries(payload).forEach(function(entry) {
      var handler = handlers[entry[0]];
      if (typeof handler === "function") handler(entry[1]);
    });
    return;
  }
  var handler = handlers[type];
  if (typeof handler === "function") handler(payload);
}

function parseEventPayload(rawPayload) {
  if (!rawPayload) return {};
  try { return JSON.parse(rawPayload); }
  catch (e) { return { message: rawPayload }; }
}

/* ── Health panel (status strip) ─────────────────────────── */

function updateHealth(data) {
  if (!isRecord(data)) return;
  var uptime = data.uptime_seconds;
  if (uptime !== undefined) {
    var el = document.getElementById("last-event");
    if (el) el.textContent = "Uptime: " + formatUptime(uptime);
  }
}

/* ── Portfolio panel ─────────────────────────────────────── */

function updatePortfolio(data) {
  var target = document.getElementById("portfolio-content");
  if (!target || !isRecord(data)) {
    window.renderPlaceholder("portfolio-content", data, "Portfolio");
    return;
  }
  target.innerHTML = "";
  target.classList.remove("placeholder");

  var grid = document.createElement("div");
  grid.style.display = "grid";
  grid.style.gridTemplateColumns = "repeat(auto-fit, minmax(140px, 1fr))";
  grid.style.gap = "12px";

  var items = [
    { label: "Total Value", value: "$" + fmt(data.total_value_usd, 2) },
    { label: "Cash USD", value: "$" + fmt(data.cash_usd, 2) },
    { label: "Cash DOGE", value: fmt(data.cash_doge, 2) + " DOGE" },
    { label: "Exposure", value: fmt((data.directional_exposure || 0) * 100, 1) + "%" },
    { label: "Max Drawdown", value: fmt((data.max_drawdown || 0) * 100, 1) + "%" },
  ];

  items.forEach(function(item) {
    var card = document.createElement("div");
    card.style.cssText = "padding:12px;border-radius:12px;background:rgba(15,118,110,0.06);";
    card.innerHTML = '<div style="font-size:0.78rem;color:#596274;text-transform:uppercase;letter-spacing:0.06em">'
      + item.label + '</div><div style="font-size:1.3rem;font-weight:700;margin-top:4px">'
      + item.value + '</div>';
    grid.appendChild(card);
  });
  target.appendChild(grid);
}

/* ── Positions panel ─────────────────────────────────────── */

function updatePositions(data) {
  var target = document.getElementById("positions-content");
  if (!target) return;

  var raw = Array.isArray(data) ? data : (isRecord(data) && Array.isArray(data.positions) ? data.positions : []);

  if (raw.length === 0) {
    target.innerHTML = '<div style="color:#596274;padding:8px">No open positions</div>';
    target.classList.remove("placeholder");
    return;
  }

  target.innerHTML = "";
  target.classList.remove("placeholder");
  var table = document.createElement("table");
  table.style.cssText = "width:100%;border-collapse:collapse;font-size:0.9rem";
  table.innerHTML = '<thead><tr style="text-align:left;color:#596274;font-size:0.78rem;text-transform:uppercase;letter-spacing:0.05em">'
    + '<th style="padding:6px 8px">Pair</th><th>Side</th><th>Qty</th><th>Entry</th><th>Price</th><th>P&L</th></tr></thead>';
  var tbody = document.createElement("tbody");
  raw.forEach(function(item) {
    var pos = isRecord(item.position) ? item.position : item;
    var pnl = item.unrealized_pnl_usd !== undefined ? item.unrealized_pnl_usd : (item.unrealized_pnl || 0);
    var price = item.current_price;
    var tr = document.createElement("tr");
    tr.style.borderTop = "1px solid rgba(108,79,57,0.1)";
    var pnlNum = Number(pnl) || 0;
    var pnlColor = pnlNum >= 0 ? "#15803d" : "#b91c1c";
    tr.innerHTML = '<td style="padding:6px 8px;font-weight:600">' + (pos.pair || "") + '</td>'
      + '<td>' + (pos.side || "") + '</td>'
      + '<td>' + fmt(pos.quantity, 4) + '</td>'
      + '<td>$' + fmt(pos.entry_price, 4) + '</td>'
      + '<td>$' + fmt(price, 4) + '</td>'
      + '<td style="color:' + pnlColor + ';font-weight:600">$' + fmt(pnl, 2) + '</td>';
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  target.appendChild(table);
}

/* ── Grid panel (D3 bridge handles this) ─────────────────── */

function updateGrid(payload) {
  window.renderPlaceholder("grid-content", payload, "Grid status");
}

/* ── Beliefs panel (D3 bridge handles this) ──────────────── */

function updateBeliefs(payload) {
  window.renderPlaceholder("beliefs-content", payload, "Belief sources");
}

/* ── Stats panel ─────────────────────────────────────────── */

function updateStats(data) {
  var target = document.getElementById("stats-content");
  if (!target || !isRecord(data)) {
    window.renderPlaceholder("stats-content", data, "Strategy statistics");
    return;
  }
  target.innerHTML = "";
  target.classList.remove("placeholder");

  var items = [
    { label: "Trades", value: String(data.trade_count || 0) },
    { label: "Win Rate", value: fmt((data.win_rate || 0) * 100, 1) + "%" },
    { label: "Sharpe", value: fmt(data.sharpe_ratio || 0, 2) },
    { label: "Avg P&L", value: fmt(data.avg_pnl_bps || 0, 1) + " bps" },
  ];

  var grid = document.createElement("div");
  grid.style.display = "grid";
  grid.style.gridTemplateColumns = "1fr 1fr";
  grid.style.gap = "10px";
  items.forEach(function(item) {
    var el = document.createElement("div");
    el.style.cssText = "padding:10px;border-radius:10px;background:rgba(15,118,110,0.06);";
    el.innerHTML = '<div style="font-size:0.75rem;color:#596274;text-transform:uppercase">' + item.label
      + '</div><div style="font-size:1.15rem;font-weight:700;margin-top:2px">' + item.value + '</div>';
    grid.appendChild(el);
  });
  target.appendChild(grid);
}

/* ── Reconciliation panel ────────────────────────────────── */

function updateReconciliation(data) {
  var target = document.getElementById("reconciliation-content");
  if (!target || !isRecord(data)) {
    window.renderPlaceholder("reconciliation-content", data, "Reconciliation");
    return;
  }
  target.innerHTML = "";
  target.classList.remove("placeholder");

  /* Normalize: SSE sends {report: {...}}, REST sends flat fields */
  var d = data;
  if (isRecord(data.report)) {
    d = data.report;
    d.checked_at = d.checked_at || data.checked_at;
  }

  var hasDiscrepancy = d.discrepancy_detected || false;
  var statusColor = hasDiscrepancy ? "#b91c1c" : "#15803d";
  var statusText = hasDiscrepancy ? "Discrepancies Detected" : "Clean";

  var html = '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">'
    + '<span style="width:10px;height:10px;border-radius:50%;background:' + statusColor + ';display:inline-block"></span>'
    + '<span style="font-weight:700;color:' + statusColor + '">' + statusText + '</span></div>';

  var details = [];
  var ghosts = Array.isArray(d.ghost_positions) ? d.ghost_positions.length : 0;
  var foreign = Array.isArray(d.foreign_orders) ? d.foreign_orders.length : 0;
  var untracked = Array.isArray(d.untracked_assets) ? d.untracked_assets.length : 0;
  var fees = Array.isArray(d.fee_drift) ? d.fee_drift.length : 0;
  if (ghosts) details.push("Ghost positions: " + ghosts);
  if (foreign) details.push("Foreign orders: " + foreign);
  if (untracked) details.push("Untracked assets: " + untracked);
  if (fees) details.push("Fee drift: " + fees);
  if (details.length > 0) html += '<div style="color:#596274;font-size:0.9rem">' + details.join(" | ") + '</div>';
  if (d.checked_at) html += '<div style="color:#94a3b8;font-size:0.78rem;margin-top:4px">Last check: ' + d.checked_at + '</div>';

  target.innerHTML = html;
}

/* ── Alerts panel ────────────────────────────────────────── */

function updateAlerts(payload) {
  if (Array.isArray(payload)) {
    payload.forEach(function(item) { appendAlert(stringifyPayload(item)); });
    return;
  }
  appendAlert(stringifyPayload(payload));
}

function appendAlert(message) {
  var list = document.getElementById("alerts-content");
  if (!list) return;
  if (list.children.length === 1 && list.firstElementChild.textContent === "No alerts received yet.") {
    list.textContent = "";
  }
  var item = document.createElement("li");
  var now = new Date();
  item.textContent = now.toLocaleTimeString() + " " + message;
  list.prepend(item);
  while (list.children.length > 50) list.removeChild(list.lastChild);
}

/* ── Rotation Tree panel ────────────────────────────────── */

function updateRotationTree(data) {
  var target = document.getElementById("rotation-tree-content");
  var summary = document.getElementById("rotation-tree-summary");
  if (!target || !isRecord(data)) {
    window.renderPlaceholder("rotation-tree-content", data, "Rotation Tree");
    return;
  }

  // Summary bar
  if (summary) {
    summary.innerHTML = '<div class="rotation-summary-grid">'
      + summaryCard("Tree Value", "$" + fmt(data.rotation_tree_value_usd, 2))
      + summaryCard("Open", String(data.open_count || 0))
      + summaryCard("Closed", String(data.closed_count || 0))
      + summaryCard("Deployed", "$" + fmt(data.total_deployed, 2))
      + summaryCard("Realized P&L", "$" + fmt(data.total_realized_pnl, 2))
      + '</div>';
  }

  var nodes = Array.isArray(data.nodes) ? data.nodes : [];
  if (nodes.length === 0) {
    target.innerHTML = '<div style="color:#596274;padding:8px">No rotation nodes</div>';
    target.classList.remove("placeholder");
    return;
  }

  target.innerHTML = "";
  target.classList.remove("placeholder");

  var table = document.createElement("table");
  table.style.cssText = "width:100%;border-collapse:collapse;font-size:0.85rem";
  table.innerHTML = '<thead><tr style="text-align:left;color:#596274;font-size:0.75rem;text-transform:uppercase;letter-spacing:0.05em">'
    + '<th style="padding:6px 8px">Asset</th><th>Status</th><th>Direction</th>'
    + '<th>Conf</th><th>Deadline</th><th>TTL</th><th>P&L</th></tr></thead>';

  // Build parent lookup for tree ordering
  var byId = {};
  nodes.forEach(function(n) { byId[n.node_id] = n; });
  var rootIds = Array.isArray(data.root_node_ids) ? data.root_node_ids : [];
  var ordered = [];
  function dfs(nodeId, depth) {
    var node = byId[nodeId];
    if (!node) return;
    if (node.status === "cancelled") return;
    ordered.push(node);
    // Find children
    nodes.forEach(function(n) {
      if (n.parent_node_id === nodeId) dfs(n.node_id, depth + 1);
    });
  }
  rootIds.forEach(function(id) { dfs(id, 0); });
  // Add any orphans not reached by DFS
  nodes.forEach(function(n) {
    if (ordered.indexOf(n) === -1 && n.status !== "cancelled") ordered.push(n);
  });

  var tbody = document.createElement("tbody");
  ordered.forEach(function(node) {
    var tr = document.createElement("tr");
    tr.style.borderTop = "1px solid rgba(108,79,57,0.1)";
    if (node.depth === 0) tr.style.fontWeight = "600";

    var indent = node.depth * 20;
    var assetHtml = '<span style="padding-left:' + indent + 'px">'
      + (node.depth > 0 ? '<span style="color:#94a3b8;margin-right:4px">&#x2514;</span>' : '')
      + node.asset + '</span>';

    var dirHtml = directionBadge(node.ta_direction);
    var ttlHtml = computeTtl(node.deadline_at);
    var pnlNum = Number(node.realized_pnl) || 0;
    var pnlColor = pnlNum >= 0 ? "#15803d" : "#b91c1c";
    var pnlStr = node.realized_pnl !== null && node.realized_pnl !== undefined
      ? '<span style="color:' + pnlColor + '">$' + fmt(pnlNum, 2) + '</span>' : '--';

    var deadlineStr = node.deadline_at ? formatDeadline(node.deadline_at) : '--';

    tr.innerHTML = '<td style="padding:6px 8px">' + assetHtml + '</td>'
      + '<td>' + statusBadge(node.status) + '</td>'
      + '<td>' + dirHtml + '</td>'
      + '<td>' + (node.confidence ? fmt(node.confidence, 2) : '--') + '</td>'
      + '<td style="font-size:0.8rem">' + deadlineStr + '</td>'
      + '<td>' + ttlHtml + '</td>'
      + '<td>' + pnlStr + '</td>';
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  target.appendChild(table);
}

function summaryCard(label, value) {
  return '<div style="padding:8px 12px;border-radius:10px;background:rgba(15,118,110,0.06)">'
    + '<div style="font-size:0.72rem;color:#596274;text-transform:uppercase;letter-spacing:0.06em">' + label + '</div>'
    + '<div style="font-size:1.1rem;font-weight:700;margin-top:2px">' + value + '</div></div>';
}

function directionBadge(dir) {
  if (!dir) return '<span class="badge badge-unknown">--</span>';
  var cls = dir === "bullish" ? "badge-bullish" : dir === "bearish" ? "badge-bearish" : "badge-neutral";
  return '<span class="badge ' + cls + '">' + dir + '</span>';
}

function statusBadge(status) {
  var cls = "badge-status";
  if (status === "open") cls += " badge-status-open";
  else if (status === "closed") cls += " badge-status-closed";
  else if (status === "planned") cls += " badge-status-planned";
  else if (status === "closing") cls += " badge-status-closing";
  return '<span class="' + cls + '">' + status + '</span>';
}

function computeTtl(deadlineIso) {
  if (!deadlineIso) return '<span style="color:#94a3b8">--</span>';
  var deadline = new Date(deadlineIso);
  var now = new Date();
  var diffMs = deadline - now;
  if (diffMs <= 0) return '<span class="ttl-expired">EXPIRED</span>';
  var diffMin = diffMs / 60000;
  var cls = diffMin > 120 ? "ttl-green" : diffMin > 30 ? "ttl-yellow" : "ttl-red";
  var h = Math.floor(diffMin / 60);
  var m = Math.floor(diffMin % 60);
  var text = h > 0 ? h + "h " + m + "m" : m + "m";
  return '<span class="' + cls + '">' + text + '</span>';
}

function formatDeadline(iso) {
  var d = new Date(iso);
  var month = d.getMonth() + 1;
  var day = d.getDate();
  var hours = d.getHours();
  var mins = d.getMinutes();
  return (month < 10 ? "0" : "") + month + "/" + (day < 10 ? "0" : "") + day
    + " " + (hours < 10 ? "0" : "") + hours + ":" + (mins < 10 ? "0" : "") + mins;
}

/* ── Rotation Events panel ──────────────────────────────── */

function updateRotationEvents(data) {
  var target = document.getElementById("rotation-events-content");
  if (!target) return;

  var events = Array.isArray(data) ? data : [];
  if (events.length === 0) return;

  // Clear placeholder text
  if (target.children.length === 1 && target.firstElementChild.textContent === "No rotation events yet.") {
    target.textContent = "";
  }

  // Replace entire list with most-recent-first
  target.innerHTML = "";
  var reversed = events.slice().reverse();
  reversed.forEach(function(evt) {
    var li = document.createElement("li");
    var time = evt.timestamp ? new Date(evt.timestamp).toLocaleTimeString() : "";
    var badge = eventTypeBadge(evt.event_type);
    var pair = evt.pair || "";
    var details = evt.details ? formatEventDetails(evt.details) : "";
    li.innerHTML = '<span style="color:#94a3b8;font-size:0.8rem">' + time + '</span> '
      + badge + ' <strong>' + pair + '</strong>'
      + (details ? ' <span style="color:#596274">' + details + '</span>' : '');
    target.appendChild(li);
  });

  // Cap at 50 visible
  while (target.children.length > 50) target.removeChild(target.lastChild);
}

function eventTypeBadge(type) {
  var colors = {
    fill_entry: "#2563eb", fill_exit: "#7c3aed",
    tp_hit: "#15803d", sl_hit: "#b91c1c",
    entry_timeout: "#d97706", exit_escalation: "#d97706",
    root_extended: "#15803d", root_exit: "#b91c1c",
  };
  var color = colors[type] || "#596274";
  return '<span style="display:inline-block;padding:1px 6px;border-radius:4px;font-size:0.75rem;'
    + 'font-weight:600;color:white;background:' + color + '">' + (type || "unknown") + '</span>';
}

function formatEventDetails(details) {
  if (!isRecord(details)) return "";
  var parts = [];
  Object.entries(details).forEach(function(entry) {
    parts.push(entry[0] + "=" + entry[1]);
  });
  return parts.join(", ");
}

/* ── Utilities ───────────────────────────────────────────── */

function setConnectionStatus(status) {
  var el = document.getElementById("connection-status");
  if (el) el.textContent = status;
}

function setLastEvent(type, eventId) {
  var el = document.getElementById("last-event");
  if (!el) return;
  var suffix = eventId ? " (" + eventId + ")" : "";
  el.textContent = type + suffix;
}

function stringifyPayload(payload) {
  if (payload === null || payload === undefined) return "No data";
  if (typeof payload === "string") return payload;
  return JSON.stringify(payload);
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function fmt(value, decimals) {
  var num = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(num)) return "--";
  return num.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function formatUptime(seconds) {
  var h = Math.floor(seconds / 3600);
  var m = Math.floor((seconds % 3600) / 60);
  var s = Math.floor(seconds % 60);
  if (h > 0) return h + "h " + m + "m";
  if (m > 0) return m + "m " + s + "s";
  return s + "s";
}
