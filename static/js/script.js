/* ====== GAUGE GEOMETRY & GLOBALS ====== */
const RADIUS = 90;
const CENTER_X = 100;
const CENTER_Y = 100;
const START_ANGLE = -126;
const END_ANGLE = 126;
const AUX_METER_MAX_W = 300;
const PV_METER_MAX_W = 600;

let isLongRange = false;
let metricsPaused = false;
let solarPotentialChartOpen = false;
let latestSolarBattery = null;
let latestBatteryCurve = null;
let latestSolarSessionProfile = null;
let solarSessionProfileLoading = false;
let solarSessionProfileFetchedAt = 0;
const photoPreviewState = {
  lastImageKey: null
};
const POWER_HISTORY_WINDOWS = [180, 360, 720, 1800];
const POWER_HISTORY_SAMPLE_COUNT = 180;
const LIVE_METRICS_POLL_MS = 250;
const LIVE_METRICS_POLL_HIDDEN_MS = 1500;
const FULL_METRICS_POLL_MS = 1000;
const FULL_METRICS_POLL_HIDDEN_MS = 5000;
const POWER_HISTORY_POLL_MS = 3000;
const POWER_HISTORY_POLL_HIDDEN_MS = 10000;
const NAVIGATION_METRIC_STORAGE_KEY = 'navigationMetricIds';
const NAVIGATION_METRIC_LIMIT = 9;
const NAVIGATION_DEFAULT_METRICS = ['speed', 'motor_power', 'net_wh_per_km', 'route_progress', 'ca_voltage'];
const NAVIGATION_METRIC_OPTIONS = [
  { id: 'speed', label: 'Speed' },
  { id: 'motor_power', label: 'Motor' },
  { id: 'motor_current', label: 'Current' },
  { id: 'ca_voltage', label: 'CA volts' },
  { id: 'battery_percent', label: 'Battery' },
  { id: 'battery_wh', label: 'Battery Wh' },
  { id: 'distance', label: 'Distance' },
  { id: 'route_remaining', label: 'Left' },
  { id: 'route_progress', label: 'Route' },
  { id: 'net_wh_per_km', label: 'Net Wh/km' },
  { id: 'live_net_wh_per_km', label: 'Live net' },
  { id: 'human_power', label: 'Human' },
  { id: 'solar_power', label: 'Solar' },
  { id: 'motor_temp', label: 'Temp' },
  { id: 'range_session', label: 'Range' }
];
let metricsRequestInFlight = false;
let fullMetricsRequestInFlight = false;
let powerHistoryRequestInFlight = false;
let latestRouteProgress = null;
const latestNavigationValues = {};
const dashboardDiag = {
  liveHz: 0,
  fetchMs: 0,
  renderMs: 0,
  serverMs: 0,
  skips: 0,
  lastLiveEndMs: 0,
  reader: null
};
let selectedPowerSource = localStorage.getItem('motorPowerSource') === 'sensor' ? 'sensor' : 'ca';
let selectedVoltageSource = localStorage.getItem('motorVoltageSource') === 'sensor' ? 'sensor' : 'ca';
const powerHistoryState = {
  motor: true,
  human: true,
  solar: true,
  solarRoofEnabled: true,
  cumulative: false,
  windowIndex: 0,
  windowSeconds: POWER_HISTORY_WINDOWS[0],
  serverNowMs: null,
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

function diagEma(current, value, alpha = 0.25) {
  const n = Number(value);
  if (!Number.isFinite(n)) return current || 0;
  const c = Number(current);
  return Number.isFinite(c) && c > 0 ? (c * (1 - alpha)) + (n * alpha) : n;
}

function fmtMs(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  return `${Math.round(n)}ms`;
}

function fmtHz(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return "-";
  return `${n.toFixed(1)}Hz`;
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function readNavigationMetricIds() {
  const stored = localStorage.getItem(NAVIGATION_METRIC_STORAGE_KEY);
  if (stored === null) return NAVIGATION_DEFAULT_METRICS.slice(0, NAVIGATION_METRIC_LIMIT);
  let ids = [];
  try {
    ids = JSON.parse(stored || '[]');
  } catch (e) {
    ids = [];
  }
  const allowed = new Set(NAVIGATION_METRIC_OPTIONS.map(option => option.id));
  const clean = Array.isArray(ids) ? ids.filter(id => allowed.has(id)) : [];
  return clean.slice(0, NAVIGATION_METRIC_LIMIT);
}

function formatNavigationValue(value, unit = '', decimals = 0) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '-';
  const text = n.toFixed(decimals);
  return unit ? `${text} ${unit}` : text;
}

function formatNavigationDistance(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return '-';
  if (n < 1) return `${Math.round(n * 1000)} m`;
  return `${n.toFixed(n < 10 ? 1 : 0)} km`;
}

function navigationMetricText(id) {
  const v = latestNavigationValues;
  if (id === 'speed') return formatNavigationValue(v.speed, 'km/h', 0);
  if (id === 'motor_power') return formatNavigationValue(v.motor_power, 'W', 0);
  if (id === 'motor_current') return formatNavigationValue(v.motor_current, 'A', 1);
  if (id === 'ca_voltage') return formatNavigationValue(v.ca_voltage, 'V', 1);
  if (id === 'battery_percent') return formatNavigationValue(v.battery_percent, '%', 0);
  if (id === 'battery_wh') return formatNavigationValue(v.battery_wh, 'Wh', 0);
  if (id === 'distance') return formatNavigationDistance(v.distance);
  if (id === 'route_remaining') return v.route_remaining_label || '-';
  if (id === 'route_progress') {
    const distance = formatNavigationDistance(v.distance);
    const remaining = v.route_remaining_label || '-';
    return distance === '-' && remaining === '-' ? '-' : `${distance} / ${remaining}`;
  }
  if (id === 'net_wh_per_km') return formatNavigationValue(v.net_wh_per_km, 'Wh/km', 1);
  if (id === 'live_net_wh_per_km') return formatNavigationValue(v.live_net_wh_per_km, 'Wh/km', 1);
  if (id === 'human_power') return formatNavigationValue(v.human_power, 'W', 0);
  if (id === 'solar_power') return formatNavigationValue(v.solar_power, 'W', 0);
  if (id === 'motor_temp') return formatNavigationValue(v.motor_temp, 'C', 0);
  if (id === 'range_session') return formatNavigationDistance(v.range_session);
  return '-';
}

function fitNavigationMetricText() {
  document.querySelectorAll('.navigation-metric-value').forEach((el) => {
    el.style.fontSize = '';
    const minPx = 12;
    let size = parseFloat(getComputedStyle(el).fontSize) || 24;
    while (size > minPx && el.scrollWidth > el.clientWidth) {
      size -= 1;
      el.style.fontSize = `${size}px`;
    }
  });
}

function renderNavigationMetrics() {
  const grid = document.getElementById('navigation-metrics-grid');
  if (!grid) return;
  const selected = readNavigationMetricIds();
  grid.innerHTML = selected.map((id) => {
    const option = NAVIGATION_METRIC_OPTIONS.find(item => item.id === id);
    if (!option) return '';
    return `<div class="navigation-metric-pill" data-metric="${id}">
      <span class="navigation-metric-label">${option.label}</span>
      <span class="navigation-metric-value">${navigationMetricText(id)}</span>
    </div>`;
  }).join('');
  requestAnimationFrame(fitNavigationMetricText);
}

function updateNavigationMetricsFromPayload(json) {
  const c = json.calculated_CA_values || {};
  const raw = json.raw_CA_values || [];
  const sensorValid = c.motor_sensor_valid === true;
  const caAmps = num(c.ca_current_live ?? raw?.[2], 0);
  const sensorAmps = num(c.motor_sensor_current_live, 0);
  const displayedPower = selectedPowerSource === 'sensor' && sensorValid
    ? num(c.motor_sensor_power_live, 0)
    : num(c.power_live, 0);
  const battery = c.solar_battery ?? json.solar_battery ?? latestBatteryCurve;
  const autonomy = c.autonomy || {};

  latestNavigationValues.speed = num(raw?.[3], latestNavigationValues.speed);
  latestNavigationValues.motor_power = displayedPower;
  latestNavigationValues.motor_current = selectedPowerSource === 'sensor' && sensorValid ? sensorAmps : caAmps;
  latestNavigationValues.ca_voltage = num(c.ca_voltage_live ?? raw?.[1], latestNavigationValues.ca_voltage);
  latestNavigationValues.distance = num(c.distance_km, latestNavigationValues.distance);
  latestNavigationValues.live_net_wh_per_km = num(c.live_net_Wh_per_km, latestNavigationValues.live_net_wh_per_km);
  latestNavigationValues.human_power = Math.max(0, num(c.human_power_live ?? c.solar_power_live, latestNavigationValues.human_power));
  latestNavigationValues.solar_power = Math.max(0, num(c.solar_power_live, latestNavigationValues.solar_power));
  latestNavigationValues.motor_temp = num(raw?.[5], latestNavigationValues.motor_temp);

  if (c.net_Wh_per_km !== undefined) latestNavigationValues.net_wh_per_km = num(c.net_Wh_per_km, latestNavigationValues.net_wh_per_km);
  if (battery?.enabled) {
    latestNavigationValues.battery_percent = num(battery.percent, latestNavigationValues.battery_percent);
    latestNavigationValues.battery_wh = num(battery.remaining_wh, latestNavigationValues.battery_wh);
  } else {
    latestNavigationValues.battery_percent = num(c.battery_percent_remaining, latestNavigationValues.battery_percent);
    latestNavigationValues.battery_wh = num(c.battery_Wh_remaining, latestNavigationValues.battery_wh);
  }
  if (autonomy.range_session_avg !== undefined) {
    latestNavigationValues.range_session = num(autonomy.range_session_avg, latestNavigationValues.range_session);
  }
  if (latestRouteProgress) {
    latestNavigationValues.route_remaining = latestRouteProgress.remainingKm;
    latestNavigationValues.route_remaining_label = latestRouteProgress.remainingLabel;
  }
  renderNavigationMetrics();
}

function setNavigationProfileClass() {
  const panel = document.getElementById('gpx-profile-panel');
  document.body.classList.toggle('navigation-profile-visible', Boolean(panel && !panel.hidden));
}

function setNavigationControlsOpen(open) {
  const isOpen = Boolean(open);
  document.body.classList.toggle('navigation-controls-open', isOpen);
  const button = document.getElementById('map-navigation-settings');
  if (button) button.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
}

function setNavigationMode(active) {
  const isActive = Boolean(active);
  document.body.classList.toggle('navigation-mode', isActive);
  setNavigationControlsOpen(false);
  setNavigationProfileClass();
  renderNavigationMetrics();
  if (isActive && typeof window.focusMapNavigation === 'function') {
    window.focusMapNavigation();
  }
  requestAnimationFrame(() => {
    if (typeof window.resizeLiveMap === 'function') window.resizeLiveMap();
    window.dispatchEvent(new Event('resize'));
    fitNavigationMetricText();
  });
}

function initNavigationModeUi() {
  const navButton = document.getElementById('map-navigation-btn');
  const exitButton = document.getElementById('map-navigation-exit');
  const settingsButton = document.getElementById('map-navigation-settings');
  const profilePanel = document.getElementById('gpx-profile-panel');

  if (navButton) navButton.addEventListener('click', () => setNavigationMode(true));
  if (exitButton) exitButton.addEventListener('click', () => setNavigationMode(false));
  if (settingsButton) {
    settingsButton.addEventListener('click', () => {
      setNavigationControlsOpen(!document.body.classList.contains('navigation-controls-open'));
    });
  }
  if (profilePanel) {
    new MutationObserver(setNavigationProfileClass).observe(profilePanel, { attributes: true, attributeFilter: ['hidden'] });
  }
  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.body.classList.contains('navigation-mode')) {
      setNavigationMode(false);
    }
  });
  window.addEventListener('cycle-route-progress', (event) => {
    latestRouteProgress = event.detail || null;
    if (latestRouteProgress) {
      latestNavigationValues.route_remaining = latestRouteProgress.remainingKm;
      latestNavigationValues.route_remaining_label = latestRouteProgress.remainingLabel;
    } else {
      latestNavigationValues.route_remaining = null;
      latestNavigationValues.route_remaining_label = '-';
    }
    renderNavigationMetrics();
  });
  window.addEventListener('storage', (event) => {
    if (event.key === NAVIGATION_METRIC_STORAGE_KEY) renderNavigationMetrics();
  });
  renderNavigationMetrics();
}

