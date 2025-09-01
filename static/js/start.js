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

// ===== Vehicle carousel wiring =====
const VEHICLE_ORDER = [
  "supercycle_live",
  "supercycle_test",
  "acticycle_live",
  "acticycle_test",
];

const carouselState = {
  items: [],
  track: null,
  currentIndex: 0,
  dragging: false,
  startX: 0,
};

function wrapIndex(n, len) {
  return (n % len + len) % len;
}

function applyCarouselClasses() {
  const n = carouselState.items.length;
  if (!n) return;
  const current = wrapIndex(carouselState.currentIndex, n);
  carouselState.items.forEach((el, i) => {
    el.classList.remove("center", "near-left", "near-right", "far");
    const diff = i - current;
    if (diff === 0) {
      el.classList.add("center");
    } else if (diff === -1 || diff === n - 1) {
      el.classList.add("near-left");
    } else if (diff === 1 || diff === -(n - 1)) {
      el.classList.add("near-right");
    } else {
      el.classList.add("far");
    }
  });
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

function selectCarouselIndex(idx, triggerUpdate = true) {
  const n = carouselState.items.length;
  if (!n) return;
  carouselState.currentIndex = wrapIndex(idx, n);
  applyCarouselClasses();
  if (triggerUpdate) {
    const el = carouselState.items[carouselState.currentIndex];
    const mode = el?.dataset?.mode;
    if (mode) setVehicleMode(mode);
  }
}

function initCarousel() {
  const track = document.querySelector("#vehicle-carousel .carousel-track");
  if (!track) return;
  carouselState.track = track;
  carouselState.items = Array.from(track.querySelectorAll('.carousel-item'));

  // Click to focus center
  carouselState.items.forEach((el, i) => {
    el.addEventListener('click', () => selectCarouselIndex(i, true));
  });

  // Nav buttons
  const prevBtn = document.querySelector('#vehicle-carousel .carousel-nav.prev');
  const nextBtn = document.querySelector('#vehicle-carousel .carousel-nav.next');
  if (prevBtn) prevBtn.addEventListener('click', () => selectCarouselIndex(carouselState.currentIndex - 1, true));
  if (nextBtn) nextBtn.addEventListener('click', () => selectCarouselIndex(carouselState.currentIndex + 1, true));

  // Basic swipe support (pointer events)
  track.addEventListener('pointerdown', (e) => {
    carouselState.dragging = true;
    carouselState.startX = e.clientX;
    if (carouselState.track) carouselState.track.classList.add('dragging');
  });
  window.addEventListener('pointermove', (e) => {
    if (!carouselState.dragging || !carouselState.track) return;
    const dx = e.clientX - carouselState.startX;
    // Apply a damped translation for a subtle follow effect
    const eased = Math.max(Math.min(dx, 160), -160);
    carouselState.track.style.transform = `translateX(${eased}px)`;
  });
  window.addEventListener('pointerup', (e) => {
    if (!carouselState.dragging) return;
    const dx = e.clientX - carouselState.startX;
    carouselState.dragging = false;
    if (carouselState.track) carouselState.track.classList.remove('dragging');
    const threshold = 40;
    if (dx > threshold) {
      selectCarouselIndex(carouselState.currentIndex - 1, true);
    } else if (dx < -threshold) {
      selectCarouselIndex(carouselState.currentIndex + 1, true);
    }
    // Animate back to center
    if (carouselState.track) carouselState.track.style.transform = 'translateX(0px)';
  });

  applyCarouselClasses();
}

async function fetchVehicleMode() {
  try {
    const res = await fetch("/get_vehicle_mode");
    const data = await res.json();
    const mode = data.mode;
    updateLinksForMode(mode);
    updateResumeOptions(mode);

    // Position carousel to current mode
    const idx = carouselState.items.findIndex(el => el.dataset.mode === mode);
    if (idx >= 0) {
      selectCarouselIndex(idx, false);
    } else {
      // fallback to first
      selectCarouselIndex(0, false);
    }
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
  initCarousel();
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
