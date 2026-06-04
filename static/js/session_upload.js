(function () {
  function uploadStatusElement() {
    return document.getElementById("session-upload-status");
  }

  function setStatus(message, tone) {
    const el = uploadStatusElement();
    if (!el) return;
    el.textContent = message;
    el.classList.remove("ok", "error", "working");
    if (tone) el.classList.add(tone);
  }

  function selectedSessions() {
    const select = document.getElementById("session-select");
    if (!select) return [];
    return Array.from(select.selectedOptions).map((option) => option.value);
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

  function describeUploadJob(job) {
    const result = job.result || {};
    const session = job.session || result.session || result.session_id || "session";
    if (job.complete) return describeResult(result.status ? result : job);
    if (job.phase === "checking") return `${session}: checking monitor...`;
    if (job.phase === "preparing") return `${session}: preparing data...`;
    if (job.phase === "uploading") {
      const total = Number(job.total_chunks);
      const index = Number(job.chunk_index);
      if (Number.isFinite(total) && total > 1 && Number.isFinite(index)) {
        return `${session}: uploading ${Math.min(index, total)}/${total}...`;
      }
      return `${session}: uploading...`;
    }
    return `${session}: queued...`;
  }

  function wait(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function uploadOne(session, mode) {
    const payload = { session };
    if (mode) payload.mode = mode;
    const response = await fetch("/api/upload_session_start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    let result;
    try {
      result = await response.json();
    } catch (err) {
      result = { status: "error", error: response.statusText || "invalid response", session };
    }
    if (!response.ok && !result.status) {
      result.status = "error";
    }
    if (!response.ok || !result.job_id) {
      if (!result.session && !result.session_id) result.session = session;
      return result;
    }

    let job = result;
    while (!job.complete) {
      setStatus(describeUploadJob(job), "working");
      await wait(1000);
      const statusResponse = await fetch(`/api/upload_session_status/${encodeURIComponent(job.job_id)}`, {
        cache: "no-store",
      });
      job = await statusResponse.json().catch(() => ({
        status: "error",
        error: statusResponse.statusText || "invalid response",
        session,
        complete: true,
      }));
      if (!statusResponse.ok) {
        if (!job.session && !job.session_id) job.session = session;
        return job;
      }
    }
    return job.result || job;
  }

  async function uploadSessions(sessions, mode, button) {
    if (!sessions.length) {
      setStatus("Select at least one session.", "error");
      return;
    }
    const originalText = button ? button.textContent : "";
    if (button) {
      button.disabled = true;
      button.textContent = "Uploading...";
    }
    setStatus(`Uploading ${sessions.length} session(s)...`, "working");
    const results = [];
    try {
      for (const session of sessions) {
        setStatus(`Uploading ${session}...`, "working");
        results.push(await uploadOne(session, mode));
      }
      const failed = results.some((result) => !["ok", "already_uploaded"].includes(result.status));
      setStatus(results.map(describeResult).join(" | "), failed ? "error" : "ok");
    } catch (err) {
      setStatus(err.message || "Upload failed.", "error");
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = originalText;
      }
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-upload-session]").forEach((button) => {
      button.addEventListener("click", () => {
        uploadSessions([button.dataset.uploadSession], button.dataset.mode || "", button);
      });
    });

    document.querySelectorAll("[data-upload-selected-sessions]").forEach((button) => {
      button.addEventListener("click", () => {
        uploadSessions(selectedSessions(), button.dataset.mode || "", button);
      });
    });
  });
})();
