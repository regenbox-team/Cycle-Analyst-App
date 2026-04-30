/* ====== GAUGE GEOMETRY & GLOBALS ====== */
const RADIUS = 90;
const CENTER_X = 100;
const CENTER_Y = 100;
const START_ANGLE = -126;
const END_ANGLE = 126;

let isLongRange = false;
let metricsPaused = false;
const photoPreviewState = {
  lastImageKey: null
};
const powerHistoryState = {
  motor: true,
  human: true,
  solar: true,
  points: []
};

/* === AMP ARC CONFIG === */
const AMP_MIN = -50;   // A
const AMP_MAX = 100;   // A
const AMP_ARC_RADIUS = RADIUS; // overlay same radius as power

/* ====== UTILS ====== */
function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

// Coerce anything to a finite number (else fallback)
function num(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function polarToCartesian(cx, cy, r, angleDeg) {
  const rad = (angleDeg - 90) * Math.PI / 180;
  return {
    x: cx + r * Math.cos(rad),
    y: cy + r * Math.sin(rad)
  };
}

function describeArc(cx, cy, r, startAngle, endAngle, sweep = 0) {
  const start = polarToCartesian(cx, cy, r, endAngle);
  const end = polarToCartesian(cx, cy, r, startAngle);
  const largeArcFlag = Math.abs(endAngle - startAngle) <= 180 ? "0" : "1";
  return `M ${start.x} ${start.y} A ${r} ${r} 0 ${largeArcFlag} ${sweep} ${end.x} ${end.y}`;
}

function setArcPath(id, value, min, max) {
  const percent = clamp((num(value) - min) / (max - min), 0, 1);
  const angle = START_ANGLE + percent * (END_ANGLE - START_ANGLE);
  const path = describeArc(CENTER_X, CENTER_Y, RADIUS, START_ANGLE, angle, 0);
  const arc = document.getElementById(id);
  if (arc) arc.setAttribute("d", path);
}

function setPowerArcPath(id, value) {
  const arc = document.getElementById(id);
  if (!arc) return;

  const v = num(value, 0);
  const zeroAngle = -42;

  if (v >= 0) {
    const clamped = Math.min(v, 4000);
    const percent = clamped / 4000;
    const endAngle = zeroAngle + percent * (END_ANGLE - zeroAngle);
    const path = describeArc(CENTER_X, CENTER_Y, RADIUS, zeroAngle, endAngle, 0); // clockwise
    arc.setAttribute("d", path);
  } else {
    const clamped = Math.max(v, -2000);
    const percent = clamped / -2000;
    const endAngle = zeroAngle - percent * (zeroAngle - START_ANGLE);
    const path = describeArc(CENTER_X, CENTER_Y, RADIUS, zeroAngle, endAngle, 1); // counter-clockwise
    arc.setAttribute("d", path);
  }
}

function setBackgroundArc(id) {
  const path = describeArc(CENTER_X, CENTER_Y, RADIUS, START_ANGLE, END_ANGLE, 0);
  const arc = document.getElementById(id);
  if (arc) arc.setAttribute("d", path);
}

/* ====== TICKS ====== */
function drawTicks(
  containerId,
  min,
  max,
  step,
  majorStep,
  radiusOuter,
  radiusInnerMajor,
  radiusInnerMinor,
  startAngle = START_ANGLE,
  endAngle = END_ANGLE,
  labelRadiusOffset = -20
) {
  const container = document.getElementById(containerId);
  if (!container) return;

  for (let val = min; val <= max; val += step) {
    const percent = (val - min) / (max - min);
    const angle = startAngle + percent * (endAngle - startAngle);
    const isMajor = (val - min) % majorStep === 0;

    const outer = polarToCartesian(CENTER_X, CENTER_Y, radiusOuter, angle);
    const inner = polarToCartesian(
      CENTER_X,
      CENTER_Y,
      isMajor ? radiusInnerMajor : radiusInnerMinor,
      angle
    );

    const tick = document.createElementNS("http://www.w3.org/2000/svg", "line");
    tick.setAttribute("x1", outer.x);
    tick.setAttribute("y1", outer.y);
    tick.setAttribute("x2", inner.x);
    tick.setAttribute("y2", inner.y);
    tick.setAttribute("stroke", "#999");
    tick.setAttribute("stroke-width", isMajor ? "2" : "1");
    container.appendChild(tick);

    if (isMajor) {
      const labelPos = polarToCartesian(
        CENTER_X,
        CENTER_Y,
        radiusOuter + labelRadiusOffset,
        angle
      );
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", labelPos.x);
      label.setAttribute("y", labelPos.y);
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("font-size", "8");
      label.setAttribute("fill", "#333");
      label.textContent = val.toString();
      container.appendChild(label);
    }
  }
}

/* ====== UI UPDATERS ====== */

function updateHeaderMode() {
  // Find active flag in dashboard
  const activeFlag = document.querySelector('#primary-flag-row .flag-pill.active');
  const modeButton = document.getElementById('mode-button');

  if (activeFlag && modeButton) {
    modeButton.textContent = activeFlag.textContent;
    modeButton.setAttribute('data-flag', activeFlag.getAttribute('data-flag'));
    // Copy style (color/bg) from active pill
    modeButton.className = 'flag-pill active';
    modeButton.dataset.flag = activeFlag.dataset.flag;
  }
}

function rotateNeedle(id, value, min, max, labelText = null, labelId = null, color = "#000") {
  const clamped = clamp(num(value, 0), min, max);
  const angle = START_ANGLE + ((clamped - min) / (max - min)) * (END_ANGLE - START_ANGLE);
  const line = document.getElementById(id);
  if (line) line.setAttribute("transform", `rotate(${angle} ${CENTER_X} ${CENTER_Y})`);

  if (labelText !== null && labelId !== null) {
    let label = document.getElementById(labelId);
    if (!label) {
      label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("id", labelId);
      label.setAttribute("font-size", "9");
      label.setAttribute("font-weight", "bold");
      label.setAttribute("fill", color);
      label.setAttribute("text-anchor", "middle");
      const container = line?.parentNode;
      if (container) container.appendChild(label);
    }
    const pos = polarToCartesian(CENTER_X, CENTER_Y, RADIUS + 5, angle);
    label.setAttribute("x", pos.x.toFixed(1));
    label.setAttribute("y", pos.y.toFixed(1));
    label.textContent = labelText;
  }
}

function updateSpeedometer(speed, avg, max) {
  const s = clamp(num(speed, 0), 0, 50);
  setArcPath("speed-arc", s, 0, 50);
  rotateNeedle("avg-speed-line", num(avg, 0), 0, 50, num(avg, 0).toFixed(0), "avg-speed-label", "blue");
  rotateNeedle("max-speed-line", num(max, 0), 0, 50, num(max, 0).toFixed(0), "max-speed-label", "red");
  document.getElementById("speed-number").textContent = s.toFixed(0);
  document.getElementById("speed-unit").textContent = "km/h";
}

function updatePowerMeter(live, avg, max, amps) {
  const l = clamp(num(live, 0), -2000, 4000);
  setPowerArcPath("power-arc", l);
  rotateNeedle("avg-power-line", num(avg, 0), -2000, 4000, num(avg, 0).toFixed(0), "avg-power-label", "blue");
  rotateNeedle("max-power-line", num(max, 0), -2000, 4000, num(max, 0).toFixed(0), "max-power-label", "red");
  document.getElementById("power-number").textContent = l.toFixed(0);
  document.getElementById("power-unit").textContent = "W";

  document.getElementById("amp-number").textContent = num(amps, 0).toFixed(1);
  document.getElementById("amp-unit").textContent = "A";
}

// Amp arc centered at 0 A (mirror the power split)
function updateAmpArc(amps) {
  const a = clamp(num(amps, 0), AMP_MIN, AMP_MAX);
  const zeroAngle = -42;
  let d;

  if (a >= 0) {
    // 0..AMP_MAX → zeroAngle..END_ANGLE (clockwise)
    const t = AMP_MAX === 0 ? 0 : a / AMP_MAX;
    const endAngle = zeroAngle + t * (END_ANGLE - zeroAngle);
    d = describeArc(CENTER_X, CENTER_Y, AMP_ARC_RADIUS, zeroAngle, endAngle, 0);
  } else {
    // 0..AMP_MIN → zeroAngle..START_ANGLE (counter‑clockwise)
    const t = AMP_MIN === 0 ? 0 : Math.abs(a) / Math.abs(AMP_MIN);
    const endAngle = zeroAngle - t * (zeroAngle - START_ANGLE);
    d = describeArc(CENTER_X, CENTER_Y, AMP_ARC_RADIUS, zeroAngle, endAngle, 1);
  }

  const el = document.getElementById("amp-arc");
  if (el) el.setAttribute("d", d);
}

function updateAuxMeter(prefix, live, avg, max) {
  const clampedLive = Math.max(0, num(live, 0));
  const clampedAvg = Math.max(0, num(avg, 0));
  const clampedMax = Math.max(0, num(max, 0));

  setArcPath(`${prefix}-arc`, clampedLive, 0, 300);
  rotateNeedle(`avg-${prefix}-line`, clampedAvg, 0, 300, clampedAvg.toFixed(0), `avg-${prefix}-label`, "blue");
  rotateNeedle(`max-${prefix}-line`, clampedMax, 0, 300, clampedMax.toFixed(0), `max-${prefix}-label`, "red");
  document.getElementById(`${prefix}-number`).textContent = clampedLive.toFixed(0);
  document.getElementById(`${prefix}-unit`).textContent = "W";
}

function updateTempBar(live, avg, max) {
  const svg = document.getElementById("temp-bar");
  if (!svg) return;
  svg.innerHTML = "";

  const width = 120;
  const height = 10;
  const maxTemp = 120;

  live = num(live, 0);
  avg  = num(avg, 0);
  max  = num(max, 0);

  const track = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  track.setAttribute("x", 0);
  track.setAttribute("y", 3);
  track.setAttribute("width", width);
  track.setAttribute("height", 4);
  track.setAttribute("fill", "#ccc");
  svg.appendChild(track);

  const liveWidth = Math.min(width, (live / maxTemp) * width);
  const fill = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  fill.setAttribute("x", 0);
  fill.setAttribute("y", 3);
  fill.setAttribute("width", liveWidth);
  fill.setAttribute("height", 4);
  fill.setAttribute("fill", "orange");
  svg.appendChild(fill);

  const avgX = (avg / maxTemp) * width;
  const avgLine = document.createElementNS("http://www.w3.org/2000/svg", "line");
  avgLine.setAttribute("x1", avgX);
  avgLine.setAttribute("x2", avgX);
  avgLine.setAttribute("y1", 1);
  avgLine.setAttribute("y2", 9);
  avgLine.setAttribute("stroke", "blue");
  avgLine.setAttribute("stroke-width", 1);
  svg.appendChild(avgLine);

  const maxX = (max / maxTemp) * width;
  const maxLine = document.createElementNS("http://www.w3.org/2000/svg", "line");
  maxLine.setAttribute("x1", maxX);
  maxLine.setAttribute("x2", maxX);
  maxLine.setAttribute("y1", 1);
  maxLine.setAttribute("y2", 9);
  maxLine.setAttribute("stroke", "red");
  maxLine.setAttribute("stroke-width", 1);
  svg.appendChild(maxLine);

  const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
  label.setAttribute("x", width + 4);
  label.setAttribute("y", height / 2 + 2);
  label.setAttribute("fill", "#222");
  label.setAttribute("font-size", "7");
  label.setAttribute("font-weight", "bold");
  label.textContent = `${Math.round(live)}°C`;
  svg.appendChild(label);

  for (let t = 0; t <= maxTemp; t += 10) {
    const x = (t / maxTemp) * width;
    const tick = document.createElementNS("http://www.w3.org/2000/svg", "line");
    tick.setAttribute("x1", x);
    tick.setAttribute("x2", x);
    tick.setAttribute("y1", 0);
    tick.setAttribute("y2", 10);
    tick.setAttribute("stroke", "#888");
    tick.setAttribute("stroke-width", 0.3);
    svg.appendChild(tick);
  }
}

/* ====== CHARTS ====== */
function renderPowerHistoryChart() {
  const canvas = document.getElementById("power-history-chart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(320, canvas.clientWidth || 320);
  const height = Math.max(180, canvas.clientHeight || 180);
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const points = Array.isArray(powerHistoryState.points) ? powerHistoryState.points : [];
  const enabled = {
    motor: powerHistoryState.motor,
    human: powerHistoryState.human,
    solar: powerHistoryState.solar
  };

  if (!points.length) {
    ctx.fillStyle = "#666";
    ctx.font = "12px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Awaiting power history", width / 2, height / 2);
    return;
  }

  const series = [
    { key: "motor_power", enabled: enabled.motor, color: "#d1492e" },
    { key: "human_power", enabled: enabled.human, color: "#f08a24" },
    { key: "solar_power", enabled: enabled.solar, color: "#c7b600" }
  ];
  const activeSeries = series.filter(s => s.enabled);

  if (!activeSeries.length) {
    ctx.fillStyle = "#666";
    ctx.font = "12px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("All curves hidden", width / 2, height / 2);
    return;
  }

  const margin = { top: 10, right: 12, bottom: 22, left: 42 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;

  let minVal = 0;
  let maxVal = 100;
  activeSeries.forEach(seriesDef => {
    points.forEach(point => {
      const value = num(point[seriesDef.key], 0);
      minVal = Math.min(minVal, value);
      maxVal = Math.max(maxVal, value);
    });
  });

  const pad = Math.max(50, (maxVal - minVal) * 0.1);
  minVal = Math.min(0, Math.floor((minVal - pad) / 50) * 50);
  maxVal = Math.max(100, Math.ceil((maxVal + pad) / 50) * 50);
  if (maxVal <= minVal) maxVal = minVal + 100;

  const xForIndex = (index) => margin.left + (plotWidth * index) / Math.max(1, points.length - 1);
  const yForValue = (value) => margin.top + (maxVal - value) * plotHeight / (maxVal - minVal);
  const zeroY = yForValue(0);

  ctx.strokeStyle = "#d0d0d0";
  ctx.lineWidth = 1;
  ctx.fillStyle = "#666";
  ctx.font = "10px sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";

  const gridSteps = 5;
  for (let i = 0; i <= gridSteps; i++) {
    const value = minVal + (i * (maxVal - minVal)) / gridSteps;
    const y = yForValue(value);
    ctx.beginPath();
    ctx.moveTo(margin.left, y);
    ctx.lineTo(width - margin.right, y);
    ctx.stroke();
    ctx.fillText(`${Math.round(value)}`, margin.left - 6, y);
  }

  ctx.strokeStyle = "#999";
  ctx.beginPath();
  ctx.moveTo(margin.left, margin.top);
  ctx.lineTo(margin.left, height - margin.bottom);
  ctx.lineTo(width - margin.right, height - margin.bottom);
  ctx.stroke();

  if (zeroY >= margin.top && zeroY <= height - margin.bottom) {
    ctx.strokeStyle = "#b5b5b5";
    ctx.beginPath();
    ctx.moveTo(margin.left, zeroY);
    ctx.lineTo(width - margin.right, zeroY);
    ctx.stroke();
  }

  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  const xTicks = [
    { index: 0, label: "-3m" },
    { index: Math.max(0, Math.floor((points.length - 1) / 3)), label: "-2m" },
    { index: Math.max(0, Math.floor((2 * (points.length - 1)) / 3)), label: "-1m" },
    { index: Math.max(0, points.length - 1), label: "now" }
  ];
  xTicks.forEach(tick => {
    const x = xForIndex(tick.index);
    ctx.strokeStyle = "#999";
    ctx.beginPath();
    ctx.moveTo(x, height - margin.bottom);
    ctx.lineTo(x, height - margin.bottom + 4);
    ctx.stroke();
    ctx.fillStyle = "#666";
    ctx.fillText(tick.label, x, height - margin.bottom + 6);
  });

  activeSeries.forEach(seriesDef => {
    ctx.strokeStyle = seriesDef.color;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();
    const coords = points.map((point, index) => ({
      x: xForIndex(index),
      y: yForValue(num(point[seriesDef.key], 0))
    }));
    if (coords.length === 1) {
      ctx.moveTo(coords[0].x, coords[0].y);
      ctx.lineTo(coords[0].x + 0.01, coords[0].y);
    } else {
      ctx.moveTo(coords[0].x, coords[0].y);
      for (let i = 0; i < coords.length - 1; i++) {
        const current = coords[i];
        const next = coords[i + 1];
        const xc = (current.x + next.x) / 2;
        const yc = (current.y + next.y) / 2;
        ctx.quadraticCurveTo(current.x, current.y, xc, yc);
      }
      const last = coords[coords.length - 1];
      ctx.lineTo(last.x, last.y);
    }
    ctx.stroke();
  });
}

function setPowerSeriesButtonState() {
  [
    ["power-series-motor", powerHistoryState.motor],
    ["power-series-human", powerHistoryState.human],
    ["power-series-solar", powerHistoryState.solar]
  ].forEach(([id, enabled]) => {
    const button = document.getElementById(id);
    if (!button) return;
    button.classList.toggle("active-chart", enabled);
  });
}

async function fetchPowerHistory() {
  if (document.body.classList.contains('edit-mode')) return;
  try {
    const res = await fetch('/power_history', { cache: 'no-store' });
    const json = await res.json();
    powerHistoryState.points = Array.isArray(json.points) ? json.points : [];
    renderPowerHistoryChart();
  } catch (err) {
    console.error('Error fetching power history:', err);
  }
}

function updateWhPerKmChart(totalValues, netValues = [], liveTotal = 0, liveNet = 0, range = 10) {
  const chart = document.getElementById("wh-per-km-chart");
  if (!chart) return;

  chart.innerHTML = '';

  const width = 400;
  const chartHeight = 150;
  const barHeight = 120;
  const labelOffset = 20;
  const totalBars = range + 1; // history + live
  const gapRatio = 0.3;
  const marginLeft = 32;
  const barAreaWidth = width - marginLeft;
  const barWidth = barAreaWidth / (totalBars + (totalBars - 1) * gapRatio);
  const spacing = barWidth * gapRatio;

  const totalVals = (Array.isArray(totalValues) ? totalValues : []).map(v => num(v, 0));
  const netVals   = (Array.isArray(netValues)   ? netValues   : []).map(v => num(v, 0));
  liveTotal = num(liveTotal, 0);
  liveNet   = num(liveNet, 0);

  const count = Math.min(range, totalVals.length);
  const paddedTotal = new Array(range - count).fill(undefined).concat([...totalVals].slice(-count));

  const countNet = Math.min(range, netVals.length);
  const paddedNet = new Array(range - countNet).fill(undefined).concat([...netVals].slice(-countNet));

  const values = [...paddedTotal, liveTotal];
  const netFull = [...paddedNet, liveNet];

  const maxAbsVal = Math.max(
    ...values.map(v => Math.abs(v ?? 0)),
    ...netFull.map(v => Math.abs(v ?? 0)),
    1
  );
  const maxDisplayWh = Math.max(1, Math.ceil(maxAbsVal / 10) * 10);

  const zeroY = chartHeight - labelOffset - barHeight * 0.35;

  for (let t = -maxDisplayWh; t <= maxDisplayWh; t += 10) {
    const y = zeroY - (t / maxDisplayWh) * (barHeight * 0.65);

    const tick = document.createElementNS("http://www.w3.org/2000/svg", "line");
    tick.setAttribute("x1", marginLeft);
    tick.setAttribute("x2", width);
    tick.setAttribute("y1", y);
    tick.setAttribute("y2", y);
    tick.setAttribute("stroke", "#888");
    tick.setAttribute("stroke-width", "0.5");
    chart.appendChild(tick);

    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", marginLeft - 8);
    label.setAttribute("y", y + 3);
    label.setAttribute("text-anchor", "end");
    label.setAttribute("font-size", "8");
    label.setAttribute("fill", "#333");
    label.textContent = t.toString();
    chart.appendChild(label);
  }

  for (let i = 0; i < totalBars; i++) {
    const total = values[i];
    const net = netFull[i];

    const x = marginLeft + i * (barWidth + spacing);

    if (total === undefined) continue;

    const totalSafe = num(total, 0);
    const netSafe   = num(net, 0);

    const totalH = (Math.abs(totalSafe) / maxDisplayWh) * (barHeight * 0.65);
    const netH   = (Math.abs(netSafe)   / maxDisplayWh) * (barHeight * 0.65);

    const totalY = totalSafe >= 0 ? zeroY - totalH : zeroY;
    const netY   = netSafe   >= 0 ? zeroY - netH   : zeroY;

    const totalRect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    totalRect.setAttribute("x", x);
    totalRect.setAttribute("y", totalY);
    totalRect.setAttribute("width", barWidth);
    totalRect.setAttribute("height", totalH);
    totalRect.setAttribute("rx", 2);
    totalRect.setAttribute("ry", 2);
    totalRect.setAttribute("fill", "orange");
    chart.appendChild(totalRect);

    if (net !== undefined) {
      const netRect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      netRect.setAttribute("x", x + barWidth * 0.15);
      netRect.setAttribute("y", netY);
      netRect.setAttribute("width", barWidth * 0.7);
      netRect.setAttribute("height", netH);
      netRect.setAttribute("rx", 1);
      netRect.setAttribute("ry", 1);
      netRect.setAttribute("fill", "#ff6600");
      chart.appendChild(netRect);
    }

    if (range === 10) {
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", x + barWidth / 2);
      label.setAttribute("y", chartHeight - 6);
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("font-size", "8");
      label.setAttribute("font-weight", "bold");
      label.setAttribute("fill", "black");
      label.textContent = totalSafe.toFixed(1);
      chart.appendChild(label);
    }
  }
}

function updatePctChart(chartId, pctValues, livePct = 0, range = 10, liveColor = "orange") {
  const chart = document.getElementById(chartId);
  if (!chart) return;

  chart.innerHTML = '';

  const width = 400;
  const height = 150;
  const marginLeft = 32;
  const barHeight = 120;
  const labelOffset = 20;
  const totalBars = range + 1;
  const gapRatio = 0.3;
  const barWidth = (width - marginLeft) / (totalBars + (totalBars - 1) * gapRatio);
  const spacing = barWidth * gapRatio;

  const vals = (Array.isArray(pctValues) ? pctValues : []).map(v => num(v, 0));
  livePct = num(livePct, 0);

  const count = Math.min(range, vals.length);
  const padded = new Array(range - count).fill(undefined).concat([...vals].slice(-count));
  const values = [...padded, livePct];

  const maxVal = Math.max(...values.map(v => v ?? 0), 1);
  const maxDisplay = Math.max(1, Math.ceil(maxVal / 10) * 10);

  const zeroY = height - labelOffset;

  for (let t = 0; t <= maxDisplay; t += 10) {
    const y = zeroY - (t / maxDisplay) * barHeight;

    const tick = document.createElementNS("http://www.w3.org/2000/svg", "line");
    tick.setAttribute("x1", marginLeft);
    tick.setAttribute("x2", width);
    tick.setAttribute("y1", y);
    tick.setAttribute("y2", y);
    tick.setAttribute("stroke", "#888");
    tick.setAttribute("stroke-width", "0.5");
    chart.appendChild(tick);

    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", marginLeft - 6);
    label.setAttribute("y", y + 3);
    label.setAttribute("text-anchor", "end");
    label.setAttribute("font-size", "8");
    label.setAttribute("fill", "#333");
    label.textContent = `${t}`;
    chart.appendChild(label);
  }

  for (let i = 0; i < totalBars; i++) {
    const val = values[i];
    if (val === undefined) continue;

    const v = num(val, 0);
    const x = marginLeft + i * (barWidth + spacing);
    const h = (v / maxDisplay) * barHeight;
    const y = zeroY - h;

    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("x", x);
    rect.setAttribute("y", y);
    rect.setAttribute("width", barWidth);
    rect.setAttribute("height", h);
    rect.setAttribute("rx", 2);
    rect.setAttribute("ry", 2);
    rect.setAttribute("fill", i === totalBars - 1 ? liveColor : "#888");
    chart.appendChild(rect);

    if (range === 10) {
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", x + barWidth / 2);
      label.setAttribute("y", height - 6);
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("font-size", "8");
      label.setAttribute("font-weight", "bold");
      label.setAttribute("fill", "black");
      label.textContent = v.toFixed(1);
      chart.appendChild(label);
    }
  }
}

function updateRegenPctChart(pctValues, livePct = 0, range = 10) {
  const chart = document.getElementById("regen-pct-per-km-chart");
  if (!chart) return;

  chart.innerHTML = '';

  const width = 400;
  const height = 150;
  const marginLeft = 32;
  const barHeight = 120;
  const labelOffset = 20;
  const totalBars = range + 1;
  const gapRatio = 0.3;
  const barWidth = (width - marginLeft) / (totalBars + (totalBars - 1) * gapRatio);
  const spacing = barWidth * gapRatio;

  const vals = (Array.isArray(pctValues) ? pctValues : []).map(v => num(v, 0));
  livePct = Math.max(0, num(livePct, 0));

  const count = Math.min(range, vals.length);
  const padded = new Array(range - count).fill(undefined).concat([...vals].slice(-count));
  const values = [...padded, livePct].map(v => num(v, 0));

  const maxVal = Math.max(...values, 1);
  const maxDisplay = Math.max(1, Math.ceil(maxVal / 10) * 10);
  const zeroY = height - labelOffset;

  for (let t = 0; t <= maxDisplay; t += 10) {
    const y = zeroY - (t / maxDisplay) * barHeight;
    const tick = document.createElementNS("http://www.w3.org/2000/svg", "line");
    tick.setAttribute("x1", marginLeft);
    tick.setAttribute("x2", width);
    tick.setAttribute("y1", y);
    tick.setAttribute("y2", y);
    tick.setAttribute("stroke", "#888");
    tick.setAttribute("stroke-width", "0.5");
    chart.appendChild(tick);

    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", marginLeft - 6);
    label.setAttribute("y", y + 3);
    label.setAttribute("text-anchor", "end");
    label.setAttribute("font-size", "8");
    label.setAttribute("fill", "#333");
    label.textContent = `${t}`;
    chart.appendChild(label);
  }

  for (let i = 0; i < totalBars; i++) {
    const v = values[i];
    if (v === undefined) continue;

    const x = marginLeft + i * (barWidth + spacing);
    const h = (v / maxDisplay) * barHeight;
    const y = zeroY - h;

    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("x", x);
    rect.setAttribute("y", y);
    rect.setAttribute("width", barWidth);
    rect.setAttribute("height", h);
    rect.setAttribute("rx", 2);
    rect.setAttribute("ry", 2);
    rect.setAttribute("fill", i === totalBars - 1 ? "#0099cc" : "#888");
    chart.appendChild(rect);

    if (range === 10) {
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", x + barWidth / 2);
      label.setAttribute("y", height - 6);
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("font-size", "8");
      label.setAttribute("font-weight", "bold");
      label.setAttribute("fill", "black");
      label.textContent = v.toFixed(1);
      chart.appendChild(label);
    }
  }
}

/* ====== FLAGS & MISC ====== */


function updateFlagsDisplay(flagString) {
  const allPills = document.querySelectorAll("#flags-container .flag-pill");
  const activeSet = new Set((flagString ? String(flagString) : "").split(""));
  allPills.forEach(pill => {
    const code = pill.getAttribute("data-flag");
    pill.classList.toggle("active", activeSet.has(code));
  });
}

function updatePhotoPreview(photoCapture) {
  const box = document.getElementById("photo-preview-box");
  const status = document.getElementById("photo-preview-status");
  const interval = document.getElementById("photo-preview-interval");
  const count = document.getElementById("photo-preview-count");
  const captured = document.getElementById("photo-preview-captured");
  const uploaded = document.getElementById("photo-preview-uploaded");
  const link = document.getElementById("photo-preview-link");
  const image = document.getElementById("photo-preview-image");
  if (!box || !status || !interval || !count || !captured || !uploaded || !link || !image) return;

  const cfg = (photoCapture && typeof photoCapture === "object") ? photoCapture : {};
  const enabled = !!cfg.enabled;
  box.hidden = !enabled;
  if (!enabled) {
    photoPreviewState.lastImageKey = null;
    image.hidden = true;
    link.hidden = true;
    status.classList.remove("error");
    return;
  }

  interval.textContent = `Every: ${num(cfg.interval_km, 0).toFixed(1)} km`;
  count.textContent = `Captures: ${num(cfg.capture_count, 0).toFixed(0)}`;
  captured.textContent = `Captured: ${cfg.last_captured_at || "–"}`;
  uploaded.textContent = `Uploaded: ${cfg.last_uploaded_at || "–"}`;

  const imageUrl = cfg.latest_local_url || cfg.latest_public_url || "";
  const cacheToken = encodeURIComponent(cfg.last_captured_at || cfg.last_uploaded_at || "");
  const imageKey = imageUrl ? `${imageUrl}|${cacheToken}` : null;

  if (cfg.last_error) {
    status.textContent = `Capture error: ${cfg.last_error}`;
    status.classList.add("error");
  } else if (imageUrl) {
    status.textContent = "Last capture available.";
    status.classList.remove("error");
  } else {
    status.textContent = "Waiting for first capture.";
    status.classList.remove("error");
  }

  if (imageUrl) {
    if (imageKey !== photoPreviewState.lastImageKey) {
      const separator = imageUrl.includes("?") ? "&" : "?";
      image.src = `${imageUrl}${separator}t=${cacheToken || Date.now()}`;
      link.href = image.src;
      photoPreviewState.lastImageKey = imageKey;
    }
    image.hidden = false;
    link.hidden = false;
  } else {
    image.hidden = true;
    link.hidden = true;
    photoPreviewState.lastImageKey = null;
  }
}

function showAhPopup() {
  document.getElementById("ah-popup").style.display = "block";
}
function hideAhPopup() {
  document.getElementById("ah-popup").style.display = "none";
}
async function submitAh() {
  const val = num(document.getElementById("ah-input").value, NaN);
  if (Number.isNaN(val)) return;
  await fetch("/add_ah", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ added_ah: val })
  });
  hideAhPopup();
}

