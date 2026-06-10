const sourceEl = document.querySelector("#source");
const builderModeButton = document.querySelector("#builderModeButton");
const rawModeButton = document.querySelector("#rawModeButton");
const builderPanel = document.querySelector("#builderPanel");
const rawPanel = document.querySelector("#rawPanel");
const searchEventEl = document.querySelector("#searchEvent");
const searchDeckEl = document.querySelector("#searchDeck");
const searchPlayerEl = document.querySelector("#searchPlayer");
const searchFormatEl = document.querySelector("#searchFormat");
const searchArchetypeEl = document.querySelector("#searchArchetype");
const levelChecksEl = document.querySelector("#levelChecks");
const searchCardsEl = document.querySelector("#searchCards");
const searchMainDeckEl = document.querySelector("#searchMainDeck");
const searchSideboardEl = document.querySelector("#searchSideboard");
const searchDateStartEl = document.querySelector("#searchDateStart");
const searchDateEndEl = document.querySelector("#searchDateEnd");
const cardAliasesEl = document.querySelector("#cardAliases");
const projectionEl = document.querySelector("#projection");
const scopeEl = document.querySelector("#scope");
const clusterSpaceEl = document.querySelector("#clusterSpace");
const clusterMethodEl = document.querySelector("#clusterMethod");
const outlierModeControlEl = document.querySelector("#outlierModeControl");
const outlierModeEl = document.querySelector("#outlierMode");
const clusterKControlEl = document.querySelector("#clusterKControl");
const clusterKEl = document.querySelector("#clusterK");
const scaleClustersEl = document.querySelector("#scaleClusters");
const analyzeButton = document.querySelector("#analyzeButton");
const demoButton = document.querySelector("#demoButton");
const statusEl = document.querySelector("#status");
const plotEl = document.querySelector("#plot");
const tooltipEl = document.querySelector("#tooltip");
const metricsEl = document.querySelector("#metrics");
const summaryEl = document.querySelector("#summary");
const legendEl = document.querySelector("#legend");
const cardsEl = document.querySelector("#cards");
const archetypeViewerEl = document.querySelector("#archetypeViewer");

const colors = [
  "#2b6cb0",
  "#c2410c",
  "#15803d",
  "#7c3aed",
  "#be123c",
  "#0f766e",
  "#a16207",
  "#4338ca",
];

let currentResult = null;
let selectedArchetypeCluster = null;
let activeInputMode = "builder";
let searchOptions = {
  formats: [{ value: "", label: "All" }],
  archetypes: {},
  levels: [
    { code: "P", label: "Professional", checked: true },
    { code: "M", label: "Major", checked: true },
    { code: "C", label: "Competitive", checked: true },
    { code: "R", label: "Regular", checked: true },
  ],
};
const defaultSearchFormat = "PAU";
const defaultSearchDateStart = "2026-01-01";

function setStatus(text, kind = "idle") {
  statusEl.textContent = text;
  statusEl.dataset.kind = kind;
}