function diagnoseDashboardCause(reader) {
  if (!reader || !Number(reader.updated_at)) return "reader diag missing";
  const rawAge = Number(reader?.raw_age_ms);
  const rawHz = Number(reader?.raw_hz);
  const loopMs = Number(reader?.loop_ms);
  const sourceMs = Number(reader?.source_ms);
  const fetchMs = Number(dashboardDiag.fetchMs);
  const renderMs = Number(dashboardDiag.renderMs);
  const liveHz = Number(dashboardDiag.liveHz);

  if (Number.isFinite(rawAge) && rawAge > 2000) return "source data stale";
  if (Number.isFinite(sourceMs) && sourceMs > 700) return "serial/source wait";
  if (Number.isFinite(loopMs) && loopMs > 700) return "reader loop slow";
  if (Number.isFinite(rawHz) && rawHz > 0 && rawHz < 1.5) return "source < 1.5Hz";
  if (Number.isFinite(fetchMs) && fetchMs > 350) return "server/network";
  if (Number.isFinite(renderMs) && renderMs > 80) return "browser render";
  if (Number.isFinite(liveHz) && liveHz > 0 && liveHz < 2) return "dashboard loop";
  return "OK";
}

function updateDashboardDiagDisplay(reader = dashboardDiag.reader) {
  const cause = diagnoseDashboardCause(reader);
  setText(
    "sys-dash-diag",
    `Dash: ${fmtHz(dashboardDiag.liveHz)} fetch ${fmtMs(dashboardDiag.fetchMs)} render ${fmtMs(dashboardDiag.renderMs)} srv ${fmtMs(dashboardDiag.serverMs)} skip ${dashboardDiag.skips}`
  );
  setText(
    "sys-reader-diag",
    reader && Number(reader.updated_at)
      ? `Reader: ${fmtHz(reader?.raw_hz)} age ${fmtMs(reader?.raw_age_ms)} loop ${fmtMs(reader?.loop_ms)} src ${fmtMs(reader?.source_ms)} i2c ${fmtMs(num(reader?.solar_ms, 0) + num(reader?.motor_ms, 0))}`
      : "Reader: no diag"
  );
  setText("sys-cause-diag", `Cause: ${cause}`);
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

function updateAuxMeter(prefix, live, avg, max, meterMax = AUX_METER_MAX_W) {
  const clampedLive = Math.max(0, num(live, 0));
  const clampedAvg = Math.max(0, num(avg, 0));
  const clampedMax = Math.max(0, num(max, 0));

  setArcPath(`${prefix}-arc`, clampedLive, 0, meterMax);
  rotateNeedle(`avg-${prefix}-line`, clampedAvg, 0, meterMax, clampedAvg.toFixed(0), `avg-${prefix}-label`, "blue");
  rotateNeedle(`max-${prefix}-line`, clampedMax, 0, meterMax, clampedMax.toFixed(0), `max-${prefix}-label`, "red");
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
function formatPowerWindow(seconds) {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  return `${minutes} min`;
}

function powerAgoLabel(seconds) {
  if (seconds <= 0) return "now";
  if (seconds < 60) return `-${Math.round(seconds)}s`;
  return `-${Math.round(seconds / 60)}m`;
}

function parsePowerTimestamp(timestamp) {
  if (!timestamp) return null;
  const raw = String(timestamp);
  const hasTimezone = /(?:z|[+-]\d{2}:?\d{2})$/i.test(raw);
  const parsed = Date.parse(hasTimezone ? raw : `${raw}Z`);
  return Number.isFinite(parsed) ? parsed : null;
}

function powerTimeScale(width, margin, points) {
  const plotWidth = width - margin.left - margin.right;
  const windowMs = Math.max(1, (powerHistoryState.windowSeconds || POWER_HISTORY_WINDOWS[0]) * 1000);
  const fallbackNow = Date.now();
  const latestPointMs = Math.max(
    0,
    ...points.map(point => parsePowerTimestamp(point.timestamp) || 0)
  );
  const nowMs = powerHistoryState.serverNowMs || latestPointMs || fallbackNow;
  const startMs = nowMs - windowMs;

  const xForTime = (timestamp, fallbackIndex = 0) => {
    const pointMs = parsePowerTimestamp(timestamp);
    if (pointMs === null) {
      return margin.left + (plotWidth * fallbackIndex) / Math.max(1, points.length - 1);
    }
    const ratio = clamp((pointMs - startMs) / windowMs, 0, 1);
    return margin.left + plotWidth * ratio;
  };

  const xForAgo = (agoSeconds) => {
    const ratio = clamp((windowMs - agoSeconds * 1000) / windowMs, 0, 1);
    return margin.left + plotWidth * ratio;
  };

  return { xForTime, xForAgo };
}

function updatePowerHistoryControls() {
  powerHistoryState.windowSeconds = POWER_HISTORY_WINDOWS[powerHistoryState.windowIndex] || POWER_HISTORY_WINDOWS[0];

  const windowLabel = document.getElementById("power-history-window");
  if (windowLabel) windowLabel.textContent = formatPowerWindow(powerHistoryState.windowSeconds);

  const zoomIn = document.getElementById("power-history-zoom-in");
  if (zoomIn) zoomIn.disabled = powerHistoryState.windowIndex <= 0;

  const zoomOut = document.getElementById("power-history-zoom-out");
  if (zoomOut) zoomOut.disabled = powerHistoryState.windowIndex >= POWER_HISTORY_WINDOWS.length - 1;

  const cumulativeButton = document.getElementById("power-chart-cumulative");
  if (cumulativeButton) cumulativeButton.classList.toggle("active-chart", powerHistoryState.cumulative);
}

function traceSmoothLine(ctx, coords, continuePath = false) {
  if (!coords.length) return;
  if (coords.length === 1) {
    if (!continuePath) ctx.moveTo(coords[0].x, coords[0].y);
    ctx.lineTo(coords[0].x + 0.01, coords[0].y);
    return;
  }

  if (!continuePath) ctx.moveTo(coords[0].x, coords[0].y);
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

function drawSmoothLine(ctx, coords, color, width = 2) {
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.beginPath();
  traceSmoothLine(ctx, coords);
  ctx.stroke();
}

function drawFilledBand(ctx, xs, lowerValues, upperValues, yForValue, color) {
  if (!xs.length) return;
  const upper = xs.map((x, index) => ({ x, y: yForValue(upperValues[index]) }));
  const lower = xs.map((x, index) => ({ x, y: yForValue(lowerValues[index]) })).reverse();

  ctx.fillStyle = color;
  ctx.beginPath();
  traceSmoothLine(ctx, upper);
  if (lower.length) {
    ctx.lineTo(lower[0].x, lower[0].y);
    traceSmoothLine(ctx, lower, true);
  }
  ctx.closePath();
  ctx.fill();
}

function buildCumulativePowerPoint(point) {
  const human = Math.max(0, num(point.human_power, 0));
  const solar = Math.max(0, num(point.solar_power, 0));
  const motorPower = num(point.motor_power, 0);
  const motorUse = Math.max(0, motorPower);
  const regen = Math.max(0, -motorPower);
  const production = human + solar + regen;
  const net = production - motorUse;

  if (net <= 0 || production <= 0) {
    return {
      human_net: 0,
      solar_net: 0,
      regen_net: 0,
      total_positive: 0,
      deficit: Math.min(0, net),
      net
    };
  }

  const humanNet = net * (human / production);
  const solarNet = net * (solar / production);
  const regenNet = net - humanNet - solarNet;
  return {
    human_net: humanNet,
    solar_net: solarNet,
    regen_net: regenNet,
    total_positive: net,
    deficit: 0,
    net
  };
}

function drawPowerChartFrame(ctx, width, height, margin, minVal, maxVal, yForValue) {
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

  const zeroY = yForValue(0);
  if (zeroY >= margin.top && zeroY <= height - margin.bottom) {
    ctx.strokeStyle = "#777";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(margin.left, zeroY);
    ctx.lineTo(width - margin.right, zeroY);
    ctx.stroke();
  }
}

function drawPowerTimeTicks(ctx, width, height, margin, xForAgo) {
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  const totalSeconds = powerHistoryState.windowSeconds || 180;
  const ticks = [
    { ago: totalSeconds },
    { ago: (2 * totalSeconds) / 3 },
    { ago: totalSeconds / 3 },
    { ago: 0 }
  ];

  ticks.forEach(tick => {
    const x = xForAgo(tick.ago);
    ctx.strokeStyle = "#999";
    ctx.beginPath();
    ctx.moveTo(x, height - margin.bottom);
    ctx.lineTo(x, height - margin.bottom + 4);
    ctx.stroke();
    ctx.fillStyle = "#666";
    ctx.fillText(powerAgoLabel(tick.ago), x, height - margin.bottom + 6);
  });
}

function renderCumulativePowerHistory(ctx, points, width, height, margin) {
  const plotHeight = height - margin.top - margin.bottom;
  const stacked = points.map(buildCumulativePowerPoint);
  const maxAbsRaw = Math.max(
    ...stacked.map(point => Math.abs(point.net)),
    ...stacked.map(point => point.total_positive),
    100
  );
  const maxAbsVal = Math.max(100, Math.ceil(maxAbsRaw / 50) * 50);
  const minVal = -maxAbsVal;
  const maxVal = maxAbsVal;

  const yForValue = (value) => margin.top + (maxVal - value) * plotHeight / (maxVal - minVal);
  const { xForTime, xForAgo } = powerTimeScale(width, margin, points);
  const xs = points.map((point, index) => xForTime(point.timestamp, index));
  const zeroValues = stacked.map(() => 0);
  const humanValues = stacked.map(point => point.human_net);
  const solarValues = stacked.map(point => point.human_net + point.solar_net);
  const totalValues = stacked.map(point => point.total_positive);
  const deficitValues = stacked.map(point => point.deficit);

  drawPowerChartFrame(ctx, width, height, margin, minVal, maxVal, yForValue);

  drawFilledBand(ctx, xs, zeroValues, humanValues, yForValue, "rgba(240, 138, 36, 0.55)");
  drawFilledBand(ctx, xs, humanValues, solarValues, yForValue, "rgba(199, 182, 0, 0.48)");
  drawFilledBand(ctx, xs, solarValues, totalValues, yForValue, "rgba(38, 146, 126, 0.48)");
  drawFilledBand(ctx, xs, deficitValues, zeroValues, yForValue, "rgba(209, 73, 46, 0.36)");

  drawSmoothLine(ctx, xs.map((x, index) => ({ x, y: yForValue(totalValues[index]) })), "#116b5e", 1.5);
  drawSmoothLine(ctx, xs.map((x, index) => ({ x, y: yForValue(stacked[index].net) })), "#111", 1.25);
  drawPowerTimeTicks(ctx, width, height, margin, xForAgo);
}

function renderStandardPowerHistory(ctx, points, width, height, margin) {
  const enabled = {
    motor: powerHistoryState.motor,
    human: powerHistoryState.human,
    solar: powerHistoryState.solar
  };
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

  const yForValue = (value) => margin.top + (maxVal - value) * plotHeight / (maxVal - minVal);
  const { xForTime, xForAgo } = powerTimeScale(width, margin, points);

  drawPowerChartFrame(ctx, width, height, margin, minVal, maxVal, yForValue);
  drawPowerTimeTicks(ctx, width, height, margin, xForAgo);

  activeSeries.forEach(seriesDef => {
    const coords = points.map((point, index) => ({
      x: xForTime(point.timestamp, index),
      y: yForValue(num(point[seriesDef.key], 0))
    }));
    drawSmoothLine(ctx, coords, seriesDef.color, 2);
  });
}

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
  if (!points.length) {
    ctx.fillStyle = "#666";
    ctx.font = "12px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Awaiting power history", width / 2, height / 2);
    return;
  }

  const margin = { top: 10, right: 12, bottom: 22, left: 42 };
  if (powerHistoryState.cumulative) {
    renderCumulativePowerHistory(ctx, points, width, height, margin);
  } else {
    renderStandardPowerHistory(ctx, points, width, height, margin);
  }
}

function setPowerSeriesButtonState() {
  [
    ["power-series-motor", powerHistoryState.motor],
    ["power-series-human", powerHistoryState.human],
    ["power-series-solar", powerHistoryState.solar]
  ].forEach(([id, enabled]) => {
    const button = document.getElementById(id);
    if (!button) return;
    if (id === "power-series-solar") {
      button.disabled = !powerHistoryState.solarRoofEnabled;
    }
    button.classList.toggle("active-chart", enabled);
  });
  updatePowerHistoryControls();
}

function showChartById(chartId) {
  const chartButtons = {
    "btn-wh": "wh-per-km-chart",
    "btn-human": "human-pct-per-km-chart",
    "btn-solar": "solar-pct-per-km-chart",
    "btn-regen": "regen-pct-per-km-chart"
  };
  Object.entries(chartButtons).forEach(([buttonId, targetChartId]) => {
    const button = document.getElementById(buttonId);
    const chart = document.getElementById(targetChartId);
    if (chart) chart.style.display = targetChartId === chartId ? "" : "none";
    if (button) button.classList.toggle("active-chart", targetChartId === chartId);
  });
}

function setSolarUiEnabled(enabled) {
  const isEnabled = enabled !== false;
  powerHistoryState.solarRoofEnabled = isEnabled;
  if (!isEnabled) {
    powerHistoryState.solar = false;
  }

  document.querySelectorAll(".solar-box").forEach(el => {
    el.style.display = isEnabled ? "" : "none";
  });
  const solarSeriesButton = document.getElementById("power-series-solar");
  if (solarSeriesButton) {
    solarSeriesButton.style.display = isEnabled ? "" : "none";
    solarSeriesButton.disabled = !isEnabled;
  }
  const solarChartButton = document.getElementById("btn-solar");
  if (solarChartButton) {
    solarChartButton.style.display = isEnabled ? "" : "none";
    solarChartButton.disabled = !isEnabled;
  }
  const solarChart = document.getElementById("solar-pct-per-km-chart");
  if (!isEnabled && solarChart && solarChart.style.display !== "none") {
    showChartById("wh-per-km-chart");
  }
  setPowerSeriesButtonState();
}

async function fetchPowerHistory() {
  if (document.body.classList.contains('edit-mode')) return;
  if (powerHistoryRequestInFlight) return;
  powerHistoryRequestInFlight = true;
  try {
    const params = new URLSearchParams({
      window_seconds: String(powerHistoryState.windowSeconds || POWER_HISTORY_WINDOWS[0]),
      samples: String(POWER_HISTORY_SAMPLE_COUNT)
    });
    const res = await fetch(`/power_history?${params.toString()}`, { cache: 'no-store' });
    const json = await res.json();
    powerHistoryState.points = Array.isArray(json.points) ? json.points : [];
    if (Number.isFinite(Number(json.window_seconds))) {
      powerHistoryState.windowSeconds = Number(json.window_seconds);
    }
    powerHistoryState.serverNowMs = parsePowerTimestamp(json.server_time) || Date.now();
    renderPowerHistoryChart();
  } catch (err) {
    console.error('Error fetching power history:', err);
  } finally {
    powerHistoryRequestInFlight = false;
  }
}

function startPowerHistoryLoop() {
  const tick = async () => {
    await fetchPowerHistory();
    setTimeout(tick, document.hidden ? POWER_HISTORY_POLL_HIDDEN_MS : POWER_HISTORY_POLL_MS);
  };
  tick();
}

function setSvgAttrs(el, attrs) {
  Object.entries(attrs).forEach(([key, value]) => {
    if (value !== undefined && value !== null) el.setAttribute(key, value);
  });
  return el;
}

function addSvgElement(parent, tag, attrs, text) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  setSvgAttrs(el, attrs);
  if (text !== undefined && text !== null) el.textContent = text;
  parent.appendChild(el);
  return el;
}