function showCaResetPopup() {
  document.getElementById("ca-reset-popup").style.display = "block";
}

function hideCaResetPopup() {
  document.getElementById("ca-reset-popup").style.display = "none";
}

async function resetSession() {
  try {
    await fetch('/reset', { method: 'POST' });
    console.log("Session reset.");
  } catch (err) {
    console.error("Reset failed:", err);
  }
}

/* ====== BACKGROUND ARCS & TICKS INIT ====== */
["speed-bg", "power-bg", "solar-bg", "pv-bg"].forEach(setBackgroundArc);
drawTicks("speed-ticks", 0, 50, 1, 5, 80, 70, 75);
drawTicks("power-ticks", -2000, 4000, 100, 500, 80, 70, 75);
drawTicks("solar-ticks", 0, 300, 5, 50, 80, 70, 75);
drawTicks("pv-ticks", 0, 300, 5, 50, 80, 70, 75);

/* ====== FETCH + RENDER LOOP ====== */
async function fetchMetrics() {
  if (document.body.classList.contains('edit-mode')) {
    metricsPaused = true;
    return;
  }
  try {
    const res = await fetch('/metrics');
    const json = await res.json();

    if (json.ca_reset_prompt) {
      showCaResetPopup();
    } else {
      hideCaResetPopup();
    }

    updatePhotoPreview(json.photo_capture);

    // Speed
    updateSpeedometer(
      num(json.raw_CA_values?.[3], 0),
      num(json.calculated_CA_values?.speed_avg, 0),
      num(json.calculated_CA_values?.speed_max, 0)
    );

    // Power + Amps
    const amps = num(json.raw_CA_values?.[2], 0);
    updatePowerMeter(
      num(json.calculated_CA_values?.power_live, 0),
      num(json.calculated_CA_values?.power_avg, 0),
      num(json.calculated_CA_values?.power_max, 0),
      amps
    );
    updateAmpArc(amps);

    // Temperature
    updateTempBar(
      num(json.raw_CA_values?.[5], 0),
      num(json.calculated_CA_values?.temp_avg, 0),
      num(json.calculated_CA_values?.temp_max, 0)
    );

    // Human power
    let solarLive = Math.max(0, num(json.calculated_CA_values?.human_power_live ?? json.calculated_CA_values?.solar_power_live, 0));
    updateAuxMeter(
      "solar",
      solarLive,
      num(json.calculated_CA_values?.human_power_avg ?? json.calculated_CA_values?.solar_power_avg, 0),
      num(json.calculated_CA_values?.human_power_max ?? json.calculated_CA_values?.solar_power_max, 0)
    );

    const powerLive = num(json.calculated_CA_values?.power_live, 0);
    const solarPercent = powerLive > 0 ? (100 * solarLive / powerLive) : 0;
    document.getElementById("solar-percent").textContent = `${solarPercent.toFixed(1)}`;

    // Solar panel power
    const pvLive = Math.max(0, num(json.calculated_CA_values?.solar_power_live, 0));
    updateAuxMeter(
      "pv",
      pvLive,
      num(json.calculated_CA_values?.solar_power_avg, 0),
      num(json.calculated_CA_values?.solar_power_max, 0)
    );

    const pvPercent = powerLive > 0 ? (100 * pvLive / powerLive) : 0;
    document.getElementById("pv-percent").textContent = `${pvPercent.toFixed(1)}`;

    // Battery / Ah
    const ahConsumed = num(
      json.calculated_CA_values?.battery_ah_used_net ?? json.battery_ah_used_net,
      0
    );
    const capacityAh = Number(json.battery_capacity_ah ?? 64);
    const pctRemaining = Math.max(0, Math.min(1, 1 - (capacityAh > 0 ? (ahConsumed / capacityAh) : 0)));
    document.getElementById('ah-bar').style.width = `${pctRemaining * 100}%`;
    document.getElementById('ah-bar-value').innerText = `${ahConsumed.toFixed(1)} Ah`;
    const grossAh = num(json.calculated_CA_values?.battery_ah_used_gross, ahConsumed);
    const recoveredAh = num(json.calculated_CA_values?.battery_ah_recovered, 0);
    document.getElementById('ah-bar-detail').innerText = `gross ${grossAh.toFixed(1)} Ah / recovered ${recoveredAh.toFixed(1)} Ah`;

    const voltage = num(json.raw_CA_values?.[1], 0);
    document.getElementById('ah-voltage-value').innerText = `${voltage.toFixed(1)} V`;

    // Human cumulative & kcal
    const solarWh = num(json.calculated_CA_values?.human_Wh ?? json.calculated_CA_values?.solar_Wh, 0);
    document.getElementById("solar-cumulative").textContent = solarWh.toFixed(1);
    const calories = num(json.calculated_CA_values?.human_calories_burned ?? json.calculated_CA_values?.calories_burned, 0);
    document.getElementById("solar-calories").textContent = calories.toFixed(0);

    const pvWh = num(json.calculated_CA_values?.solar_Wh, 0);
    document.getElementById("pv-cumulative").textContent = pvWh.toFixed(1);
    const pvCurrent = num(json.calculated_CA_values?.solar_current_live, 0);
    document.getElementById("pv-current").textContent = pvCurrent.toFixed(1);

    // Trip metrics
    const distKm = num(json.calculated_CA_values?.distance_km, 0);
    document.getElementById("trip-distance").innerText = distKm.toFixed(2) + " km";
    document.getElementById("trip-net-wh-per-km").innerText =
      num(json.calculated_CA_values?.net_Wh_per_km, 0).toFixed(1);
    const Wh_pos = num(json.calculated_CA_values?.Wh_pos, 0);
    document.getElementById("trip-wh-per-km").innerText =
      (Wh_pos / Math.max(1e-6, distKm || 1)).toFixed(1);
    document.getElementById("trip-net-wh").innerText =
      num(json.calculated_CA_values?.net_Wh, 0).toFixed(1);
    document.getElementById("trip-total-wh").innerText = Wh_pos.toFixed(1);

    // Range block
    const autonomy = json.calculated_CA_values?.autonomy ?? {};
    const rangeLast = num(autonomy.range_last_km, 0);
    document.getElementById("range-last").textContent = rangeLast > 0 ? rangeLast.toFixed(1) : "–";
    const range10 = num(autonomy.range_10km_avg, 0);
    document.getElementById("range-10").textContent = range10 > 0 ? range10.toFixed(1) : "–";
    const rangeAvg = num(autonomy.range_session_avg, 0);
    document.getElementById("range-avg").textContent = rangeAvg > 0 ? rangeAvg.toFixed(1) : "–";

    // Charts (10 or 50)
    const range = isLongRange ? 50 : 10;
    updateWhPerKmChart(
      json.calculated_CA_values?.Wh_per_km_last ?? [],
      json.calculated_CA_values?.net_Wh_per_km_last ?? [],
      num(json.calculated_CA_values?.live_Wh_per_km, 0),
      num(json.calculated_CA_values?.live_net_Wh_per_km, 0),
      range
    );
    updatePctChart(
      "human-pct-per-km-chart",
      json.calculated_CA_values?.human_pct_per_km_last ?? [],
      solarPercent,
      range,
      "orange"
    );
    updatePctChart(
      "solar-pct-per-km-chart",
      json.calculated_CA_values?.solar_pct_per_km_last ?? [],
      pvPercent,
      range,
      "#e0b400"
    );
    updateRegenPctChart(
      json.calculated_CA_values?.regen_pct_per_km_last ?? [],
      num(json.calculated_CA_values?.["%_regen"], 0),
      range
    );

    // Flags, user, session
    const flags = json.raw_CA_values?.[14] ?? "";
    updateFlagsDisplay(flags);
    updateHeaderMode();

    document.getElementById("switch-user-button").textContent = "User: " + (json.user ?? "JD");
    document.getElementById("session-id").textContent = json.session_id ?? "-";

  } catch (err) {
    console.error('Error fetching metrics:', err);
  }
}