function updateClusterControls() {
  clusterKControlEl.hidden = clusterMethodEl.value !== "fixed";
  outlierModeControlEl.hidden = clusterMethodEl.value !== "auto";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function setInputMode(mode) {
  activeInputMode = mode;
  const useBuilder = mode === "builder";
  builderPanel.hidden = !useBuilder;
  rawPanel.hidden = useBuilder;
  builderModeButton.classList.toggle("active", useBuilder);
  rawModeButton.classList.toggle("active", !useBuilder);
}

function optionMarkup(option) {
  return `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`;
}

function populateSelect(selectEl, options) {
  const previous = selectEl.value;
  selectEl.innerHTML = options.map(optionMarkup).join("");
  if (options.some((option) => option.value === previous)) {
    selectEl.value = previous;
  }
}

function todayForDateInput() {
  const now = new Date();
  const day = String(now.getDate()).padStart(2, "0");
  const month = String(now.getMonth() + 1).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

function dateInputToMtgtop8(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  return match ? `${match[3]}/${match[2]}/${match[1]}` : value;
}

function applySearchDefaults() {
  if (!searchDateStartEl.value) searchDateStartEl.value = defaultSearchDateStart;
  if (!searchDateEndEl.value) searchDateEndEl.value = todayForDateInput();
  if (!searchFormatEl.value && [...searchFormatEl.options].some((option) => option.value === defaultSearchFormat)) {
    searchFormatEl.value = defaultSearchFormat;
  }
}

function updateArchetypeOptions() {
  const options = searchOptions.archetypes[searchFormatEl.value] || [{ value: "", label: "All" }];
  populateSelect(searchArchetypeEl, options);
  searchArchetypeEl.disabled = options.length <= 1;
}

function renderLevelChecks() {
  levelChecksEl.innerHTML = searchOptions.levels
    .map(
      (level) => `
        <label class="check-row">
          <input type="checkbox" data-level="${escapeHtml(level.code)}" ${level.checked ? "checked" : ""} />
          <span>${escapeHtml(level.label)}</span>
        </label>
      `
    )
    .join("");
}

function populateSearchOptions(options) {
  searchOptions = {
    formats: options.formats?.length ? options.formats : searchOptions.formats,
    archetypes: options.archetypes || {},
    levels: options.levels?.length ? options.levels : searchOptions.levels,
  };
  populateSelect(searchFormatEl, searchOptions.formats);
  applySearchDefaults();
  renderLevelChecks();
  updateArchetypeOptions();
}

async function loadSearchOptions() {
  renderLevelChecks();
  try {
    const response = await fetch("/api/search-options");
    const options = await response.json();
    if (!response.ok) throw new Error(options.error || "Search options unavailable.");
    populateSearchOptions(options);
  } catch (error) {
    setStatus(error.message, "error");
  }
}

function collectSearchCriteria() {
  const levels = [...levelChecksEl.querySelectorAll("input[data-level]:checked")].map((input) => input.dataset.level);
  return {
    event: searchEventEl.value.trim(),
    deck: searchDeckEl.value.trim(),
    player: searchPlayerEl.value.trim(),
    format: searchFormatEl.value,
    archetype: searchArchetypeEl.disabled ? "" : searchArchetypeEl.value,
    levels,
    cards: searchCardsEl.value.trim(),
    mainDeck: searchMainDeckEl.checked,
    sideboard: searchSideboardEl.checked,
    dateStart: dateInputToMtgtop8(searchDateStartEl.value.trim()),
    dateEnd: dateInputToMtgtop8(searchDateEndEl.value.trim()),
  };
}

function metric(label, value) {
  return `<span class="metric">${escapeHtml(label)} ${escapeHtml(value)}</span>`;
}

function clusterColor(cluster) {
  if (Number(cluster) < 0) return "#98a2b3";
  return colors[Number(cluster) % colors.length];
}

function clusterLabel(cluster) {
  return Number(cluster) < 0 ? "Noise" : `Cluster ${Number(cluster) + 1}`;
}

function paddedExtent(values) {
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const pad = (max - min) * 0.08;
  return [min - pad, max + pad];
}

function niceTicks(min, max, count = 6) {
  const span = max - min;
  const raw = span / Math.max(1, count - 1);
  const pow = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * pow).find((candidate) => candidate >= raw) || raw;
  const start = Math.ceil(min / step) * step;
  const ticks = [];
  for (let value = start; value <= max + step * 0.5; value += step) {
    ticks.push(Number(value.toFixed(6)));
  }
  return ticks;
}

function axisLabel(label, value) {
  if (value == null) return label;
  return `${label} (${(Number(value || 0) * 100).toFixed(1)}%)`;
}

function varianceMetric(value) {
  return value == null ? "n/a" : `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function drawPlot(result) {
  const points = result.points;
  const diagnostics = result.diagnostics;
  const xAxis = axisLabel(diagnostics.axis_labels?.[0] || "Axis 1", diagnostics.explained_variance?.[0]);
  const yAxis = axisLabel(diagnostics.axis_labels?.[1] || "Axis 2", diagnostics.explained_variance?.[1]);
  const width = plotEl.clientWidth || 900;
  const height = Math.max(plotEl.clientHeight || 520, 420);
  const margin = { top: 28, right: 32, bottom: 54, left: 66 };
  const innerW = width - margin.left - margin.right;
  const innerH = height - margin.top - margin.bottom;
  const [minX, maxX] = paddedExtent(points.map((point) => point.x));
  const [minY, maxY] = paddedExtent(points.map((point) => point.y));
  const sx = (value) => margin.left + ((value - minX) / (maxX - minX)) * innerW;
  const sy = (value) => margin.top + innerH - ((value - minY) / (maxY - minY)) * innerH;

  const xTicks = niceTicks(minX, maxX);
  const yTicks = niceTicks(minY, maxY);
  plotEl.setAttribute("viewBox", `0 0 ${width} ${height}`);
  plotEl.setAttribute("width", "100%");
  plotEl.setAttribute("height", "100%");
  let svg = "";
  svg += `<rect x="0" y="0" width="${width}" height="${height}" fill="#fff"/>`;

  for (const tick of xTicks) {
    const x = sx(tick);
    svg += `<line class="grid-line" x1="${x}" y1="${margin.top}" x2="${x}" y2="${margin.top + innerH}"/>`;
    svg += `<text x="${x}" y="${height - 20}" text-anchor="middle" fill="#667085" font-size="11">${tick}</text>`;
  }
  for (const tick of yTicks) {
    const y = sy(tick);
    svg += `<line class="grid-line" x1="${margin.left}" y1="${y}" x2="${margin.left + innerW}" y2="${y}"/>`;
    svg += `<text x="${margin.left - 10}" y="${y + 4}" text-anchor="end" fill="#667085" font-size="11">${tick}</text>`;
  }
  if (minX < 0 && maxX > 0) {
    svg += `<line class="zero-line" x1="${sx(0)}" y1="${margin.top}" x2="${sx(0)}" y2="${margin.top + innerH}"/>`;
  }
  if (minY < 0 && maxY > 0) {
    svg += `<line class="zero-line" x1="${margin.left}" y1="${sy(0)}" x2="${margin.left + innerW}" y2="${sy(0)}"/>`;
  }
  svg += `<text x="${margin.left + innerW / 2}" y="${height - 4}" text-anchor="middle" fill="#364152" font-size="12">${escapeHtml(xAxis)}</text>`;
  svg += `<text transform="translate(18 ${margin.top + innerH / 2}) rotate(-90)" text-anchor="middle" fill="#364152" font-size="12">${escapeHtml(yAxis)}</text>`;

  const renderPoint = (point, index) => {
    const x = sx(point.x);
    const y = sy(point.y);
    const color = clusterColor(point.cluster);
    const href = escapeHtml(point.deck_url || `https://www.mtgtop8.com/event?d=${point.deck_id}`);
    const radius = pointRadius(point);
    svg += `<a class="point-link" href="${href}" target="_blank" rel="noopener">`;
    if (point.featured_finish) {
      svg += starPath(
        x,
        y,
        radius,
        "point point-shape point-star big-star-point",
        `data-index="${index}" fill="${color}"`
      );
    } else {
      const hitRadius = Math.max(12, radius + 5);
      svg += `<circle class="point-shape point-dot" cx="${x}" cy="${y}" r="${radius}" fill="${color}"/>`;
      svg += `<circle class="point" data-index="${index}" cx="${x}" cy="${y}" r="${hitRadius}" fill="transparent"/>`;
    }
    svg += `</a>`;
  };

  points.forEach((point, index) => {
    if (!point.featured_finish) renderPoint(point, index);
  });
  points.forEach((point, index) => {
    if (point.featured_finish) renderPoint(point, index);
  });

  plotEl.innerHTML = svg;
  bindPointEvents(points);
}

