(function () {
  function numberInput(id) {
    return document.getElementById(id);
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function currentSolarPoint() {
    const latInput = numberInput('APP_SOLAR_LAT');
    const lonInput = numberInput('APP_SOLAR_LON');
    const lat = latInput ? parseFloat(latInput.value) : NaN;
    const lon = lonInput ? parseFloat(lonInput.value) : NaN;
    return {
      lat: Number.isFinite(lat) ? clamp(lat, -85, 85) : 48.8566,
      lon: Number.isFinite(lon) ? clamp(lon, -180, 180) : 2.3522,
    };
  }

  function setSolarPoint(lat, lon) {
    const latInput = numberInput('APP_SOLAR_LAT');
    const lonInput = numberInput('APP_SOLAR_LON');
    const cleanLat = clamp(Number(lat), -85, 85);
    const cleanLon = clamp(Number(lon), -180, 180);
    if (latInput) latInput.value = cleanLat.toFixed(6);
    if (lonInput) lonInput.value = cleanLon.toFixed(6);
    drawSolarMap();
  }

  function lonLatToCanvas(lon, lat, width, height) {
    return {
      x: ((lon + 180) / 360) * width,
      y: ((85 - lat) / 170) * height,
    };
  }

  function canvasToLonLat(x, y, width, height) {
    return {
      lon: (x / width) * 360 - 180,
      lat: 85 - (y / height) * 170,
    };
  }

  function drawBlob(ctx, points, width, height) {
    ctx.beginPath();
    points.forEach(([lon, lat], index) => {
      const p = lonLatToCanvas(lon, lat, width, height);
      if (index === 0) ctx.moveTo(p.x, p.y);
      else ctx.lineTo(p.x, p.y);
    });
    ctx.closePath();
    ctx.fill();
  }

  function drawSolarMap() {
    const canvas = document.getElementById('solar-position-map');
    const readout = document.getElementById('solar-position-readout');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const width = canvas.width;
    const height = canvas.height;
    const point = currentSolarPoint();

    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = '#b8d7dd';
    ctx.fillRect(0, 0, width, height);

    ctx.fillStyle = '#d9d0ad';
    drawBlob(ctx, [[-170, 70], [-55, 72], [-35, 15], [-82, 7], [-118, 15], [-150, 55]], width, height);
    drawBlob(ctx, [[-82, 12], [-35, 8], [-45, -55], [-75, -55], [-82, -10]], width, height);
    drawBlob(ctx, [[-10, 72], [45, 70], [105, 55], [145, 15], [110, -10], [40, -35], [-15, 35]], width, height);
    drawBlob(ctx, [[-18, 35], [50, 32], [42, -35], [15, -35], [-10, 5]], width, height);
    drawBlob(ctx, [[112, -10], [155, -12], [154, -44], [115, -42]], width, height);

    ctx.strokeStyle = 'rgba(0, 0, 0, 0.18)';
    ctx.lineWidth = 1;
    for (let lon = -180; lon <= 180; lon += 30) {
      const p1 = lonLatToCanvas(lon, -85, width, height);
      const p2 = lonLatToCanvas(lon, 85, width, height);
      ctx.beginPath();
      ctx.moveTo(p1.x, p1.y);
      ctx.lineTo(p2.x, p2.y);
      ctx.stroke();
    }
    for (let lat = -60; lat <= 60; lat += 30) {
      const p1 = lonLatToCanvas(-180, lat, width, height);
      const p2 = lonLatToCanvas(180, lat, width, height);
      ctx.beginPath();
      ctx.moveTo(p1.x, p1.y);
      ctx.lineTo(p2.x, p2.y);
      ctx.stroke();
    }

    const marker = lonLatToCanvas(point.lon, point.lat, width, height);
    ctx.fillStyle = 'orange';
    ctx.strokeStyle = '#111';
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.arc(marker.x, marker.y, 8, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();

    if (readout) {
      readout.textContent = `${point.lat.toFixed(6)}, ${point.lon.toFixed(6)}`;
    }
  }

  function wireSolarMap() {
    const canvas = document.getElementById('solar-position-map');
    if (!canvas) return;
    canvas.addEventListener('click', (event) => {
      const rect = canvas.getBoundingClientRect();
      const scaleX = canvas.width / rect.width;
      const scaleY = canvas.height / rect.height;
      const pos = canvasToLonLat(
        (event.clientX - rect.left) * scaleX,
        (event.clientY - rect.top) * scaleY,
        canvas.width,
        canvas.height
      );
      setSolarPoint(pos.lat, pos.lon);
    });

    ['APP_SOLAR_LAT', 'APP_SOLAR_LON'].forEach((id) => {
      const input = numberInput(id);
      if (input) input.addEventListener('input', drawSolarMap);
    });

    const gpsButton = document.getElementById('solar-use-gps');
    if (gpsButton) {
      gpsButton.addEventListener('click', async () => {
        try {
          const response = await fetch('/gps_status', { cache: 'no-store' });
          const gps = await response.json();
          if (gps && gps.has_fix && !gps.stale && gps.lat != null && gps.lon != null) {
            setSolarPoint(gps.lat, gps.lon);
          }
        } catch (error) {
          // Keep the current point if GPS is unavailable.
        }
      });
    }

    drawSolarMap();
  }

  function formatLog(data) {
    const lines = Array.isArray(data && data.lines) ? data.lines : [];
    if (!lines.length) return JSON.stringify(data, null, 2);
    return lines.join('\n');
  }

  async function runDiagnostic(name, button) {
    const log = document.getElementById(`diagnostic-${name}`);
    if (!log) return;
    const method = name === 'solar_sensor' || name === 'cycle_analyst' ? 'GET' : 'POST';
    log.textContent = 'Running...';
    log.classList.remove('ok', 'error', 'warning');
    if (button) button.disabled = true;
    try {
      const response = await fetch(`/settings/diagnostics/${name}`, { method, cache: 'no-store' });
      const data = await response.json();
      log.textContent = formatLog(data);
      log.classList.add(data.status || (response.ok ? 'ok' : 'error'));
      if (name === 'camera' && data.image_url) {
        const image = document.getElementById('diagnostic-camera-image');
        if (image) {
          image.src = data.image_url;
          image.hidden = false;
        }
      }
    } catch (error) {
      log.textContent = `Diagnostic failed: ${error}`;
      log.classList.add('error');
    } finally {
      if (button) button.disabled = false;
    }
  }

  function wireDiagnostics() {
    document.querySelectorAll('[data-diagnostic]').forEach((button) => {
      button.addEventListener('click', () => runDiagnostic(button.dataset.diagnostic, button));
    });
  }

  function formatSolarProfileStatus(profile) {
    if (!profile || !profile.enabled) {
      const path = profile && profile.path ? `\nPath: ${profile.path}` : '';
      return `No imported profile. Automatic solar estimation is active.${path}`;
    }
    const lines = [
      'Imported solar profile active.',
      `Name: ${profile.name || 'Imported solar profile'}`,
      `Points: ${profile.point_count || 0}`,
      `Peak: ${Math.round(Number(profile.peak_w || 0))} W`,
    ];
    if (profile.panel_max_w) lines.push(`Panels: ${Math.round(Number(profile.panel_max_w))} Wc`);
    if (profile.created_at) lines.push(`Created: ${profile.created_at}`);
    if (profile.path) lines.push(`Path: ${profile.path}`);
    return lines.join('\n');
  }

  async function refreshSolarProfileStatus() {
    const status = document.getElementById('solar-profile-status');
    if (!status) return;
    status.textContent = 'Loading profile status...';
    status.classList.remove('ok', 'error', 'warning');
    try {
      const response = await fetch('/settings/solar_profile', { cache: 'no-store' });
      const data = await response.json();
      status.textContent = formatSolarProfileStatus(data);
      status.classList.add(data.enabled ? 'ok' : 'warning');
    } catch (error) {
      status.textContent = `Profile status failed: ${error}`;
      status.classList.add('error');
    }
  }

  function wireSolarProfileImport() {
    const input = document.getElementById('solar-profile-import-file');
    const importButton = document.getElementById('solar-profile-import-button');
    const deleteButton = document.getElementById('solar-profile-delete-button');
    const status = document.getElementById('solar-profile-status');

    if (importButton && input) {
      importButton.addEventListener('click', async () => {
        const file = input.files && input.files[0];
        if (!file) {
          if (status) {
            status.textContent = 'Select a JSON profile file first.';
            status.classList.remove('ok', 'error');
            status.classList.add('warning');
          }
          return;
        }
        const formData = new FormData();
        formData.append('profile', file);
        importButton.disabled = true;
        if (status) {
          status.textContent = 'Importing profile...';
          status.classList.remove('ok', 'error', 'warning');
        }
        try {
          const response = await fetch('/settings/solar_profile/import', { method: 'POST', body: formData });
          const data = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(data.error || 'Profile import failed');
          input.value = '';
          if (status) {
            status.textContent = formatSolarProfileStatus(data.profile);
            status.classList.add('ok');
          }
        } catch (error) {
          if (status) {
            status.textContent = `Profile import failed: ${error.message || error}`;
            status.classList.add('error');
          }
        } finally {
          importButton.disabled = false;
        }
      });
    }

    if (deleteButton) {
      deleteButton.addEventListener('click', async () => {
        deleteButton.disabled = true;
        if (status) {
          status.textContent = 'Deleting imported profile...';
          status.classList.remove('ok', 'error', 'warning');
        }
        try {
          const response = await fetch('/settings/solar_profile', { method: 'DELETE', cache: 'no-store' });
          const data = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(data.error || 'Profile deletion failed');
          if (status) {
            status.textContent = formatSolarProfileStatus(data.profile);
            status.classList.add('warning');
          }
        } catch (error) {
          if (status) {
            status.textContent = `Profile deletion failed: ${error.message || error}`;
            status.classList.add('error');
          }
        } finally {
          deleteButton.disabled = false;
        }
      });
    }
  }

  window.addEventListener('DOMContentLoaded', () => {
    wireSolarMap();
    wireDiagnostics();
    wireSolarProfileImport();
    refreshSolarProfileStatus();
  });
})();