/* ====== EVENT LISTENERS ====== */
document.getElementById("switch-user-button").addEventListener("click", async () => {
  try {
    const res = await fetch("/switch_user", { method: "POST" });
    const data = await res.json();
    document.getElementById("switch-user-button").textContent = "User: " + data.user;
  } catch (err) {
    console.error("Failed to switch user:", err);
  }
});

document.getElementById("toggle-range-button").addEventListener("click", () => {
  isLongRange = !isLongRange;
  document.getElementById("toggle-range-button").textContent = isLongRange ? "10km" : "50km";
});

document.getElementById("end-button").addEventListener("click", async () => {
  try {
    const res = await fetch('/end_session', { method: 'POST' });
    if (res.redirected) {
      window.location.href = res.url;
    }
  } catch (err) {
    console.error("Failed to end session:", err);
  }
});

// ====== LAYOUT EDIT MODE (Drag & Drop) ======
function ensureBoxIds() {
  const grid = document.querySelector('.grid-container');
  if (!grid) return;
  const boxes = Array.from(grid.querySelectorAll(':scope > .box'));
  boxes.forEach((el, idx) => {
    if (!el.id) {
      const base = (el.className || 'box').split(/\s+/).find(c => c && c !== 'box') || 'box';
      el.id = `${base}-${idx}`;
    }
  });
}