function pointRadius(point) {
  if (point.featured_finish) return 13.5;
  return 6.5;
}

function starPath(cx, cy, radius, className = "point-star", attrs = "") {
  const points = [];
  for (let i = 0; i < 10; i += 1) {
    const angle = -Math.PI / 2 + (i * Math.PI) / 5;
    const r = i % 2 === 0 ? radius : radius * 0.45;
    points.push(`${cx + Math.cos(angle) * r},${cy + Math.sin(angle) * r}`);
  }
  return `<polygon class="${className}" ${attrs} points="${points.join(" ")}"/>`;
}

function starSummary(point) {
  const stars = Number(point.tournament_stars || 0);
  if (point.featured_finish && point.finish_percent != null) {
    return `Big-star top ${(Number(point.finish_percent) * 100).toFixed(1)}%`;
  }
  if (point.featured_finish) return "Big-star Top 8";
  if (point.tournament_big_star) return "Big-star event";
  if (stars === 1) return "1 star event";
  if (stars > 1) return `${stars} star event`;
  return "Unstarred event";
}

function manaPips(colors) {
  if (!colors?.length) return "";
  return `
    <span class="mana-pips" aria-label="Deck colors ${escapeHtml(colors.join(""))}">
      ${colors
        .map((color) => `<img class="mana-pip" src="/mana/${escapeHtml(color)}.svg" alt="${escapeHtml(color)}" />`)
        .join("")}
    </span>
  `;
}

