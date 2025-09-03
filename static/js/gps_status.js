async function updateGpsStatus() {
  try {
    const res = await fetch('/gps_status', { cache: 'no-store' });
    const s = await res.json();
    const pill = document.getElementById('gps-status-pill');
    const pos = document.getElementById('gps-position');
    const extra = document.getElementById('gps-extra');

    if (!pill) return;

    if (s.stale) {
      pill.textContent = 'GPS: No Data';
      pill.classList.remove('active');
      pill.classList.add('inactive');
      if (pos) pos.textContent = '-';
      if (extra) extra.textContent = '';
      return;
    }

    const hasFix = !!s.has_fix || (s.fix_quality && s.fix_quality >= 1);
    pill.textContent = hasFix ? `GPS: Fix (${s.sats || 0} sats)` : 'GPS: Searching...';
    pill.classList.toggle('active', hasFix);
    pill.classList.toggle('inactive', !hasFix);

    if (pos) {
      if (s.lat != null && s.lon != null) {
        pos.textContent = `${s.lat.toFixed(6)}, ${s.lon.toFixed(6)}`;
      } else {
        pos.textContent = '-';
      }
    }

    const parts = [];
    if (extra) {
      if (s.alt != null) parts.push(`Alt ${Number(s.alt).toFixed(1)} m`);
      if (s.speed_kph != null) parts.push(`${Number(s.speed_kph).toFixed(1)} km/h`);
      if (s.hdop != null) parts.push(`HDOP ${Number(s.hdop).toFixed(1)}`);
      extra.textContent = parts.join(' • ');
    }
  } catch (e) {
    // swallow errors
  }
}

window.addEventListener('DOMContentLoaded', () => {
  updateGpsStatus();
  setInterval(updateGpsStatus, 1000);
});
