// Live Map (MapLibre + PMTiles), dark minimal style with follow toggle
(function () {
  let map = null;
  let follow = false;
  const coords = [];
  const MAX_POINTS = 5000;
  let posSource = null;
  let trackSource = null;
  let lastLon = null, lastLat = null;
  const pmtilesPath = '/tiles/basemap.pmtiles';
  const BASEMAPS = { AUTO: 'auto', VECTOR_DARK: 'vector_dark', VECTOR_LIGHT: 'vector_light', RASTER_OSM: 'raster_osm' };
  const OFFLINE_PM_TILES_DEFAULT = false; // set to true if you always ship offline tiles
  const FONT_STACK = 'Courier New'; // change to your font name

  async function glyphsAvailable() {
    try {
      const url = `/static/vendor/fonts/${encodeURIComponent(FONT_STACK)}/0-255.pbf`;
      const r = await fetch(url, { method: 'HEAD', cache: 'no-store' });
      return r.ok;
    } catch (e) {
      return false;
    }
  }

  async function ensurePmtiles() {
    if (typeof pmtiles !== 'undefined') return;
    async function loadScript(src) {
      return new Promise((resolve) => {
        const s = document.createElement('script');
        s.src = src;
        s.onload = () => resolve(true);
        s.onerror = () => resolve(false);
        document.head.appendChild(s);
      });
    }
    // Try local vendor bundle first for offline use, then fall back to CDN.
    const loadedLocal = await loadScript('/static/vendor/pmtiles.js');
    if (!loadedLocal) {
      await loadScript('https://unpkg.com/pmtiles@4.0.0/dist/pmtiles.js');
    }
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

  function createMinimalDarkStyle(pmtilesUrl, includeLabels) {
    return {
      version: 8,
      ...(includeLabels ? { glyphs: '/static/vendor/fonts/{fontstack}/{range}.pbf' } : {}),
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
        // Land/Earth base (if missing in dataset, this is harmless)
        { id: 'earth', type: 'fill', source: 'basemap', 'source-layer': 'earth', paint: { 'fill-color': '#0b0c12' } },
        { id: 'land', type: 'fill', source: 'basemap', 'source-layer': 'land', paint: { 'fill-color': '#0b0c12' } },
        // Landuse (parks/greens etc.)
        { id: 'landuse-park', type: 'fill', source: 'basemap', 'source-layer': 'landuse', filter: ['==', ['get', 'class'], 'park'], paint: { 'fill-color': '#0f2617', 'fill-opacity': 0.7 } },
        { id: 'landuse-forest', type: 'fill', source: 'basemap', 'source-layer': 'landuse', filter: ['in', ['get', 'class'], ['literal', ['forest', 'wood']]], paint: { 'fill-color': '#0e1f14', 'fill-opacity': 0.7 } },
        { id: 'landuse-grass', type: 'fill', source: 'basemap', 'source-layer': 'landuse', filter: ['in', ['get', 'class'], ['literal', ['grass', 'scrub', 'meadow']]], paint: { 'fill-color': '#0e1b12', 'fill-opacity': 0.6 } },
        // Water
        { id: 'water', type: 'fill', source: 'basemap', 'source-layer': 'water', paint: { 'fill-color': '#0b1a2a', 'fill-opacity': 0.9 } },
        // Boundaries
        { id: 'boundaries', type: 'line', source: 'basemap', 'source-layer': 'boundaries', paint: { 'line-color': '#2b2b39', 'line-width': ['interpolate', ['linear'], ['zoom'], 4, 0.2, 8, 0.6, 10, 0.8, 12, 1.0] } },
        // Roads (Protomaps)
        { id: 'roads', type: 'line', source: 'basemap', 'source-layer': 'roads', paint: { 'line-color': '#2a2a2a', 'line-width': ['interpolate', ['linear'], ['zoom'], 6, ['match', ['get', 'class'], 'motorway', 0.6, 'trunk', 0.55, 'primary', 0.5, 'secondary', 0.4, 'tertiary', 0.35, 'residential', 0.25, 'service', 0.2, 0.2], 10, ['match', ['get', 'class'], 'motorway', 1.2, 'trunk', 1.0, 'primary', 1.0, 'secondary', 0.9, 'tertiary', 0.8, 'residential', 0.6, 'service', 0.5, 0.6], 12, ['match', ['get', 'class'], 'motorway', 2.2, 'trunk', 2.0, 'primary', 1.8, 'secondary', 1.5, 'tertiary', 1.2, 'residential', 0.9, 'service', 0.7, 0.8], 14, ['match', ['get', 'class'], 'motorway', 3.5, 'trunk', 3.0, 'primary', 2.6, 'secondary', 2.2, 'tertiary', 1.8, 'residential', 1.4, 'service', 1.0, 1.2] ] } },
        // Buildings (Protomaps layer name is 'buildings')
        { id: 'buildings', type: 'fill', source: 'basemap', 'source-layer': 'buildings', paint: { 'fill-color': '#161616', 'fill-opacity': 0.45 } },

        ...(includeLabels ? [
          { id: 'water-labels', type: 'symbol', source: 'basemap', 'source-layer': 'water', layout: { 'text-field': ['coalesce', ['get', 'name:en'], ['get', 'name']], 'text-font': [FONT_STACK], 'text-size': ['interpolate', ['linear'], ['zoom'], 8, 10, 14, 14], 'text-max-width': 8, 'symbol-placement': 'point' }, paint: { 'text-color': '#7fb0ff', 'text-halo-color': '#0a0b0f', 'text-halo-width': 1 } },
          { id: 'road-labels', type: 'symbol', source: 'basemap', 'source-layer': 'roads', layout: { 'symbol-placement': 'line', 'text-field': ['coalesce', ['get', 'name:en'], ['get', 'name']], 'text-font': [FONT_STACK], 'text-size': ['match', ['get', 'class'], 'motorway', 12, 'trunk', 12, 'primary', 11, 'secondary', 10, 'tertiary', 10, 'residential', 9, 9] }, paint: { 'text-color': '#a0a3aa', 'text-halo-color': '#0a0b0f', 'text-halo-width': 1 } },
          { id: 'place-labels', type: 'symbol', source: 'basemap', 'source-layer': 'places', layout: { 'text-field': ['coalesce', ['get', 'name:en'], ['get', 'name']], 'text-font': [FONT_STACK], 'text-size': ['match', ['get', 'class'], 'city', 16, 'town', 14, 'village', 12, 'hamlet', 11, 12] }, paint: { 'text-color': '#e0e3e9', 'text-halo-color': '#0a0b0f', 'text-halo-width': 1.2 } },
        ] : []),

        // Live track & position
        { id: 'track-line', type: 'line', source: 'track', paint: { 'line-color': '#ff7a00', 'line-width': 3, 'line-opacity': 0.9 } },
        { id: 'pos-dot', type: 'circle', source: 'pos', paint: { 'circle-color': '#ff7a00', 'circle-radius': 5, 'circle-stroke-color': '#fff', 'circle-stroke-width': 2 } }
      ]
    };
  }

  function createMinimalLightStyle(pmtilesUrl, includeLabels) {
    return {
      version: 8,
      ...(includeLabels ? { glyphs: '/static/vendor/fonts/{fontstack}/{range}.pbf' } : {}),
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
        // Land/Earth base
        { id: 'earth', type: 'fill', source: 'basemap', 'source-layer': 'earth', paint: { 'fill-color': '#f3f6fa' } },
        { id: 'land', type: 'fill', source: 'basemap', 'source-layer': 'land', paint: { 'fill-color': '#f3f6fa' } },
        // Landuse (parks/greens etc.)
        { id: 'landuse-park', type: 'fill', source: 'basemap', 'source-layer': 'landuse', filter: ['==', ['get', 'class'], 'park'], paint: { 'fill-color': '#d5efda', 'fill-opacity': 0.8 } },
        { id: 'landuse-forest', type: 'fill', source: 'basemap', 'source-layer': 'landuse', filter: ['in', ['get', 'class'], ['literal', ['forest', 'wood']]], paint: { 'fill-color': '#cfe7d4', 'fill-opacity': 0.8 } },
        { id: 'landuse-grass', type: 'fill', source: 'basemap', 'source-layer': 'landuse', filter: ['in', ['get', 'class'], ['literal', ['grass', 'scrub', 'meadow']]], paint: { 'fill-color': '#e3f3e7', 'fill-opacity': 0.7 } },
        // Water
        { id: 'water', type: 'fill', source: 'basemap', 'source-layer': 'water', paint: { 'fill-color': '#cfe8ff', 'fill-opacity': 1.0 } },
        // Boundaries
        { id: 'boundaries', type: 'line', source: 'basemap', 'source-layer': 'boundaries', paint: { 'line-color': '#b6c0c9', 'line-width': ['interpolate', ['linear'], ['zoom'], 4, 0.2, 8, 0.6, 10, 0.8, 12, 1.0] } },
        // Roads (Protomaps)
        { id: 'roads', type: 'line', source: 'basemap', 'source-layer': 'roads', paint: { 'line-color': '#9aa4ad', 'line-width': ['interpolate', ['linear'], ['zoom'], 6, ['match', ['get', 'class'], 'motorway', 0.7, 'trunk', 0.6, 'primary', 0.55, 'secondary', 0.45, 'tertiary', 0.4, 'residential', 0.3, 'service', 0.25, 0.25], 10, ['match', ['get', 'class'], 'motorway', 1.4, 'trunk', 1.2, 'primary', 1.2, 'secondary', 1.0, 'tertiary', 0.9, 'residential', 0.7, 'service', 0.6, 0.7], 12, ['match', ['get', 'class'], 'motorway', 2.6, 'trunk', 2.3, 'primary', 2.0, 'secondary', 1.7, 'tertiary', 1.4, 'residential', 1.1, 'service', 0.9, 1.0], 14, ['match', ['get', 'class'], 'motorway', 4.0, 'trunk', 3.4, 'primary', 3.0, 'secondary', 2.6, 'tertiary', 2.0, 'residential', 1.6, 'service', 1.2, 1.4] ] } },
        // Buildings (Protomaps layer name is 'buildings')
        { id: 'buildings', type: 'fill', source: 'basemap', 'source-layer': 'buildings', paint: { 'fill-color': '#d7dee6', 'fill-opacity': 0.7 } },

        ...(includeLabels ? [
          { id: 'water-labels', type: 'symbol', source: 'basemap', 'source-layer': 'water', layout: { 'text-field': ['coalesce', ['get', 'name:en'], ['get', 'name']], 'text-font': [FONT_STACK], 'text-size': ['interpolate', ['linear'], ['zoom'], 8, 10, 14, 14], 'text-max-width': 8, 'symbol-placement': 'point' }, paint: { 'text-color': '#2a5885', 'text-halo-color': '#f4f7fb', 'text-halo-width': 1 } },
          { id: 'road-labels', type: 'symbol', source: 'basemap', 'source-layer': 'roads', layout: { 'symbol-placement': 'line', 'text-field': ['coalesce', ['get', 'name:en'], ['get', 'name']], 'text-font': [FONT_STACK], 'text-size': ['match', ['get', 'class'], 'motorway', 12, 'trunk', 12, 'primary', 11, 'secondary', 10, 'tertiary', 10, 'residential', 9, 9] }, paint: { 'text-color': '#5f6871', 'text-halo-color': '#ffffff', 'text-halo-width': 1 } },
          { id: 'place-labels', type: 'symbol', source: 'basemap', 'source-layer': 'places', layout: { 'text-field': ['coalesce', ['get', 'name:en'], ['get', 'name']], 'text-font': [FONT_STACK], 'text-size': ['match', ['get', 'class'], 'city', 16, 'town', 14, 'village', 12, 'hamlet', 11, 12] }, paint: { 'text-color': '#30363d', 'text-halo-color': '#ffffff', 'text-halo-width': 1.2 } },
        ] : []),

        // Live track & position
        { id: 'track-line', type: 'line', source: 'track', paint: { 'line-color': '#ff7a00', 'line-width': 3, 'line-opacity': 0.9 } },
        { id: 'pos-dot', type: 'circle', source: 'pos', paint: { 'circle-color': '#ff7a00', 'circle-radius': 5, 'circle-stroke-color': '#fff', 'circle-stroke-width': 2 } }
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
    const includeLabels = await glyphsAvailable();
    if (choice === BASEMAPS.RASTER_OSM) {
      return createRasterFallbackStyle();
    }
    if (choice === BASEMAPS.VECTOR_DARK || choice === BASEMAPS.VECTOR_LIGHT) {
      if (useOffline && await pmtilesExists(pmtilesPath)) {
        return choice === BASEMAPS.VECTOR_DARK ? createMinimalDarkStyle(pmtilesPath, includeLabels) : createMinimalLightStyle(pmtilesPath, includeLabels);
      }
      return createRasterFallbackStyle();
    }
    // AUTO: prefer offline vector dark if available, else raster
    if (useOffline && await pmtilesExists(pmtilesPath)) {
      return createMinimalDarkStyle(pmtilesPath, includeLabels);
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
