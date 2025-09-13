// ===== Connection status + image switching =====
async function checkConnection() {
  try {
    const res = await fetch("/metrics", { cache: "no-store" });
    const data = await res.json();
    const hasData = Array.isArray(data.raw_CA_values) && data.raw_CA_values.length === 15;

    const statusBtn = document.getElementById("connection-status");
    const startBtn = document.getElementById("start-btn");

    if (hasData) {
      statusBtn.textContent = "Connection: Active";
      statusBtn.classList.remove("inactive");
      statusBtn.classList.add("active");
      if (startBtn) {
        startBtn.disabled = false;
        startBtn.style.opacity = 1;
      }
    } else {
      throw new Error("No valid data");
    }

    updateVehicleImages(true);
  } catch {
    const statusBtn = document.getElementById("connection-status");
    const startBtn = document.getElementById("start-btn");
    statusBtn.textContent = "Connection: Inactive";
    statusBtn.classList.remove("active");
    statusBtn.classList.add("inactive");
    if (startBtn) {
      startBtn.disabled = true;
      startBtn.style.opacity = 0.5;
    }
    updateVehicleImages(false);
  }
}

// ===== Two-vehicle selection (no swipe) =====
let selectedVehicle = "supercycle"; // 'supercycle' | 'acticycle'
let isTest = false;

function deriveMode(vehicle, test) {
  const suffix = test ? "_test" : "_live";
  return `${vehicle}${suffix}`;
}

function applySelectionUI() {
  const elSuper = document.getElementById("vehicle-supercycle");
  const elActi = document.getElementById("vehicle-acticycle");
  if (!elSuper || !elActi) return;
  const superSelected = selectedVehicle === "supercycle";

  elSuper.classList.toggle("selected", superSelected);
  elSuper.classList.toggle("unselected", !superSelected);
  elActi.classList.toggle("selected", !superSelected);
  elActi.classList.toggle("unselected", superSelected);
}

function updateVehicleImages(connectionActive) {
  const imgSuper = document.getElementById("img-supercycle");
  const imgActi = document.getElementById("img-acticycle");
  if (imgSuper) {
    const src = connectionActive ? imgSuper.dataset.activeSrc : imgSuper.dataset.inactiveSrc;
    if (src && imgSuper.src !== src) imgSuper.src = src;
  }
  if (imgActi) {
    const src = connectionActive ? imgActi.dataset.activeSrc : imgActi.dataset.inactiveSrc;
    if (src && imgActi.src !== src) imgActi.src = src;
  }
}

async function setVehicleMode(mode) {
  try {
    await fetch("/set_vehicle_mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode })
    });
    updateLinksForMode(mode);
    await updateResumeOptions(mode);
    checkConnection();
  } catch (err) {
    console.error("Failed to set vehicle mode", err);
  }
}

async function fetchVehicleMode() {
  try {
    const res = await fetch("/get_vehicle_mode");
    const data = await res.json();
    const mode = data.mode;
    if (typeof mode === 'string') {
      isTest = mode.endsWith('_test');
      if (mode.startsWith('supercycle')) selectedVehicle = 'supercycle';
      else if (mode.startsWith('acticycle')) selectedVehicle = 'acticycle';
    }
    applySelectionUI();
    updateLinksForMode(mode);
    updateResumeOptions(mode);
    const testToggle = document.getElementById('test-mode-toggle');
    if (testToggle) testToggle.checked = isTest;
  } catch (err) {
    console.error("Failed to fetch vehicle mode", err);
  }
}

function openResumeModal() {
  document.getElementById("resume-modal").style.display = "block";
}
function closeResumeModal() {
  document.getElementById("resume-modal").style.display = "none";
}
window.onclick = function(event) {
  const modal = document.getElementById("resume-modal");
  if (event.target === modal) {
    modal.style.display = "none";
  }
};

window.addEventListener("DOMContentLoaded", () => {
  const superEl = document.getElementById('vehicle-supercycle');
  const actiEl = document.getElementById('vehicle-acticycle');
  const testToggle = document.getElementById('test-mode-toggle');

  if (superEl) superEl.addEventListener('click', () => {
    selectedVehicle = 'supercycle';
    applySelectionUI();
    setVehicleMode(deriveMode(selectedVehicle, isTest));
  });
  if (actiEl) actiEl.addEventListener('click', () => {
    selectedVehicle = 'acticycle';
    applySelectionUI();
    setVehicleMode(deriveMode(selectedVehicle, isTest));
  });
  if (testToggle) testToggle.addEventListener('change', () => {
    isTest = !!testToggle.checked;
    setVehicleMode(deriveMode(selectedVehicle, isTest));
  });

  fetchVehicleMode();
  checkConnection();
  setInterval(checkConnection, 1000);
});

function updateLinksForMode(mode) {
  const qs = `?mode=${encodeURIComponent(mode)}`;
  const sel = document.getElementById("link-select-session");
  const edit = document.getElementById("link-edit-session");
  const logs = document.getElementById("link-live-logs");
  if (sel) sel.href = "/select_session" + qs;
  if (edit) edit.href = "/edit_session" + qs;
  if (logs) logs.href = "/live_logs"; // logs page may not need mode
}

async function updateResumeOptions(mode) {
  try {
    const res = await fetch(`/sessions?mode=${encodeURIComponent(mode)}`, { cache: "no-store" });
    const sessions = await res.json();
    const sel = document.getElementById("resume-session-select");
    if (!sel) return;
    sel.innerHTML = "";
    (sessions || []).forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.session || s; // supports both object or plain string
      opt.textContent = s.session || s;
      sel.appendChild(opt);
    });
  } catch (e) {
    console.error('Failed fetching sessions for resume', e);
  }
}