function niceChartBounds(minValue, maxValue, targetTicks = 4) {
  let min = Number.isFinite(minValue) ? minValue : 0;
  let max = Number.isFinite(maxValue) ? maxValue : 1;
  if (min === max) {
    min = Math.min(0, min);
    max = Math.max(1, max);
  }
  const span = Math.max(1e-6, max - min);
  const rawStep = span / Math.max(1, targetTicks);
  const pow = 10 ** Math.floor(Math.log10(rawStep));
  const step = [1, 2, 5, 10].map(m => m * pow).find(s => rawStep <= s) || 10 * pow;
  return {
    min: Math.floor(min / step) * step,
    max: Math.ceil(max / step) * step,
    step
  };
}

function formatCompactChartValue(value) {
  const v = num(value, 0);
  if (Math.abs(v) >= 100) return v.toFixed(0);
  if (Math.abs(v) >= 10) return v.toFixed(0);
  return v.toFixed(1);
}

function prepareChartSvg(chart, width, height) {
  chart.innerHTML = '';
  chart.setAttribute("viewBox", `0 0 ${width} ${height}`);
  chart.setAttribute("preserveAspectRatio", "xMidYMid meet");
}

function addChartBottomLabel(chart, x, y, text, options = {}) {
  const label = String(text);
  const width = Math.max(options.minWidth || 17, label.length * 4.4 + 8);
  addSvgElement(chart, "rect", {
    x: x - width / 2,
    y: y - 9,
    width,
    height: 12,
    rx: 3,
    ry: 3,
    fill: "rgba(255,255,255,0.34)",
    stroke: "rgba(0,0,0,0.18)",
    "stroke-width": 0.6
  });
  addSvgElement(chart, "text", {
    x,
    y,
    "text-anchor": "middle",
    "font-size": 7.2,
    "font-weight": "bold",
    fill: "rgba(0,0,0,0.72)"
  }, label);
}

