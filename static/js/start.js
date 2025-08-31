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
  }
}

async function fetchVehicleMode() {
  try {
    const res = await fetch("/get_vehicle_mode");
    const data = await res.json();
    const sel = document.getElementById("mode-select");
    sel.value = data.mode;
    updateLinksForMode(data.mode);
    updateResumeOptions(data.mode);
  } catch (err) {
    console.error("Failed to fetch vehicle mode", err);
  }
}

document.getElementById("mode-select").addEventListener("change", async (e) => {
  const mode = e.target.value;
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
});

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
