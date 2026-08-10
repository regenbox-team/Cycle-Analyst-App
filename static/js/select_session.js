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

function formatSize(sizeKb) {
  const n = Number(sizeKb);
  if (!Number.isFinite(n) || n <= 0) return "--";
  if (n >= 1024) return `${formatNumber(n / 1024, 1)} Mo`;
  if (n < 1) return "< 1 Ko";
  return `${formatNumber(n, n >= 10 ? 0 : 1)} Ko`;
}

function parseSessionDate(sessionId) {
  const match = String(sessionId || "").match(/^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})$/);
  if (!match) return null;
  const [, year, month, day, hour, minute, second] = match;
  return new Date(`${year}-${month}-${day}T${hour}:${minute}:${second}`);
}

function formatSessionDate(session) {
  const value = session.start_ts || session.session;
  let date = value ? new Date(String(value).replace(" ", "T")) : null;
  if (!date || Number.isNaN(date.getTime())) date = parseSessionDate(session.session);
  if (!date || Number.isNaN(date.getTime())) return value || "--";
  return date.toLocaleString(undefined, {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
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
  const size = result.size_kb != null ? `, ${formatSize(result.size_kb)}` : "";
  if (result.status === "ok") return `${session}: envoye${size}`;
  if (result.status === "already_uploaded") return `${session}: deja envoye`;
  if (result.status === "missing_config") return `${session}: MONITOR_URL n'est pas configure`;
  if (result.status === "active_session") return `${session}: termine la session avant l'envoi`;
  if (result.status === "not_found") return `${session}: aucune donnee locale trouvee`;
  return `${session}: ${result.error || result.status || "echec de l'envoi"}`;
}

function describeUploadJob(job) {
  const result = job.result || {};
  const session = job.session || result.session || result.session_id || "session";
  if (job.complete) return describeResult(result.status ? result : job);
  if (job.phase === "checking") return `${session}: verification monitor...`;
  if (job.phase === "preparing") return `${session}: preparation des donnees...`;
  if (job.phase === "uploading") {
    const total = Number(job.total_chunks);
    const index = Number(job.chunk_index);
    if (Number.isFinite(total) && total > 1 && Number.isFinite(index)) {
      return `${session}: envoi ${Math.min(index, total)}/${total}...`;
    }
    return `${session}: envoi en cours...`;
  }
  return `${session}: en attente...`;
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function startUploadJob(session) {
  const payload = { session };
  const mode = currentMode();
  if (mode) payload.mode = mode;
  const response = await fetch("/api/upload_session_start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json().catch(() => ({ status: "error", error: response.statusText, session }));
  if (!response.ok || !result.job_id) {
    if (!result.session && !result.session_id) result.session = session;
    throw new Error(describeResult(result));
  }
  return result;
}

async function pollUploadJob(job, session) {
  let current = job;
  while (!current.complete) {
    setStatus(describeUploadJob(current), "working");
    await wait(1000);
    const response = await fetch(`/api/upload_session_status/${encodeURIComponent(current.job_id)}`, {
      cache: "no-store",
    });
    current = await response.json().catch(() => ({ status: "error", error: response.statusText, session, complete: true }));
    if (!response.ok) {
      if (!current.session && !current.session_id) current.session = session;
      return current;
    }
  }
  return current.result || current;
}

function sessionSummaryUrl(session) {
  const params = new URLSearchParams({ session });
  const mode = currentMode();
  if (mode) params.set("mode", mode);
  return `/summary?${params.toString()}`;
}

function sessionDownloadUrl(session) {
  const params = new URLSearchParams({ session });
  const mode = currentMode();
  if (mode) params.set("mode", mode);
  return `/api/download_session?${params.toString()}`;
}

function monitorBadge(uploaded) {
  if (uploaded === true) return '<span class="session-badge uploaded">Envoyee</span>';
  if (uploaded === false) return '<span class="session-badge pending">Locale</span>';
  return '<span class="session-badge unknown">?</span>';
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
    tbody.innerHTML = '<div class="session-empty">Aucune session locale.</div>';
    return;
  }

  sessions.forEach((session) => {
    const tr = document.createElement("article");
    tr.className = `session-row session-card-row ${rowClass(session)}`;
    tr.dataset.session = session.session || "";
    tr.setAttribute("role", "listitem");
    const sessionName = escapeHtml(session.session);
    const sessionDate = escapeHtml(formatSessionDate(session));
    const summaryUrl = escapeHtml(sessionSummaryUrl(session.session || ""));
    tr.innerHTML = `
      <div class="session-card-main">
        <div class="session-name">${sessionName}</div>
        <div class="session-sub">${sessionDate}</div>
      </div>
      <div class="session-card-stats" aria-label="Details session">
        <div class="session-stat">
          <span class="session-stat-label">Distance</span>
          <strong>${formatDistance(session.distance_km)}</strong>
        </div>
        <div class="session-stat">
          <span class="session-stat-label">Poids</span>
          <strong>${formatSize(session.size_kb)}</strong>
        </div>
        <div class="session-stat session-stat-monitor">
          <span class="session-stat-label">Monitor</span>
          ${monitorBadge(session.uploaded)}
        </div>
      </div>
      <div class="session-row-actions">
        <a class="session-action" href="${summaryUrl}">Voir</a>
        <button class="session-action primary upload-session-row" type="button">Envoyer</button>
        <a class="session-action download-session-row" href="${escapeHtml(sessionDownloadUrl(session.session || ""))}" download>Telecharger</a>
        <button class="session-action danger delete-session-row" type="button">Supprimer</button>
      </div>
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

async function refreshSessions({ clearStatus = true } = {}) {
  const button = document.getElementById("refresh-sessions-button");
  if (button) button.disabled = true;
  try {
    renderSessions(await fetchSessions());
    if (clearStatus) setStatus("", null);
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
    button.textContent = "Envoi...";
  }
  setStatus(`Envoi de ${session}...`, "working");
  try {
    const result = await pollUploadJob(await startUploadJob(session), session);
    if (!result.session && !result.session_id) result.session = session;
    setStatus(describeResult(result), ["ok", "already_uploaded"].includes(result.status) ? "ok" : "error");
    refreshSessions({ clearStatus: false }).catch((err) => {
      setStatus(err.message || "Failed to load sessions.", "error");
    });
  } catch (err) {
    setStatus(err.message || "Echec de l'envoi.", "error");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

async function deleteSession(session, button) {
  if (!session) return;
  if (!confirm(`Supprimer la session ${session} ?`)) return;
  const originalText = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "Suppression...";
  }
  setStatus(`Suppression de ${session}...`, "working");
  try {
    const mode = currentMode();
    const params = mode ? `?mode=${encodeURIComponent(mode)}` : "";
    const response = await fetch(`/delete_session${params}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session, mode }),
    });
    const result = await response.json().catch(() => ({ error: response.statusText }));
    if (!response.ok || result.error) throw new Error(result.error || "Delete failed.");
    button?.closest("[data-session]")?.remove();
    setStatus(result.status || `${session}: supprimee`, "ok");
    await refreshSessions({ clearStatus: false });
  } catch (err) {
    setStatus(err.message || "Echec de la suppression.", "error");
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
    const row = event.target.closest("[data-session]");
    if (!row) return;
    if (event.target.closest(".upload-session-row")) {
      uploadSession(row.dataset.session, event.target.closest("button"));
    }
    if (event.target.closest(".delete-session-row")) {
      deleteSession(row.dataset.session, event.target.closest("button"));
    }
  });
});