function solarPowerAtHour(points, hour) {
  if (!Array.isArray(points) || points.length === 0) return 0;
  const sorted = [...points]
    .map(point => ({ hour: num(point.hour, 0), power: num(point.power_w ?? point.power, 0) }))
    .sort((a, b) => a.hour - b.hour);
  if (hour <= sorted[0].hour) return sorted[0].power;
  if (hour >= sorted[sorted.length - 1].hour) return sorted[sorted.length - 1].power;
  for (let i = 1; i < sorted.length; i++) {
    const prev = sorted[i - 1];
    const next = sorted[i];
    if (hour <= next.hour) {
      const ratio = (hour - prev.hour) / Math.max(1e-6, next.hour - prev.hour);
      return prev.power + (next.power - prev.power) * ratio;
    }
  }
  return 0;
}

async function refreshSolarSessionProfile(force = false) {
  if (!solarPotentialChartOpen || solarSessionProfileLoading) return;
  const now = Date.now();
  if (!force && now - solarSessionProfileFetchedAt < 5000) return;
  solarSessionProfileLoading = true;
  solarSessionProfileFetchedAt = now;
  try {
    const response = await fetch('/solar_session_profile?bucket_minutes=5&samples=360', { cache: 'no-store' });
    const data = await response.json();
    latestSolarSessionProfile = response.ok ? data : null;
  } catch (error) {
    latestSolarSessionProfile = null;
  } finally {
    solarSessionProfileLoading = false;
    if (solarPotentialChartOpen) renderSolarPotentialChart();
  }
}

function renderSolarPotentialChart(solarBattery = latestSolarBattery) {
  const box = document.getElementById("solar-potential-chart-box");
  const chart = document.getElementById("solar-potential-chart");
  if (!box || !chart || box.hidden) return;

  const profile = solarBattery?.solar_power_profile_today;
  const points = Array.isArray(profile?.points) ? profile.points : [];
  const usablePoints = points
    .map(point => ({ hour: num(point.hour, 0), power: Math.max(0, num(point.power_w, 0)) }))
    .filter(point => Number.isFinite(point.hour) && point.hour >= 0 && point.hour <= 24);
  const sessionPoints = (Array.isArray(latestSolarSessionProfile?.points) ? latestSolarSessionProfile.points : [])
    .map(point => ({ hour: num(point.hour, 0), power: Math.max(0, num(point.power_w, 0)) }))
    .filter(point => Number.isFinite(point.hour) && point.hour >= 0 && point.hour <= 24);

  const width = 400;
  const height = 168;
  prepareChartSvg(chart, width, height);

  if (usablePoints.length < 2) {
    addSvgElement(chart, "text", {
      x: width / 2,
      y: height / 2,
      "text-anchor": "middle",
      "font-size": 9,
      fill: "rgba(0,0,0,0.55)"
    }, "No solar profile");
    return;
  }

  const plot = { left: 34, right: 10, top: 12, bottom: 24 };
  plot.width = width - plot.left - plot.right;
  plot.height = height - plot.top - plot.bottom;

  const peak = Math.max(1, ...usablePoints.map(point => point.power), ...sessionPoints.map(point => point.power));
  const bounds = niceChartBounds(0, peak, 4);
  bounds.min = 0;
  const xFor = hour => plot.left + (clamp(hour, 0, 24) / 24) * plot.width;
  const yFor = power => plot.top + ((bounds.max - power) / Math.max(1e-6, bounds.max - bounds.min)) * plot.height;
  const baselineY = yFor(0);

  for (let t = 0; t <= bounds.max + bounds.step / 2; t += bounds.step) {
    const y = yFor(t);
    addSvgElement(chart, "line", {
      x1: plot.left,
      x2: width - plot.right,
      y1: y,
      y2: y,
      stroke: t === 0 ? "#111" : "rgba(0,0,0,0.18)",
      "stroke-width": t === 0 ? 0.9 : 0.5
    });
    addSvgElement(chart, "text", {
      x: plot.left - 7,
      y: y + 3,
      "text-anchor": "end",
      "font-size": 7.5,
      fill: "rgba(0,0,0,0.64)"
    }, formatCompactChartValue(t));
  }

  [0, 6, 12, 18, 24].forEach(hour => {
    const x = xFor(hour);
    addSvgElement(chart, "line", {
      x1: x,
      x2: x,
      y1: plot.top,
      y2: baselineY,
      stroke: "rgba(0,0,0,0.12)",
      "stroke-width": 0.5
    });
    addSvgElement(chart, "text", {
      x,
      y: height - 7,
      "text-anchor": "middle",
      "font-size": 7.5,
      "font-weight": "bold",
      fill: "rgba(0,0,0,0.62)"
    }, hour === 24 ? "24" : `${hour}h`);
  });

  const linePoints = usablePoints.map(point => ({
    x: xFor(point.hour),
    y: yFor(point.power)
  }));
  const linePath = linePoints.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(" ");
  const areaPath = `${linePath} L ${xFor(24).toFixed(2)} ${baselineY.toFixed(2)} L ${xFor(0).toFixed(2)} ${baselineY.toFixed(2)} Z`;

  addSvgElement(chart, "path", {
    d: areaPath,
    fill: "rgba(239, 143, 48, 0.22)"
  });
  addSvgElement(chart, "path", {
    d: linePath,
    fill: "none",
    stroke: "orange",
    "stroke-width": 2.1,
    "stroke-linecap": "round",
    "stroke-linejoin": "round"
  });

  if (sessionPoints.length >= 2) {
    const sessionLinePath = sessionPoints
      .map((point, index) => `${index === 0 ? "M" : "L"} ${xFor(point.hour).toFixed(2)} ${yFor(point.power).toFixed(2)}`)
      .join(" ");
    addSvgElement(chart, "path", {
      d: sessionLinePath,
      fill: "none",
      stroke: "#0ea5e9",
      "stroke-width": 2,
      "stroke-linecap": "round",
      "stroke-linejoin": "round"
    });
  }

  const legendY = plot.top + 7;
  addSvgElement(chart, "line", {
    x1: width - 132,
    x2: width - 118,
    y1: legendY,
    y2: legendY,
    stroke: "orange",
    "stroke-width": 2
  });
  addSvgElement(chart, "text", {
    x: width - 114,
    y: legendY + 3,
    "font-size": 7.3,
    "font-weight": "bold",
    fill: "rgba(0,0,0,0.64)"
  }, profile?.imported_profile ? "imported" : "ideal");
  if (sessionPoints.length >= 2) {
    addSvgElement(chart, "line", {
      x1: width - 64,
      x2: width - 50,
      y1: legendY,
      y2: legendY,
      stroke: "#0ea5e9",
      "stroke-width": 2
    });
    addSvgElement(chart, "text", {
      x: width - 46,
      y: legendY + 3,
      "font-size": 7.3,
      "font-weight": "bold",
      fill: "rgba(0,0,0,0.64)"
    }, "session");
  }

  const nowHour = clamp(num(profile?.now_hour, new Date().getHours() + new Date().getMinutes() / 60), 0, 24);
  const nowPower = Math.max(0, solarPowerAtHour(usablePoints, nowHour));
  const nowX = xFor(nowHour);
  const nowY = yFor(nowPower);
  addSvgElement(chart, "line", {
    x1: nowX,
    x2: nowX,
    y1: plot.top - 2,
    y2: baselineY + 2,
    stroke: "#111",
    "stroke-width": 1.2,
    "stroke-dasharray": "3 3"
  });
  addSvgElement(chart, "circle", {
    cx: nowX,
    cy: nowY,
    r: 4,
    fill: "#111",
    stroke: "orange",
    "stroke-width": 1.2
  });
  addChartBottomLabel(chart, nowX, plot.top + 8, "now", { minWidth: 20 });

  const nowEl = document.getElementById("solar-chart-now-value");
  const peakEl = document.getElementById("solar-chart-peak-value");
  const sessionPeakEl = document.getElementById("solar-chart-session-peak-value");
  const remainingEl = document.getElementById("solar-chart-remaining-value");
  if (nowEl) nowEl.textContent = num(solarBattery?.potential_power_now_w, nowPower).toFixed(0);
  if (peakEl) peakEl.textContent = peak.toFixed(0);
  if (sessionPeakEl) {
    const sessionPeak = sessionPoints.length ? Math.max(...sessionPoints.map(point => point.power)) : 0;
    sessionPeakEl.textContent = sessionPeak > 0 ? sessionPeak.toFixed(0) : "-";
  }
  if (remainingEl) remainingEl.textContent = num(solarBattery?.potential_remaining_today_wh, 0).toFixed(0);
}