function saveLayoutOrder() {
  const grid = document.querySelector('.grid-container');
  if (!grid) return;
  const ids = Array.from(grid.querySelectorAll(':scope > .box')).map(el => el.id);
  try { localStorage.setItem('dashboardLayoutOrder', JSON.stringify(ids)); } catch (e) {}
}

function restoreLayoutOrder() {
  const grid = document.querySelector('.grid-container');
  if (!grid) return;
  let order = [];
  try { order = JSON.parse(localStorage.getItem('dashboardLayoutOrder') || '[]'); } catch(e) { order = []; }
  if (!Array.isArray(order) || order.length === 0) return;
  const present = new Set(Array.from(grid.children).map(ch => ch.id));
  order.filter(id => present.has(id)).forEach(id => {
    const el = document.getElementById(id);
    if (el) grid.appendChild(el);
  });
}

function setEditMode(active) {
  const body = document.body;
  const grid = document.querySelector('.grid-container');
  const resetBtn = document.getElementById('layout-reset');
  const pageRoot = document.getElementById('page-scale-root');
  const editBtn = document.getElementById('layout-edit-toggle');
  if (!grid) return;
  if (active) {
    body.classList.add('edit-mode');
    if (resetBtn) resetBtn.style.display = '';
    metricsPaused = true;

    // Compute scale to fit vertically
    function computeScale() {
      if (!pageRoot) return;
      // Reset scale to 1 to measure intrinsic height
      pageRoot.style.setProperty('--edit-scale', 1);
      const rect = pageRoot.getBoundingClientRect();
      const contentHeight = rect.height;
      const contentWidth = rect.width;
      const vh = window.innerHeight || document.documentElement.clientHeight;
      const vw = window.innerWidth || document.documentElement.clientWidth;
      const scaleH = contentHeight > 0 ? (vh - 8) / contentHeight : 1;
      const scaleW = contentWidth > 0 ? (vw - 8) / contentWidth : 1;
      const scale = Math.min(1, Math.max(0.1, Math.min(scaleH, scaleW)));
      pageRoot.style.setProperty('--edit-scale', scale);
    }
    computeScale();
    window.addEventListener('resize', computeScale, { passive: true });
    body._computeEditScale = computeScale;

    // Create visible grab handles next to each title
    try {
      Array.from(grid.querySelectorAll(':scope > .box')).forEach(box => {
        const title = box.querySelector('.box-title');
        if (!title) return;
        if (title.querySelector('.drag-handle')) return;
        const h = document.createElement('span');
        h.className = 'drag-handle';
        h.title = 'Drag to reorder';
        h.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">\
          <path d="M7 5h2v2H7V5zm4 0h2v2h-2V5zm4 0h2v2h-2V5zM7 9h2v2H7V9zm4 0h2v2h-2V9zm4 0h2v2h-2V9zM7 13h2v2H7v-2zm4 0h2v2h-2v-2zm4 0h2v2h-2v-2zM7 17h2v2H7v-2zm4 0h2v2h-2v-2zm4 0h2v2h-2v-2z"/></svg>';
        title.appendChild(h);
      });
    } catch (_) {}

    // Create a floating Validate button if missing
    let vbtn = document.getElementById('edit-validate-button');
    if (!vbtn) {
      vbtn = document.createElement('button');
      vbtn.id = 'edit-validate-button';
      vbtn.className = 'edit-validate-button';
      vbtn.textContent = 'Validate';
      vbtn.addEventListener('click', () => {
        // Save current order for safety, then exit edit mode
        saveLayoutOrder();
        setEditMode(false);
      });
      document.body.appendChild(vbtn);
    }
    vbtn.style.display = '';

    // Create a floating Reset button (bottom-left) if missing
    let rbtn = document.getElementById('edit-reset-button');
    if (!rbtn) {
      rbtn = document.createElement('button');
      rbtn.id = 'edit-reset-button';
      rbtn.className = 'edit-reset-button';
      rbtn.textContent = 'Reset';
      rbtn.addEventListener('click', () => {
        try { localStorage.removeItem('dashboardLayoutOrder'); } catch (e) {}
        window.location.reload();
      });
      document.body.appendChild(rbtn);
    }
    rbtn.style.display = '';
  } else {
    body.classList.remove('edit-mode');
    if (resetBtn) resetBtn.style.display = 'none';
    metricsPaused = false;
    const pageRoot = document.getElementById('page-scale-root');
    if (pageRoot) pageRoot.style.setProperty('--edit-scale', 1);
    if (body._computeEditScale) {
      window.removeEventListener('resize', body._computeEditScale);
      delete body._computeEditScale;
    }
    const vbtn = document.getElementById('edit-validate-button');
    if (vbtn) vbtn.style.display = 'none';
    const rbtn = document.getElementById('edit-reset-button');
    if (rbtn) rbtn.style.display = 'none';
    // Remove drag handles to keep UI clean in normal mode
    document.querySelectorAll('.drag-handle').forEach(el => el.remove());
  }
}