function bindPointEvents(points) {
  document.querySelectorAll(".point").forEach((node) => {
    const updateTooltip = (event) => {
      const point = points[Number(event.currentTarget.dataset.index)];
      tooltipEl.innerHTML = `
        <strong>${escapeHtml(point.player || "Unknown player")}</strong>
        ${manaPips(point.colors)}
        ${escapeHtml(point.deck_name || "Deck")}<br>
        ${escapeHtml(clusterLabel(point.cluster))}<br>
        ${escapeHtml(point.event || "")}<br>
        ${escapeHtml(point.rank || "")} ${escapeHtml(point.date || "")}<br>
        ${escapeHtml(starSummary(point))}<br>
        ${escapeHtml(point.total)} cards
      `;
      tooltipEl.hidden = false;
      const wrap = document.querySelector(".plot-wrap").getBoundingClientRect();
      const tooltipRect = tooltipEl.getBoundingClientRect();
      const gap = 14;
      const edgePadding = 8;
      let left = event.clientX - wrap.left + gap;
      let top = event.clientY - wrap.top + gap;

      if (left + tooltipRect.width + edgePadding > wrap.width) {
        left = event.clientX - wrap.left - tooltipRect.width - gap;
      }
      if (top + tooltipRect.height + edgePadding > wrap.height) {
        top = event.clientY - wrap.top - tooltipRect.height - gap;
      }

      left = Math.max(edgePadding, Math.min(left, wrap.width - tooltipRect.width - edgePadding));
      top = Math.max(edgePadding, Math.min(top, wrap.height - tooltipRect.height - edgePadding));
      tooltipEl.style.left = `${left}px`;
      tooltipEl.style.top = `${top}px`;
    };
    const hideTooltip = () => {
      tooltipEl.hidden = true;
    };
    node.addEventListener("mouseenter", updateTooltip);
    node.addEventListener("mousemove", updateTooltip);
    node.addEventListener("mouseleave", hideTooltip);
    node.addEventListener("pointerenter", updateTooltip);
    node.addEventListener("pointermove", updateTooltip);
    node.addEventListener("pointerleave", hideTooltip);
  });
}

function renderResult(result) {
  currentResult = result;
  selectedArchetypeCluster = null;
  const d = result.diagnostics;
  const xLabel = d.axis_labels?.[0] || "Axis 1";
  const yLabel = d.axis_labels?.[1] || "Axis 2";
  metricsEl.innerHTML = [
    metric("Decks", d.deck_count),
    metric("Cards", d.card_columns),
    metric("Features", d.feature_columns),
    metric(xLabel, varianceMetric(d.explained_variance[0])),
    metric(yLabel, varianceMetric(d.explained_variance[1])),
    metric("Silhouette", d.silhouette == null ? "n/a" : d.silhouette.toFixed(3)),
    metric("Big Top 2%", `${d.featured_decks}/${d.deck_count}`),
  ].join("");
  const noiseDecks = Number(d.noise_decks || 0);
  const noiseNote = noiseDecks > 0 ? ` · ${noiseDecks} noise ${noiseDecks === 1 ? "deck" : "decks"}` : "";
  const outlierNote =
    d.cluster_method === "auto-linkage" && d.outlier_mode_requested === "auto"
      ? ` · auto outliers: ${d.outlier_mode === "noise" ? "noise" : "keep"}`
      : "";
  const clusterNote =
    d.cluster_method === "auto-linkage"
      ? `auto hierarchical · ${d.auto_clusters} clusters${noiseNote}${outlierNote} · min branch ${d.min_branch_size}`
      : d.cluster_method === "hdbscan"
      ? `HDBSCAN min ${d.min_cluster_size} · samples ${d.min_samples}`
      : d.cluster_space === "deck" && d.distance_metric
      ? `${d.distance_metric} ${d.cluster_method}`
      : `${d.scale_clusters ? "standardized" : "raw"} ${d.cluster_method}`;
  const clusterSpace = d.cluster_space === "plot" ? "plot-space" : "deck-space";
  summaryEl.textContent = `${d.scope} · ${d.projection_label} · ${clusterSpace} ${clusterNote}`;
  drawPlot(result);
  drawLegend(result);
  drawCards(result);
  drawClusterArchetypes(result);
}

