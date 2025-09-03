// Live Map (MapLibre + PMTiles), dark minimal style with follow toggle
(function () {
  let map = null;
  let follow = false;
  const coords = [];
  const MAX_POINTS = 5000;
  let posSource = null;
  let trackSource = null;
  let lastLon = null, lastLat = null;
  const pmtilesPath = '/static/tiles/basemap.pmtiles';
  const BASEMAPS = { AUTO: 'auto', VECTOR_DARK: 'vector_dark', VECTOR_LIGHT: 'vector_light', RASTER_OSM: 'raster_osm' };
  const OFFLINE_PM_TILES_DEFAULT = false; // set to true if you always ship offline tiles

  async function ensurePmtiles() {
    if (typeof pmtiles !== 'undefined') return;
    await new Promise((resolve) => {
      const s = document.createElement('script');
      s.src = 'https://unpkg.com/pmtiles@4.0.0/dist/pmtiles.js';
      s.onload = resolve;
      s.onerror = resolve; // resolve anyway; we'll check existence after
      document.head.appendChild(s);
    });
  }

  function setFollowButton() {
    const btn = document.getElementById('map-follow-toggle');
    if (!btn) return;
    btn.textContent = `Follow: ${follow ? 'On' : 'Off'}`;
  }

  function toggleFollow() {
    follow = !follow;
    setFollowButton();
  }

  function createMinimalDarkStyle(pmtilesUrl) {
    return {
      version: 8,
      sources: {
        basemap: {
          type: 'vector',
          url: `pmtiles://${pmtilesUrl}`
        },
        track: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
        pos: { type: 'geojson', data: { type: 'Feature', geometry: { type: 'Point', coordinates: [0, 0] } } }
      },
      layers: [
        { id: 'bg', type: 'background', paint: { 'background-color': '#0a0b0f' } },
        // Landcover/landuse base
        { id: 'landcover', type: 'fill', source: 'basemap', 'source-layer': 'landcover', paint: { 'fill-color': '#0f1116', 'fill-opacity': 0.35 } },
        { id: 'landuse', type: 'fill', source: 'basemap', 'source-layer': 'landuse', paint: { 'fill-color': '#0f1116', 'fill-opacity': 0.25 } },
        // Water
        { id: 'water', type: 'fill', source: 'basemap', 'source-layer': 'water', paint: { 'fill-color': '#0b1a2a', 'fill-opacity': 0.9 } },
        // Roads (transportation in OpenMapTiles)
        { id: 'roads', type: 'line', source: 'basemap', 'source-layer': 'transportation', paint: { 'line-color': '#2a2a2a', 'line-width': ['interpolate', ['linear'], ['zoom'], 8, 0.2, 12, 1.1, 14, 1.8] } },
        // Buildings
        { id: 'buildings', type: 'fill', source: 'basemap', 'source-layer': 'building', paint: { 'fill-color': '#161616', 'fill-opacity': 0.45 } },

        // Live track & position
        { id: 'track-line', type: 'line', source: 'track', paint: { 'line-color': '#ff7a00', 'line-width': 3, 'line-opacity': 0.9 } },
        { id: 'pos-dot', type: 'circle', source: 'pos', paint: { 'circle-color': '#1e90ff', 'circle-radius': 5, 'circle-stroke-color': '#fff', 'circle-stroke-width': 2 } }
      ]
    };
  }

  function createMinimalLightStyle(pmtilesUrl) {
    return {
      version: 8,
      sources: {
        basemap: {
          type: 'vector',
          url: `pmtiles://${pmtilesUrl}`
        },
        track: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
        pos: { type: 'geojson', data: { type: 'Feature', geometry: { type: 'Point', coordinates: [0, 0] } } }
      },
      layers: [
        { id: 'bg', type: 'background', paint: { 'background-color': '#f4f7fb' } },
        // Landcover/landuse base
        { id: 'landcover', type: 'fill', source: 'basemap', 'source-layer': 'landcover', paint: { 'fill-color': '#eaeef3', 'fill-opacity': 0.6 } },
        { id: 'landuse', type: 'fill', source: 'basemap', 'source-layer': 'landuse', paint: { 'fill-color': '#eaeef3', 'fill-opacity': 0.45 } },
        // Water
        { id: 'water', type: 'fill', source: 'basemap', 'source-layer': 'water', paint: { 'fill-color': '#cfe8ff', 'fill-opacity': 1.0 } },
        // Roads
        { id: 'roads', type: 'line', source: 'basemap', 'source-layer': 'transportation', paint: { 'line-color': '#9aa4ad', 'line-width': ['interpolate', ['linear'], ['zoom'], 8, 0.2, 12, 1.1, 14, 1.8] } },
        // Buildings
        { id: 'buildings', type: 'fill', source: 'basemap', 'source-layer': 'building', paint: { 'fill-color': '#d7dee6', 'fill-opacity': 0.7 } },

        // Live track & position
        { id: 'track-line', type: 'line', source: 'track', paint: { 'line-color': '#d33', 'line-width': 3, 'line-opacity': 0.9 } },
        { id: 'pos-dot', type: 'circle', source: 'pos', paint: { 'circle-color': '#0077cc', 'circle-radius': 5, 'circle-stroke-color': '#fff', 'circle-stroke-width': 2 } }
      ]
    };
  }

  function createRasterFallbackStyle() {
    return {
      version: 8,
      sources: {
        osm: {
          type: 'raster',
          tiles: [
            // Development-only fallback; respect OSM tile usage policy for production
            'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
          ],
          tileSize: 256,
          attribution: '© OpenStreetMap contributors'
        },
        track: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
        pos: { type: 'geojson', data: { type: 'Feature', geometry: { type: 'Point', coordinates: [0, 0] } } }
      },
      layers: [
        { id: 'bg', type: 'background', paint: { 'background-color': '#0a0b0f' } },
        { id: 'osm-raster', type: 'raster', source: 'osm' },
        { id: 'track-line', type: 'line', source: 'track', paint: { 'line-color': '#ff7a00', 'line-width': 3, 'line-opacity': 0.9 } },
        { id: 'pos-dot', type: 'circle', source: 'pos', paint: { 'circle-color': '#1e90ff', 'circle-radius': 5, 'circle-stroke-color': '#fff', 'circle-stroke-width': 2 } }
      ]
    };
  }

  async function pmtilesExists(url) {
    try {
      const head = await fetch(url, { method: 'HEAD', cache: 'no-store' });
      if (head.ok) return true;
      // Some servers don’t support HEAD
      if (head.status === 405 || head.status === 501) {
        const probe = await fetch(url, { method: 'GET', headers: { Range: 'bytes=0-0' }, cache: 'no-store' });
        return probe.ok || probe.status === 206; // partial content acceptable
      }
      return false;
    } catch (e) {
      return false;
    }
  }

  function wantOfflineTiles() {
    // Priority: data attribute on container, then localStorage flag, then default
    const el = document.getElementById('live-map');
    const attr = el && el.getAttribute('data-use-offline-tiles');
    if (attr && (attr === '1' || attr === 'true')) return true;
    const ls = localStorage.getItem('useOfflineTiles');
    if (ls === '1' || ls === 'true') return true;
    return OFFLINE_PM_TILES_DEFAULT;
  }

  function getBasemapChoice() {
    const v = localStorage.getItem('basemap');
    if (v === BASEMAPS.VECTOR_DARK || v === BASEMAPS.VECTOR_LIGHT || v === BASEMAPS.RASTER_OSM || v === BASEMAPS.AUTO) return v;
    return BASEMAPS.AUTO;
  }

  async function chooseStyle(choice) {
    const useOffline = wantOfflineTiles();
    if (choice === BASEMAPS.RASTER_OSM) {
      return createRasterFallbackStyle();
    }
    if (choice === BASEMAPS.VECTOR_DARK || choice === BASEMAPS.VECTOR_LIGHT) {
      if (useOffline && await pmtilesExists(pmtilesPath)) {
        return choice === BASEMAPS.VECTOR_DARK ? createMinimalDarkStyle(pmtilesPath) : createMinimalLightStyle(pmtilesPath);
      }
      return createRasterFallbackStyle();
    }
    // AUTO: prefer offline vector dark if available, else raster
    if (useOffline && await pmtilesExists(pmtilesPath)) {
      return createMinimalDarkStyle(pmtilesPath);
    }
    return createRasterFallbackStyle();
  }

  function rebindSourcesAndRefresh() {
    posSource = map.getSource('pos');
    trackSource = map.getSource('track');
    if (posSource && isFinite(lastLon) && isFinite(lastLat)) {
      posSource.setData({ type: 'Feature', geometry: { type: 'Point', coordinates: [lastLon, lastLat] } });
    }
    if (trackSource && coords.length) {
      trackSource.setData({ type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'LineString', coordinates: coords.slice() } }] });
    }
  }

  async function switchBasemap(choice) {
    const style = await chooseStyle(choice);
    map.setStyle(style);
    map.once('styledata', rebindSourcesAndRefresh);
  }

  async function initMap() {
    const container = document.getElementById('live-map');
    if (!container) return;

    // Try to ensure pmtiles is available (load CDN if missing)
    await ensurePmtiles();

    if (typeof maplibregl === 'undefined' || typeof pmtiles === 'undefined') {
      container.innerHTML = '<div style="color:#ccc; padding:0.5rem;">Map libraries not loaded. Place local vendor files under static/vendor or connect to the internet once.</div>';
      return;
    }

    // Register PMTiles protocol
    const protocol = new pmtiles.Protocol();
    maplibregl.addProtocol('pmtiles', protocol.tile);

    const initialChoice = getBasemapChoice();
    const style = await chooseStyle(initialChoice);

    map = new maplibregl.Map({
      container: 'live-map',
      style,
      center: [2.35, 48.86],
      zoom: 12,
      antialias: false,
      attributionControl: false,
    });

    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right');

    map.on('load', () => {
      posSource = map.getSource('pos');
      trackSource = map.getSource('track');
    });

    // Follow toggle button
    const btn = document.getElementById('map-follow-toggle');
    if (btn) btn.addEventListener('click', toggleFollow);
    setFollowButton();

    // Basemap selector
    const sel = document.getElementById('basemap-select');
    if (sel) {
      sel.value = getBasemapChoice();
      sel.addEventListener('change', async (e) => {
        const val = e.target.value;
        localStorage.setItem('basemap', val);
        await switchBasemap(val);
      });
    }

    // Resize observer to keep square
    new ResizeObserver(() => { map?.resize(); }).observe(container);

    // Start GPS polling
    setInterval(tickGps, 1000);
  }

  async function tickGps() {
    try {
      const res = await fetch('/gps_status', { cache: 'no-store' });
      const s = await res.json();
      if (!s || !s.has_fix || s.stale) return;
      const lon = Number(s.lon), lat = Number(s.lat);
      if (!isFinite(lon) || !isFinite(lat)) return;

      coords.push([lon, lat]);
      if (coords.length > MAX_POINTS) coords.shift();

      lastLon = lon; lastLat = lat;

      if (posSource) {
        posSource.setData({ type: 'Feature', geometry: { type: 'Point', coordinates: [lon, lat] } });
      }
      if (trackSource) {
        trackSource.setData({ type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'LineString', coordinates: coords.slice() } }] });
      }

      if (follow && map) {
        const z = Math.max(8, Math.min(18, map.getZoom()));
        map.easeTo({ center: [lon, lat], zoom: z, duration: 500 });
      }
    } catch (e) {
      // ignore
    }
  }

  window.addEventListener('DOMContentLoaded', initMap);
})();