function initDragAndDrop() {
  const grid = document.querySelector('.grid-container');
  if (!grid) return;

  let draggingEl = null;

  function onDragStart(e) {
    if (!document.body.classList.contains('edit-mode')) return e.preventDefault();
    draggingEl = e.currentTarget;
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', draggingEl.id); } catch (_) {}
    draggingEl.classList.add('dragging');
  }

  function onDragEnd() {
    if (draggingEl) draggingEl.classList.remove('dragging');
    draggingEl = null;
    Array.from(grid.children).forEach(ch => { ch.classList.remove('drop-before', 'drop-after'); });
  }

  function positionRelativeTo(el, clientY) {
    const r = el.getBoundingClientRect();
    const midpoint = r.top + r.height / 2;
    return clientY < midpoint ? 'before' : 'after';
  }

  grid.addEventListener('dragover', (e) => {
    if (!document.body.classList.contains('edit-mode')) return;
    e.preventDefault();
    const target = e.target.closest('.box');
    Array.from(grid.children).forEach(ch => { ch.classList.remove('drop-before', 'drop-after'); });
    if (!target || !draggingEl || target === draggingEl) return;
    const pos = positionRelativeTo(target, e.clientY);
    target.classList.add(pos === 'before' ? 'drop-before' : 'drop-after');
  });

  grid.addEventListener('drop', (e) => {
    if (!document.body.classList.contains('edit-mode')) return;
    e.preventDefault();
    const target = e.target.closest('.box');
    Array.from(grid.children).forEach(ch => { ch.classList.remove('drop-before', 'drop-after'); });
    const draggedId = (e.dataTransfer && e.dataTransfer.getData('text/plain')) || (draggingEl && draggingEl.id);
    const dragged = draggedId ? document.getElementById(draggedId) : draggingEl;
    if (!dragged) return;
    if (!target || target === dragged) return;
    const pos = positionRelativeTo(target, e.clientY);
    if (pos === 'before') {
      grid.insertBefore(dragged, target);
    } else {
      grid.insertBefore(dragged, target.nextSibling);
    }
    saveLayoutOrder();
    // Recompute scale after DOM height changes (drag reorder)
    if (document.body._computeEditScale) document.body._computeEditScale();
  });

  // Delegate dragstart/dragend to boxes
  Array.from(grid.querySelectorAll(':scope > .box')).forEach(el => {
    el.setAttribute('draggable', 'true');
    el.addEventListener('dragstart', onDragStart);
    el.addEventListener('dragend', onDragEnd);
  });

  // Touch/Pen support via Pointer Events (mobile friendly)
  function initPointerDnD() {
    let pDraggingEl = null;
    let pPointerId = null;

    function clearHints() {
      Array.from(grid.children).forEach(ch => ch.classList.remove('drop-before', 'drop-after'));
    }

    function posRelativeTo(el, clientY) {
      const r = el.getBoundingClientRect();
      return clientY < (r.top + r.height / 2) ? 'before' : 'after';
    }

    function isInteractiveTarget(target) {
      return !!target.closest('button, a, input, select, textarea');
    }

    function canStartFrom(target, boxEl) {
      if (isInteractiveTarget(target)) return false;
      const title = boxEl.querySelector('.box-title');
      // If a title exists, only allow dragging starting from the title to avoid interfering with widgets
      if (title) return !!target.closest('.box-title, .drag-handle');
      return true; // otherwise allow from anywhere in the box
    }

    function onPointerDown(e) {
      if (!document.body.classList.contains('edit-mode')) return;
      if (e.pointerType === 'mouse') return; // mouse uses native HTML5 DnD
      const boxEl = e.currentTarget;
      if (!canStartFrom(e.target, boxEl)) return;

      pDraggingEl = boxEl;
      pPointerId = e.pointerId;
      try { boxEl.setPointerCapture(pPointerId); } catch(_) {}
      boxEl.classList.add('dragging');
      e.preventDefault();
    }

    function onPointerMove(e) {
      if (!document.body.classList.contains('edit-mode')) return;
      if (!pDraggingEl || e.pointerId !== pPointerId) return;
      e.preventDefault();

      // Temporarily ignore the dragging element for hit testing
      const prevPE = pDraggingEl.style.pointerEvents;
      pDraggingEl.style.pointerEvents = 'none';
      const el = document.elementFromPoint(e.clientX, e.clientY);
      pDraggingEl.style.pointerEvents = prevPE;
      const target = el && el.closest && el.closest('.box');

      clearHints();
      if (!target || target === pDraggingEl) return;
      const pos = posRelativeTo(target, e.clientY);
      target.classList.add(pos === 'before' ? 'drop-before' : 'drop-after');
    }

    function onPointerUp(e) {
      if (!document.body.classList.contains('edit-mode')) return;
      if (!pDraggingEl || e.pointerId !== pPointerId) return;
      e.preventDefault();

      const boxEl = pDraggingEl;
      try { boxEl.releasePointerCapture(pPointerId); } catch(_) {}
      boxEl.classList.remove('dragging');

      // Determine drop target under pointer
      const prevPE = boxEl.style.pointerEvents;
      boxEl.style.pointerEvents = 'none';
      const el = document.elementFromPoint(e.clientX, e.clientY);
      boxEl.style.pointerEvents = prevPE;
      const target = el && el.closest && el.closest('.box');

      if (target && target !== boxEl) {
        const pos = posRelativeTo(target, e.clientY);
        if (pos === 'before') grid.insertBefore(boxEl, target);
        else grid.insertBefore(boxEl, target.nextSibling);
        saveLayoutOrder();
      }

      pDraggingEl = null;
      pPointerId = null;
      clearHints();
      if (document.body._computeEditScale) document.body._computeEditScale();
    }

    Array.from(grid.querySelectorAll(':scope > .box')).forEach(el => {
      el.addEventListener('pointerdown', onPointerDown);
      el.addEventListener('pointermove', onPointerMove);
      el.addEventListener('pointerup', onPointerUp);
      el.addEventListener('pointercancel', onPointerUp);
    });
  }

  initPointerDnD();
}