function ensureSelectedArchetypeCluster(result) {
  const clusters = result.cluster_archetypes || [];
  if (!clusters.length) return null;
  if (
    selectedArchetypeCluster === null ||
    !clusters.some((cluster) => Number(cluster.cluster) === Number(selectedArchetypeCluster))
  ) {
    selectedArchetypeCluster = (clusters.find((cluster) => Number(cluster.cluster) >= 0) || clusters[0]).cluster;
  }
  return clusters.find((cluster) => Number(cluster.cluster) === Number(selectedArchetypeCluster)) || clusters[0];
}

function drawLegend(result) {
  const counts = new Map();
  result.points.forEach((point) => counts.set(point.cluster, (counts.get(point.cluster) || 0) + 1));
  legendEl.innerHTML = [...counts.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([cluster, count]) => `
      <div class="legend-item">
        <span class="swatch" style="background:${clusterColor(cluster)}"></span>
        <span>${clusterLabel(cluster)} · ${count} decks</span>
      </div>
    `)
    .join("");
}

function drawCards(result) {
  if (!result.card_differences.length) {
    cardsEl.innerHTML = `<div class="cluster-detail">No comparison cluster.</div>`;
    return;
  }
  ensureSelectedArchetypeCluster(result);
  cardsEl.innerHTML = result.card_differences
    .map((cluster) => {
      const higher = cluster.higher.slice(0, 5).map((item) => `${escapeHtml(item.card)} (${item.diff.toFixed(1)})`).join(", ");
      const lower = cluster.lower.slice(0, 3).map((item) => `${escapeHtml(item.card)} (${item.diff.toFixed(1)})`).join(", ");
      const active = Number(cluster.cluster) === Number(selectedArchetypeCluster) ? " active" : "";
      return `
        <button class="cluster-detail cluster-card${active}" type="button" data-cluster="${cluster.cluster}" style="--cluster-color:${clusterColor(cluster.cluster)}">
          <strong>${clusterLabel(cluster.cluster)}</strong><br>
          More: ${higher || "n/a"}<br>
          Less: ${lower || "n/a"}
        </button>
      `;
    })
    .join("");

  cardsEl.querySelectorAll("button[data-cluster]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedArchetypeCluster = Number(button.dataset.cluster);
      drawCards(result);
      drawClusterArchetypes(result);
    });
  });
}

function pctText(value) {
  return `${Math.round(Number(value || 0) * 100)}%`;
}

function distEntries(card) {
  return Object.entries(card.dist || {})
    .map(([qty, count]) => ({ qty: Number(qty), count: Number(count) }))
    .sort((a, b) => b.qty - a.qty);
}

function dominantQty(card) {
  let best = null;
  distEntries(card).forEach((entry) => {
    if (entry.qty <= 0) return;
    if (!best || entry.count > best.count || (entry.count === best.count && entry.qty > best.qty)) {
      best = entry;
    }
  });
  return best?.qty;
}

function qtyPills(card, n) {
  const dominant = dominantQty(card);
  const entries = distEntries(card)
    .filter((entry) => entry.qty > 0)
    .map((entry) => ({
      ...entry,
      percent: n ? Math.round((entry.count / n) * 100) : 0,
    }));
  const visible = entries.filter((entry) => entry.percent >= 5 || entry.qty === dominant).slice(0, 4);
  return (visible.length ? visible : entries.slice(0, 1))
    .map((entry) => {
      const percent = n ? Math.round((entry.count / n) * 100) : 0;
      const active = entry.qty === dominant ? " active" : "";
      return `<span class="qty-pill${active}">x${entry.qty}<span>${percent}%</span></span>`;
    })
    .join("");
}

