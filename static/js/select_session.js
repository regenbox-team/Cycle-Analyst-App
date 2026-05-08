function currentMode() {
  const uploadButton = document.getElementById("upload-session-button");
  if (uploadButton?.dataset.mode) return uploadButton.dataset.mode;
  return new URLSearchParams(window.location.search).get("mode") || "";
}

async function fetchSessions() {
  try {
    const mode = currentMode();
    const url = mode ? `/sessions?mode=${encodeURIComponent(mode)}` : "/sessions";
    const res = await fetch(url);
    const sessions = await res.json();
    const select = document.getElementById("session-select");
    select.innerHTML = "";

    sessions.forEach(({ session, size_kb }) => {
      const opt = document.createElement("option");
      opt.value = session;
      opt.textContent = `${session} (${size_kb} KB)`;
      select.appendChild(opt);
    });

    // Adjust height based on number of sessions (min 2, max 20)
    select.size = Math.min(Math.max(select.options.length, 2), 20);
  } catch (err) {
    console.error("Failed to load sessions:", err);
  }
}

async function deleteSelectedSession() {
  const select = document.getElementById("session-select");
  const selected = Array.from(select.selectedOptions).map(opt => opt.value);

  if (selected.length === 0) return;
  if (!confirm(`Delete ${selected.length} session(s)?\n\n${selected.join("\n")}`)) return;

  try {
    const mode = currentMode();
    const results = await Promise.all(selected.map(session =>
      fetch("/delete_session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session, mode })
      }).then(res => res.json())
    ));
    alert(results.map(r => r.status || r.error).join("\n"));
    await fetchSessions(); // reload updated session list
  } catch (err) {
    console.error("Failed to delete session(s):", err);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  fetchSessions();
  document.getElementById("delete-session-button").addEventListener("click", deleteSelectedSession);
});
