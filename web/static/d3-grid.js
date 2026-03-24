"use strict";

(function initGridStatusModule(root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory(root);
    return;
  }
  root.GridStatusChart = factory(root);
})(typeof globalThis !== "undefined" ? globalThis : this, function createGridStatusModule(globalRoot) {
  const PHASES = [
    { key: "S0", color: "#64748b" },
    { key: "S1a", color: "#0f766e" },
    { key: "S1b", color: "#d97706" },
    { key: "S2", color: "#2563eb" },
  ];
  const CHARTS = new WeakMap();
  const D3_CDN = "https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js";
  let d3Requested = false;

  function renderGridStatus(gridState, containerId) {
    if (typeof document === "undefined") return null;
    const container = typeof containerId === "string" && containerId ? document.getElementById(containerId) : null;
    if (!container) return null;
    const state = normalizeState(gridState);
    const d3 = globalRoot && globalRoot.d3 ? globalRoot.d3 : null;
    if (!d3) {
      requestD3(containerId, state);
      container.textContent = "Loading grid chart...";
      return null;
    }
    let chart = CHARTS.get(container);
    if (!chart) {
      chart = createChart(container, d3);
      CHARTS.set(container, chart);
    }
    updateChart(chart, d3, state);
    return chart.api;
  }

  function requestD3(containerId, state) {
    if (d3Requested || typeof document === "undefined" || !document.createElement) return;
    const mount = document.head || document.body || document.documentElement;
    if (!mount) return;
    d3Requested = true;
    const script = document.createElement("script");
    script.src = D3_CDN;
    script.async = true;
    script.setAttribute("data-grid-d3-loader", "true");
    script.onload = function handleLoad() {
      renderGridStatus(state, containerId);
    };
    mount.appendChild(script);
  }

  function createChart(container, d3) {
    container.textContent = "";
    const shell = d3.select(container).append("div").attr("data-grid-wrapper", "true").style("display", "grid").style("gap", "12px").style("font-family", '"Trebuchet MS", "Avenir Next", sans-serif');
    const title = shell.append("div").attr("class", "grid-title").style("font-weight", "600");
    const summary = shell.append("div").attr("class", "grid-summary").style("color", "#596274").style("font-size", "0.95rem");
    const phaseSvg = shell.append("svg").attr("viewBox", "0 0 320 72").attr("width", "100%").attr("height", 72);
    phaseSvg.append("rect").attr("x", 16).attr("y", 16).attr("width", 288).attr("height", 20).attr("rx", 8).attr("fill", "#e2e8f0");
    const cycleSvg = shell.append("svg").attr("viewBox", "0 0 320 118").attr("width", "100%").attr("height", 118);
    cycleSvg.append("line").attr("x1", 16).attr("x2", 304).attr("y1", 58).attr("y2", 58).attr("stroke", "#cbd5e1").attr("stroke-width", 1);
    return {
      title: title,
      summary: summary,
      phaseLayer: phaseSvg.append("g").attr("data-role", "phase-layer"),
      cycleLayer: cycleSvg.append("g").attr("data-role", "cycle-layer"),
      cycleEmpty: cycleSvg.append("text").attr("class", "cycle-empty").attr("x", 160).attr("y", 64).attr("text-anchor", "middle").attr("font-size", 12).attr("fill", "#64748b").text("No recent grid cycles"),
      api: { update: function update(nextState) { return renderGridStatus(nextState, container.id); } },
    };
  }

  function updateChart(chart, d3, state) {
    const phaseGroups = chart.phaseLayer.selectAll("g.phase-segment").data(buildPhaseData(d3, state), function byPhase(item) { return item.key; }).join(
      function enterPhase(enter) {
        const group = enter.append("g").attr("class", "phase-segment");
        group.append("rect").attr("class", "phase-fill").attr("y", 16).attr("height", 20).attr("rx", 8);
        group.append("text").attr("class", "phase-label").attr("y", 54).attr("text-anchor", "middle").attr("font-size", 12).attr("fill", "#334155");
        return group;
      },
      function updatePhase(update) { return update; },
      function exitPhase(exit) { exit.remove(); }
    );
    chart.title.text(state.label + " grid status");
    chart.summary.text(String(state.activeSlots) + " active slots");
    phaseGroups.select("rect.phase-fill").attr("x", function phaseX(item) { return item.x; }).attr("width", function phaseWidth(item) { return item.width; }).attr("fill", function phaseColor(item) { return item.color; });
    phaseGroups.select("text.phase-label").attr("x", function labelX(item) { return item.labelX; }).text(function labelText(item) { return item.key + " " + String(item.count); });
    chart.cycleLayer.selectAll("rect.cycle-bar").data(buildCycleData(d3, state.cycles), function byCycle(item) { return item.id; }).join(
      function enterCycle(enter) { return enter.append("rect").attr("class", "cycle-bar").attr("rx", 2); },
      function updateCycle(update) { return update; },
      function exitCycle(exit) { exit.remove(); }
    ).attr("x", function barX(item) { return item.x; }).attr("y", function barY(item) { return item.y; }).attr("width", function barWidth(item) { return item.width; }).attr("height", function barHeight(item) { return item.height; }).attr("fill", function barFill(item) { return item.fill; }).attr("data-cycle-id", function cycleId(item) { return item.id; });
    chart.cycleEmpty.text("No recent grid cycles").style("display", state.cycles.length === 0 ? "block" : "none");
  }

  function buildPhaseData(d3, state) {
    const total = d3.sum(PHASES, function totalPhase(phase) { return state.phases[phase.key]; }) || state.activeSlots;
    let offset = 16;
    return PHASES.map(function mapPhase(phase) {
      const count = state.phases[phase.key];
      const width = total > 0 ? (count / total) * 288 : 0;
      const x = offset;
      offset += width;
      return { key: phase.key, color: phase.color, count: count, x: x, width: width, labelX: x + (width > 0 ? width / 2 : 10) };
    });
  }

  function buildCycleData(d3, cycles) {
    const domain = d3.max(cycles, function maxCycle(item) { return Math.abs(item.pnl); }) || 1;
    const slotWidth = cycles.length > 0 ? 288 / cycles.length : 288;
    return cycles.map(function mapCycle(cycle, index) {
      const height = Math.max(4, (Math.abs(cycle.pnl) / domain) * 38);
      return { id: cycle.id, x: 16 + index * slotWidth + 2, y: cycle.pnl >= 0 ? 58 - height : 58, width: Math.max(10, slotWidth - 4), height: height, fill: cycle.pnl > 0 ? "#15803d" : cycle.pnl < 0 ? "#dc2626" : "#94a3b8" };
    });
  }

  function normalizeState(gridState) {
    const entries = toEntries(gridState);
    const phases = { S0: 0, S1a: 0, S1b: 0, S2: 0 };
    const cycles = [];
    let activeSlots = 0;
    entries.forEach(function appendEntry(entry, index) {
      const distribution = isRecord(entry.phase_distribution) ? entry.phase_distribution : {};
      PHASES.forEach(function tallyPhase(phase) { phases[phase.key] += toCount(distribution[phase.key]); });
      activeSlots += toCount(entry.active_slots);
      normalizeCycles(entry.cycle_history || entry.cycles, asText(entry.pair || entry.symbol, "Grid-" + String(index + 1))).forEach(function pushCycle(cycle) { cycles.push(cycle); });
    });
    const derivedSlots = PHASES.reduce(function total(sum, phase) { return sum + phases[phase.key]; }, 0);
    return { label: entries.length === 1 ? asText(entries[0].pair || entries[0].symbol, "Grid") : "Grid", activeSlots: activeSlots || derivedSlots, phases: phases, cycles: cycles.slice(-10) };
  }

  function toEntries(gridState) {
    if (gridState === null || gridState === undefined) return [];
    if (Array.isArray(gridState)) return gridState.filter(isRecord);
    if (!isRecord(gridState)) return [];
    if (isRecord(gridState.phase_distribution) || Array.isArray(gridState.cycle_history) || Array.isArray(gridState.cycles) || gridState.active_slots !== undefined) return [gridState];
    if (Array.isArray(gridState.pairs)) return gridState.pairs.filter(isRecord);
    return Object.keys(gridState).map(function mapEntry(key) { return gridState[key]; }).filter(isRecord);
  }

  function normalizeCycles(rawCycles, prefix) {
    if (!Array.isArray(rawCycles)) return [];
    return rawCycles.slice(-10).map(function mapCycle(item, index) {
      const source = isRecord(item) ? item : {};
      const pnl = source.realized_pnl_usd !== undefined ? source.realized_pnl_usd : source.pnl !== undefined ? source.pnl : item;
      return { id: asText(source.cycle_id, prefix + "-C" + String(index + 1)), pnl: toNumber(pnl) };
    });
  }

  function installPlaceholderBridge() {
    if (!globalRoot || typeof globalRoot.renderPlaceholder !== "function" || globalRoot.renderPlaceholder.__gridBridgeInstalled) return;
    const original = globalRoot.renderPlaceholder;
    const wrapped = function renderGridPlaceholder(targetId, payload, label) {
      if (targetId === "grid-content") {
        renderGridStatus(payload, targetId);
        return;
      }
      return original(targetId, payload, label);
    };
    wrapped.__gridBridgeInstalled = true;
    globalRoot.renderPlaceholder = wrapped;
  }

  installPlaceholderBridge();
  if (typeof document !== "undefined" && typeof document.addEventListener === "function") document.addEventListener("DOMContentLoaded", installPlaceholderBridge);
  if (globalRoot && typeof globalRoot.addEventListener === "function") globalRoot.addEventListener("load", installPlaceholderBridge);

  function isRecord(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
  function toCount(value) { return Math.max(0, Math.round(toNumber(value))); }
  function toNumber(value) { const number = typeof value === "number" ? value : Number(value); return Number.isFinite(number) ? number : 0; }
  function asText(value, fallback) { return typeof value === "string" && value.length > 0 ? value : fallback; }

  return { renderGridStatus: renderGridStatus };
});
