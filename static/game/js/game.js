(() => {
  const root = document.getElementById('game-root');
  const canvas = document.getElementById('game-canvas');
  const hudUser = document.getElementById('hud-user');
  const hudDistance = document.getElementById('hud-distance');
  const hudPower = document.getElementById('hud-power');
  const warningEl = document.getElementById('warning');
  const gameOverEl = document.getElementById('game-over');
  const finalDistanceEl = document.getElementById('final-distance');

  const user = (root?.dataset?.username || '').trim() || 'Anonymous';
  hudUser.textContent = user;

  // Sizing
  const ctx = canvas.getContext('2d');
  const DPR = Math.max(1, Math.floor(window.devicePixelRatio || 1));

  function resize() {
    const w = Math.max(320, root.clientWidth);
    const h = Math.max(240, root.clientHeight);
    canvas.width = w * DPR;
    canvas.height = h * DPR;
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }
  window.addEventListener('resize', resize);
  resize();

  // Game state
  let running = true;
  let warningStart = null; // ms timestamp
  let distance = 0; // meters
  let lastTick = performance.now();
  let power = 0; // W
  let flapPhase = 0; // 0..1

  // Zones
  function zones(h) {
    return { top: h / 3, bottom: (2 * h) / 3 };
  }

  // Piecewise mapping: 0W->bottom, 50W->center, 200W->top
  function powerToY(p, h) {
    const P = Math.max(0, Math.min(200, p));
    if (P <= 50) {
      // Map [0..50] -> [h..h/2]
      const t = P / 50; // 0..1
      return h - (h / 2) * t;
    } else {
      // Map [50..200] -> [h/2..0]
      const t = (P - 50) / 150; // 0..1
      return (h / 2) * (1 - t);
    }
  }

  // Draw helpers (pixel style via rectangles)
  function drawSky(w, h) {
    ctx.fillStyle = '#99ccff';
    ctx.fillRect(0, 0, w, h);
  }

  function drawCloudBands(w, h) {
    const z = zones(h);
    // Top cloud band
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, w, Math.floor(z.top));
    ctx.fillStyle = '#d0e6ff';
    for (let x = 0; x < w; x += 24) {
      const y = Math.floor(z.top) - 8 - ((x / 24) % 2) * 4;
      ctx.fillRect(x, y, 20, 8);
    }
    // Bottom cloud band
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, Math.floor(z.bottom), w, h - Math.floor(z.bottom));
    ctx.fillStyle = '#d0e6ff';
    for (let x = 0; x < w; x += 24) {
      const y = Math.floor(z.bottom) + ((x / 24) % 2) * 4;
      ctx.fillRect(x, y, 20, 8);
    }
  }

  function drawMidline(w, h) {
    // Center guide (50W)
    ctx.strokeStyle = '#ffffff88';
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 6]);
    ctx.beginPath();
    ctx.moveTo(0, h / 2);
    ctx.lineTo(w, h / 2);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  function drawBird(cx, cy) {
    // Simple pixel bird (profile), flap by wing offset
    const px = 2; // pixel size scale
    const ox = Math.round(cx - 8 * px);
    const oy = Math.round(cy - 6 * px);

    function p(x, y, w, h, color) {
      ctx.fillStyle = color;
      ctx.fillRect(ox + x * px, oy + y * px, w * px, h * px);
    }

    // body
    p(4, 4, 8, 4, '#333');
    p(5, 5, 6, 2, '#ffcc00');
    // head
    p(11, 4, 3, 3, '#333');
    p(14, 5, 2, 1, '#ff6600'); // beak
    // eye
    p(12, 5, 1, 1, '#fff');
    p(12, 5, 1, 1, '#fff');
    p(12, 5, 1, 1, '#fff');
    // wing (flap)
    const wingDy = Math.sin(flapPhase * Math.PI * 2) > 0 ? -2 : 1;
    p(2, 3 + wingDy, 4, 3, '#222');
  }

  function drawHUDPower(w, h) {
    // bar at right side for power indicator
    const barX = w - 20;
    ctx.fillStyle = '#000';
    ctx.fillRect(barX, 10, 10, h - 20);
    ctx.fillStyle = '#ffe680';
    const val = Math.max(0, Math.min(200, power));
    const filled = ((val) / 200) * (h - 24);
    ctx.fillRect(barX + 2, h - 12 - filled, 6, filled);
  }

  // Fetch metrics
  async function fetchPower() {
    try {
      const res = await fetch('/metrics', { cache: 'no-store' });
      const data = await res.json();
      const c = data && data.calculated_CA_values;
      const p = c ? Number(c.solar_power_live || 0) : 0;
      power = isFinite(p) ? Math.max(0, Math.min(200, p)) : 0;
      hudPower.textContent = Math.round(power);
    } catch (e) {
      // keep previous power
    }
  }

  const powerTimer = setInterval(fetchPower, 200);
  fetchPower();

  // Game loop
  function tick(now) {
    if (!running) return;
    const dt = Math.min(0.05, (now - lastTick) / 1000); // seconds
    lastTick = now;
    flapPhase = (flapPhase + dt * 4) % 1; // flap speed

    const w = canvas.clientWidth;
    const h = canvas.clientHeight;

    drawSky(w, h);
    drawCloudBands(w, h);
    drawMidline(w, h);

    const y = powerToY(power, h);
    drawBird(Math.floor(w / 2), Math.floor(y));
    drawHUDPower(w, h);

    const z = zones(h);
    const inCloud = y < z.top || y > z.bottom;

    if (!warningStart && inCloud) {
      warningStart = now;
      warningEl.style.display = 'block';
    }

    if (warningStart) {
      const elapsed = (now - warningStart) / 1000;
      const remain = Math.max(0, 3 - Math.floor(elapsed));
      warningEl.textContent = remain > 0 ? `CLOUD ZONE! ${remain}…` : 'CLOUD ZONE!';
      if (elapsed >= 3) {
        endGame();
        return;
      }
    } else {
      // Only count distance while safe
      const speed_mps = 6; // fake 6 m/s (~21.6 km/h)
      distance += speed_mps * dt;
      hudDistance.textContent = Math.floor(distance);
    }

    requestAnimationFrame(tick);
  }

  async function endGame() {
    running = false;
    clearInterval(powerTimer);
    warningEl.style.display = 'none';
    finalDistanceEl.textContent = Math.floor(distance);
    gameOverEl.style.display = 'flex';
    try {
      await fetch('/game/score', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user, distance_m: Math.floor(distance) })
      });
    } catch (e) {
      // ignore
    }
  }

  requestAnimationFrame((t) => { lastTick = t; tick(t); });
})();

