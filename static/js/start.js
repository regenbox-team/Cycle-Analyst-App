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

async function fetchTestMode() {
  try {
    const res = await fetch("/get_test_mode");
    const data = await res.json();
    const toggle = document.getElementById("test-mode-toggle");
    toggle.checked = data.test_mode;
    document.getElementById("test-mode-status").textContent = data.test_mode ? "ON" : "OFF";
  } catch (err) {
    console.error("Failed to fetch test mode", err);
  }
}

document.getElementById("test-mode-toggle").addEventListener("change", async (e) => {
  const enabled = e.target.checked;
  try {
    const res = await fetch("/set_test_mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled })
    });
    const data = await res.json();
    document.getElementById("test-mode-status").textContent = data.test_mode ? "ON" : "OFF";
  } catch (err) {
    console.error("Failed to set test mode", err);
    alert("Error updating test mode.");
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
  fetchTestMode();
  checkConnection();
  setInterval(checkConnection, 1000);
});