function setSolarPotentialChartOpen(open) {
  const row = document.getElementById("solar-potential-row");
  const box = document.getElementById("solar-potential-chart-box");
  if (!row || !box) return;
  if (open) {
    const tripBox = row.closest(".box");
    if (tripBox && tripBox.nextElementSibling !== box) {
      tripBox.insertAdjacentElement("afterend", box);
    }
  }
  solarPotentialChartOpen = Boolean(open);
  box.hidden = !solarPotentialChartOpen;
  row.classList.toggle("is-expanded", solarPotentialChartOpen);
  row.setAttribute("aria-expanded", solarPotentialChartOpen ? "true" : "false");
  if (solarPotentialChartOpen) {
    renderSolarPotentialChart();
    refreshSolarSessionProfile(true);
  }
}

function updateWhPerKmChart(totalValues, netValues = [], liveTotal = 0, liveNet = 0, range = 10) {
  const chart = document.getElementById("wh-per-km-chart");
  if (!chart) return;

  const width = 400;
  const chartHeight = 210;
  prepareChartSvg(chart, width, chartHeight);

  const plot = { left: 32, right: 8, top: 13, bottom: 24 };
  plot.width = width - plot.left - plot.right;
  plot.height = chartHeight - plot.top - plot.bottom;
  const totalBars = range + 1; // history + live
  const gapRatio = range > 10 ? 0.18 : 0.28;
  const barWidth = plot.width / (totalBars + (totalBars - 1) * gapRatio);
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

  const numericValues = [...values, ...netFull].filter(v => v !== undefined && Number.isFinite(Number(v))).map(Number);
  const minValue = Math.min(0, ...numericValues);
  const maxValue = Math.max(1, ...numericValues);
  const bounds = niceChartBounds(minValue, maxValue, 4);
  const yFor = (v) => plot.top + ((bounds.max - v) / Math.max(1e-6, bounds.max - bounds.min)) * plot.height;
  const zeroY = yFor(0);

  for (let t = bounds.min; t <= bounds.max + bounds.step / 2; t += bounds.step) {
    const y = yFor(t);
    const isZero = Math.abs(t) < bounds.step / 1000;
    addSvgElement(chart, "line", {
      x1: plot.left,
      x2: width - plot.right,
      y1: y,
      y2: y,
      stroke: isZero ? "#111" : "rgba(0,0,0,0.24)",
      "stroke-width": isZero ? 1 : 0.5
    });
    addSvgElement(chart, "text", {
      x: plot.left - 7,
      y: y + 3,
      "text-anchor": "end",
      "font-size": 7.5,
      fill: "rgba(0,0,0,0.68)"
    }, formatCompactChartValue(t));
  }

  for (let i = 0; i < totalBars; i++) {
    const total = values[i];
    const net = netFull[i];

    const x = plot.left + i * (barWidth + spacing);

    if (total === undefined) continue;

    const totalSafe = num(total, 0);
    const netSafe   = num(net, 0);

    const totalYValue = yFor(totalSafe);
    const totalY = Math.min(zeroY, totalYValue);
    const totalH = Math.max(0.5, Math.abs(zeroY - totalYValue));
    const isLive = i === totalBars - 1;

    addSvgElement(chart, "rect", {
      x,
      y: totalY,
      width: barWidth,
      height: totalH,
      rx: 2,
      ry: 2,
      fill: "orange",
      "fill-opacity": isLive ? 1 : 0.72,
      stroke: isLive ? "#111" : "none",
      "stroke-width": isLive ? 0.8 : 0
    });

    if (net !== undefined) {
      const netYValue = yFor(netSafe);
      const netY = Math.min(zeroY, netYValue);
      const netH = Math.max(0.5, Math.abs(zeroY - netYValue));
      addSvgElement(chart, "rect", {
        x: x + barWidth * 0.18,
        y: netY,
        width: barWidth * 0.64,
        height: netH,
        rx: 1.5,
        ry: 1.5,
        fill: "#111",
        "fill-opacity": isLive ? 0.62 : 0.42
      });
    }

    if (range === 10) {
      addChartBottomLabel(chart, x + barWidth / 2, chartHeight - 7, isLive ? "live" : formatCompactChartValue(totalSafe));
    }
  }

  addSvgElement(chart, "rect", { x: width - 80, y: 5, width: 7, height: 7, rx: 1, fill: "orange", "fill-opacity": 0.75 });
  addSvgElement(chart, "text", { x: width - 69, y: 11, "font-size": 7.5, fill: "rgba(0,0,0,0.72)" }, "total");
  addSvgElement(chart, "rect", { x: width - 37, y: 5, width: 7, height: 7, rx: 1, fill: "#111", "fill-opacity": 0.45 });
  addSvgElement(chart, "text", { x: width - 26, y: 11, "font-size": 7.5, fill: "rgba(0,0,0,0.72)" }, "net");
}

function updatePctChart(chartId, pctValues, livePct = 0, range = 10, liveColor = "orange") {
  const chart = document.getElementById(chartId);
  if (!chart) return;

  const width = 400;
  const height = 210;
  prepareChartSvg(chart, width, height);

  const plot = { left: 32, right: 8, top: 13, bottom: 24 };
  plot.width = width - plot.left - plot.right;
  plot.height = height - plot.top - plot.bottom;
  const totalBars = range + 1;
  const gapRatio = range > 10 ? 0.18 : 0.28;
  const barWidth = plot.width / (totalBars + (totalBars - 1) * gapRatio);
  const spacing = barWidth * gapRatio;

  const vals = (Array.isArray(pctValues) ? pctValues : []).map(v => num(v, 0));
  livePct = num(livePct, 0);

  const count = Math.min(range, vals.length);
  const padded = new Array(range - count).fill(undefined).concat([...vals].slice(-count));
  const values = [...padded, livePct];

  const maxVal = Math.max(...values.filter(v => v !== undefined).map(v => Math.max(0, num(v, 0))), 1);
  const bounds = niceChartBounds(0, maxVal, 4);
  bounds.min = 0;
  const yFor = (v) => plot.top + ((bounds.max - v) / Math.max(1e-6, bounds.max - bounds.min)) * plot.height;
  const zeroY = yFor(0);

  for (let t = 0; t <= bounds.max + bounds.step / 2; t += bounds.step) {
    const y = yFor(t);
    const isZero = Math.abs(t) < bounds.step / 1000;
    addSvgElement(chart, "line", {
      x1: plot.left,
      x2: width - plot.right,
      y1: y,
      y2: y,
      stroke: isZero ? "#111" : "rgba(0,0,0,0.24)",
      "stroke-width": isZero ? 1 : 0.5
    });
    addSvgElement(chart, "text", {
      x: plot.left - 7,
      y: y + 3,
      "text-anchor": "end",
      "font-size": 7.5,
      fill: "rgba(0,0,0,0.68)"
    }, formatCompactChartValue(t));
  }

  for (let i = 0; i < totalBars; i++) {
    const val = values[i];
    if (val === undefined) continue;

    const v = num(val, 0);
    const x = plot.left + i * (barWidth + spacing);
    const yValue = yFor(Math.max(0, v));
    const h = Math.max(0.5, zeroY - yValue);
    const isLive = i === totalBars - 1;

    addSvgElement(chart, "rect", {
      x,
      y: yValue,
      width: barWidth,
      height: h,
      rx: 2,
      ry: 2,
      fill: isLive ? liveColor : "orange",
      "fill-opacity": isLive ? 0.98 : 0.72,
      stroke: isLive ? "#111" : "none",
      "stroke-width": isLive ? 0.8 : 0
    });

    if (range === 10) {
      addChartBottomLabel(chart, x + barWidth / 2, height - 7, isLive ? "live" : formatCompactChartValue(v));
    }
  }
}

