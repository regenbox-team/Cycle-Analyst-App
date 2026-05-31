function formatUptime(seconds) {
  if (!Number.isFinite(seconds)) return '–';
  const d = Math.floor(seconds / 86400);
  seconds -= d * 86400;
  const h = Math.floor(seconds / 3600);
  seconds -= h * 3600;
  const m = Math.floor(seconds / 60);
  const parts = [];
  if (d) parts.push(`${d}d`);
  parts.push(`${h}h`);
  parts.push(`${m}m`);
  return parts.join(' ');
}

let sysMetricsRequestInFlight = false;

async function updateSysMetrics() {
  const elLoad = document.getElementById('sys-load');
  const elTemp = document.getElementById('sys-temp');
  const elCpu  = document.getElementById('sys-cpu');
  const elMem  = document.getElementById('sys-mem');
  const elUp   = document.getElementById('sys-uptime');
  const elThr  = document.getElementById('sys-throttled');
  if (!elLoad || !elTemp || !elCpu || !elMem || !elUp || !elThr) return;
  if (sysMetricsRequestInFlight) return;
  sysMetricsRequestInFlight = true;
  try {
    const res = await fetch('/sys_metrics', { cache: 'no-store' });
    const m = await res.json();
    const l1 = (m.load && m.load['1'] != null) ? Number(m.load['1']).toFixed(2) : '–';
    const l5 = (m.load && m.load['5'] != null) ? Number(m.load['5']).toFixed(2) : '–';
    const l15 = (m.load && m.load['15'] != null) ? Number(m.load['15']).toFixed(2) : '–';
    elLoad.textContent = `Load: ${l1} / ${l5} / ${l15}`;

    elTemp.textContent = `Temp: ${m.temp_c != null ? Math.round(Number(m.temp_c)) + '°C' : '–'}`;
    elCpu.textContent = `CPU: ${m.cpu_mhz != null ? Math.round(Number(m.cpu_mhz)) + ' MHz' : '–'}`;

    const total = m.mem && m.mem.total_mb != null ? Math.round(m.mem.total_mb) : null;
    const used  = m.mem && m.mem.used_mb  != null ? Math.round(m.mem.used_mb)  : null;
    const pct   = m.mem && m.mem.used_pct != null ? Math.round(m.mem.used_pct) : null;
    elMem.textContent = `Mem: ${used ?? '–'} / ${total ?? '–'} MB (${pct ?? '–'}%)`;

    elUp.textContent = `Uptime: ${formatUptime(m.uptime_s)}`;
    elThr.textContent = `Throttled: ${m.throttled ?? '–'}`;
  } catch (e) {
    // leave previous values
  } finally {
    sysMetricsRequestInFlight = false;
  }
}

window.addEventListener('DOMContentLoaded', () => {
  updateSysMetrics();
  setInterval(updateSysMetrics, 5000);
});
