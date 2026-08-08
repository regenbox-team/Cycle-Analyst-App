const state = {
  db: "",
  sessions: [],
  activeSession: "",
  selected: new Set(),
  lastPoints: [],
  lastCurve: [], originalPoints: [], excludedPoints: new Set(), excludedSessions: new Set(),
  source: "db", jsonKeys: [], monitorDevice: "", monitorMode: "supercycle_live"
};

const $ = (id) => document.getElementById(id);

function num(value, fallback = 0) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

async function getJson(url, options = {}) {
  const response = await fetch(url, options);
  const raw = await response.text();
  let data;
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch (error) {
    const hint = response.status === 404
      ? "Route unavailable. Restart Battery Curve Lab to load the updated server."
      : `Server returned HTML instead of JSON (HTTP ${response.status}). Check the Battery Curve Lab terminal.`;
    throw new Error(hint);
  }
  if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
  return data;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

async function loadDatabases() {
  const data = await getJson("/api/databases");
  const select = $("db-select");
  select.innerHTML = "";
  data.databases.forEach(db => {
    const option = document.createElement("option");
    option.value = db.path;
    option.textContent = `${db.name} (${db.size_mb} MB)`;
    select.appendChild(option);
  });
  if (data.databases[0]) {
    state.db = data.databases[0].path;
    select.value = state.db;
    await loadSessions();
  }
}

async function loadSessions() {
  state.source = $("source-select").value;
  if (state.source === "json") return loadJsonSessions();
  if (state.source === "monitor") return loadMonitorSessions();
  state.db = $("db-select").value;
  state.selected.clear();
  const data = await getJson(`/api/sessions?db=${encodeURIComponent(state.db)}`);
  state.sessions = data.sessions;
  renderSessions();
  if (state.sessions[0]) {
    await previewSession(state.sessions[0].session);
  }
}

async function loadJsonSessions() {
  const files = $("json-files").files;
  if (!files.length) { $("json-files").click(); return; }
  const body = new FormData(); Array.from(files).forEach(file => body.append("files", file));
  const data = await getJson("/api/json_sources", {method: "POST", body});
  state.jsonKeys = data.sources.map(source => source.key);
  state.sessions = data.sources.flatMap(source => source.sessions);
  state.selected.clear(); renderSessions();
}

async function loadMonitorSessions() {
  state.monitorDevice = $("monitor-device").value.trim(); state.monitorMode = $("monitor-mode").value.trim() || "supercycle_live";
  const data = await getJson(`/api/monitor_sessions?device=${encodeURIComponent(state.monitorDevice)}&mode=${encodeURIComponent(state.monitorMode)}`);
  state.monitorDevice = data.device; state.sessions = data.sessions; state.selected.clear(); renderSessions();
}

function renderSessions() {
  const filter = $("session-filter").value.trim().toLowerCase();
  const list = $("sessions-list");
  list.innerHTML = "";
  state.sessions
    .filter(s => !filter || s.session.toLowerCase().includes(filter))
    .forEach(session => {
      const row = document.createElement("div");
      row.className = `session-row ${session.session === state.activeSession ? "active" : ""}`;
      row.innerHTML = `
        <input type="checkbox" ${state.selected.has(session.session) ? "checked" : ""} aria-label="Select session">
        <div>
          <div class="session-name">${escapeHtml(session.session)}</div>
          <div class="session-stats">
            ${session.duration_min ?? "—"} min · ${session.samples ?? "remote"} samples<br>
            V ${session.voltage_min ?? "—"}-${session.voltage_max ?? "—"} · Ah ${session.ah_min ?? "—"}-${session.ah_max ?? "—"}<br>
            ${session.distance_km ?? "—"} km · W ${session.power_min ?? "—"}-${session.power_max ?? "—"}
          </div>
        </div>
        <button>View</button>
      `;
      row.querySelector("input").addEventListener("change", (event) => {
        if (event.target.checked) state.selected.add(session.session);
        else state.selected.delete(session.session);
      });
      row.querySelector("button").addEventListener("click", () => previewSession(session.session));
      list.appendChild(row);
    });
}

async function previewSession(session) {
  state.activeSession = session;
  renderSessions();
  const maxPoints = Math.max(200, Math.min(5000, num($("preview-samples").value, 1600)));
  const requestData = {source: state.source, db: state.db, session, json_keys: state.jsonKeys, device: state.monitorDevice, mode: state.monitorMode};
  const data = state.source === "db"
    ? await getJson(`/api/session_series?db=${encodeURIComponent(state.db)}&session=${encodeURIComponent(session)}&max_points=${maxPoints}`)
    : await getJson(`/api/session_series?max_points=${maxPoints}`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(requestData)});
  $("preview-title").textContent = session;
  const settled = data.initial_stabilized_voltage;
  const settledText = settled
    ? ` · initial stabilized voltage ${num(settled.voltage).toFixed(3)} V after ${num(settled.duration_s).toFixed(0)}s (max |I| ${num(settled.current_max_a).toFixed(2)} A)`
    : " · no stabilized voltage detected at session start";
  $("preview-meta").textContent = `${data.returned_samples} displayed points from ${data.total_samples} samples${settledText}`;
  drawMultiChart($("session-chart"), data.points);
}