function updateRegenPctChart(pctValues, livePct = 0, range = 10) {
  const chart = document.getElementById("regen-pct-per-km-chart");
  if (!chart) return;

  const width = 400;
  const height = 210;
  prepareChartSvg(chart, width, height);

  const plot = { left: 32, right: 8, top: 13, bottom: 24 };
  plot.width = width - plot.left - plot.right;
  plot.height = height - plot.top - plot.bottom;
  const totalBars = range + 1;
  const gapRatio = range > 10 ? 0.18 : 0.28;
  const barWidth = plot.width / (totalBars + (totalBars - 1) * gapRatio);
  const spacing = barWidth * gapRatio;

  const vals = (Array.isArray(pctValues) ? pctValues : []).map(v => num(v, 0));
  livePct = Math.max(0, num(livePct, 0));

  const count = Math.min(range, vals.length);
  const padded = new Array(range - count).fill(undefined).concat([...vals].slice(-count));
  const values = [...padded, livePct];

  const maxVal = Math.max(...values.filter(v => v !== undefined).map(v => Math.max(0, num(v, 0))), 1);
  const bounds = niceChartBounds(0, maxVal, 4);
  bounds.min = 0;
  const yFor = (v) => plot.top + ((bounds.max - v) / Math.max(1e-6, bounds.max - bounds.min)) * plot.height;
  const zeroY = yFor(0);

  for (let t = 0; t <= bounds.max + bounds.step / 2; t += bounds.step) {
    const y = yFor(t);
    const isZero = Math.abs(t) < bounds.step / 1000;
    addSvgElement(chart, "line", {
      x1: plot.left,
      x2: width - plot.right,
      y1: y,
      y2: y,
      stroke: isZero ? "#111" : "rgba(0,0,0,0.24)",
      "stroke-width": isZero ? 1 : 0.5
    });
    addSvgElement(chart, "text", {
      x: plot.left - 7,
      y: y + 3,
      "text-anchor": "end",
      "font-size": 7.5,
      fill: "rgba(0,0,0,0.68)"
    }, formatCompactChartValue(t));
  }

  for (let i = 0; i < totalBars; i++) {
    const raw = values[i];
    if (raw === undefined) continue;
    const v = Math.max(0, num(raw, 0));

    const x = plot.left + i * (barWidth + spacing);
    const yValue = yFor(v);
    const h = Math.max(0.5, zeroY - yValue);
    const isLive = i === totalBars - 1;

    addSvgElement(chart, "rect", {
      x,
      y: yValue,
      width: barWidth,
      height: h,
      rx: 2,
      ry: 2,
      fill: isLive ? "#0099cc" : "orange",
      "fill-opacity": isLive ? 0.98 : 0.72,
      stroke: isLive ? "#111" : "none",
      "stroke-width": isLive ? 0.8 : 0
    });

    if (range === 10) {
      addChartBottomLabel(chart, x + barWidth / 2, height - 7, isLive ? "live" : formatCompactChartValue(v));
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
  const pendingUploads = num(cfg.pending_upload_count, 0);
  uploaded.textContent = `Uploaded: ${cfg.last_uploaded_at || "–"}${
    pendingUploads > 0 ? ` (${pendingUploads.toFixed(0)} pending)` : ""
  }`;

  const imageUrl = cfg.latest_local_url || cfg.latest_public_url || "";
  const cacheToken = encodeURIComponent(cfg.last_captured_at || cfg.last_uploaded_at || "");
  const imageKey = imageUrl ? `${imageUrl}|${cacheToken}` : null;

  if (cfg.last_error && pendingUploads > 0) {
    status.textContent = `Upload pending: ${pendingUploads.toFixed(0)} photo${
      pendingUploads === 1 ? "" : "s"
    } queued.`;
    status.classList.remove("error");
  } else if (cfg.last_error) {
    status.textContent = `Capture error: ${cfg.last_error}`;
    status.classList.add("error");
  } else if (pendingUploads > 0) {
    status.textContent = `Upload pending: ${pendingUploads.toFixed(0)} photo${
      pendingUploads === 1 ? "" : "s"
    } queued.`;
    status.classList.remove("error");
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
drawTicks("solar-ticks", 0, AUX_METER_MAX_W, 5, 50, 80, 70, 75);
drawTicks("pv-ticks", 0, PV_METER_MAX_W, 10, 100, 80, 70, 75);

/* ====== FETCH + RENDER LOOP ====== */
async function fetchMetrics({ full = false } = {}) {
  if (document.body.classList.contains('edit-mode')) {
    metricsPaused = true;
    return;
  }
  if (full) {
    if (fullMetricsRequestInFlight) return;
    fullMetricsRequestInFlight = true;
  } else {
    if (metricsRequestInFlight) {
      dashboardDiag.skips += 1;
      updateDashboardDiagDisplay();
      return;
    }
    metricsRequestInFlight = true;
  }
  let fetchMs = 0;
  let renderStartMs = 0;
  try {
    const requestStartMs = performance.now();
    const res = await fetch(full ? '/metrics' : '/metrics_live', { cache: 'no-store' });
    const json = await res.json();
    fetchMs = performance.now() - requestStartMs;
    renderStartMs = performance.now();
    const solarEnabled = json.solar_enabled !== false && json.calculated_CA_values?.solar_enabled !== false;
    setSolarUiEnabled(solarEnabled);

    if (json.ca_reset_prompt && !solarEnabled) {
      showCaResetPopup();
    } else {
      hideCaResetPopup();
    }

    if (full) updatePhotoPreview(json.photo_capture);

    // Speed
    updateSpeedometer(
      num(json.raw_CA_values?.[3], 0),
      num(json.calculated_CA_values?.speed_avg, 0),
      num(json.calculated_CA_values?.speed_max, 0)
    );

    // Power + Amps. The sensor value is the pure motor current:
    // hub sensor - (solar sensor + human generator).
    const caAmps = num(json.calculated_CA_values?.ca_current_live ?? json.raw_CA_values?.[2], 0);
    const sensorValid = json.calculated_CA_values?.motor_sensor_valid === true;
    const sensorAmps = num(json.calculated_CA_values?.motor_sensor_current_live, 0);
    if (!sensorValid) {
      selectedPowerSource = 'ca';
      selectedVoltageSource = 'ca';
    }
    const amps = selectedPowerSource === 'sensor' && sensorValid ? sensorAmps : caAmps;
    const displayedPower = selectedPowerSource === 'sensor' && sensorValid
      ? num(json.calculated_CA_values?.motor_sensor_power_live, 0)
      : num(json.calculated_CA_values?.power_live, 0);
    updatePowerMeter(
      displayedPower,
      num(json.calculated_CA_values?.power_avg, 0),
      num(json.calculated_CA_values?.power_max, 0),
      amps
    );
    updateAmpArc(amps);
    const comparison = document.getElementById('amp-comparison');
    if (comparison) comparison.textContent = sensorValid
      ? `CA ${caAmps.toFixed(1)} A / sensor ${sensorAmps.toFixed(1)} A`
      : `CA ${caAmps.toFixed(1)} A / sensor --`;
    const powerToggle = document.getElementById('power-source-toggle');
    if (powerToggle) {
      powerToggle.disabled = !sensorValid;
      powerToggle.textContent = `Power: ${selectedPowerSource === 'sensor' && sensorValid ? 'sensor' : 'CA'}`;
    }
    const voltageToggle = document.getElementById('voltage-source-toggle');
    if (voltageToggle) {
      voltageToggle.disabled = !sensorValid;
      voltageToggle.textContent = `Voltage: ${selectedVoltageSource === 'sensor' && sensorValid ? 'sensor' : 'CA'}`;
    }

    if (full) {
      updateTempBar(
        num(json.raw_CA_values?.[5], 0),
        num(json.calculated_CA_values?.temp_avg, 0),
        num(json.calculated_CA_values?.temp_max, 0)
      );
    }

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
    const pvLive = solarEnabled ? Math.max(0, num(json.calculated_CA_values?.solar_power_live, 0)) : 0;
    updateAuxMeter(
      "pv",
      pvLive,
      solarEnabled ? num(json.calculated_CA_values?.solar_power_avg, 0) : 0,
      solarEnabled ? num(json.calculated_CA_values?.solar_power_max, 0) : 0,
      PV_METER_MAX_W
    );

    const pvPercent = powerLive > 0 ? (100 * pvLive / powerLive) : 0;
    document.getElementById("pv-percent").textContent = `${pvPercent.toFixed(1)}`;

    if (full) {
      // Battery / Ah
      const solarBattery = json.calculated_CA_values?.solar_battery ?? json.solar_battery ?? null;
      latestBatteryCurve = solarBattery;
      const ahConsumed = num(
        json.calculated_CA_values?.battery_ah_used_net ?? json.battery_ah_used_net,
        0
      );
      const capacityAh = Number(json.battery_capacity_ah ?? 64);
      const pctRemaining = solarBattery?.enabled
        ? Math.max(0, Math.min(1, num(solarBattery.percent, 0) / 100))
        : Math.max(0, Math.min(1, 1 - (capacityAh > 0 ? (ahConsumed / capacityAh) : 0)));
      document.getElementById('ah-bar').style.width = `${pctRemaining * 100}%`;
      document.getElementById('ah-bar-value').innerText = solarBattery?.enabled
        ? `${(pctRemaining * 100).toFixed(0)}%`
        : `${ahConsumed.toFixed(1)} Ah`;
      const grossAh = num(json.calculated_CA_values?.battery_ah_used_gross, ahConsumed);
      const recoveredAh = num(json.calculated_CA_values?.battery_ah_recovered, 0);
      if (solarBattery?.enabled) {
        const remainingWh = num(solarBattery.remaining_wh, 0);
        const potentialWh = num(solarBattery.potential_remaining_today_wh, 0);
        document.getElementById('ah-bar-detail').innerText = `${remainingWh.toFixed(0)} Wh left / solar today +${potentialWh.toFixed(0)} Wh`;
      } else {
        document.getElementById('ah-bar-detail').innerText = `gross ${grossAh.toFixed(1)} Ah / recovered ${recoveredAh.toFixed(1)} Ah`;
      }
      const resetFullButton = document.getElementById('battery-reset-full');
      if (resetFullButton) resetFullButton.hidden = !solarBattery?.can_reset_full;
      if (!document.getElementById('battery-curve-panel')?.hidden) renderBatteryCurve();

      const caVoltage = num(json.calculated_CA_values?.ca_voltage_live ?? json.raw_CA_values?.[1], 0);
      const sensorVoltage = num(json.calculated_CA_values?.motor_sensor_voltage_live, 0);
      const voltage = selectedVoltageSource === 'sensor' && sensorValid ? sensorVoltage : caVoltage;
      const voltageComparison = sensorValid ? ` (CA ${caVoltage.toFixed(1)} / sensor ${sensorVoltage.toFixed(1)})` : "";
      document.getElementById('ah-voltage-value').innerText = `${voltage.toFixed(1)} V${voltageComparison}`;

      // Human cumulative & kcal
      const solarWh = num(json.calculated_CA_values?.human_Wh ?? json.calculated_CA_values?.solar_Wh, 0);
      document.getElementById("solar-cumulative").textContent = solarWh.toFixed(1);
      const calories = num(json.calculated_CA_values?.human_calories_burned ?? json.calculated_CA_values?.calories_burned, 0);
      document.getElementById("solar-calories").textContent = calories.toFixed(0);

      const pvWh = solarEnabled ? num(json.calculated_CA_values?.solar_Wh, 0) : 0;
      document.getElementById("pv-cumulative").textContent = pvWh.toFixed(1);
      const pvCurrent = solarEnabled ? num(json.calculated_CA_values?.solar_current_live, 0) : 0;
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
      const liveWhPerKm = num(json.calculated_CA_values?.live_Wh_per_km, 0);
      const liveNetWhPerKm = num(json.calculated_CA_values?.live_net_Wh_per_km, 0);
      const liveWhEl = document.getElementById("trip-live-wh-per-km");
      const liveNetEl = document.getElementById("trip-live-net-wh-per-km");
      if (liveWhEl) liveWhEl.textContent = liveWhPerKm.toFixed(1);
      if (liveNetEl) liveNetEl.textContent = liveNetWhPerKm.toFixed(1);

      // Range block
      const autonomy = json.calculated_CA_values?.autonomy ?? {};
      const rangeLast = num(autonomy.range_last_km, 0);
      document.getElementById("range-last").textContent = rangeLast > 0 ? rangeLast.toFixed(1) : "–";
      const range10 = num(autonomy.range_10km_avg, 0);
      document.getElementById("range-10").textContent = range10 > 0 ? range10.toFixed(1) : "–";
      const rangeAvg = num(autonomy.range_session_avg, 0);
      document.getElementById("range-avg").textContent = rangeAvg > 0 ? rangeAvg.toFixed(1) : "–";

      const rangeSolarRow = document.getElementById("range-solar-row");
      const rangeSolarToday = document.getElementById("range-solar-today");
      if (rangeSolarRow && rangeSolarToday) {
        const solarTodayRange = num(autonomy.solar_today_session_avg, 0);
        rangeSolarRow.style.display = solarEnabled && solarTodayRange > 0 ? "flex" : "none";
        rangeSolarToday.textContent = solarTodayRange > 0 ? solarTodayRange.toFixed(1) : "-";
      }
      const solarPotentialRow = document.getElementById("solar-potential-row");
      const solarPotentialNow = document.getElementById("solar-potential-now");
      const solarPotentialToday = document.getElementById("solar-potential-today");
      if (solarPotentialRow && solarPotentialNow && solarPotentialToday) {
        const potentialNow = num(solarBattery?.potential_power_now_w, 0);
        const potentialToday = num(solarBattery?.potential_remaining_today_wh, 0);
        const showSolarPotential = solarEnabled && solarBattery?.enabled;
        latestSolarBattery = showSolarPotential ? solarBattery : null;
        solarPotentialRow.style.display = showSolarPotential ? "flex" : "none";
        solarPotentialNow.textContent = potentialNow.toFixed(0);
        solarPotentialToday.textContent = potentialToday.toFixed(0);
        if (!showSolarPotential && solarPotentialChartOpen) {
          setSolarPotentialChartOpen(false);
        } else if (showSolarPotential && solarPotentialChartOpen) {
          refreshSolarSessionProfile(false);
          renderSolarPotentialChart(solarBattery);
        }
      }

      // Charts (10 or 50)
      const range = isLongRange ? 50 : 10;
      updateWhPerKmChart(
        json.calculated_CA_values?.Wh_per_km_last ?? [],
        json.calculated_CA_values?.net_Wh_per_km_last ?? [],
        liveWhPerKm,
        liveNetWhPerKm,
        range
      );
      updatePctChart(
        "human-pct-per-km-chart",
        json.calculated_CA_values?.human_pct_per_km_last ?? [],
        solarPercent,
        range,
        "orange"
      );
      if (solarEnabled) {
        updatePctChart(
          "solar-pct-per-km-chart",
          json.calculated_CA_values?.solar_pct_per_km_last ?? [],
          pvPercent,
          range,
          "#e0b400"
        );
      }
      updateRegenPctChart(
        json.calculated_CA_values?.regen_pct_per_km_last ?? [],
        num(json.calculated_CA_values?.["%_regen"], 0),
        range
      );
    }

    updateNavigationMetricsFromPayload(json);

    // Flags, user, session
    const flags = json.raw_CA_values?.[14] ?? "";
    updateFlagsDisplay(flags);
    updateHeaderMode();

    document.getElementById("switch-user-button").textContent = "User: " + (json.user ?? "JD");
    document.getElementById("session-id").textContent = json.session_id ?? "-";

    if (!full) {
      const finishedMs = performance.now();
      const observedHz = dashboardDiag.lastLiveEndMs
        ? 1000 / Math.max(1, finishedMs - dashboardDiag.lastLiveEndMs)
        : 0;
      dashboardDiag.lastLiveEndMs = finishedMs;
      dashboardDiag.liveHz = diagEma(dashboardDiag.liveHz, observedHz);
      dashboardDiag.fetchMs = diagEma(dashboardDiag.fetchMs, fetchMs);
      dashboardDiag.renderMs = diagEma(dashboardDiag.renderMs, finishedMs - renderStartMs);
      dashboardDiag.serverMs = diagEma(dashboardDiag.serverMs, json.diag?.server_ms);
      dashboardDiag.reader = json.diag?.reader || null;
      updateDashboardDiagDisplay();
    }

  } catch (err) {
    console.error('Error fetching metrics:', err);
  } finally {
    if (full) {
      fullMetricsRequestInFlight = false;
    } else {
      metricsRequestInFlight = false;
    }
  }
}

function startMetricsLoop() {
  const tick = async () => {
    await fetchMetrics({ full: false });
    setTimeout(tick, document.hidden ? LIVE_METRICS_POLL_HIDDEN_MS : LIVE_METRICS_POLL_MS);
  };
  tick();
}

function startFullMetricsLoop() {
  const tick = async () => {
    await fetchMetrics({ full: true });
    setTimeout(tick, document.hidden ? FULL_METRICS_POLL_HIDDEN_MS : FULL_METRICS_POLL_MS);
  };
  tick();
}

function batteryCurveInterpolate(points, wh) {
  const ordered = points.slice().sort((a, b) => num(a.remaining_wh) - num(b.remaining_wh));
  if (!ordered.length) return null;
  if (wh <= num(ordered[0].remaining_wh)) return ordered[0];
  if (wh >= num(ordered[ordered.length - 1].remaining_wh)) return ordered[ordered.length - 1];
  for (let i = 1; i < ordered.length; i += 1) {
    if (wh <= num(ordered[i].remaining_wh)) {
      const a = ordered[i - 1], b = ordered[i];
      const ratio = (wh - num(a.remaining_wh)) / Math.max(.001, num(b.remaining_wh) - num(a.remaining_wh));
      return {
        remaining_wh: wh,
        soc: num(a.soc) + ratio * (num(b.soc) - num(a.soc)),
        voltage: num(a.voltage) + ratio * (num(b.voltage) - num(a.voltage)),
      };
    }
  }
  return ordered[0];
}

function renderBatteryCurve(selectedWh = null) {
  const battery = latestBatteryCurve;
  const curve = battery?.curve;
  const points = curve?.points || [];
  const chart = document.getElementById('battery-curve-chart');
  if (!chart || !points.length) {
    if (chart) chart.innerHTML = '<p>No valid battery curve loaded.</p>';
    return;
  }
  const width = 860, height = 320, pad = {left: 70, right: 25, top: 25, bottom: 48};
  const capacity = Math.max(1, num(curve.capacity_wh));
  const voltages = points.map(p => num(p.voltage));
  let minV = Math.min(...voltages), maxV = Math.max(...voltages);
  const vp = Math.max(.25, (maxV - minV) * .08); minV -= vp; maxV += vp;
  const x = wh => pad.left + (1 - num(wh) / capacity) * (width - pad.left - pad.right);
  const y = v => pad.top + (1 - (num(v) - minV) / Math.max(.01, maxV - minV)) * (height - pad.top - pad.bottom);
  const ordered = points.slice().sort((a,b) => num(a.remaining_wh) - num(b.remaining_wh));
  const path = ordered.map((p,i) => `${i ? 'L' : 'M'} ${x(p.remaining_wh).toFixed(1)} ${y(p.voltage).toFixed(1)}`).join(' ');
  const xGrid = [0,.25,.5,.75,1].map(t => `<line class="battery-curve-grid" x1="${x(capacity*t)}" y1="${pad.top}" x2="${x(capacity*t)}" y2="${height-pad.bottom}"/><text class="battery-curve-axis" x="${x(capacity*t)-18}" y="${height-20}">${Math.round(capacity*t)} Wh</text>`).join('');
  const yGrid = [0,.25,.5,.75,1].map(t => { const value=maxV-t*(maxV-minV), py=pad.top+t*(height-pad.top-pad.bottom); return `<line class="battery-curve-grid" x1="${pad.left}" y1="${py}" x2="${width-pad.right}" y2="${py}"/><text class="battery-curve-axis" x="8" y="${py+4}">${value.toFixed(1)} V</text>`; }).join('');
  const obs = curve.rest_observation;
  const currentDot = obs ? `<circle class="battery-curve-current ${curve.rest_observation_fresh ? 'fresh' : ''}" cx="${x(obs.remaining_wh)}" cy="${y(obs.voltage)}" r="8"><title>Last rested voltage observation</title></circle>` : '';
  const selected = selectedWh === null ? null : batteryCurveInterpolate(points, selectedWh);
  const selectedDot = selected ? `<circle class="battery-curve-selected" cx="${x(selected.remaining_wh)}" cy="${y(selected.voltage)}" r="7"/>` : '';
  const selectedLabel = selected ? (() => {
    const px = x(selected.remaining_wh), py = y(selected.voltage);
    const placeRight = px < width - pad.right - 175;
    const labelX = px + (placeRight ? 14 : -14);
    const labelY = Math.max(pad.top + 18, py - 13);
    return `<text class="battery-curve-point-label" x="${labelX}" y="${labelY}" text-anchor="${placeRight ? 'start' : 'end'}">${selected.voltage.toFixed(2)} V · ${selected.remaining_wh.toFixed(0)} Wh</text>`;
  })() : '';
  const remainingWh = Math.max(0, Math.min(capacity, num(battery?.remaining_wh, 0)));
  const remainingPoint = batteryCurveInterpolate(points, remainingWh);
  const fillPoints = remainingPoint
    ? [remainingPoint, ...ordered.filter(point => num(point.remaining_wh) < remainingWh).reverse()]
    : [];
  const fillPath = fillPoints.length
    ? `M ${x(fillPoints[0].remaining_wh).toFixed(1)} ${height-pad.bottom} ` + fillPoints.map(point => `L ${x(point.remaining_wh).toFixed(1)} ${y(point.voltage).toFixed(1)}`).join(' ') + ` L ${x(0).toFixed(1)} ${height-pad.bottom} Z`
    : '';
  const guidePoint = selected || (obs ? batteryCurveInterpolate(points, obs.remaining_wh) : remainingPoint);
  const guide = guidePoint ? `<line class="battery-curve-guide" x1="${x(guidePoint.remaining_wh)}" y1="${y(guidePoint.voltage)}" x2="${x(guidePoint.remaining_wh)}" y2="${height-pad.bottom}"/>` : '';
  chart.innerHTML = `<svg viewBox="0 0 ${width} ${height}" data-left="${pad.left}" data-right="${pad.right}" data-width="${width}">${xGrid}${yGrid}${fillPath ? `<path class="battery-curve-energy-fill" d="${fillPath}"/>` : ''}<path class="battery-curve-line" d="${path}"/>${guide}${currentDot}${selectedDot}${selectedLabel}</svg>`;
  const status = document.getElementById('battery-curve-status');
  status.textContent = obs
    ? `Last rested observation: ${num(obs.voltage).toFixed(2)} V · ${num(obs.soc_percent).toFixed(1)}% · ${num(obs.remaining_wh).toFixed(0)} Wh${curve.rest_observation_fresh ? ' (fresh)' : ` (stored, ${num(curve.wh_since_observation).toFixed(1)} Wh since)`}`
    : `Waiting for ${curve.rest_required_seconds || 120}s at ≤1 km/h and ≤1.5 A for a voltage-based observation.`;
  if (selected) document.getElementById('battery-curve-readout').textContent = `Selected: ${selected.voltage.toFixed(2)} V · ${selected.soc.toFixed(1)}% · ${selected.remaining_wh.toFixed(0)} Wh available`;
  chart.querySelector('svg').addEventListener('pointerdown', event => {
    const rect = event.currentTarget.getBoundingClientRect();
    const svgX = (event.clientX - rect.left) / rect.width * width;
    const wh = Math.max(0, Math.min(capacity, (1 - (svgX - pad.left) / (width - pad.left - pad.right)) * capacity));
    renderBatteryCurve(wh);
  });
}

function openBatteryCurve() {
  const panel = document.getElementById('battery-curve-panel');
  panel.hidden = false;
  renderBatteryCurve();
}

document.getElementById('battery-box')?.addEventListener('click', openBatteryCurve);
document.getElementById('battery-box')?.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') openBatteryCurve(); });
document.getElementById('battery-curve-close')?.addEventListener('click', event => { event.stopPropagation(); document.getElementById('battery-curve-panel').hidden = true; });
document.getElementById('battery-curve-panel')?.addEventListener('click', event => event.stopPropagation());
document.getElementById('battery-reset-full')?.addEventListener('click', async () => {
  const status = document.getElementById('battery-reset-status');
  const response = await fetch('/battery/reset_full', {method: 'POST'});
  const data = await response.json();
  status.textContent = response.ok ? 'Battery set to 100%.' : (data.error || 'Reset failed.');
  if (response.ok) await fetchMetrics({full: true});
});

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
  initNavigationModeUi();

  const powerSourceToggle = document.getElementById('power-source-toggle');
  if (powerSourceToggle) powerSourceToggle.addEventListener('click', () => {
    selectedPowerSource = selectedPowerSource === 'ca' ? 'sensor' : 'ca';
    localStorage.setItem('motorPowerSource', selectedPowerSource);
  });
  const voltageSourceToggle = document.getElementById('voltage-source-toggle');
  if (voltageSourceToggle) voltageSourceToggle.addEventListener('click', () => {
    selectedVoltageSource = selectedVoltageSource === 'ca' ? 'sensor' : 'ca';
    localStorage.setItem('motorVoltageSource', selectedVoltageSource);
  });

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

  const solarPotentialRow = document.getElementById("solar-potential-row");
  if (solarPotentialRow) {
    solarPotentialRow.addEventListener("click", () => {
      if (solarPotentialRow.style.display === "none") return;
      setSolarPotentialChartOpen(!solarPotentialChartOpen);
    });
    solarPotentialRow.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      if (solarPotentialRow.style.display === "none") return;
      setSolarPotentialChartOpen(!solarPotentialChartOpen);
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
        if (buttonId === "btn-solar" && !powerHistoryState.solarRoofEnabled) return;
        showChartById(chartId);
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
      if (key === "solar" && !powerHistoryState.solarRoofEnabled) return;
      powerHistoryState[key] = !powerHistoryState[key];
      setPowerSeriesButtonState();
      renderPowerHistoryChart();
    });
  });

  const cumulativeButton = document.getElementById("power-chart-cumulative");
  if (cumulativeButton) {
    cumulativeButton.addEventListener("click", () => {
      powerHistoryState.cumulative = !powerHistoryState.cumulative;
      updatePowerHistoryControls();
      renderPowerHistoryChart();
    });
  }

  const zoomInButton = document.getElementById("power-history-zoom-in");
  if (zoomInButton) {
    zoomInButton.addEventListener("click", () => {
      powerHistoryState.windowIndex = Math.max(0, powerHistoryState.windowIndex - 1);
      updatePowerHistoryControls();
      fetchPowerHistory();
    });
  }

  const zoomOutButton = document.getElementById("power-history-zoom-out");
  if (zoomOutButton) {
    zoomOutButton.addEventListener("click", () => {
      powerHistoryState.windowIndex = Math.min(POWER_HISTORY_WINDOWS.length - 1, powerHistoryState.windowIndex + 1);
      updatePowerHistoryControls();
      fetchPowerHistory();
    });
  }

  setPowerSeriesButtonState();
  updatePowerHistoryControls();
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
  startMetricsLoop();
  startFullMetricsLoop();
  startPowerHistoryLoop();
});