function archetypeCardRow(card, n, color) {
  const playedPct = Math.round(Number(card.played_pct || 0) * 100);
  const width = Math.max(3, playedPct);
  return `
    <div class="archetype-card-row">
      <div class="archetype-card-name">${escapeHtml(card.name)}</div>
      <div class="archetype-card-stats">
        <div class="qty-pills">${qtyPills(card, n)}</div>
        <div class="archetype-bar" aria-hidden="true">
          <span style="width:${width}%; background:${escapeHtml(color)}"></span>
        </div>
        <span class="archetype-pct">${playedPct}%</span>
      </div>
    </div>
  `;
}

function archetypeSection(section, n, color) {
  const rows = section.cards.map((card) => archetypeCardRow(card, n, color)).join("");
  const more = section.omitted
    ? `<div class="archetype-more">+ ${section.omitted} more</div>`
    : "";
  return `
    <section class="archetype-section">
      <div class="archetype-section-title">${escapeHtml(section.label)}</div>
      ${rows || `<div class="cluster-detail">No cards.</div>`}
      ${more}
    </section>
  `;
}

function drawClusterArchetypes(result) {
  const clusters = result.cluster_archetypes || [];
  if (!clusters.length) {
    archetypeViewerEl.innerHTML = `<div class="cluster-detail">No cluster viewer data.</div>`;
    return;
  }

  const selected = ensureSelectedArchetypeCluster(result);
  const color = clusterColor(selected.cluster);
  const sections = selected.sections.length
    ? selected.sections.map((section) => archetypeSection(section, selected.n, color)).join("")
    : `<div class="cluster-detail">No card-level data for this cluster.</div>`;

  archetypeViewerEl.innerHTML = `
    <div class="archetype-heading">
      <strong>${escapeHtml(clusterLabel(selected.cluster))}</strong>
      <span>${escapeHtml(selected.n)} decks</span>
    </div>
    <div class="archetype-sections">${sections}</div>
  `;
}

async function analyze() {
  setStatus("Fetching...");
  analyzeButton.disabled = true;
  try {
    const response = await fetch("/api/analyze-stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source: activeInputMode === "raw" ? sourceEl.value : "",
        search: activeInputMode === "builder" ? collectSearchCriteria() : null,
        aliases: cardAliasesEl.value,
        projection: projectionEl.value,
        scope: scopeEl.value,
        clusterSpace: clusterSpaceEl.value,
        clusterMethod: clusterMethodEl.value,
        outlierMode: outlierModeEl.value,
        clusterK: clusterMethodEl.value === "fixed" ? Number(clusterKEl.value) : null,
        scaleClusters: scaleClustersEl.checked,
      }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.error || "Analysis failed.");
    }

    const result = await readAnalysisStream(response);
    renderResult(result);
    setStatus("Done");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    analyzeButton.disabled = false;
  }
}

async function readAnalysisStream(response) {
  if (!response.body) {
    return response.json();
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result = null;

  const handleLine = (line) => {
    if (!line.trim()) return;
    const message = JSON.parse(line);
    if (message.status) setStatus(message.status);
    if (message.error) throw new Error(message.error);
    if (message.result) result = message.result;
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop();
    lines.forEach(handleLine);
  }

  buffer += decoder.decode();
  if (buffer) handleLine(buffer);
  if (!result) throw new Error("Analysis finished without a result.");
  return result;
}

builderModeButton.addEventListener("click", () => setInputMode("builder"));
rawModeButton.addEventListener("click", () => setInputMode("raw"));
searchFormatEl.addEventListener("change", updateArchetypeOptions);
clusterMethodEl.addEventListener("change", updateClusterControls);
analyzeButton.addEventListener("click", analyze);
window.addEventListener("resize", () => {
  if (currentResult) drawPlot(currentResult);
});
demoButton.addEventListener("click", async () => {
  setStatus("Loading demo...");
  try {
    const response = await fetch("/api/demo-source");
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || "Demo unavailable.");
    setInputMode("raw");
    sourceEl.value = body.source;
    setStatus("Demo loaded");
  } catch (error) {
    setStatus(error.message, "error");
  }
});
updateClusterControls();
setInputMode("builder");
applySearchDefaults();
loadSearchOptions();