function rangeFor(points, key) {
  const values = points.map(p => num(p[key], NaN)).filter(Number.isFinite);
  if (!values.length) return [0, 1];
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const pad = (max - min) * 0.08;
  return [min - pad, max + pad];
}

function pathFor(points, key, xMax, yMin, yMax, width, height, pad) {
  if (!points.length) return "";
  return points.map((p, i) => {
    const x = pad.left + (num(p.time_min) / Math.max(0.001, xMax)) * (width - pad.left - pad.right);
    const y = pad.top + (1 - ((num(p[key]) - yMin) / Math.max(0.001, yMax - yMin))) * (height - pad.top - pad.bottom);
    return `${i === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)}`;
  }).join(" ");
}

function drawSinglePlot(metric, points, xMax) {
  const width = 1100;
  const height = 150;
  const pad = { left: 52, right: 18, top: 14, bottom: 25 };
  const [yMin, yMax] = rangeFor(points, metric.key);
  const path = pathFor(points, metric.key, xMax, yMin, yMax, width, height, pad);
  const grid = [0, 0.5, 1].map(t => {
    const y = pad.top + t * (height - pad.top - pad.bottom);
    const val = yMax - t * (yMax - yMin);
    return `<line class="grid-line" x1="${pad.left}" y1="${y}" x2="${width - pad.right}" y2="${y}"></line>
      <text class="axis-label" x="8" y="${y + 3}">${val.toFixed(metric.decimals)}</text>`;
  }).join("");
  const xGrid = [0, 0.25, 0.5, 0.75, 1].map(t => {
    const x = pad.left + t * (width - pad.left - pad.right);
    return `<line class="grid-line" x1="${x}" y1="${pad.top}" x2="${x}" y2="${height - pad.bottom}"></line>
      <text class="axis-label" x="${x - 12}" y="${height - 7}">${(xMax * t).toFixed(0)}m</text>`;
  }).join("");
  return `
    <div class="plot">
      <div class="plot-title"><span>${metric.label}</span><span>${yMin.toFixed(metric.decimals)} to ${yMax.toFixed(metric.decimals)} ${metric.unit}</span></div>
      <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
        ${grid}${xGrid}
        <path class="metric-line ${metric.className}" d="${path}"></path>
      </svg>
    </div>
  `;
}

function drawMultiChart(container, points) {
  if (!points.length) {
    container.innerHTML = `<p>No samples for this session.</p>`;
    return;
  }
  const xMax = Math.max(...points.map(p => num(p.time_min, 0)), 1);
  const metrics = [
    { key: "voltage", label: "Voltage", unit: "V", decimals: 2, className: "voltage-line" },
    { key: "ah", label: "Ah consumed", unit: "Ah", decimals: 2, className: "ah-line" },
    { key: "power", label: "Power", unit: "W", decimals: 0, className: "power-line" },
    { key: "distance", label: "Distance", unit: "km", decimals: 2, className: "distance-line" },
  ];
  container.innerHTML = metrics.map(metric => drawSinglePlot(metric, points, xMax)).join("");
}

