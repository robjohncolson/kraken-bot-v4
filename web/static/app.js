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
