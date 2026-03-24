"use strict";

const handlers = {
  portfolio: updatePortfolio,
  positions: updatePositions,
  grid: updateGrid,
  beliefs: updateBeliefs,
  stats: updateStats,
  reconciliation: updateReconciliation,
  alert: updateAlerts,
  alerts: updateAlerts,
};

initializeDashboard();

function initializeDashboard() {
  setConnectionStatus("Connecting...");
  connectSse();
}

function connectSse() {
  if (typeof EventSource === "undefined") {
    setConnectionStatus("EventSource unsupported");
    appendAlert("Browser does not support server-sent events.");
    return;
  }

  const eventSource = new EventSource("/sse/updates");
  const eventTypes = [
    "portfolio",
    "positions",
    "grid",
    "beliefs",
    "stats",
    "reconciliation",
    "alert",
    "alerts",
    "dashboard.update",
  ];

  eventSource.addEventListener("open", function handleOpen() {
    setConnectionStatus("Live");
  });

  eventSource.addEventListener("error", function handleError() {
    setConnectionStatus("Reconnecting...");
    appendAlert("Connection lost. Waiting for the next SSE retry.");
  });

  eventTypes.forEach(function registerEvent(type) {
    eventSource.addEventListener(type, function handleEvent(event) {
      dispatchUpdate(type, parseEventPayload(event.data), event.lastEventId);
    });
  });

  eventSource.onmessage = function handleMessage(event) {
    dispatchUpdate("dashboard.update", parseEventPayload(event.data), event.lastEventId);
  };
}

function dispatchUpdate(type, payload, eventId) {
  setLastEvent(type, eventId);

  if (type === "dashboard.update" && isRecord(payload)) {
    Object.entries(payload).forEach(function dispatchSection(entry) {
      const name = entry[0];
      const value = entry[1];
      const handler = handlers[name];
      if (typeof handler === "function") {
        handler(value);
      }
    });
    return;
  }

  const handler = handlers[type];
  if (typeof handler === "function") {
    handler(payload);
  }
}

function parseEventPayload(rawPayload) {
  if (!rawPayload) {
    return {};
  }

  try {
    return JSON.parse(rawPayload);
  } catch (error) {
    if (error instanceof SyntaxError) {
      return { message: rawPayload };
    }
    throw error;
  }
}

function updatePortfolio(payload) {
  renderPlaceholder("portfolio-content", payload, "Portfolio totals and account health");
}

function updatePositions(payload) {
  renderPlaceholder("positions-content", payload, "Position-level summaries");
}

function updateGrid(payload) {
  renderPlaceholder("grid-content", payload, "Grid status by pair");
}

function updateBeliefs(payload) {
  renderPlaceholder("beliefs-content", payload, "Belief source agreement");
}

function updateStats(payload) {
  renderPlaceholder("stats-content", payload, "Strategy statistics");
}

function updateReconciliation(payload) {
  renderPlaceholder(
    "reconciliation-content",
    payload,
    "Reconciliation state and discrepancies"
  );
}

function updateAlerts(payload) {
  if (Array.isArray(payload)) {
    payload.forEach(function appendItem(item) {
      appendAlert(stringifyPayload(item));
    });
    return;
  }

  appendAlert(stringifyPayload(payload));
}

function renderPlaceholder(targetId, payload, label) {
  const target = document.getElementById(targetId);
  if (!target) {
    return;
  }

  target.textContent = label + ": " + stringifyPayload(payload);
}

function appendAlert(message) {
  const list = document.getElementById("alerts-content");
  if (!list) {
    return;
  }

  if (list.children.length === 1 && list.firstElementChild.textContent === "No alerts received yet.") {
    list.textContent = "";
  }

  const item = document.createElement("li");
  item.textContent = message;
  list.prepend(item);
}

function setConnectionStatus(status) {
  const element = document.getElementById("connection-status");
  if (element) {
    element.textContent = status;
  }
}

function setLastEvent(type, eventId) {
  const element = document.getElementById("last-event");
  if (!element) {
    return;
  }

  const suffix = eventId ? " (" + eventId + ")" : "";
  element.textContent = type + suffix;
}

function stringifyPayload(payload) {
  if (payload === null || payload === undefined) {
    return "No data";
  }

  if (typeof payload === "string") {
    return payload;
  }

  return JSON.stringify(payload);
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