function payloadFromControls() {
  return {
    source: state.source,
    json_keys: state.jsonKeys,
    device: state.monitorDevice,
    mode: state.monitorMode,
    db: state.db,
    sessions: Array.from(state.selected),
    capacity_ah: num($("capacity-ah").value, 64),
    max_speed_kph: num($("max-speed-kph").value, 1),
    max_abs_current_a: num($("max-abs-current-a").value, 1.5),
    min_rest_seconds: num($("min-rest-seconds").value, 120),
    tail_seconds: num($("tail-seconds").value, 60),
    max_voltage_std: num($("max-voltage-std").value, 0.05),
    min_samples: num($("min-samples").value, 20),
    bin_percent: num($("bin-percent").value, 5),
    min_points_per_bin: 1,
    fallback_hz: 1
  };
}

function extractionControls() {
  return {
    capacity_ah: num($("capacity-ah").value, 64),
    max_speed_kph: num($("max-speed-kph").value, 1),
    max_abs_current_a: num($("max-abs-current-a").value, 1.5),
    min_rest_seconds: num($("min-rest-seconds").value, 120),
    tail_seconds: num($("tail-seconds").value, 60),
    max_voltage_std: num($("max-voltage-std").value, 0.05),
    min_samples: num($("min-samples").value, 20),
    bin_percent: num($("bin-percent").value, 5),
    min_points_per_bin: 1,
    fallback_hz: 1,
  };
}

async function addJsonToResult() {
  const files = $("result-json-files").files;
  if (!files.length) throw new Error("Choose at least one JSON export.");
  const status = $("result-add-json-status");
  status.textContent = "Importing and extracting stable points…";
  const body = new FormData();
  Array.from(files).forEach(file => body.append("files", file));
  const uploaded = await getJson("/api/json_sources", {method: "POST", body});
  const keys = uploaded.sources.map(source => source.key);
  const sessions = uploaded.sources.flatMap(source => source.sessions.map(item => item.session));
  const data = await getJson("/api/generate", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({source: "json", json_keys: keys, sessions, ...extractionControls()}),
  });
  const offset = state.originalPoints.length;
  const additions = data.points.map((point, index) => ({...point, _id: `${point.session}|${point.timestamp}|json-${offset + index}`}));
  state.originalPoints.push(...additions);
  additions.forEach(point => { state.excludedSessions.delete(point.session); state.excludedPoints.delete(point._id); });
  renderRetainedResult();
  status.textContent = `${additions.length} stable point${additions.length === 1 ? "" : "s"} added from ${sessions.length} session${sessions.length === 1 ? "" : "s"}.`;
  $("result-json-files").value = "";
}

async function generateCurve() {
  $("result-meta").textContent = "Generating...";
  const data = await getJson("/api/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payloadFromControls())
  });
  state.originalPoints = data.points.map((point, index) => ({...point, _id: `${point.session}|${point.timestamp}|${index}`}));
  state.excludedPoints.clear();
  state.excludedSessions.clear();
  state.lastPoints = state.originalPoints.slice();
  state.lastCurve = data.curve;
  $("result-meta").textContent = `${data.points.length} stable points · ${data.curve.length} curve points`;
  $("python-snippet").value = data.python_snippet;
  $("csv-link").href = data.downloads.csv;
  $("json-link").href = data.downloads.json;
  $("csv-link").hidden = false;
  $("json-link").hidden = false;
  renderRetainedResult();
}

function sessionColor(session) {
  let hash = 0;
  for (const char of String(session)) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0;
  return `hsl(${Math.abs(hash) % 360} 62% 43%)`;
}