window.addEventListener("DOMContentLoaded", () => {
  // Restore and enable layout editing
  ensureBoxIds();
  restoreLayoutOrder();
  initDragAndDrop();

  const editBtn = document.getElementById('layout-edit-toggle');
  const resetBtn = document.getElementById('layout-reset');
  if (editBtn) {
    editBtn.addEventListener('click', () => {
      const isActive = document.body.classList.contains('edit-mode');
      setEditMode(!isActive);
    });
  }
  if (resetBtn) {
    resetBtn.addEventListener('click', () => {
      try { localStorage.removeItem('dashboardLayoutOrder'); } catch (e) {}
      window.location.reload();
    });
  }

  // Chart picker buttons
  const chartButtons = {
    "btn-wh": "wh-per-km-chart",
    "btn-human": "human-pct-per-km-chart",
    "btn-solar": "solar-pct-per-km-chart",
    "btn-regen": "regen-pct-per-km-chart"
  };

  Object.keys(chartButtons).forEach(buttonId => {
    const btn = document.getElementById(buttonId);
    const chartId = chartButtons[buttonId];
    const chartEl = document.getElementById(chartId);

    if (btn && chartEl) {
      btn.addEventListener("click", () => {
        Object.entries(chartButtons).forEach(([b, c]) => {
          document.getElementById(c).style.display = "none";
          document.getElementById(b).classList.remove("active-chart");
        });

        chartEl.style.display = "";
        btn.classList.add("active-chart");
      });
    }
  });

  [
    ["power-series-motor", "motor"],
    ["power-series-human", "human"],
    ["power-series-solar", "solar"]
  ].forEach(([id, key]) => {
    const button = document.getElementById(id);
    if (!button) return;
    button.addEventListener("click", () => {
      powerHistoryState[key] = !powerHistoryState[key];
      setPowerSeriesButtonState();
      renderPowerHistoryChart();
    });
  });
  setPowerSeriesButtonState();
  window.addEventListener("resize", renderPowerHistoryChart);

  // Restart service button
  const restartBtn = document.getElementById('restart-service-button');
  const restartStatus = document.getElementById('restart-status');
  if (restartBtn) {
    restartBtn.addEventListener('click', async () => {
      if (!confirm('Restart the Cycle Analyst service now?')) return;
      restartBtn.disabled = true;
      if (restartStatus) restartStatus.textContent = 'Restarting...';
      try {
        const res = await fetch('/restart_service', { method: 'POST' });
        let msg = 'Restart requested';
        try { const j = await res.json(); msg = j.message || j.status || msg; } catch {}
        if (restartStatus) restartStatus.textContent = msg;
        setTimeout(() => window.location.reload(), 5000);
      } catch (e) {
        if (restartStatus) restartStatus.textContent = 'Restart failed';
        restartBtn.disabled = false;
      }
    });
  }

  // Start metrics loop
  fetchMetrics();
  setInterval(fetchMetrics, 100);
  fetchPowerHistory();
  setInterval(fetchPowerHistory, 1000);
});
