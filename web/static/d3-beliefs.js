"use strict";

(function initBeliefMatrixModule(root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory(root);
    return;
  }
  root.BeliefMatrixChart = factory(root);
})(typeof globalThis !== "undefined" ? globalThis : this, function createBeliefMatrixModule(globalRoot) {
  const SOURCE_ORDER = ["claude", "codex", "autoresearch"];
  const SOURCE_LABELS = { claude: "Claude", codex: "Codex", autoresearch: "Auto" };
  const CHARTS = new WeakMap();
  const D3_CDN = "https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js";
  let d3Requested = false;

  function renderBeliefMatrix(beliefData, containerId) {
    if (typeof document === "undefined") return null;
    const container = typeof containerId === "string" && containerId ? document.getElementById(containerId) : null;
    if (!container) return null;
    const state = normalizeState(beliefData);
    const d3 = globalRoot && globalRoot.d3 ? globalRoot.d3 : null;
    if (!d3) {
      if (state.pairs.length === 0) {
        container.textContent = "No belief data";
        return null;
      }
      requestD3(containerId, state);
      container.textContent = "Loading belief matrix...";
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
    script.setAttribute("data-belief-d3-loader", "true");
    script.onload = function handleLoad() {
      renderBeliefMatrix(state, containerId);
    };
    mount.appendChild(script);
  }

  function createChart(container, d3) {
    container.textContent = "";
    const shell = d3.select(container).append("div").attr("data-belief-wrapper", "true").style("display", "grid").style("gap", "10px").style("font-family", '"Trebuchet MS", "Avenir Next", sans-serif');
    const title = shell.append("div").style("font-weight", "600");
    const summary = shell.append("div").style("font-size", "0.95rem").style("color", "#596274");
    const empty = shell.append("div").style("color", "#64748b").style("font-size", "0.95rem");
    const svg = shell.append("svg").attr("width", "100%");
    return {
      title: title,
      summary: summary,
      empty: empty,
      svg: svg,
      headerLayer: svg.append("g").attr("data-role", "belief-headers"),
      labelLayer: svg.append("g").attr("data-role", "belief-labels"),
      cellLayer: svg.append("g").attr("data-role", "belief-cells"),
      api: { update: function update(nextState) { return renderBeliefMatrix(nextState, container.id); } },
    };
  }

  function updateChart(chart, d3, state) {
    const rowHeight = 34;
    const labelWidth = 94;
    const topOffset = 48;
    const columnWidth = state.pairs.length > 0 ? Math.max(84, Math.min(116, 460 / state.pairs.length)) : 84;
    const width = labelWidth + state.pairs.length * columnWidth + 8;
    const height = topOffset + state.sources.length * rowHeight + 10;
    chart.title.text("Belief agreement matrix");
    chart.summary.text(buildSummary(state));
    chart.empty.text("No belief data").style("display", state.pairs.length === 0 ? "block" : "none");
    chart.svg.attr("viewBox", "0 0 " + width + " " + height).attr("height", height).style("display", state.pairs.length === 0 ? "none" : "block");

    chart.labelLayer.selectAll("text.source-label").data(state.sources, function bySource(source) { return source; }).join(
      function enterSource(enter) { return enter.append("text").attr("class", "source-label").attr("text-anchor", "end").attr("font-size", 12).attr("fill", "#334155"); },
      function updateSource(update) { return update; },
      function exitSource(exit) { exit.remove(); }
    ).attr("x", labelWidth - 8).attr("y", function sourceY(source, index) { return topOffset + index * rowHeight + 20; }).text(function sourceText(source) { return SOURCE_LABELS[source] || source; });

    chart.headerLayer.selectAll("g.pair-header").data(state.pairs, function byPair(item) { return item.name; }).join(
      function enterHeader(enter) {
        const group = enter.append("g").attr("class", "pair-header");
        group.append("text").attr("class", "pair-label").attr("text-anchor", "middle").attr("font-size", 11).attr("font-weight", "600").attr("fill", "#0f172a");
        group.append("text").attr("class", "pair-consensus").attr("text-anchor", "middle").attr("font-size", 10);
        group.append("line").attr("class", "pair-rule").attr("y1", 38).attr("y2", 38).attr("stroke", "#cbd5e1");
        return group;
      },
      function updateHeader(update) { return update; },
      function exitHeader(exit) { exit.remove(); }
    ).attr("transform", function headerTransform(item, index) { return "translate(" + (labelWidth + index * columnWidth + columnWidth / 2) + ",0)"; }).call(function decorate(selection) {
      selection.select("text.pair-label").attr("y", 14).text(function pairText(item) { return item.name; });
      selection.select("text.pair-consensus").attr("y", 30).attr("fill", function pairFill(item) { return consensusColor(item.consensusDirection); }).text(function pairConsensus(item) { return formatConsensus(item); });
      selection.select("line.pair-rule").attr("x1", -columnWidth / 2 + 4).attr("x2", columnWidth / 2 - 4);
    });

    chart.cellLayer.selectAll("g.belief-cell").data(buildCells(state), function byCell(item) { return item.source + "::" + item.pair; }).join(
      function enterCell(enter) {
        const group = enter.append("g").attr("class", "belief-cell");
        group.append("rect").attr("rx", 6).attr("ry", 6);
        group.append("text").attr("class", "cell-direction").attr("text-anchor", "middle").attr("font-size", 11).attr("font-weight", "600");
        group.append("text").attr("class", "cell-confidence").attr("text-anchor", "middle").attr("font-size", 10);
        group.append("title");
        return group;
      },
      function updateCell(update) { return update; },
      function exitCell(exit) { exit.remove(); }
    ).attr("transform", function cellTransform(item) { return "translate(" + (labelWidth + item.column * columnWidth + 4) + "," + (topOffset + item.row * rowHeight) + ")"; }).call(function decorate(selection) {
      selection.select("rect").attr("width", columnWidth - 8).attr("height", rowHeight - 6).attr("fill", function cellFill(item) { return colorForCell(item); }).attr("stroke", function cellStroke(item) { return strokeForCell(item); }).attr("stroke-width", 1);
      selection.select("text.cell-direction").attr("x", (columnWidth - 8) / 2).attr("y", 14).attr("fill", function cellText(item) { return textColorForCell(item); }).text(function cellDirection(item) { return directionToken(item.direction); });
      selection.select("text.cell-confidence").attr("x", (columnWidth - 8) / 2).attr("y", 27).attr("fill", function cellText(item) { return textColorForCell(item); }).text(function cellConfidence(item) { return formatConfidence(item.confidence); });
      selection.select("title").text(function cellTitle(item) { return buildCellTitle(item); });
    });
  }

  function buildCells(state) {
    const cells = [];
    state.pairs.forEach(function eachPair(pairState, column) {
      state.sources.forEach(function eachSource(source, row) {
        const belief = isRecord(pairState.beliefs[source]) ? pairState.beliefs[source] : null;
        const direction = belief ? asDirection(belief.direction) : "missing";
        cells.push({
          pair: pairState.name,
          source: source,
          row: row,
          column: column,
          direction: direction,
          confidence: belief ? toNumber(belief.confidence) : null,
          agreement: classifyAgreement(direction, pairState.consensusDirection),
          consensusDirection: pairState.consensusDirection,
        });
      });
    });
    return cells;
  }

  function normalizeState(beliefData) {
    const grouped = toGroupedBeliefs(beliefData);
    const extraSources = [];
    Object.keys(grouped).forEach(function collectSources(pair) {
      Object.keys(grouped[pair]).forEach(function collectSource(source) {
        if (SOURCE_ORDER.indexOf(source) === -1 && extraSources.indexOf(source) === -1) extraSources.push(source);
      });
    });
    return {
      sources: SOURCE_ORDER.concat(extraSources.sort()),
      pairs: Object.keys(grouped).sort().map(function mapPair(pair) {
        const beliefs = grouped[pair];
        const consensus = computeConsensus(beliefs);
        return {
          name: pair,
          beliefs: beliefs,
          consensusDirection: consensus.direction,
          consensusStrength: consensus.strength,
          agreementCount: consensus.agreementCount,
          totalSources: consensus.totalSources,
        };
      }),
    };
  }

  function toGroupedBeliefs(value) {
    if (value === null || value === undefined) return {};
    if (isRecord(value) && isRecord(value.beliefs)) return toGroupedBeliefs(value.beliefs);
    if (Array.isArray(value)) {
      return value.filter(isRecord).reduce(function fromEntries(result, entry) {
        const pair = asText(entry.pair || entry.symbol, "");
        const source = asText(entry.source, "");
        if (!pair || !source) return result;
        result[pair] = result[pair] || {};
        result[pair][source] = entry;
        return result;
      }, {});
    }
    if (!isRecord(value)) return {};
    return Object.keys(value).reduce(function fromMap(result, pair) {
      if (!isRecord(value[pair])) return result;
      result[pair] = value[pair];
      return result;
    }, {});
  }

  function computeConsensus(beliefs) {
    const snapshots = Object.keys(isRecord(beliefs) ? beliefs : {}).map(function toSnapshot(source) { return beliefs[source]; }).filter(isRecord);
    if (snapshots.length === 0) return { direction: "neutral", strength: 0, agreementCount: 0, totalSources: 0 };
    const counts = {};
    snapshots.forEach(function countDirection(snapshot) {
      const direction = asDirection(snapshot.direction);
      counts[direction] = (counts[direction] || 0) + 1;
    });
    const agreementCount = Object.keys(counts).reduce(function maxCount(maximum, direction) { return Math.max(maximum, counts[direction]); }, 0);
    const requiredVotes = Math.ceil(snapshots.length * (2 / 3));
    const winners = Object.keys(counts).filter(function findWinner(direction) { return counts[direction] === agreementCount; });
    const direction = agreementCount >= requiredVotes && winners.length === 1 ? winners[0] : "neutral";
    const strength = direction === "neutral" ? 0 : round2(snapshots.filter(function matchDirection(snapshot) { return asDirection(snapshot.direction) === direction; }).reduce(function sumConfidence(total, snapshot) { return total + toNumber(snapshot.confidence); }, 0) / snapshots.length);
    return { direction: direction, strength: strength, agreementCount: agreementCount, totalSources: snapshots.length };
  }

  function installPlaceholderBridge() {
    if (!globalRoot || typeof globalRoot.renderPlaceholder !== "function" || globalRoot.renderPlaceholder.__beliefBridgeInstalled) return;
    const original = globalRoot.renderPlaceholder;
    const wrapped = function renderBeliefPlaceholder(targetId, payload, label) {
      if (targetId === "beliefs-content") {
        renderBeliefMatrix(payload, targetId);
        return;
      }
      return original(targetId, payload, label);
    };
    wrapped.__beliefBridgeInstalled = true;
    globalRoot.renderPlaceholder = wrapped;
  }

  installPlaceholderBridge();
  if (typeof document !== "undefined" && typeof document.addEventListener === "function") document.addEventListener("DOMContentLoaded", installPlaceholderBridge);
  if (globalRoot && typeof globalRoot.addEventListener === "function") globalRoot.addEventListener("load", installPlaceholderBridge);

  function buildSummary(state) {
    const consensusPairs = state.pairs.filter(function hasConsensus(pair) { return pair.consensusDirection !== "neutral"; }).length;
    return String(state.pairs.length) + " pairs | " + String(state.sources.length) + " sources | " + String(consensusPairs) + " consensus signals";
  }
  function formatConsensus(pairState) {
    if (pairState.totalSources === 0) return "No consensus";
    return directionLabel(pairState.consensusDirection) + " " + formatConfidence(pairState.consensusStrength) + " | " + String(pairState.agreementCount) + "/" + String(pairState.totalSources);
  }
  function buildCellTitle(item) { return (SOURCE_LABELS[item.source] || item.source) + " " + item.pair + ": " + directionLabel(item.direction) + " " + formatConfidence(item.confidence) + " vs " + directionLabel(item.consensusDirection) + " consensus"; }
  function classifyAgreement(direction, consensusDirection) { if (direction === "missing") return "missing"; if (consensusDirection === "neutral") return direction === "neutral" ? "neutral" : "mixed"; if (direction === consensusDirection) return "agree"; return direction === "neutral" ? "neutral" : "disagree"; }
  function colorForCell(item) { if (item.agreement === "agree") return item.direction === "bearish" ? "#b91c1c" : "#15803d"; if (item.agreement === "disagree") return "#f59e0b"; if (item.agreement === "neutral") return "#94a3b8"; if (item.agreement === "mixed") return "#cbd5e1"; return "#e2e8f0"; }
  function strokeForCell(item) { if (item.agreement === "agree") return item.direction === "bearish" ? "#7f1d1d" : "#166534"; if (item.agreement === "disagree") return "#b45309"; return "#94a3b8"; }
  function textColorForCell(item) { return item.agreement === "agree" || item.agreement === "disagree" ? "#ffffff" : "#0f172a"; }
  function consensusColor(direction) { return direction === "bullish" ? "#15803d" : direction === "bearish" ? "#b91c1c" : "#64748b"; }
  function directionToken(direction) { return direction === "bullish" ? "Bull" : direction === "bearish" ? "Bear" : direction === "neutral" ? "Flat" : "--"; }
  function directionLabel(direction) { return direction === "bullish" ? "Bullish" : direction === "bearish" ? "Bearish" : direction === "neutral" ? "Neutral" : "Missing"; }
  function formatConfidence(value) { return value === null || value === undefined ? "--" : toNumber(value).toFixed(2); }
  function round2(value) { return Math.round(toNumber(value) * 100) / 100; }
  function isRecord(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
  function toNumber(value) { const number = typeof value === "number" ? value : Number(value); return Number.isFinite(number) ? number : 0; }
  function asText(value, fallback) { return typeof value === "string" && value.length > 0 ? value : fallback; }
  function asDirection(value) { return value === "bullish" || value === "bearish" || value === "neutral" ? value : "neutral"; }

  return { renderBeliefMatrix: renderBeliefMatrix };
});