function median(values) {
  const sorted = values.slice().sort((a,b) => a-b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function buildCurveFromPoints(points) {
  const bin = Math.max(.1, num($("bin-percent").value, 5));
  const bins = new Map();
  points.forEach(point => {
    const soc = Math.max(0, Math.min(100, Math.round(num(point.soc) / bin) * bin));
    if (!bins.has(soc)) bins.set(soc, []);
    bins.get(soc).push(point);
  });
  return Array.from(bins.entries()).sort((a,b) => a[0]-b[0]).map(([soc, items]) => ({soc, voltage: Number(median(items.map(item => num(item.voltage))).toFixed(3)), points: items.length}));
}

function renderRetainedResult() {
  state.lastPoints = state.originalPoints.filter(point => !state.excludedSessions.has(point.session) && !state.excludedPoints.has(point._id));
  state.lastCurve = buildCurveFromPoints(state.lastPoints);
  const sessions = Array.from(new Set(state.lastPoints.map(point => point.session))).sort();
  $("result-meta").textContent = `${state.lastPoints.length} retained stable points · ${state.lastCurve.length} curve points · ${sessions.length} sessions`;
  $("python-snippet").value = state.lastCurve.map(point => `    (${point.voltage.toFixed(3)}, ${point.soc.toFixed(1)}),`).join("\n");
  const jsonOutput = state.lastCurve.map(point => ({voltage: point.voltage, soc: point.soc}));
  $("json-link").href = URL.createObjectURL(new Blob([JSON.stringify(jsonOutput, null, 2) + "\n"], {type: "application/json"}));
  const csvHeader = "session,timestamp,ah_used,soc,voltage,voltage_std,duration_s,samples\n";
  const csvRows = state.lastPoints.map(point => [point.session, point.timestamp, point.ah_used, point.soc, point.voltage, point.voltage_std, point.duration_s, point.samples].map(value => `"${String(value ?? '').replaceAll('"', '""')}"`).join(",")).join("\n");
  $("csv-link").href = URL.createObjectURL(new Blob([csvHeader + csvRows + "\n"], {type: "text/csv"}));
  drawCurveChart($("curve-chart"), state.lastPoints, state.lastCurve);
  renderPointsTable(state.lastPoints);
  $("result-session-legend").innerHTML = sessions.map(session => `<span class="session-legend-item"><i style="background:${sessionColor(session)}"></i>${escapeHtml(session)}</span>`).join("");
  const allSessions = Array.from(new Set(state.originalPoints.map(point => point.session))).sort();
  $("retained-sessions").innerHTML = allSessions.map(session => {
    const retained = state.lastPoints.filter(point => point.session === session).length;
    const excluded = state.excludedSessions.has(session);
    return `<div class="retained-session ${excluded ? 'excluded' : ''}"><i style="background:${sessionColor(session)}"></i><span>${escapeHtml(session)}</span><small>${retained} point${retained === 1 ? '' : 's'}</small><button type="button" data-session="${escapeHtml(session)}">${excluded ? 'Restore' : 'Remove session'}</button></div>`;
  }).join("");
  $("retained-sessions").querySelectorAll("button[data-session]").forEach(button => button.addEventListener("click", () => {
    const session = button.dataset.session;
    if (state.excludedSessions.has(session)) state.excludedSessions.delete(session); else state.excludedSessions.add(session);
    renderRetainedResult();
  }));
}

function drawCurveChart(container, points, curve) {
  const width = 1000;
  const height = 340;
  const pad = { left: 58, right: 20, top: 20, bottom: 38 };
  const voltages = points.map(p => num(p.voltage)).concat(curve.map(p => num(p.voltage))).filter(Number.isFinite);
  if (!voltages.length) {
    container.innerHTML = `<p>No stable points found. Relax the extraction thresholds.</p>`;
    return;
  }
  let yMin = Math.min(...voltages);
  let yMax = Math.max(...voltages);
  const yPad = Math.max(0.2, (yMax - yMin) * 0.1);
  yMin -= yPad;
  yMax += yPad;
  const xFor = (soc) => pad.left + (1 - num(soc) / 100) * (width - pad.left - pad.right);
  const yFor = (voltage) => pad.top + (1 - ((num(voltage) - yMin) / Math.max(0.001, yMax - yMin))) * (height - pad.top - pad.bottom);
  const dots = points.map(p => `<circle class="point-dot" style="fill:${sessionColor(p.session)};stroke:${sessionColor(p.session)}" cx="${xFor(p.soc).toFixed(2)}" cy="${yFor(p.voltage).toFixed(2)}" r="5"><title>${escapeHtml(p.session)} · ${num(p.soc).toFixed(1)}% · ${num(p.voltage).toFixed(3)} V · ${num(p.ah_used).toFixed(2)} Ah</title></circle>`).join("");
  const line = curve.map((p, i) => `${i === 0 ? "M" : "L"} ${xFor(p.soc).toFixed(2)} ${yFor(p.voltage).toFixed(2)}`).join(" ");
  const grid = [0, 25, 50, 75, 100].map(soc => {
    const x = xFor(soc);
    return `<line class="grid-line" x1="${x}" y1="${pad.top}" x2="${x}" y2="${height - pad.bottom}"></line>
      <text class="axis-label" x="${x - 10}" y="${height - 12}">${soc}%</text>`;
  }).join("");
  const yGrid = [0, 0.25, 0.5, 0.75, 1].map(t => {
    const y = pad.top + t * (height - pad.top - pad.bottom);
    const v = yMax - t * (yMax - yMin);
    return `<line class="grid-line" x1="${pad.left}" y1="${y}" x2="${width - pad.right}" y2="${y}"></line>
      <text class="axis-label" x="10" y="${y + 4}">${v.toFixed(2)}V</text>`;
  }).join("");
  container.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      ${grid}${yGrid}
      ${dots}
      <path class="curve-line" d="${line}"></path>
    </svg>
  `;
}

function renderPointsTable(points) {
  if (!points.length) {
    $("points-table").innerHTML = `<p>No stable points.</p>`;
    return;
  }
  const rows = points.slice().sort((a, b) => num(a.soc) - num(b.soc)).map(p => `
    <tr>
      <td>${escapeHtml(p.session)}</td>
      <td>${escapeHtml(p.timestamp)}</td>
      <td>${num(p.soc).toFixed(1)}</td>
      <td>${num(p.voltage).toFixed(3)}</td>
      <td>${num(p.ah_used).toFixed(2)}</td>
      <td>${num(p.voltage_std).toFixed(3)}</td>
      <td>${num(p.duration_s).toFixed(0)}</td>
      <td><button type="button" class="remove-point" data-point-id="${escapeHtml(p._id)}">Remove</button></td>
    </tr>
  `).join("");
  $("points-table").innerHTML = `
    <table>
      <thead><tr><th>Session</th><th>Timestamp</th><th>SOC %</th><th>V</th><th>Ah</th><th>Std</th><th>Rest s</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
  $("points-table").querySelectorAll(".remove-point").forEach(button => button.addEventListener("click", () => {
    state.excludedPoints.add(button.dataset.pointId);
    renderRetainedResult();
  }));
}

$("reload-btn").addEventListener("click", () => {
  $("sessions-list").innerHTML = "<p>Loading…</p>";
  loadSessions().catch(error => {
    $("sessions-list").innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`;
  });
});
$("source-select").addEventListener("change", () => {
  const source = $("source-select").value;
  $("db-select").hidden = source !== "db";
  $("json-files").hidden = source !== "json";
  $("monitor-device").hidden = source !== "monitor";
  $("monitor-mode").hidden = source !== "monitor";
});
$("json-files").addEventListener("change", loadSessions);
$("restore-points-btn").addEventListener("click", () => {
  state.excludedPoints.clear(); state.excludedSessions.clear(); renderRetainedResult();
});
$("result-add-json-btn").addEventListener("click", () => {
  addJsonToResult().catch(error => { $("result-add-json-status").textContent = error.message; });
});
$("session-filter").addEventListener("input", renderSessions);
$("preview-samples").addEventListener("change", () => {
  if (state.activeSession) previewSession(state.activeSession);
});
$("select-visible-btn").addEventListener("click", () => {
  const filter = $("session-filter").value.trim().toLowerCase();
  state.sessions
    .filter(s => !filter || s.session.toLowerCase().includes(filter))
    .forEach(s => state.selected.add(s.session));
  renderSessions();
});
$("generate-btn").addEventListener("click", () => {
  generateCurve().catch(error => {
    $("result-meta").textContent = error.message;
  });
});

loadDatabases().catch(error => {
  $("sessions-list").innerHTML = `<p>${escapeHtml(error.message)}</p>`;
});
