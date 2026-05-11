function currentMode() {
  return new URLSearchParams(window.location.search).get("mode") || "";
}

function statusElement() {
  return document.getElementById("session-upload-status");
}

function setStatus(message, tone) {
  const el = statusElement();
  if (!el) return;
  el.textContent = message || "";
  el.classList.remove("ok", "error", "working");
  if (tone) el.classList.add(tone);
}

function formatNumber(value, decimals = 1) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "--";
  return n.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function formatDistance(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "--";
  return `${formatNumber(n, n >= 10 ? 1 : 2)} km`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function describeResult(result) {
  const session = result.session || result.session_id || "session";
  const rows = result.rows_count != null ? `, ${result.rows_count} rows` : "";
  const size = result.size_kb != null ? `, ${result.size_kb} KB` : "";
  if (result.status === "ok") return `${session}: uploaded${rows}${size}`;
  if (result.status === "already_uploaded") return `${session}: already uploaded`;
  if (result.status === "missing_config") return `${session}: MONITOR_URL is not configured`;
  if (result.status === "active_session") return `${session}: end the session before upload`;
  if (result.status === "not_found") return `${session}: no local rows found`;
  return `${session}: ${result.error || result.status || "upload failed"}`;
}

function sessionSummaryUrl(session) {
  const params = new URLSearchParams({ session });
  const mode = currentMode();
  if (mode) params.set("mode", mode);
  return `/summary?${params.toString()}`;
}

function monitorBadge(uploaded) {
  if (uploaded === true) return '<span class="session-badge uploaded">Uploaded</span>';
  if (uploaded === false) return '<span class="session-badge pending">Local only</span>';
  return '<span class="session-badge unknown">Unknown</span>';
}

function rowClass(session) {
  if (session.uploaded === true) return "uploaded";
  if (session.uploaded === false) return "pending";
  return "unknown";
}

function renderSessions(sessions) {
  const tbody = document.getElementById("sessions-body");
  if (!tbody) return;
  tbody.innerHTML = "";
  if (!sessions.length) {
    tbody.innerHTML = '<tr><td colspan="6">No local session found.</td></tr>';
    return;
  }

  sessions.forEach((session) => {
    const tr = document.createElement("tr");
    tr.className = `session-row ${rowClass(session)}`;
    tr.dataset.session = session.session || "";
    const sessionName = escapeHtml(session.session);
    const startTs = escapeHtml(session.start_ts);
    const summaryUrl = escapeHtml(sessionSummaryUrl(session.session || ""));
    tr.innerHTML = `
      <td>
        <div class="session-name">${sessionName}</div>
        <div class="session-sub">${startTs}</div>
      </td>
      <td>${formatDistance(session.distance_km)}</td>
      <td>${session.rows_count || 0}</td>
      <td>${formatNumber(session.size_kb, 1)} KB</td>
      <td>${monitorBadge(session.uploaded)}</td>
      <td>
        <div class="session-row-actions">
          <a class="session-action" href="${summaryUrl}">Summary</a>
          <button class="session-action primary upload-session-row" type="button">Upload</button>
          <button class="session-action danger delete-session-row" type="button">Delete</button>
        </div>
      </td>
    `;
    tbody.appendChild(tr);
  });
}

async function fetchSessions() {
  const mode = currentMode();
  const url = mode ? `/sessions?mode=${encodeURIComponent(mode)}` : "/sessions";
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to load sessions.");
  return res.json();
}

async function refreshSessions() {
  const button = document.getElementById("refresh-sessions-button");
  if (button) button.disabled = true;
  try {
    renderSessions(await fetchSessions());
    setStatus("", null);
  } catch (err) {
    setStatus(err.message || "Failed to load sessions.", "error");
  } finally {
    if (button) button.disabled = false;
  }
}

async function uploadSession(session, button) {
  if (!session) return;
  const originalText = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "Uploading...";
  }
  setStatus(`Uploading ${session}...`, "working");
  try {
    const payload = { session };
    const mode = currentMode();
    if (mode) payload.mode = mode;
    const response = await fetch("/api/upload_session_now", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json().catch(() => ({ status: "error", error: response.statusText, session }));
    setStatus(describeResult(result), ["ok", "already_uploaded"].includes(result.status) ? "ok" : "error");
    await refreshSessions();
  } catch (err) {
    setStatus(err.message || "Upload failed.", "error");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

async function deleteSession(session, button) {
  if (!session) return;
  if (!confirm(`Delete session ${session}?`)) return;
  const originalText = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "Deleting...";
  }
  try {
    const mode = currentMode();
    const response = await fetch("/delete_session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session, mode }),
    });
    const result = await response.json().catch(() => ({ error: response.statusText }));
    if (!response.ok || result.error) throw new Error(result.error || "Delete failed.");
    setStatus(result.status || `${session}: deleted`, "ok");
    await refreshSessions();
  } catch (err) {
    setStatus(err.message || "Delete failed.", "error");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

document.addEventListener("DOMContentLoaded", () => {
  refreshSessions();

  document.getElementById("refresh-sessions-button")?.addEventListener("click", refreshSessions);
  document.getElementById("sessions-body")?.addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-session]");
    if (!row) return;
    if (event.target.closest(".upload-session-row")) {
      uploadSession(row.dataset.session, event.target.closest("button"));
    }
    if (event.target.closest(".delete-session-row")) {
      deleteSession(row.dataset.session, event.target.closest("button"));
    }
  });
});
