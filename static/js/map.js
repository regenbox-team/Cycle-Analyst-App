// Live Map (MapLibre + PMTiles), dark minimal style with follow toggle
(function () {
  let map = null;
  let follow = false;
  const coords = [];
  const MAX_POINTS = 5000;
  let posSource = null;
  let trackSource = null;
  let routeSource = null;
  let routeCoords = null; // cached route coordinates [[lon,lat],...]
  let routeProfile = { points: [], visible: false, zoomStartKm: 0, zoomEndKm: 0, cursorIndex: null };
  let routeDistancePoints = [];
  let routeProgress = null;
  let routeProfileDragging = false;
  let routeProfilePanStart = null;
  let routeProfileTouchDistance = null;
  let lastLon = null, lastLat = null;
  let lastGpsMoving = false;
  let lastGpsBearing = 0;
  let positionArrowImageLoading = false;
  const MIN_SPEED_KPH = 3; // do not update heading below this speed
  const NAVIGATION_ZOOM = 17;
  const NAVIGATION_PITCH = 45;
  const NAVIGATION_BUTTON_PITCH = 55;
  const HEADING_MODES = { NORTH: 'north', FREE: 'free', TRAJECTORY: 'trajectory' };
  let headingMode = HEADING_MODES.NORTH;
  let filteredBearing = 0; // smoothed map bearing
  let bearingInitialized = false;
  const PHONE_ALTITUDE_REFRESH_MS = 15000;
  const PHONE_ALTITUDE_OPTIONS = { enableHighAccuracy: true, maximumAge: 30000, timeout: 8000 };
  let phoneAltitudeTimer = null;
  const pmtilesPath = '/tiles/basemap.pmtiles';
  const BASEMAPS = { AUTO: 'auto', VECTOR_DARK: 'vector_dark', VECTOR_LIGHT: 'vector_light', RASTER_OSM: 'raster_osm', TERRAIN_3D: 'terrain_3d' };
  const OFFLINE_PM_TILES_DEFAULT = false; // set to true if you always ship offline tiles
  const FONT_STACK = 'Inter Regular'; // change to your font name
  const PROFILE_MIN_WINDOW_KM = 0.2;
  const PROFILE_ELEVATION_DEADBAND_M = 5.0;
  const PROFILE_REGEN_START_PCT = -2;
  const PROFILE_REGEN_FULL_PCT = -12;
  const PROFILE_SLOPE_THRESHOLD_PCT = 2;
  const PROFILE_SLOPE_MIN_KM = 0.5;
  const PROFILE_SLOPE_ZOOM_PADDING_KM = 0.15;
  const ROUTE_SNAP_MAX_METERS = 120;

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

  function setFollow(enabled) {
    follow = Boolean(enabled);
    setFollowButton();
  }

  function formatAltitudeMeters(value) {
    const alt = Number(value);
    if (!isFinite(alt)) return '-';
    return `${alt.toFixed(1)} m`;
  }

  function formatDistanceKm(value) {
    const km = Number(value);
    if (!isFinite(km)) return '-';
    if (km < 1) return `${Math.max(0, km * 1000).toFixed(0)} m`;
    return `${km.toFixed(km < 10 ? 1 : 0)} km`;
  }

  function setText(id, text, title) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    if (title) el.title = title;
    else el.removeAttribute('title');
  }

  function setPiAltitudeFromStatus(status) {
    if (!status || status.stale || !status.has_fix) {
      setText('pi-gps-altitude', '-', 'No fresh GPS fix from the Pi');
      return;
    }
    setText('pi-gps-altitude', formatAltitudeMeters(status.alt));
  }

  function setRouteProgress(progress) {
    routeProgress = progress;
    const row = document.getElementById('gpx-route-progress');
    const value = document.getElementById('gpx-route-remaining');
    setCurrentRouteGradient(progress);
    updateCurrentSlopeZoomUi();
    if (!row || !value) return;
    if (!progress) {
      row.hidden = false;
      row.classList.add('is-muted');
      value.textContent = '-';
      row.removeAttribute('title');
      return;
    }
    row.hidden = false;
    row.classList.remove('is-muted');
    value.textContent = formatDistanceKm(progress.remainingKm);
    row.title = `Closest point on GPX: ${Math.round(progress.distanceToRouteM)} m away`;
  }

  function setCurrentRouteGradient(progress) {
    const value = document.getElementById('trip-current-gradient');
    if (!value) return;
    const tile = value.closest('.trip-stat');
    if (!progress || !routeProfile.points.length) {
      value.textContent = '-';
      value.removeAttribute('title');
      if (tile) tile.classList.add('is-muted');
      return;
    }
    const index = nearestProfileIndexForDistance(progress.alongKm);
    const point = index == null ? null : routeProfile.points[index];
    if (!point) {
      value.textContent = '-';
      value.removeAttribute('title');
      if (tile) tile.classList.add('is-muted');
      return;
    }
    const grade = Number(point.gradePct) || 0;
    value.textContent = `${grade > 0 ? '+' : ''}${grade.toFixed(1)}%`;
    value.title = `${point.distanceKm.toFixed(2)} km on GPX · ${Math.round(point.ele)} m`;
    if (tile) tile.classList.remove('is-muted');
  }

  function mapIsExpanded() {
    const mapBox = document.querySelector('.map-box');
    return !mapBox || !mapBox.classList.contains('reduced');
  }

  function setPhoneAltitudeMessage(text, title) {
    setText('phone-gps-altitude', text, title);
  }

  function refreshPhoneAltitude() {
    if (!mapIsExpanded()) return;
    if (!('geolocation' in navigator)) {
      setPhoneAltitudeMessage('-', 'Phone/browser geolocation is not available');
      return;
    }
    if (window.isSecureContext === false) {
      setPhoneAltitudeMessage('-', 'Phone altitude requires HTTPS or localhost in most browsers');
      return;
    }

    navigator.geolocation.getCurrentPosition(
      (position) => {
        const coords = position && position.coords;
        const altitude = coords ? coords.altitude : null;
        if (altitude == null || !isFinite(Number(altitude))) {
          setPhoneAltitudeMessage('-', 'Phone GPS did not provide altitude');
          return;
        }
        const accuracy = coords.altitudeAccuracy;
        const title = isFinite(Number(accuracy)) ? `Altitude accuracy: +/- ${Number(accuracy).toFixed(1)} m` : '';
        setPhoneAltitudeMessage(formatAltitudeMeters(altitude), title);
      },
      (error) => {
        const message = error && error.message ? error.message : 'Phone GPS unavailable';
        setPhoneAltitudeMessage('-', message);
      },
      PHONE_ALTITUDE_OPTIONS
    );
  }

  function startPhoneAltitudePolling() {
    refreshPhoneAltitude();
    if (phoneAltitudeTimer) return;
    phoneAltitudeTimer = setInterval(refreshPhoneAltitude, PHONE_ALTITUDE_REFRESH_MS);
  }

  function toggleFollow() {
    setFollow(!follow);
  }

  function setHeadingButton() {
    const btn = document.getElementById('map-heading-toggle');
    if (!btn) return;
    if (headingMode === HEADING_MODES.NORTH) {
      btn.textContent = 'N';
      btn.style.opacity = '1';
      btn.title = 'Heading: North locked';
    } else if (headingMode === HEADING_MODES.FREE) {
      btn.textContent = 'N';
      btn.style.opacity = '0.4';
      btn.title = 'Heading: Free (no auto-rotate)';
    } else {
      btn.textContent = '↑';
      btn.style.opacity = '1';
      btn.title = 'Heading: Trajectory';
    }
  }

  function setHeadingMode(mode, persist = true) {
    if (!Object.values(HEADING_MODES).includes(mode)) return;
    headingMode = mode;
    if (persist) localStorage.setItem('heading_mode', headingMode);
    setHeadingButton();

    if (!map) return;
    if (headingMode === HEADING_MODES.NORTH) {
      filteredBearing = 0;
      bearingInitialized = true;
      map.easeTo({ bearing: 0, duration: 300 });
    } else if (headingMode === HEADING_MODES.TRAJECTORY) {
      filteredBearing = normalizeBearing(map.getBearing());
      bearingInitialized = true;
    }
  }

  function cycleHeadingMode() {
    if (headingMode === HEADING_MODES.NORTH) setHeadingMode(HEADING_MODES.FREE);
    else if (headingMode === HEADING_MODES.FREE) setHeadingMode(HEADING_MODES.TRAJECTORY);
    else setHeadingMode(HEADING_MODES.NORTH);
  }

  function normalizeBearing(b) {
    let x = ((b % 360) + 360) % 360;
    if (x === -0) x = 0;
    return x;
  }

  function shortestAngleDelta(from, to) {
    let a = normalizeBearing(to) - normalizeBearing(from);
    if (a > 180) a -= 360;
    if (a < -180) a += 360;
    return a;
  }

  function computeBearing(lon1, lat1, lon2, lat2) {
    // Returns initial bearing in degrees [0,360)
    const toRad = (d)=> d * Math.PI / 180;
    const toDeg = (r)=> r * 180 / Math.PI;
    const φ1 = toRad(lat1), φ2 = toRad(lat2);
    const Δλ = toRad(lon2 - lon1);
    const y = Math.sin(Δλ) * Math.cos(φ2);
    const x = Math.cos(φ1)*Math.sin(φ2) - Math.sin(φ1)*Math.cos(φ2)*Math.cos(Δλ);
    return normalizeBearing(toDeg(Math.atan2(y, x)));
  }

  function finiteNumberOrNull(value) {
    if (value === null || value === undefined || value === '') return null;
    const n = Number(value);
    return isFinite(n) ? n : null;
  }

  function gpsMotionFromStatus(status) {
    const speedKph = finiteNumberOrNull(status && status.speed_kph);
    const moving = speedKph !== null && speedKph >= MIN_SPEED_KPH;
    const gpsTrack = finiteNumberOrNull(status && status.track_deg);
    if (moving && gpsTrack !== null) {
      return { moving: true, bearing: normalizeBearing(gpsTrack) };
    }
    return { moving: false, bearing: lastGpsBearing };
  }

  function positionFeature(lon, lat, moving = lastGpsMoving, bearing = lastGpsBearing) {
    return {
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [lon, lat] },
      properties: {
        moving: Boolean(moving),
        bearing: normalizeBearing(Number(bearing) || 0)
      }
    };
  }

  function updatePositionSource(lon, lat, moving = lastGpsMoving, bearing = lastGpsBearing) {
    lastGpsMoving = Boolean(moving);
    lastGpsBearing = normalizeBearing(Number(bearing) || 0);
    if (posSource) {
      posSource.setData(positionFeature(lon, lat, lastGpsMoving, lastGpsBearing));
    }
  }

  function loadImageElement(src) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = reject;
      image.src = src;
    });
  }

  async function createPositionArrowImage() {
    const size = 96;
    const source = await loadImageElement('/static/img/arrow.png');
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext('2d');
    if (!ctx) return null;

    ctx.clearRect(0, 0, size, size);
    ctx.drawImage(source, 0, 0, size, size);
    ctx.globalCompositeOperation = 'source-in';
    ctx.fillStyle = '#1e90ff';
    ctx.fillRect(0, 0, size, size);
    ctx.globalCompositeOperation = 'source-over';
    return ctx.getImageData(0, 0, size, size);
  }

  async function ensurePositionArrowImage() {
    if (!map || (map.hasImage && map.hasImage('position-arrow'))) return;
    if (positionArrowImageLoading) return;
    positionArrowImageLoading = true;
    try {
      const image = await createPositionArrowImage();
      if (!image || !map || (map.hasImage && map.hasImage('position-arrow'))) return;
      map.addImage('position-arrow', image, { pixelRatio: 2 });
    } catch (e) {
      // Style reloads can briefly race image registration.
    } finally {
      positionArrowImageLoading = false;
    }
  }

  function positionLayers(circleColor) {
    return [
      {
        id: 'pos-dot',
        type: 'circle',
        source: 'pos',
        filter: ['!=', ['get', 'moving'], true],
        paint: { 'circle-color': circleColor, 'circle-radius': 5, 'circle-stroke-color': '#fff', 'circle-stroke-width': 2 }
      },
      {
        id: 'pos-arrow',
        type: 'symbol',
        source: 'pos',
        filter: ['==', ['get', 'moving'], true],
        layout: {
          'icon-image': 'position-arrow',
          'icon-size': 0.9,
          'icon-allow-overlap': true,
          'icon-ignore-placement': true,
          'icon-rotate': ['get', 'bearing'],
          'icon-rotation-alignment': 'map'
        }
      }
    ];
  }

  function applyNavigationView(lon, lat, options = {}) {
    if (!map || !isFinite(lon) || !isFinite(lat)) return;
    setFollow(true);
    setHeadingMode(HEADING_MODES.TRAJECTORY);
    const bearing = options.bearing == null ? null : normalizeBearing(options.bearing);
    if (bearing != null) {
      filteredBearing = bearing;
      bearingInitialized = true;
      lastGpsBearing = bearing;
    }
    const targetZoom = Math.max(map.getZoom() || 0, NAVIGATION_ZOOM);
    const easeOptions = {
      center: [lon, lat],
      zoom: targetZoom,
      pitch: options.pitch == null ? NAVIGATION_PITCH : options.pitch,
      duration: options.duration == null ? 600 : options.duration
    };
    if (bearing != null) easeOptions.bearing = bearing;
    map.easeTo(easeOptions);
  }

  // ==== Inline Protomaps-like styles (copied from basemaps.js) ====
  function getDarkPalette() {
    // Based on basemaps DARK palette, with CSS-aligned background
    return {
      background: '#000000',
      earth: '#000000',
      park_a: '#1c2421', park_b: '#192a24', hospital: '#252424', industrial: '#222222', school: '#262323',
      wood_a: '#202121', wood_b: '#202121', pedestrian: '#1e1e1e', scrub_a: '#222323', scrub_b: '#222323',
      glacier: '#1c1c1c', sand: '#212123', beach: '#28282a', aerodrome: '#1e1e1e', runway: '#333333',
      water: '#31353f', zoo: '#222323', military: '#242323',
      tunnel_other_casing: '#141414', tunnel_minor_casing: '#141414', tunnel_link_casing: '#141414', tunnel_major_casing: '#141414', tunnel_highway_casing: '#141414',
      tunnel_other: '#292929', tunnel_minor: '#292929', tunnel_link: '#292929', tunnel_major: '#292929', tunnel_highway: '#292929',
      pier: '#333333', buildings: '#111111',
      minor_service_casing: '#1f1f1f', minor_casing: '#1f1f1f', link_casing: '#1f1f1f', major_casing_late: '#1f1f1f', highway_casing_late: '#1f1f1f',
      other: '#333333', minor_service: '#333333', minor_a: '#3d3d3d', minor_b: '#333333', link: '#3d3d3d',
      major_casing_early: '#1f1f1f', major: '#3d3d3d', highway_casing_early: '#1f1f1f', highway: '#474747',
      railway: '#000000', boundaries: '#5b6374',
      bridges_other_casing: '#2b2b2b', bridges_minor_casing: '#1f1f1f', bridges_link_casing: '#1f1f1f', bridges_major_casing: '#1f1f1f', bridges_highway_casing: '#1f1f1f',
      bridges_other: '#333333', bridges_minor: '#333333', bridges_link: '#3d3d3d', bridges_major: '#3d3d3d', bridges_highway: '#474747',
      roads_label_minor: '#525252', roads_label_minor_halo: '#1f1f1f',
      roads_label_major: '#666666', roads_label_major_halo: '#1f1f1f',
      ocean_label: '#717784',
      subplace_label: '#525252', subplace_label_halo: '#1f1f1f',
      city_label: '#7a7a7a', city_label_halo: '#212121',
      state_label: '#3d3d3d', state_label_halo: '#1f1f1f',
      country_label: '#5c5c5c',
      address_label: '#525252', address_label_halo: '#1f1f1f',
      landcover: {
        grassland: 'rgba(30, 41, 31, 1)', barren: 'rgba(38, 38, 36, 1)', urban_area: 'rgba(28, 28, 28, 1)',
        farmland: 'rgba(31, 36, 32, 1)', glacier: 'rgba(43, 43, 43, 1)', scrub: 'rgba(34, 36, 30, 1)', forest: 'rgba(28, 41, 37, 1)'
      }
    };
  }

  function getLightPalette() {
    // Based on basemaps LIGHT palette, with CSS-aligned background/earth to --bg-box
    return {
      background: '#e5e1cf',
      earth: '#e5e1cf',
      park_a: '#cfddd5', park_b: '#9cd3b4', hospital: '#e4dad9', industrial: '#d1dde1', school: '#e4ded7',
      wood_a: '#d0ded0', wood_b: '#a0d9a0', pedestrian: '#e3e0d4', scrub_a: '#cedcd7', scrub_b: '#99d2bb',
      glacier: '#e7e7e7', sand: '#e2e0d7', beach: '#e8e4d0', aerodrome: '#dadbdf', runway: '#e9e9ed',
      water: '#80deea', zoo: '#c6dcdc', military: '#dcdcdc',
      tunnel_other_casing: '#e0e0e0', tunnel_minor_casing: '#e0e0e0', tunnel_link_casing: '#e0e0e0', tunnel_major_casing: '#e0e0e0', tunnel_highway_casing: '#e0e0e0',
      tunnel_other: '#d5d5d5', tunnel_minor: '#d5d5d5', tunnel_link: '#d5d5d5', tunnel_major: '#d5d5d5', tunnel_highway: '#d5d5d5',
      pier: '#e0e0e0', buildings: '#cccccc',
      minor_service_casing: '#e0e0e0', minor_casing: '#e0e0e0', link_casing: '#e0e0e0', major_casing_late: '#e0e0e0', highway_casing_late: '#e0e0e0',
      other: '#ebebeb', minor_service: '#ebebeb', minor_a: '#ebebeb', minor_b: '#ffffff', link: '#ffffff',
      major_casing_early: '#e0e0e0', major: '#ffffff', highway_casing_early: '#e0e0e0', highway: '#ffffff',
      railway: '#a7b1b3', boundaries: '#adadad',
      bridges_other_casing: '#e0e0e0', bridges_minor_casing: '#e0e0e0', bridges_link_casing: '#e0e0e0', bridges_major_casing: '#e0e0e0', bridges_highway_casing: '#e0e0e0',
      bridges_other: '#ebebeb', bridges_minor: '#ffffff', bridges_link: '#ffffff', bridges_major: '#f5f5f5', bridges_highway: '#ffffff',
      roads_label_minor: '#91888b', roads_label_minor_halo: '#ffffff',
      roads_label_major: '#938a8d', roads_label_major_halo: '#ffffff',
      ocean_label: '#728dd4',
      subplace_label: '#8f8f8f', subplace_label_halo: '#e0e0e0',
      city_label: '#5c5c5c', city_label_halo: '#e0e0e0',
      state_label: '#b3b3b3', state_label_halo: '#e0e0e0',
      country_label: '#a3a3a3',
      address_label: '#91888b', address_label_halo: '#ffffff',
      landcover: {
        grassland: 'rgba(210, 239, 207, 1)', barren: 'rgba(255, 243, 215, 1)', urban_area: 'rgba(230, 230, 230, 1)',
        farmland: 'rgba(216, 239, 210, 1)', glacier: 'rgba(255, 255, 255, 1)', scrub: 'rgba(234, 239, 210, 1)', forest: 'rgba(196, 231, 210, 1)'
      }
    };
  }

  function buildBaseLayers(sourceId, e) {
    const Z = [
      { id: 'background', type: 'background', paint: { 'background-color': e.background } },
      { id: 'earth', type: 'fill', filter: ['==', '$type', 'Polygon'], source: sourceId, 'source-layer': 'earth', paint: { 'fill-color': e.earth } },
    ];
    if (e.landcover) {
      Z.push({
        id: 'landcover', type: 'fill', source: sourceId, 'source-layer': 'landcover',
        paint: {
          'fill-color': ['match', ['get', 'kind'], 'grassland', e.landcover.grassland, 'barren', e.landcover.barren, 'urban_area', e.landcover.urban_area, 'farmland', e.landcover.farmland, 'glacier', e.landcover.glacier, 'scrub', e.landcover.scrub, e.landcover.forest],
          'fill-opacity': ['interpolate', ['linear'], ['zoom'], 5, 1, 7, 0]
        }
      });
    }
    Z.push(
      { id: 'landuse_park', type: 'fill', source: sourceId, 'source-layer': 'landuse',
        filter: ['in', 'kind', 'national_park', 'park', 'cemetery', 'protected_area', 'nature_reserve', 'forest', 'golf_course', 'wood', 'nature_reserve', 'forest', 'scrub', 'grassland', 'grass', 'military', 'naval_base', 'airfield'],
        paint: {
          'fill-opacity': ['interpolate', ['linear'], ['zoom'], 6, 0, 11, 1],
          'fill-color': ['case',
            ['in', ['get', 'kind'], ['literal', ['national_park','park','cemetery','protected_area','nature_reserve','forest','golf_course']]], e.park_b,
            ['in', ['get', 'kind'], ['literal', ['wood','nature_reserve','forest']]], e.wood_b,
            ['in', ['get', 'kind'], ['literal', ['scrub','grassland','grass']]], e.scrub_b,
            ['in', ['get', 'kind'], ['literal', ['glacier']]], e.glacier,
            ['in', ['get', 'kind'], ['literal', ['sand']]], e.sand,
            ['in', ['get', 'kind'], ['literal', ['military','naval_base','airfield']]], e.zoo,
            e.earth
          ]
        }
      },
      { id: 'landuse_urban_green', type: 'fill', source: sourceId, 'source-layer': 'landuse', filter: ['in','kind','allotments','village_green','playground'], paint: { 'fill-color': e.park_b, 'fill-opacity': 0.7 } },
      { id: 'landuse_hospital', type: 'fill', source: sourceId, 'source-layer': 'landuse', filter: ['==','kind','hospital'], paint: { 'fill-color': e.hospital } },
      { id: 'landuse_industrial', type: 'fill', source: sourceId, 'source-layer': 'landuse', filter: ['==','kind','industrial'], paint: { 'fill-color': e.industrial } },
      { id: 'landuse_school', type: 'fill', source: sourceId, 'source-layer': 'landuse', filter: ['in','kind','school','university','college'], paint: { 'fill-color': e.school } },
      { id: 'landuse_beach', type: 'fill', source: sourceId, 'source-layer': 'landuse', filter: ['in','kind','beach'], paint: { 'fill-color': e.beach } },
      { id: 'landuse_zoo', type: 'fill', source: sourceId, 'source-layer': 'landuse', filter: ['in','kind','zoo'], paint: { 'fill-color': e.zoo } },
      { id: 'landuse_aerodrome', type: 'fill', source: sourceId, 'source-layer': 'landuse', filter: ['in','kind','aerodrome'], paint: { 'fill-color': e.aerodrome } },
      { id: 'roads_runway', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['==','kind_detail','runway'], paint: { 'line-color': e.runway, 'line-width': ['interpolate',['exponential',1.6],['zoom'],10,0,12,4,18,30] } },
      { id: 'roads_taxiway', type: 'line', source: sourceId, 'source-layer': 'roads', minzoom: 13, filter: ['==','kind_detail','taxiway'], paint: { 'line-color': e.runway, 'line-width': ['interpolate',['exponential',1.6],['zoom'],13,0,13.5,1,15,6] } },
      { id: 'landuse_runway', type: 'fill', source: sourceId, 'source-layer': 'landuse', filter: ['any',['in','kind','runway','taxiway']], paint: { 'fill-color': e.runway } },
      { id: 'water', type: 'fill', filter: ['==','$type','Polygon'], source: sourceId, 'source-layer': 'water', paint: { 'fill-color': e.water } },
      { id: 'water_stream', type: 'line', source: sourceId, 'source-layer': 'water', minzoom: 14, filter: ['in','kind','stream'], paint: { 'line-color': e.water, 'line-width': 0.5 } },
      { id: 'water_river', type: 'line', source: sourceId, 'source-layer': 'water', minzoom: 9, filter: ['in','kind','river'], paint: { 'line-color': e.water, 'line-width': ['interpolate',['exponential',1.6],['zoom'],9,0,9.5,1,18,12] } },
      { id: 'landuse_pedestrian', type: 'fill', source: sourceId, 'source-layer': 'landuse', filter: ['in','kind','pedestrian','dam'], paint: { 'fill-color': e.pedestrian } },
      { id: 'landuse_pier', type: 'fill', source: sourceId, 'source-layer': 'landuse', filter: ['==','kind','pier'], paint: { 'fill-color': e.pier } },
      // Tunnels casings and lines
      { id: 'roads_tunnels_other_casing', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['all',['has','is_tunnel'],['in','kind','other','path']], paint: { 'line-color': e.tunnel_other_casing, 'line-gap-width': ['interpolate',['exponential',1.6],['zoom'],14,0,20,7] } },
      { id: 'roads_tunnels_minor_casing', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['all',['has','is_tunnel'],['==','kind','minor_road']], paint: { 'line-color': e.tunnel_minor_casing, 'line-dasharray': [3,2], 'line-gap-width': ['interpolate',['exponential',1.6],['zoom'],11,0,12.5,0.5,15,2,18,11], 'line-width': ['interpolate',['exponential',1.6],['zoom'],12,0,12.5,1] } },
      { id: 'roads_tunnels_link_casing', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['all',['has','is_tunnel'],['has','is_link']], paint: { 'line-color': e.tunnel_link_casing, 'line-dasharray': [3,2], 'line-gap-width': ['interpolate',['exponential',1.6],['zoom'],13,0,13.5,1,18,11], 'line-width': ['interpolate',['exponential',1.6],['zoom'],12,0,12.5,1] } },
      { id: 'roads_tunnels_major_casing', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['all',['!has','is_tunnel'],['!has','is_bridge'],['==','kind','major_road']], paint: { 'line-color': e.tunnel_major_casing, 'line-dasharray': [3,2], 'line-gap-width': ['interpolate',['exponential',1.6],['zoom'],7,0,7.5,0.5,18,13], 'line-width': ['interpolate',['exponential',1.6],['zoom'],9,0,9.5,1] } },
      { id: 'roads_tunnels_highway_casing', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['all',['!has','is_tunnel'],['!has','is_bridge'],['==','kind','highway'],['!has','is_link']], paint: { 'line-color': e.tunnel_highway_casing, 'line-dasharray': [6,0.5], 'line-gap-width': ['interpolate',['exponential',1.6],['zoom'],3,0,3.5,0.5,18,15], 'line-width': ['interpolate',['exponential',1.6],['zoom'],7,0,7.5,1,20,15] } },
      { id: 'roads_tunnels_other', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['all',['has','is_tunnel'],['in','kind','other','path']], paint: { 'line-color': e.tunnel_other, 'line-dasharray': [4.5,0.5], 'line-width': ['interpolate',['exponential',1.6],['zoom'],14,0,20,7] } },
      { id: 'roads_tunnels_minor', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['all',['has','is_tunnel'],['==',['get','kind'],'minor_road']], paint: { 'line-color': e.tunnel_minor, 'line-width': ['interpolate',['exponential',1.6],['zoom'],11,0,12.5,0.5,15,2,18,11] } },
      { id: 'roads_tunnels_link', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['all',['has','is_tunnel'],['has','is_link']], paint: { 'line-color': e.tunnel_minor, 'line-width': ['interpolate',['exponential',1.6],['zoom'],13,0,13.5,1,18,11] } },
      { id: 'roads_tunnels_major', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['all',['has','is_tunnel'],['==','kind','major_road']], paint: { 'line-color': e.tunnel_major, 'line-width': ['interpolate',['exponential',1.6],['zoom'],6,0,12,1.6,15,3,18,13] } },
      { id: 'roads_tunnels_highway', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['all',['has','is_tunnel'],['==',['get','kind'],'highway'],['!', ['has','is_link']]], paint: { 'line-color': e.tunnel_highway, 'line-width': ['interpolate',['exponential',1.6],['zoom'],3,0,6,1.1,12,1.6,15,5,18,15] } },
      // Buildings
      { id: 'buildings', type: 'fill', source: sourceId, 'source-layer': 'buildings', filter: ['in','kind','building','building_part'], paint: { 'fill-color': e.buildings, 'fill-opacity': 0.5 } },
      // Piers and minor casings/roads
      { id: 'roads_pier', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['==','kind_detail','pier'], paint: { 'line-color': e.pier, 'line-width': ['interpolate',['exponential',1.6],['zoom'],12,0,12.5,0.5,20,16] } },
      { id: 'roads_minor_service_casing', type: 'line', source: sourceId, 'source-layer': 'roads', minzoom: 13, filter: ['all',['!has','is_tunnel'],['!has','is_bridge'],['==','kind','minor_road'],['==','kind_detail','service']], paint: { 'line-color': e.minor_service_casing, 'line-gap-width': ['interpolate',['exponential',1.6],['zoom'],13,0,18,8], 'line-width': ['interpolate',['exponential',1.6],['zoom'],13,0,13.5,0.8] } },
      { id: 'roads_minor_casing', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['all',['!has','is_tunnel'],['!has','is_bridge'],['==','kind','minor_road'],['!=','kind_detail','service']], paint: { 'line-color': e.minor_casing, 'line-gap-width': ['interpolate',['exponential',1.6],['zoom'],11,0,12.5,0.5,15,2,18,11], 'line-width': ['interpolate',['exponential',1.6],['zoom'],12,0,12.5,1] } },
      { id: 'roads_link_casing', type: 'line', source: sourceId, 'source-layer': 'roads', minzoom: 13, filter: ['has','is_link'], paint: { 'line-color': e.minor_casing, 'line-gap-width': ['interpolate',['exponential',1.6],['zoom'],13,0,13.5,1,18,11], 'line-width': ['interpolate',['exponential',1.6],['zoom'],13,0,13.5,1.5] } },
      { id: 'roads_major_casing_late', type: 'line', source: sourceId, 'source-layer': 'roads', minzoom: 12, filter: ['all',['!has','is_tunnel'],['!has','is_bridge'],['==','kind','major_road']], paint: { 'line-color': e.major_casing_late, 'line-gap-width': ['interpolate',['exponential',1.6],['zoom'],6,0,12,1.6,15,3,18,13], 'line-width': ['interpolate',['exponential',1.6],['zoom'],9,0,9.5,1] } },
      { id: 'roads_highway_casing_late', type: 'line', source: sourceId, 'source-layer': 'roads', minzoom: 12, filter: ['all',['!has','is_tunnel'],['!has','is_bridge'],['==','kind','highway'],['!has','is_link']], paint: { 'line-color': e.highway_casing_late, 'line-gap-width': ['interpolate',['exponential',1.6],['zoom'],3,0,3.5,0.5,18,15], 'line-width': ['interpolate',['exponential',1.6],['zoom'],7,0,7.5,1,20,15] } },
      { id: 'roads_other', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['all',['!has','is_tunnel'],['!has','is_bridge'],['in','kind','other','path'],['!=','kind_detail','pier']], paint: { 'line-color': e.other, 'line-dasharray': [3,1], 'line-width': ['interpolate',['exponential',1.6],['zoom'],14,0,20,7] } },
      { id: 'roads_link', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['has','is_link'], paint: { 'line-color': e.link, 'line-width': ['interpolate',['exponential',1.6],['zoom'],13,0,13.5,1,18,11] } },
      { id: 'roads_minor_service', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['all',['!has','is_tunnel'],['!has','is_bridge'],['==','kind','minor_road'],['==','kind_detail','service']], paint: { 'line-color': e.minor_service, 'line-width': ['interpolate',['exponential',1.6],['zoom'],13,0,18,8] } },
      { id: 'roads_minor', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['all',['!has','is_tunnel'],['!has','is_bridge'],['==','kind','minor_road'],['!=','kind_detail','service']], paint: { 'line-color': ['interpolate',['exponential',1.6],['zoom'],11, e.minor_a, 16, e.minor_b], 'line-width': ['interpolate',['exponential',1.6],['zoom'],11,0,12.5,0.5,15,2,18,11] } },
      { id: 'roads_major_casing_early', type: 'line', source: sourceId, 'source-layer': 'roads', maxzoom: 12, filter: ['all',['!has','is_tunnel'],['!has','is_bridge'],['==','kind','major_road']], paint: { 'line-color': e.major_casing_early, 'line-gap-width': ['interpolate',['exponential',1.6],['zoom'],7,0,7.5,0.5,18,13], 'line-width': ['interpolate',['exponential',1.6],['zoom'],9,0,9.5,1] } },
      { id: 'roads_major', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['all',['!has','is_tunnel'],['!has','is_bridge'],['==','kind','major_road']], paint: { 'line-color': e.major, 'line-width': ['interpolate',['exponential',1.6],['zoom'],6,0,12,1.6,15,3,18,13] } },
      { id: 'roads_highway_casing_early', type: 'line', source: sourceId, 'source-layer': 'roads', maxzoom: 12, filter: ['all',['!has','is_tunnel'],['!has','is_bridge'],['==','kind','highway'],['!has','is_link']], paint: { 'line-color': e.highway_casing_early, 'line-gap-width': ['interpolate',['exponential',1.6],['zoom'],3,0,3.5,0.5,18,15], 'line-width': ['interpolate',['exponential',1.6],['zoom'],7,0,7.5,1] } },
      { id: 'roads_highway', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['all',['!has','is_tunnel'],['!has','is_bridge'],['==','kind','highway'],['!has','is_link']], paint: { 'line-color': e.highway, 'line-width': ['interpolate',['exponential',1.6],['zoom'],3,0,6,1.1,12,1.6,15,5,18,15] } },
      { id: 'roads_rail', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['==','kind','rail'], paint: { 'line-dasharray': [0.3, 0.75], 'line-opacity': 0.5, 'line-color': e.railway, 'line-width': ['interpolate',['exponential',1.6],['zoom'],3,0,6,0.15,18,9] } },
      { id: 'boundaries_country', type: 'line', source: sourceId, 'source-layer': 'boundaries', filter: ['<=','kind_detail',2], paint: { 'line-color': e.boundaries, 'line-width': 0.7, 'line-dasharray': ['step', ['zoom'], ['literal',[2,0]], 4, ['literal',[2,1]]] } },
      { id: 'boundaries', type: 'line', source: sourceId, 'source-layer': 'boundaries', filter: ['>','kind_detail',2], paint: { 'line-color': e.boundaries, 'line-width': 0.4, 'line-dasharray': ['step', ['zoom'], ['literal',[2,0]], 4, ['literal',[2,1]]] } },
      // Bridges
      { id: 'roads_bridges_other_casing', type: 'line', source: sourceId, 'source-layer': 'roads', minzoom: 12, filter: ['all',['has','is_bridge'],['in','kind','other','path']], paint: { 'line-color': e.bridges_other_casing, 'line-gap-width': ['interpolate',['exponential',1.6],['zoom'],14,0,20,7] } },
      { id: 'roads_bridges_link_casing', type: 'line', source: sourceId, 'source-layer': 'roads', minzoom: 12, filter: ['all',['has','is_bridge'],['has','is_link']], paint: { 'line-color': e.bridges_minor_casing, 'line-gap-width': ['interpolate',['exponential',1.6],['zoom'],13,0,13.5,1,18,11], 'line-width': ['interpolate',['exponential',1.6],['zoom'],12,0,12.5,1.5] } },
      { id: 'roads_bridges_minor_casing', type: 'line', source: sourceId, 'source-layer': 'roads', minzoom: 12, filter: ['all',['has','is_bridge'],['==','kind','minor_road']], paint: { 'line-color': e.bridges_minor_casing, 'line-gap-width': ['interpolate',['exponential',1.6],['zoom'],11,0,12.5,0.5,15,2,18,11], 'line-width': ['interpolate',['exponential',1.6],['zoom'],13,0,13.5,0.8] } },
      { id: 'roads_bridges_major_casing', type: 'line', source: sourceId, 'source-layer': 'roads', minzoom: 12, filter: ['all',['has','is_bridge'],['==','kind','major_road']], paint: { 'line-color': e.bridges_major_casing, 'line-gap-width': ['interpolate',['exponential',1.6],['zoom'],7,0,7.5,0.5,18,10], 'line-width': ['interpolate',['exponential',1.6],['zoom'],9,0,9.5,1.5] } },
      { id: 'roads_bridges_other', type: 'line', source: sourceId, 'source-layer': 'roads', minzoom: 12, filter: ['all',['has','is_bridge'],['in','kind','other','path']], paint: { 'line-color': e.bridges_other, 'line-dasharray': [2,1], 'line-width': ['interpolate',['exponential',1.6],['zoom'],14,0,20,7] } },
      { id: 'roads_bridges_minor', type: 'line', source: sourceId, 'source-layer': 'roads', minzoom: 12, filter: ['all',['has','is_bridge'],['==','kind','minor_road']], paint: { 'line-color': e.bridges_minor, 'line-width': ['interpolate',['exponential',1.6],['zoom'],11,0,12.5,0.5,15,2,18,11] } },
      { id: 'roads_bridges_link', type: 'line', source: sourceId, 'source-layer': 'roads', minzoom: 12, filter: ['all',['has','is_bridge'],['has','is_link']], paint: { 'line-color': e.bridges_minor, 'line-width': ['interpolate',['exponential',1.6],['zoom'],13,0,13.5,1,18,11] } },
      { id: 'roads_bridges_major', type: 'line', source: sourceId, 'source-layer': 'roads', minzoom: 12, filter: ['all',['has','is_bridge'],['==','kind','major_road']], paint: { 'line-color': e.bridges_major, 'line-width': ['interpolate',['exponential',1.6],['zoom'],6,0,12,1.6,15,3,18,13] } },
      { id: 'roads_bridges_highway_casing', type: 'line', source: sourceId, 'source-layer': 'roads', minzoom: 12, filter: ['all',['has','is_bridge'],['==','kind','highway'],['!has','is_link']], paint: { 'line-color': e.bridges_highway_casing, 'line-gap-width': ['interpolate',['exponential',1.6],['zoom'],3,0,3.5,0.5,18,15], 'line-width': ['interpolate',['exponential',1.6],['zoom'],7,0,7.5,1,20,15] } },
      { id: 'roads_bridges_highway', type: 'line', source: sourceId, 'source-layer': 'roads', filter: ['all',['has','is_bridge'],['==','kind','highway'],['!has','is_link']], paint: { 'line-color': e.bridges_highway, 'line-width': ['interpolate',['exponential',1.6],['zoom'],3,0,6,1.1,12,1.6,15,5,18,15] } }
    );
    return Z;
  }

  function buildLabelLayers(sourceId, e) {
    // Simplified labels with same palette colors; stick to FONT_STACK and English where available
    const textField = ['coalesce', ['get','name:en'], ['get','name']];
    return [
      { id: 'address_label', type: 'symbol', source: sourceId, 'source-layer': 'buildings', minzoom: 18, filter: ['==','kind','address'], layout: { 'symbol-placement': 'point', 'text-font': [FONT_STACK], 'text-field': ['get','addr_housenumber'], 'text-size': 12 }, paint: { 'text-color': e.address_label, 'text-halo-color': e.address_label_halo, 'text-halo-width': 1 } },
      { id: 'water_waterway_label', type: 'symbol', source: sourceId, 'source-layer': 'water', minzoom: 13, filter: ['in','kind','river','stream'], layout: { 'symbol-placement': 'line', 'text-font': [FONT_STACK], 'text-field': textField, 'text-size': 12, 'text-letter-spacing': 0.2 }, paint: { 'text-color': e.ocean_label, 'text-halo-color': e.water, 'text-halo-width': 1 } },
      { id: 'roads_oneway', type: 'symbol', source: sourceId, 'source-layer': 'roads', minzoom: 16, filter: ['==',['get','oneway'],'yes'], layout: { 'symbol-placement': 'line', 'icon-image': 'arrow', 'icon-rotate': 90, 'symbol-spacing': 100 } },
      { id: 'roads_labels_minor', type: 'symbol', source: sourceId, 'source-layer': 'roads', minzoom: 15, filter: ['in','kind','minor_road','other','path'], layout: { 'symbol-sort-key': ['get','min_zoom'], 'symbol-placement': 'line', 'text-font': [FONT_STACK], 'text-field': textField, 'text-size': 12 }, paint: { 'text-color': e.roads_label_minor, 'text-halo-color': e.roads_label_minor_halo, 'text-halo-width': 1 } },
      { id: 'water_label_ocean', type: 'symbol', source: sourceId, 'source-layer': 'water', filter: ['in','kind','sea','ocean','bay','strait','fjord'], layout: { 'text-font': [FONT_STACK], 'text-field': textField, 'text-size': ['interpolate',['linear'],['zoom'],3,10,10,12], 'text-letter-spacing': 0.1, 'text-max-width': 9, 'text-transform': 'uppercase' }, paint: { 'text-color': e.ocean_label, 'text-halo-width': 1, 'text-halo-color': e.water } },
      { id: 'earth_label_islands', type: 'symbol', source: sourceId, 'source-layer': 'earth', filter: ['in','kind','island'], layout: { 'text-font': [FONT_STACK], 'text-field': textField, 'text-size': 10, 'text-letter-spacing': 0.1, 'text-max-width': 8 }, paint: { 'text-color': e.subplace_label, 'text-halo-color': e.subplace_label_halo, 'text-halo-width': 1 } },
      { id: 'water_label_lakes', type: 'symbol', source: sourceId, 'source-layer': 'water', filter: ['in','kind','lake','water'], layout: { 'text-font': [FONT_STACK], 'text-field': textField, 'text-size': ['interpolate',['linear'],['zoom'],3,10,6,12,10,12], 'text-letter-spacing': 0.1, 'text-max-width': 9 }, paint: { 'text-color': e.ocean_label, 'text-halo-color': e.water, 'text-halo-width': 1 } },
      { id: 'roads_labels_major', type: 'symbol', source: sourceId, 'source-layer': 'roads', minzoom: 11, filter: ['in','kind','highway','major_road'], layout: { 'symbol-sort-key': ['get','min_zoom'], 'symbol-placement': 'line', 'text-font': [FONT_STACK], 'text-field': textField, 'text-size': 12 }, paint: { 'text-color': e.roads_label_major, 'text-halo-color': e.roads_label_major_halo, 'text-halo-width': 1 } },
      { id: 'places_subplace', type: 'symbol', source: sourceId, 'source-layer': 'places', filter: ['in','kind','neighbourhood','macrohood'], layout: { 'symbol-sort-key': ['case',['has','sort_key'],['get','sort_key'],['get','min_zoom']], 'text-field': textField, 'text-font': [FONT_STACK], 'text-max-width': 7, 'text-letter-spacing': 0.1, 'text-padding': ['interpolate',['linear'],['zoom'],5,2,8,4,12,18,15,20], 'text-size': ['interpolate',['exponential',1.2],['zoom'],11,8,14,14,18,24], 'text-transform': 'uppercase' }, paint: { 'text-color': e.subplace_label, 'text-halo-color': e.subplace_label_halo, 'text-halo-width': 1 } },
      { id: 'places_region', type: 'symbol', source: sourceId, 'source-layer': 'places', filter: ['==','kind','region'], layout: { 'symbol-sort-key': ['get','sort_key'], 'text-field': textField, 'text-font': [FONT_STACK], 'text-size': ['interpolate',['linear'],['zoom'],3,11,7,16], 'text-radial-offset': 0.2, 'text-anchor': 'center', 'text-transform': 'uppercase' }, paint: { 'text-color': e.state_label, 'text-halo-color': e.state_label_halo, 'text-halo-width': 1 } },
      { id: 'places_locality', type: 'symbol', source: sourceId, 'source-layer': 'places', filter: ['==','kind','locality'], layout: { 'icon-image': ['step',['zoom'], ['case',['==',['get','capital'],'yes'],'capital','townspot'], 8, '' ], 'icon-size': 0.7, 'text-field': textField, 'text-font': [FONT_STACK], 'symbol-sort-key': ['case',['has','sort_key'],['get','sort_key'],['get','min_zoom']], 'text-padding': ['interpolate',['linear'],['zoom'],5,3,8,7,12,11], 'text-size': ['interpolate',['linear'],['zoom'],2,8,4,10,6,11,8,11,10,12,15,12], 'icon-padding': ['interpolate',['linear'],['zoom'],0,0,8,4,10,8,12,6,22,2], 'text-justify': 'auto', 'text-variable-anchor': ['step',['zoom'], ['literal',['bottom','left','right','top']], 8, ['literal',['center']] ], 'text-radial-offset': 0.3 }, paint: { 'text-color': e.city_label, 'text-halo-color': e.city_label_halo, 'text-halo-width': 1 } },
      { id: 'places_country', type: 'symbol', source: sourceId, 'source-layer': 'places', filter: ['==','kind','country'], layout: { 'symbol-sort-key': ['case',['has','sort_key'],['get','sort_key'],['get','min_zoom']], 'text-field': textField, 'text-font': [FONT_STACK], 'text-size': ['interpolate',['linear'],['zoom'],2,8,6,10,8,11], 'icon-padding': ['interpolate',['linear'],['zoom'],0,2,14,2,16,20,17,2,22,2], 'text-transform': 'uppercase' }, paint: { 'text-color': e.country_label, 'text-halo-color': e.earth, 'text-halo-width': 1 } }
    ];
  }

  function createMinimalDarkStyle(pmtilesUrl, includeLabels) {
    const palette = getDarkPalette();
    const layers = [
      ...buildBaseLayers('basemap', palette),
      ...(includeLabels ? buildLabelLayers('basemap', palette) : []),
      // Live track & position styled per CSS highlight
      { id: 'track-casing', type: 'line', source: 'track', layout: { 'line-join': 'round', 'line-cap': 'round' }, paint: { 'line-color': '#050505', 'line-width': 7, 'line-opacity': 0.8 } },
      { id: 'track-line', type: 'line', source: 'track', layout: { 'line-join': 'round', 'line-cap': 'round' }, paint: { 'line-color': 'orange', 'line-width': 4, 'line-opacity': 0.95 } },
      ...positionLayers('orange')
    ];

    return {
      version: 8,
      ...(includeLabels ? { glyphs: '/static/vendor/fonts/{fontstack}/{range}.pbf' } : {}),
      sources: {
        basemap: { type: 'vector', url: `pmtiles://${pmtilesUrl}` },
        track: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
        pos: { type: 'geojson', data: positionFeature(0, 0, false, 0) }
      },
      layers
    };
  }

  function createMinimalLightStyle(pmtilesUrl, includeLabels) {
    const palette = getLightPalette();
    const layers = [
      ...buildBaseLayers('basemap', palette),
      ...(includeLabels ? buildLabelLayers('basemap', palette) : []),
      { id: 'track-casing', type: 'line', source: 'track', layout: { 'line-join': 'round', 'line-cap': 'round' }, paint: { 'line-color': '#050505', 'line-width': 7, 'line-opacity': 0.8 } },
      { id: 'track-line', type: 'line', source: 'track', layout: { 'line-join': 'round', 'line-cap': 'round' }, paint: { 'line-color': 'orange', 'line-width': 4, 'line-opacity': 0.95 } },
      ...positionLayers('orange')
    ];

    return {
      version: 8,
      ...(includeLabels ? { glyphs: '/static/vendor/fonts/{fontstack}/{range}.pbf' } : {}),
      sources: {
        basemap: { type: 'vector', url: `pmtiles://${pmtilesUrl}` },
        track: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
        pos: { type: 'geojson', data: positionFeature(0, 0, false, 0) }
      },
      layers
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
        pos: { type: 'geojson', data: positionFeature(0, 0, false, 0) }
      },
      layers: [
        { id: 'bg', type: 'background', paint: { 'background-color': '#0a0b0f' } },
        { id: 'osm-raster', type: 'raster', source: 'osm' },
        { id: 'track-casing', type: 'line', source: 'track', layout: { 'line-join': 'round', 'line-cap': 'round' }, paint: { 'line-color': '#050505', 'line-width': 7, 'line-opacity': 0.8 } },
        { id: 'track-line', type: 'line', source: 'track', layout: { 'line-join': 'round', 'line-cap': 'round' }, paint: { 'line-color': '#ff7a00', 'line-width': 4, 'line-opacity': 0.95 } },
        ...positionLayers('#1e90ff')
      ]
    };
  }

  // Raster OSM + 3D terrain using Terrarium DEM + hillshade
  function createTerrainRasterStyle() {
    return {
      version: 8,
      sources: {
        osm: {
          type: 'raster',
          tiles: [
            // Base raster; use a proper, policy-compliant provider for production
            'https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020_3857/default/GoogleMapsCompatible/{z}/{y}/{x}.jpg'
          ],
          tileSize: 256,
          attribution: '© OpenStreetMap contributors'
        },
        // Terrarium DEM (Mapzen on AWS Open Data). Online only.
        dem: {
          type: 'raster-dem',
          tiles: ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'],
          encoding: 'terrarium',
          tileSize: 256,
          maxzoom: 15,
          attribution: 'Terrain © Mapzen, AWS; data from SRTM/other sources'
        },
        track: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
        pos: { type: 'geojson', data: positionFeature(0, 0, false, 0) }
      },
      terrain: {
        source: 'dem',
        exaggeration: 1
      },
      layers: [
        { id: 'bg', type: 'background', paint: { 'background-color': '#0a0b0f' } },
        { id: 'osm-raster', type: 'raster', source: 'osm' },
        // Hillshade computed client-side from DEM (overlay above raster)
        { id: 'hillshade', type: 'hillshade', source: 'dem',
          paint: {
            'hillshade-exaggeration': 0.35,
            'hillshade-highlight-color': 'rgba(255,255,255,0.20)',
            'hillshade-shadow-color': 'rgba(0,0,0,0.30)'
          }
        },
        { id: 'track-casing', type: 'line', source: 'track', layout: { 'line-join': 'round', 'line-cap': 'round' }, paint: { 'line-color': '#050505', 'line-width': 7, 'line-opacity': 0.8 } },
        { id: 'track-line', type: 'line', source: 'track', layout: { 'line-join': 'round', 'line-cap': 'round' }, paint: { 'line-color': '#ff7a00', 'line-width': 4, 'line-opacity': 0.95 } },
        ...positionLayers('#1e90ff')
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
    if (v === BASEMAPS.VECTOR_DARK || v === BASEMAPS.VECTOR_LIGHT || v === BASEMAPS.RASTER_OSM || v === BASEMAPS.TERRAIN_3D || v === BASEMAPS.AUTO) return v;
    return BASEMAPS.AUTO;
  }

  async function chooseStyle(choice) {
    const useOffline = wantOfflineTiles();
    const includeLabels = await glyphsAvailable();
    if (choice === BASEMAPS.RASTER_OSM) {
      return createRasterFallbackStyle();
    }
    if (choice === BASEMAPS.TERRAIN_3D) {
      // Online-only; falls back to OSM raster if terrain source unavailable
      return createTerrainRasterStyle();
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
    ensurePositionArrowImage();
    posSource = map.getSource('pos');
    trackSource = map.getSource('track');
    routeSource = map.getSource('route');
    if (posSource && isFinite(lastLon) && isFinite(lastLat)) {
      posSource.setData(positionFeature(lastLon, lastLat));
    }
    if (trackSource && coords.length) {
      trackSource.setData({ type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'LineString', coordinates: coords.slice() } }] });
    }
    if (routeSource && routeCoords && routeCoords.length) {
      routeSource.setData({ type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'LineString', coordinates: routeCoords.slice() } }] });
      ensureRouteLayer();
    }
    if (routeProfile.points.length) {
      updateRouteProfileCursorOnMap();
    }
    updateRouteCurrentOnMap(routeProgress);
  }

  function trackFeatureCollection() {
    return {
      type: 'FeatureCollection',
      features: [{
        type: 'Feature',
        geometry: { type: 'LineString', coordinates: coords.slice() }
      }]
    };
  }

  function refreshTrackSource() {
    if (trackSource) trackSource.setData(trackFeatureCollection());
  }

  function sameCoord(a, b) {
    if (!a || !b) return false;
    return Math.abs(a[0] - b[0]) < 0.0000001 && Math.abs(a[1] - b[1]) < 0.0000001;
  }

  function appendTrackCoord(coord) {
    const last = coords[coords.length - 1];
    if (!sameCoord(last, coord)) coords.push(coord);
    while (coords.length > MAX_POINTS) coords.shift();
  }

  function placeRouteControlsInMapToolbar() {
    const toolbar = document.querySelector('.map-controls-top');
    const bottomControls = document.querySelector('.map-controls-bottom');
    if (!toolbar) return;

    let routeControls = toolbar.querySelector('.map-route-controls');
    if (!routeControls) {
      routeControls = document.createElement('div');
      routeControls.className = 'map-route-controls';
      toolbar.appendChild(routeControls);
    }

    [
      document.getElementById('gpx-file-input'),
      document.getElementById('gpx-profile-toggle'),
      document.getElementById('gpx-upload-btn'),
      document.getElementById('gpx-erase-btn')
    ].forEach((control) => {
      if (control) routeControls.appendChild(control);
    });

    if (bottomControls && bottomControls.children.length === 0) {
      bottomControls.remove();
    }
  }

  async function loadSessionTrackFromServer() {
    try {
      const res = await fetch(`/session_track?samples=${MAX_POINTS}`, { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      const points = Array.isArray(data.points) ? data.points : [];
      const loaded = [];
      points.forEach((point) => {
        const lon = Number(point.lon);
        const lat = Number(point.lat);
        if (isFinite(lon) && isFinite(lat)) loaded.push([lon, lat]);
      });
      if (!loaded.length) return;

      coords.length = 0;
      loaded.slice(-MAX_POINTS).forEach((coord) => coords.push(coord));
      const last = coords[coords.length - 1];
      lastLon = last[0];
      lastLat = last[1];
      updatePositionSource(lastLon, lastLat, false, lastGpsBearing);
      refreshTrackSource();
      updateRouteProgressFromGps(lastLon, lastLat);
    } catch (e) {
      // ignore
    }
  }

  async function switchBasemap(choice) {
    const style = await chooseStyle(choice);
    map.setStyle(style);
    map.once('styledata', () => {
      rebindSourcesAndRefresh();
      // Give a hint of 3D when terrain basemap is chosen
      if (choice === BASEMAPS.TERRAIN_3D) {
        try { map.easeTo({ pitch: 55, duration: 600 }); } catch {}
      }
    });
  }

  async function initMap() {
    const container = document.getElementById('live-map');
    if (!container) return;
    placeRouteControlsInMapToolbar();

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
      maxPitch: 85,
      antialias: false,
      attributionControl: false,
    });

    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right');

    map.on('styleimagemissing', (event) => {
      if (event && event.id === 'position-arrow') ensurePositionArrowImage();
    });

    map.on('load', () => {
      ensurePositionArrowImage();
      posSource = map.getSource('pos');
      trackSource = map.getSource('track');
      routeSource = map.getSource('route');
      loadSessionTrackFromServer();
      // Attempt loading persisted GPX route, if any
      loadRouteFromServer();
      if (initialChoice === BASEMAPS.TERRAIN_3D) {
        try { map.easeTo({ pitch: 55, duration: 600 }); } catch {}
      }
      // On initial load, if GPS has a fresh fix, center the map once
      initialFocusOnGps();
    });

    // Follow toggle button
    const btn = document.getElementById('map-follow-toggle');
    if (btn) btn.addEventListener('click', toggleFollow);
    setFollowButton();

    // Heading toggle button
    const hb = document.getElementById('map-heading-toggle');
    const savedHeadingMode = localStorage.getItem('heading_mode');
    if (savedHeadingMode && Object.values(HEADING_MODES).includes(savedHeadingMode)) {
      headingMode = savedHeadingMode;
    }
    if (hb) hb.addEventListener('click', cycleHeadingMode);
    setHeadingButton();

    const nb = document.getElementById('map-navigation-btn');
    if (nb) nb.addEventListener('click', focusNavigationFromGps);

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
      setPiAltitudeFromStatus(s);
      if (!s || !s.has_fix || s.stale) return;
      const lon = Number(s.lon), lat = Number(s.lat);
      if (!isFinite(lon) || !isFinite(lat)) return;

      appendTrackCoord([lon, lat]);

      lastLon = lon; lastLat = lat;
      updateRouteProgressFromGps(lon, lat);

      const speedKph = finiteNumberOrNull(s.speed_kph);
      const moving = speedKph !== null && speedKph >= MIN_SPEED_KPH;
      const gpsTrack = finiteNumberOrNull(s.track_deg);
      let targetBearing = null;
      if (moving && gpsTrack !== null) {
        targetBearing = normalizeBearing(gpsTrack);
      } else if (moving && coords.length >= 2) {
        const [plon, plat] = coords[coords.length - 2];
        const [clon, clat] = coords[coords.length - 1];
        targetBearing = computeBearing(plon, plat, clon, clat);
      }
      updatePositionSource(lon, lat, targetBearing !== null, targetBearing === null ? lastGpsBearing : targetBearing);

      if (trackSource) {
        refreshTrackSource();
      }

      // Determine target bearing if in trajectory mode and moving sufficiently
      let applyBearing = null;
      if (headingMode === HEADING_MODES.NORTH) {
        applyBearing = 0;
      } else if (headingMode === HEADING_MODES.TRAJECTORY) {
        if (targetBearing !== null) {
          if (!bearingInitialized) {
            filteredBearing = normalizeBearing(map.getBearing());
            bearingInitialized = true;
          }
          const delta = shortestAngleDelta(filteredBearing, targetBearing);
          const alpha = 0.25; // smoothing factor
          filteredBearing = normalizeBearing(filteredBearing + delta * alpha);
          applyBearing = filteredBearing;
        }
      }

      if (map) {
        const opts = { duration: 500 };
        if (follow) {
          const z = Math.max(8, Math.min(18, map.getZoom()));
          opts.center = [lon, lat];
          opts.zoom = z;
        }
        if (applyBearing !== null) {
          opts.bearing = applyBearing;
        }
        if (opts.center || opts.bearing !== undefined) {
          map.easeTo(opts);
        }
      }
    } catch (e) {
      // ignore
    }
  }

  async function focusNavigationFromGps() {
    setFollow(true);
    setHeadingMode(HEADING_MODES.TRAJECTORY);
    try {
      const res = await fetch('/gps_status', { cache: 'no-store' });
      const s = await res.json();
      setPiAltitudeFromStatus(s);
      if (!s || !s.has_fix || s.stale) return;
      const lon = Number(s.lon), lat = Number(s.lat);
      if (!isFinite(lon) || !isFinite(lat)) return;

      lastLon = lon;
      lastLat = lat;
      const motion = gpsMotionFromStatus(s);
      updatePositionSource(lon, lat, motion.moving, motion.bearing);
      applyNavigationView(lon, lat, {
        duration: 500,
        pitch: NAVIGATION_BUTTON_PITCH,
        bearing: motion.moving ? motion.bearing : null
      });
    } catch (e) {
      // ignore
    }
  }

  // One-time focus on current GPS position after map load
  async function initialFocusOnGps() {
    try {
      const res = await fetch('/gps_status', { cache: 'no-store' });
      const s = await res.json();
      setPiAltitudeFromStatus(s);
      if (!s || !s.has_fix || s.stale) return;
      const lon = Number(s.lon), lat = Number(s.lat);
      if (!isFinite(lon) || !isFinite(lat)) return;
      lastLon = lon;
      lastLat = lat;
      const motion = gpsMotionFromStatus(s);
      updatePositionSource(lon, lat, motion.moving, motion.bearing);
      applyNavigationView(lon, lat, {
        duration: 500,
        bearing: motion.moving ? motion.bearing : null
      });
    } catch (e) {
      // ignore
    }
  }

  window.addEventListener('DOMContentLoaded', initMap);

  // ===== GPX handling and UI (dashboard) =====
  function getChildrenByLocalName(parent, localName) {
    const direct = Array.from(parent.getElementsByTagName(localName));
    const namespaced = Array.from(parent.getElementsByTagNameNS('*', localName));
    return Array.from(new Set([...direct, ...namespaced]));
  }

  function firstChildText(parent, localName) {
    const child = getChildrenByLocalName(parent, localName)[0];
    return child ? child.textContent : null;
  }

  function parseGpxToRoutePoints(xmlText) {
    try {
      const doc = new DOMParser().parseFromString(xmlText, 'application/xml');
      const parseError = getChildrenByLocalName(doc, 'parsererror')[0];
      if (parseError) return [];

      let nodes = getChildrenByLocalName(doc, 'trkpt');
      if (!nodes.length) nodes = getChildrenByLocalName(doc, 'rtept');
      if (!nodes.length) nodes = getChildrenByLocalName(doc, 'wpt');

      const points = nodes.map((p) => {
        const lon = Number(p.getAttribute('lon'));
        const lat = Number(p.getAttribute('lat'));
        const eleText = firstChildText(p, 'ele');
        const ele = eleText == null || eleText === '' ? null : Number(eleText);
        return {
          lon,
          lat,
          ele: isFinite(ele) ? ele : null
        };
      }).filter(p => isFinite(p.lon) && isFinite(p.lat));
      return points;
    } catch (e) {
      return [];
    }
  }

  function haversineKm(a, b) {
    const toRad = (d) => d * Math.PI / 180;
    const lat1 = toRad(a.lat), lat2 = toRad(b.lat);
    const dlat = lat2 - lat1;
    const dlon = toRad(b.lon - a.lon);
    const h = Math.sin(dlat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dlon / 2) ** 2;
    return 6371.0088 * 2 * Math.asin(Math.min(1, Math.sqrt(h)));
  }

  function buildRouteProfile(points) {
    let distanceKm = 0;
    const withDistance = points.map((point, index) => {
      if (index > 0) distanceKm += haversineKm(points[index - 1], point);
      return { ...point, distanceKm, gradePct: 0 };
    });

    withDistance.forEach((point, index) => {
      if (point.ele == null) return;
      let before = index;
      let after = index;
      while (before > 0 && point.distanceKm - withDistance[before].distanceKm < 0.04) before -= 1;
      while (after < withDistance.length - 1 && withDistance[after].distanceKm - point.distanceKm < 0.04) after += 1;
      const a = withDistance[before];
      const b = withDistance[after];
      if (!a || !b || a.ele == null || b.ele == null || b.distanceKm <= a.distanceKm) return;
      point.gradePct = ((b.ele - a.ele) / ((b.distanceKm - a.distanceKm) * 1000)) * 100;
    });

    return withDistance.filter(p => p.ele != null);
  }

  function buildRouteDistancePoints(points) {
    let distanceKm = 0;
    return points.map((point, index) => {
      if (index > 0) distanceKm += haversineKm(points[index - 1], point);
      return { ...point, distanceKm };
    });
  }

  function nearestRouteProgress(lon, lat) {
    if (!routeDistancePoints.length) return null;
    if (routeDistancePoints.length === 1) {
      const only = routeDistancePoints[0];
      const distanceToRouteM = haversineKm({ lon, lat }, only) * 1000;
      if (distanceToRouteM > ROUTE_SNAP_MAX_METERS) return null;
      return { lon: only.lon, lat: only.lat, alongKm: 0, remainingKm: 0, distanceToRouteM };
    }

    const metersPerLat = 111320;
    const metersPerLon = Math.max(1, 111320 * Math.cos(lat * Math.PI / 180));
    let best = null;

    for (let i = 1; i < routeDistancePoints.length; i++) {
      const a = routeDistancePoints[i - 1];
      const b = routeDistancePoints[i];
      const ax = (a.lon - lon) * metersPerLon;
      const ay = (a.lat - lat) * metersPerLat;
      const bx = (b.lon - lon) * metersPerLon;
      const by = (b.lat - lat) * metersPerLat;
      const dx = bx - ax;
      const dy = by - ay;
      const lenSq = dx * dx + dy * dy;
      if (lenSq <= 0.0001) continue;
      const t = Math.max(0, Math.min(1, -(ax * dx + ay * dy) / lenSq));
      const px = ax + dx * t;
      const py = ay + dy * t;
      const distanceToRouteM = Math.sqrt(px * px + py * py);
      const segmentKm = Math.max(0, b.distanceKm - a.distanceKm);
      const alongKm = a.distanceKm + segmentKm * t;
      const candidate = {
        lon: lon + px / metersPerLon,
        lat: lat + py / metersPerLat,
        alongKm,
        remainingKm: Math.max(0, routeDistancePoints[routeDistancePoints.length - 1].distanceKm - alongKm),
        distanceToRouteM
      };
      if (!best || candidate.distanceToRouteM < best.distanceToRouteM) best = candidate;
    }

    if (!best || best.distanceToRouteM > ROUTE_SNAP_MAX_METERS) return null;
    return best;
  }

  function updateRouteProgressFromGps(lon, lat) {
    const progress = nearestRouteProgress(lon, lat);
    setRouteProgress(progress);
    updateRouteCurrentOnMap(progress);
    renderRouteProfileChart();
  }

  function resetRouteProfileZoom() {
    const last = routeProfile.points[routeProfile.points.length - 1];
    routeProfile.zoomStartKm = 0;
    routeProfile.zoomEndKm = last ? Math.max(0, last.distanceKm) : 0;
  }

  function gradeColor(grade) {
    const raw = Number(grade) || 0;
    if (raw <= PROFILE_REGEN_START_PCT) {
      const t = Math.max(0, Math.min(1, (PROFILE_REGEN_START_PCT - raw) / (PROFILE_REGEN_START_PCT - PROFILE_REGEN_FULL_PCT)));
      const r = Math.round(125 + (7 - 125) * t);
      const ge = Math.round(211 + (89 - 211) * t);
      const b = Math.round(252 + (133 - 252) * t);
      return `rgb(${r},${ge},${b})`;
    }
    const g = Math.max(0, Math.min(14, raw));
    if (g <= 6) {
      const t = g / 6;
      const r = Math.round(47 + (245 - 47) * t);
      const ge = Math.round(158 + (159 - 158) * t);
      const b = Math.round(68 + (0 - 68) * t);
      return `rgb(${r},${ge},${b})`;
    }
    const t = (g - 6) / 8;
    const r = Math.round(245 + (217 - 245) * t);
    const ge = Math.round(159 + (72 - 159) * t);
    const b = Math.round(0 + (15 - 0) * t);
    return `rgb(${r},${ge},${b})`;
  }

  function gradeFillColor(grade) {
    return gradeColor(grade).replace('rgb(', 'rgba(').replace(')', ',0.24)');
  }

  function gradeZoneKey(grade) {
    const g = Number(grade) || 0;
    if (g <= PROFILE_REGEN_START_PCT) {
      return `regen-${Math.min(5, Math.floor((Math.abs(g) - Math.abs(PROFILE_REGEN_START_PCT)) / 2))}`;
    }
    return `climb-${Math.min(7, Math.floor(Math.max(0, g) / 2))}`;
  }

  function nearestProfileIndexForDistance(distanceKm) {
    const points = routeProfile.points;
    if (!points.length) return null;
    let lo = 0, hi = points.length - 1;
    while (lo < hi) {
      const mid = Math.floor((lo + hi) / 2);
      if (points[mid].distanceKm < distanceKm) lo = mid + 1;
      else hi = mid;
    }
    if (lo > 0 && Math.abs(points[lo - 1].distanceKm - distanceKm) < Math.abs(points[lo].distanceKm - distanceKm)) {
      return lo - 1;
    }
    return lo;
  }

  function interpolateProfileAt(distanceKm) {
    const points = routeProfile.points;
    if (!points.length) return null;
    const km = Math.max(points[0].distanceKm, Math.min(points[points.length - 1].distanceKm, Number(distanceKm) || 0));
    if (km <= points[0].distanceKm) return { ...points[0], distanceKm: km };
    for (let i = 1; i < points.length; i++) {
      const a = points[i - 1];
      const b = points[i];
      if (km <= b.distanceKm) {
        const span = Math.max(0.000001, b.distanceKm - a.distanceKm);
        const t = (km - a.distanceKm) / span;
        return {
          ...b,
          distanceKm: km,
          ele: a.ele + (b.ele - a.ele) * t,
          gradePct: a.gradePct + ((b.gradePct || 0) - (a.gradePct || 0)) * t
        };
      }
    }
    return { ...points[points.length - 1], distanceKm: km };
  }

  function profileSliceBetween(startKm, endKm) {
    const points = routeProfile.points;
    if (!points.length || endKm <= startKm) return [];
    const slice = [];
    const start = interpolateProfileAt(startKm);
    if (start) slice.push(start);
    points.forEach((point) => {
      if (point.distanceKm > startKm && point.distanceKm < endKm) slice.push(point);
    });
    const end = interpolateProfileAt(endKm);
    if (end && (!slice.length || Math.abs(slice[slice.length - 1].distanceKm - end.distanceKm) > 0.00001)) {
      slice.push(end);
    }
    return slice;
  }

  function elevationStatsBetween(startKm, endKm) {
    const slice = profileSliceBetween(startKm, endKm);
    let uphill = 0;
    let downhill = 0;
    for (let i = 1; i < slice.length; i++) {
      const diff = slice[i].ele - slice[i - 1].ele;
      if (diff > 0) uphill += diff;
      else downhill += Math.abs(diff);
    }
    return { uphill, downhill };
  }

  function getCurrentRouteSlopeSegment() {
    const points = routeProfile.points;
    if (!points.length || !routeProgress) return null;
    const currentKm = Math.max(points[0].distanceKm, Math.min(points[points.length - 1].distanceKm, Number(routeProgress.alongKm) || 0));
    const currentIndex = nearestProfileIndexForDistance(currentKm);
    const currentPoint = currentIndex == null ? null : points[currentIndex];
    const currentGrade = Number(currentPoint && currentPoint.gradePct) || 0;
    if (Math.abs(currentGrade) < PROFILE_SLOPE_THRESHOLD_PCT) return null;

    const direction = currentGrade >= 0 ? 1 : -1;
    const qualifies = (point) => direction * (Number(point && point.gradePct) || 0) >= PROFILE_SLOPE_THRESHOLD_PCT;
    let startIndex = currentIndex;
    let endIndex = currentIndex;
    while (startIndex > 0 && qualifies(points[startIndex - 1])) startIndex -= 1;
    while (endIndex < points.length - 1 && qualifies(points[endIndex + 1])) endIndex += 1;

    const startKm = points[startIndex].distanceKm;
    const endKm = points[endIndex].distanceKm;
    const totalKm = Math.max(0, endKm - startKm);
    if (totalKm < PROFILE_SLOPE_MIN_KM) return null;

    const remainingStartKm = Math.max(startKm, Math.min(endKm, currentKm));
    const remainingKm = Math.max(0, endKm - remainingStartKm);
    const stats = elevationStatsBetween(remainingStartKm, endKm);
    return {
      startKm,
      endKm,
      currentKm,
      totalKm,
      remainingKm,
      uphill: stats.uphill,
      downhill: stats.downhill,
      direction,
      gradePct: currentGrade
    };
  }

  function updateCurrentSlopeZoomUi() {
    const button = document.getElementById('gpx-profile-current-slope');
    const info = document.getElementById('gpx-profile-slope-info');
    const segment = getCurrentRouteSlopeSegment();
    if (button) {
      button.disabled = !segment;
      button.title = segment ? 'Zoom to current climb/descent' : 'No current slope over 500 m and +/-2%';
    }
    if (!info) return;
    if (!routeProfile.points.length) {
      info.textContent = 'No profile';
      return;
    }
    if (!routeProgress) {
      info.textContent = 'Waiting for route position';
      return;
    }
    if (!segment) {
      info.textContent = 'No slope >500 m';
      return;
    }
    const elevationText = segment.direction > 0
      ? `D+ ${Math.round(segment.uphill)} m`
      : `D- ${Math.round(segment.downhill)} m`;
    info.textContent = `${formatDistanceKm(segment.remainingKm)} left · ${elevationText}`;
  }

  function focusCurrentRouteSlope() {
    const segment = getCurrentRouteSlopeSegment();
    const points = routeProfile.points;
    if (!segment || !points.length) return;
    const totalEnd = points[points.length - 1].distanceKm;
    const pad = Math.max(PROFILE_SLOPE_ZOOM_PADDING_KM, segment.totalKm * 0.12);
    let start = Math.max(0, segment.startKm - pad);
    let end = Math.min(totalEnd, segment.endKm + pad);
    if (end - start < PROFILE_MIN_WINDOW_KM) {
      const center = (start + end) / 2;
      start = Math.max(0, center - PROFILE_MIN_WINDOW_KM / 2);
      end = Math.min(totalEnd, start + PROFILE_MIN_WINDOW_KM);
      start = Math.max(0, end - PROFILE_MIN_WINDOW_KM);
    }
    routeProfile.zoomStartKm = start;
    routeProfile.zoomEndKm = end;
    routeProfile.cursorIndex = nearestProfileIndexForDistance(segment.currentKm);
    updateProfileCursorLabel();
    updateRouteProfileCursorOnMap();
    renderRouteProfileChart();
  }

  function updateRouteProfileSummary() {
    const summary = document.getElementById('gpx-profile-summary');
    if (!summary) return;
    const points = routeProfile.points;
    if (!points.length) {
      summary.textContent = 'No elevation data in GPX';
      return;
    }
    const last = points[points.length - 1];
    const minEle = Math.min(...points.map(p => p.ele));
    const maxEle = Math.max(...points.map(p => p.ele));
    let anchor = points[0].ele;
    let uphill = 0;
    let downhill = 0;
    points.slice(1).forEach((point) => {
      const diff = point.ele - anchor;
      if (diff >= PROFILE_ELEVATION_DEADBAND_M) {
        uphill += diff;
        anchor = point.ele;
      } else if (diff <= -PROFILE_ELEVATION_DEADBAND_M) {
        downhill += Math.abs(diff);
        anchor = point.ele;
      }
    });
    summary.textContent = `${last.distanceKm.toFixed(1)} km · D+ ${Math.round(uphill)} m · D- ${Math.round(downhill)} m · ${Math.round(minEle)}-${Math.round(maxEle)} m`;
  }

  function setRouteProfileAvailability(points) {
    const toggle = document.getElementById('gpx-profile-toggle');
    const panel = document.getElementById('gpx-profile-panel');
    routeProfile.points = points;
    routeProfile.cursorIndex = points.length ? 0 : null;
    resetRouteProfileZoom();
    updateRouteProfileSummary();
    updateProfileCursorLabel();
    updateCurrentSlopeZoomUi();
    updateRouteProfileCursorOnMap();

    if (toggle) {
      toggle.hidden = !points.length;
      toggle.classList.toggle('active-chart', routeProfile.visible && points.length);
    }
    if (panel) {
      panel.hidden = !routeProfile.visible || !points.length;
    }
    renderRouteProfileChart();
  }

  function routeProfileScales(width, height, margin) {
    const points = routeProfile.points;
    const visible = points.filter(p => p.distanceKm >= routeProfile.zoomStartKm && p.distanceKm <= routeProfile.zoomEndKm);
    const scoped = visible.length >= 2 ? visible : points;
    const minEleRaw = Math.min(...scoped.map(p => p.ele));
    const maxEleRaw = Math.max(...scoped.map(p => p.ele));
    const pad = Math.max(10, (maxEleRaw - minEleRaw) * 0.12);
    const minEle = Math.floor((minEleRaw - pad) / 10) * 10;
    const maxEle = Math.ceil((maxEleRaw + pad) / 10) * 10;
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const xForKm = (km) => margin.left + ((km - routeProfile.zoomStartKm) / Math.max(0.001, routeProfile.zoomEndKm - routeProfile.zoomStartKm)) * plotWidth;
    const yForEle = (ele) => margin.top + ((maxEle - ele) / Math.max(1, maxEle - minEle)) * plotHeight;
    return { scoped, minEle, maxEle, xForKm, yForEle };
  }

  function drawProfileFrame(ctx, width, height, margin, scales) {
    ctx.strokeStyle = '#c8c1ad';
    ctx.lineWidth = 1;
    ctx.fillStyle = '#5f594b';
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';

    for (let i = 0; i <= 4; i++) {
      const ele = scales.minEle + (i * (scales.maxEle - scales.minEle)) / 4;
      const y = scales.yForEle(ele);
      ctx.beginPath();
      ctx.moveTo(margin.left, y);
      ctx.lineTo(width - margin.right, y);
      ctx.stroke();
      ctx.fillText(`${Math.round(ele)} m`, margin.left - 6, y);
    }

    ctx.strokeStyle = '#88806f';
    ctx.beginPath();
    ctx.moveTo(margin.left, margin.top);
    ctx.lineTo(margin.left, height - margin.bottom);
    ctx.lineTo(width - margin.right, height - margin.bottom);
    ctx.stroke();

    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (let i = 0; i <= 4; i++) {
      const km = routeProfile.zoomStartKm + (i * (routeProfile.zoomEndKm - routeProfile.zoomStartKm)) / 4;
      const x = scales.xForKm(km);
      ctx.strokeStyle = '#88806f';
      ctx.beginPath();
      ctx.moveTo(x, height - margin.bottom);
      ctx.lineTo(x, height - margin.bottom + 4);
      ctx.stroke();
      ctx.fillText(`${km.toFixed(km < 10 ? 1 : 0)} km`, x, height - margin.bottom + 6);
    }
  }

  function drawProfileFillAreas(ctx, height, margin, scales, series) {
    if (!series || series.length < 2) return;
    const baselineY = height - margin.bottom;
    let groupStartIndex = 0;
    let groupKey = null;
    let groupColor = null;

    const flushGroup = (endIndex) => {
      if (endIndex <= groupStartIndex || !groupColor) return;
      const start = series[groupStartIndex];
      const end = series[endIndex];
      ctx.fillStyle = groupColor;
      ctx.beginPath();
      ctx.moveTo(scales.xForKm(start.distanceKm), baselineY);
      for (let i = groupStartIndex; i <= endIndex; i++) {
        ctx.lineTo(scales.xForKm(series[i].distanceKm), scales.yForEle(series[i].ele));
      }
      ctx.lineTo(scales.xForKm(end.distanceKm), baselineY);
      ctx.closePath();
      ctx.fill();
    };

    for (let i = 1; i < series.length; i++) {
      const grade = ((Number(series[i - 1].gradePct) || 0) + (Number(series[i].gradePct) || 0)) / 2;
      const key = gradeZoneKey(grade);
      if (groupKey === null) {
        groupKey = key;
        groupColor = gradeFillColor(grade);
      } else if (key !== groupKey) {
        flushGroup(i - 1);
        groupStartIndex = i - 1;
        groupKey = key;
        groupColor = gradeFillColor(grade);
      }
    }
    flushGroup(series.length - 1);
  }

  function renderRouteProfileChart() {
    const canvas = document.getElementById('gpx-elevation-profile');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    const width = Math.max(320, canvas.clientWidth || 320);
    const height = Math.max(150, canvas.clientHeight || 150);
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const points = routeProfile.points;
    if (!points.length) {
      ctx.fillStyle = '#666';
      ctx.font = '12px sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText('No GPX elevation data', width / 2, height / 2);
      return;
    }

    const margin = { top: 12, right: 14, bottom: 26, left: 48 };
    const scales = routeProfileScales(width, height, margin);

    const visible = points.filter(p => p.distanceKm >= routeProfile.zoomStartKm && p.distanceKm <= routeProfile.zoomEndKm);
    const series = visible.length >= 2 ? visible : points;
    drawProfileFillAreas(ctx, height, margin, scales, series);
    drawProfileFrame(ctx, width, height, margin, scales);

    ctx.lineWidth = 2.25;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    for (let i = 1; i < series.length; i++) {
      const a = series[i - 1];
      const b = series[i];
      ctx.strokeStyle = gradeColor((a.gradePct + b.gradePct) / 2);
      ctx.beginPath();
      ctx.moveTo(scales.xForKm(a.distanceKm), scales.yForEle(a.ele));
      ctx.lineTo(scales.xForKm(b.distanceKm), scales.yForEle(b.ele));
      ctx.stroke();
    }

    if (routeProfile.cursorIndex != null && points[routeProfile.cursorIndex]) {
      const cursor = points[routeProfile.cursorIndex];
      if (cursor.distanceKm >= routeProfile.zoomStartKm && cursor.distanceKm <= routeProfile.zoomEndKm) {
        const x = scales.xForKm(cursor.distanceKm);
        const y = scales.yForEle(cursor.ele);
        ctx.strokeStyle = '#111';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, margin.top);
        ctx.lineTo(x, height - margin.bottom);
        ctx.stroke();
        ctx.fillStyle = '#111';
        ctx.beginPath();
        ctx.arc(x, y, 4, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    if (routeProgress && routeProgress.alongKm >= routeProfile.zoomStartKm && routeProgress.alongKm <= routeProfile.zoomEndKm) {
      const currentIndex = nearestProfileIndexForDistance(routeProgress.alongKm);
      const currentPoint = currentIndex == null ? null : points[currentIndex];
      const x = scales.xForKm(routeProgress.alongKm);
      ctx.strokeStyle = '#1e90ff';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(x, margin.top);
      ctx.lineTo(x, height - margin.bottom);
      ctx.stroke();
      if (currentPoint) {
        ctx.fillStyle = '#1e90ff';
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(x, scales.yForEle(currentPoint.ele), 5, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      }
    }
  }

  function updateProfileCursorLabel() {
    const label = document.getElementById('gpx-profile-cursor');
    if (!label) return;
    const point = routeProfile.cursorIndex == null ? null : routeProfile.points[routeProfile.cursorIndex];
    if (!point) {
      label.textContent = 'Move over the profile for gradient';
      return;
    }
    const grade = Number(point.gradePct) || 0;
    label.textContent = `${point.distanceKm.toFixed(2)} km · ${Math.round(point.ele)} m · gradient ${grade.toFixed(1)}%`;
  }

  function setProfileCursorFromCanvasX(clientX) {
    const canvas = document.getElementById('gpx-elevation-profile');
    if (!canvas || !routeProfile.points.length) return;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(320, canvas.clientWidth || 320);
    const margin = { top: 12, right: 14, bottom: 26, left: 48 };
    const plotWidth = Math.max(1, width - margin.left - margin.right);
    const pct = Math.max(0, Math.min(1, (clientX - rect.left - margin.left) / plotWidth));
    const distanceKm = routeProfile.zoomStartKm + pct * (routeProfile.zoomEndKm - routeProfile.zoomStartKm);
    routeProfile.cursorIndex = nearestProfileIndexForDistance(distanceKm);
    updateProfileCursorLabel();
    updateRouteProfileCursorOnMap();
    renderRouteProfileChart();
  }

  function zoomRouteProfileByFactor(factor, focusPct = 0.5) {
    const points = routeProfile.points;
    if (!points.length) return;
    const totalEnd = points[points.length - 1].distanceKm;
    if (totalEnd <= PROFILE_MIN_WINDOW_KM) return;
    const focusKm = routeProfile.zoomStartKm + focusPct * (routeProfile.zoomEndKm - routeProfile.zoomStartKm);
    let windowKm = (routeProfile.zoomEndKm - routeProfile.zoomStartKm) * factor;
    windowKm = Math.max(PROFILE_MIN_WINDOW_KM, Math.min(totalEnd, windowKm));
    let start = focusKm - windowKm * focusPct;
    let end = start + windowKm;
    if (start < 0) { end -= start; start = 0; }
    if (end > totalEnd) { start -= end - totalEnd; end = totalEnd; }
    routeProfile.zoomStartKm = Math.max(0, start);
    routeProfile.zoomEndKm = Math.min(totalEnd, end);
    renderRouteProfileChart();
  }

  function routeProfileIsZoomed() {
    const points = routeProfile.points;
    const totalEnd = points.length ? points[points.length - 1].distanceKm : 0;
    return totalEnd > 0 && (routeProfile.zoomEndKm - routeProfile.zoomStartKm) < totalEnd - 0.01;
  }

  function panRouteProfileByPixels(deltaPx, canvasWidth) {
    const points = routeProfile.points;
    if (!points.length || !routeProfileIsZoomed()) return;
    const totalEnd = points[points.length - 1].distanceKm;
    const margin = { top: 12, right: 14, bottom: 26, left: 48 };
    const plotWidth = Math.max(1, (canvasWidth || 320) - margin.left - margin.right);
    const windowKm = routeProfile.zoomEndKm - routeProfile.zoomStartKm;
    let start = routeProfile.zoomStartKm - (deltaPx * windowKm / plotWidth);
    start = Math.max(0, Math.min(totalEnd - windowKm, start));
    routeProfile.zoomStartKm = start;
    routeProfile.zoomEndKm = start + windowKm;
    renderRouteProfileChart();
  }

  function zoomRouteProfile(deltaY, clientX) {
    const canvas = document.getElementById('gpx-elevation-profile');
    const rect = canvas ? canvas.getBoundingClientRect() : null;
    const focusPct = rect ? Math.max(0, Math.min(1, (clientX - rect.left) / Math.max(1, rect.width))) : 0.5;
    zoomRouteProfileByFactor(deltaY < 0 ? 0.78 : 1.28, focusPct);
  }

  function ensureRouteSource() {
    if (!map) return null;
    let src = map.getSource('route');
    if (!src) {
      map.addSource('route', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
      src = map.getSource('route');
    }
    routeSource = src;
    return src;
  }

  function ensureRouteCursorSource() {
    if (!map) return null;
    let src = map.getSource('route-cursor');
    if (!src) {
      map.addSource('route-cursor', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
      src = map.getSource('route-cursor');
    }
    return src;
  }

  function ensureRouteCurrentSource() {
    if (!map) return null;
    let src = map.getSource('route-current');
    if (!src) {
      map.addSource('route-current', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
      src = map.getSource('route-current');
    }
    return src;
  }

  function ensureRouteLayer() {
    if (!map) return;
    const beforeFollowedTrack = map.getLayer('track-casing') ? 'track-casing' : (map.getLayer('track-line') ? 'track-line' : undefined);
    if (!map.getLayer('route-casing')) {
      map.addLayer({
        id: 'route-casing', type: 'line', source: 'route',
        layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint: { 'line-color': '#050505', 'line-opacity': 0.45, 'line-width': 8 }
      }, beforeFollowedTrack);
    }
    if (!map.getLayer('route-line')) {
      map.addLayer({
        id: 'route-line', type: 'line', source: 'route',
        layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint: { 'line-color': 'orange', 'line-opacity': 0.42, 'line-width': 5 }
      }, beforeFollowedTrack);
    }
    if (beforeFollowedTrack) {
      try {
        if (map.getLayer('route-casing')) map.moveLayer('route-casing', beforeFollowedTrack);
        if (map.getLayer('route-line')) map.moveLayer('route-line', beforeFollowedTrack);
      } catch {}
    }
  }

  function ensureRouteCursorLayer() {
    if (!map) return;
    ensureRouteCursorSource();
    if (!map.getLayer('route-cursor-dot')) {
      map.addLayer({
        id: 'route-cursor-dot',
        type: 'circle',
        source: 'route-cursor',
        paint: {
          'circle-color': '#111',
          'circle-radius': 6,
          'circle-stroke-color': '#fff',
          'circle-stroke-width': 2
        }
      });
    }
  }

  function ensureRouteCurrentLayer() {
    if (!map) return;
    ensureRouteCurrentSource();
    if (!map.getLayer('route-current-dot')) {
      map.addLayer({
        id: 'route-current-dot',
        type: 'circle',
        source: 'route-current',
        paint: {
          'circle-color': '#1e90ff',
          'circle-radius': 6,
          'circle-stroke-color': '#050505',
          'circle-stroke-width': 3
        }
      });
    }
  }

  function updateRouteCurrentOnMap(progress) {
    if (!map) return;
    const src = map.getSource('route-current');
    if (src) {
      src.setData({ type: 'FeatureCollection', features: [] });
    }
  }

  function updateRouteProfileCursorOnMap() {
    if (!map) return;
    const point = routeProfile.cursorIndex == null ? null : routeProfile.points[routeProfile.cursorIndex];
    const src = ensureRouteCursorSource();
    if (!src) return;
    if (!point || !routeProfile.visible || !routeProfile.points.length) {
      src.setData({ type: 'FeatureCollection', features: [] });
      return;
    }
    ensureRouteCursorLayer();
    src.setData({
      type: 'FeatureCollection',
      features: [{
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [point.lon, point.lat] },
        properties: {}
      }]
    });
  }

  async function loadRouteFromServer() {
    try {
      const st = await fetch('/gpx/status', { cache: 'no-store' });
      const info = await st.json();
      if (!info || !info.exists) {
        routeCoords = null;
        routeDistancePoints = [];
        setRouteProgress(null);
        updateRouteCurrentOnMap(null);
        setRouteProfileAvailability([]);
        return;
      }
      const res = await fetch('/gpx/track', { cache: 'no-store' });
      if (!res.ok) return;
      const text = await res.text();
      const routePoints = parseGpxToRoutePoints(text);
      if (!routePoints.length) {
        setRouteProfileAvailability([]);
        return;
      }
      routeCoords = routePoints.map(p => [p.lon, p.lat]);
      routeDistancePoints = buildRouteDistancePoints(routePoints);
      ensureRouteSource();
      routeSource.setData({ type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'LineString', coordinates: routeCoords.slice() } }] });
      ensureRouteLayer();
      setRouteProfileAvailability(buildRouteProfile(routePoints));
      if (isFinite(lastLon) && isFinite(lastLat)) updateRouteProgressFromGps(lastLon, lastLat);
    } catch (e) {
      // ignore
    }
  }

  async function uploadGpxFile(file) {
    const form = new FormData();
    form.append('file', file);
    const res = await fetch('/gpx/upload', { method: 'POST', body: form });
    let msg = 'Upload failed'; let ok=false; let data=null;
    try { data = await res.json(); } catch {}
    if (res.ok && data && data.status === 'ok') { ok = true; msg = data.message || 'file successfully uploaded and read'; }
    showGpxMessage(msg, ok);
    if (ok) await loadRouteFromServer();
  }

  async function eraseRoute() {
    try {
      const res = await fetch('/gpx/erase', { method: 'POST' });
      const ok = res.ok;
      showGpxMessage(ok ? 'Track erased' : 'Erase failed', ok);
      if (ok) {
        routeCoords = null;
        routeDistancePoints = [];
        setRouteProgress(null);
        setRouteProfileAvailability([]);
        if (map) {
          if (map.getLayer('route-cursor-dot')) map.removeLayer('route-cursor-dot');
          if (map.getSource('route-cursor')) map.removeSource('route-cursor');
          if (map.getLayer('route-current-dot')) map.removeLayer('route-current-dot');
          if (map.getSource('route-current')) map.removeSource('route-current');
          if (map.getLayer('route-line')) map.removeLayer('route-line');
          if (map.getLayer('route-casing')) map.removeLayer('route-casing');
          if (map.getSource('route')) map.removeSource('route');
        }
      }
    } catch (e) {
      showGpxMessage('Erase error', false);
    }
  }

  function showGpxMessage(text, ok=true) {
    const el = document.getElementById('gpx-message');
    if (!el) return;
    el.textContent = text;
    el.style.color = ok ? '#ccc' : '#f88';
    el.style.display = '';
    setTimeout(() => { el.style.display = 'none'; }, 3000);
  }

  // Expose reload for other scripts if needed
  window.reloadRouteOverlay = loadRouteFromServer;
  window.clearRouteOverlay = eraseRoute;

  // Wire dashboard buttons if present
  window.addEventListener('DOMContentLoaded', () => {
    const upBtn = document.getElementById('gpx-upload-btn');
    const erBtn = document.getElementById('gpx-erase-btn');
    const input = document.getElementById('gpx-file-input');
    if (upBtn && input) {
      upBtn.addEventListener('click', () => input.click());
      input.addEventListener('change', () => {
        const f = input.files && input.files[0];
        if (f) uploadGpxFile(f);
        input.value = '';
      });
    }
    if (erBtn) erBtn.addEventListener('click', eraseRoute);
    const profileToggle = document.getElementById('gpx-profile-toggle');
    const profilePanel = document.getElementById('gpx-profile-panel');
    const profileReset = document.getElementById('gpx-profile-reset');
    const profileZoomIn = document.getElementById('gpx-profile-zoom-in');
    const profileZoomOut = document.getElementById('gpx-profile-zoom-out');
    const profileCurrentSlope = document.getElementById('gpx-profile-current-slope');
    const profileCanvas = document.getElementById('gpx-elevation-profile');
    if (profileToggle && profilePanel) {
      profileToggle.addEventListener('click', () => {
        routeProfile.visible = !routeProfile.visible;
        profilePanel.hidden = !routeProfile.visible || !routeProfile.points.length;
        profileToggle.classList.toggle('active-chart', routeProfile.visible && routeProfile.points.length);
        updateRouteProfileCursorOnMap();
        renderRouteProfileChart();
      });
    }
    if (profileReset) {
      profileReset.addEventListener('click', () => {
        resetRouteProfileZoom();
        renderRouteProfileChart();
      });
    }
    if (profileZoomIn) profileZoomIn.addEventListener('click', () => zoomRouteProfileByFactor(0.72, 0.5));
    if (profileZoomOut) profileZoomOut.addEventListener('click', () => zoomRouteProfileByFactor(1.32, 0.5));
    if (profileCurrentSlope) profileCurrentSlope.addEventListener('click', focusCurrentRouteSlope);
    if (profileCanvas) {
      profileCanvas.addEventListener('pointerdown', (event) => {
        routeProfileDragging = true;
        routeProfilePanStart = {
          x: event.clientX,
          lastX: event.clientX,
          moved: false
        };
        profileCanvas.setPointerCapture(event.pointerId);
        setProfileCursorFromCanvasX(event.clientX);
      });
      profileCanvas.addEventListener('pointermove', (event) => {
        if (routeProfileDragging && routeProfilePanStart && routeProfileIsZoomed()) {
          const deltaX = event.clientX - routeProfilePanStart.lastX;
          const totalDeltaX = event.clientX - routeProfilePanStart.x;
          if (Math.abs(totalDeltaX) > 4) routeProfilePanStart.moved = true;
          if (routeProfilePanStart.moved) {
            panRouteProfileByPixels(deltaX, profileCanvas.clientWidth || 320);
            routeProfilePanStart.lastX = event.clientX;
          }
        } else if (routeProfileDragging || event.buttons === 0) {
          setProfileCursorFromCanvasX(event.clientX);
        }
      });
      profileCanvas.addEventListener('pointerup', (event) => {
        if (routeProfilePanStart && !routeProfilePanStart.moved) {
          setProfileCursorFromCanvasX(event.clientX);
        }
        routeProfileDragging = false;
        routeProfilePanStart = null;
        try { profileCanvas.releasePointerCapture(event.pointerId); } catch {}
      });
      profileCanvas.addEventListener('pointerleave', () => {
        routeProfileDragging = false;
        routeProfilePanStart = null;
      });
      profileCanvas.addEventListener('wheel', (event) => {
        if (!routeProfile.points.length) return;
        event.preventDefault();
        if (Math.abs(event.deltaX) > Math.abs(event.deltaY) && routeProfileIsZoomed()) {
          panRouteProfileByPixels(-event.deltaX, profileCanvas.clientWidth || 320);
        } else {
          zoomRouteProfile(event.deltaY, event.clientX);
        }
      }, { passive: false });
      profileCanvas.addEventListener('touchstart', (event) => {
        if (event.touches.length === 1) {
          const touchX = event.touches[0].clientX;
          routeProfilePanStart = { x: touchX, lastX: touchX, moved: false };
          setProfileCursorFromCanvasX(touchX);
        } else if (event.touches.length === 2) {
          routeProfileTouchDistance = Math.abs(event.touches[0].clientX - event.touches[1].clientX);
        }
      }, { passive: true });
      profileCanvas.addEventListener('touchmove', (event) => {
        if (!routeProfile.points.length) return;
        if (event.touches.length === 1) {
          event.preventDefault();
          if (routeProfileIsZoomed() && routeProfilePanStart) {
            const touchX = event.touches[0].clientX;
            const deltaX = touchX - routeProfilePanStart.lastX;
            const totalDeltaX = touchX - routeProfilePanStart.x;
            if (Math.abs(totalDeltaX) > 4) routeProfilePanStart.moved = true;
            if (routeProfilePanStart.moved) {
              panRouteProfileByPixels(deltaX, profileCanvas.clientWidth || 320);
              routeProfilePanStart.lastX = touchX;
            }
          } else {
            setProfileCursorFromCanvasX(event.touches[0].clientX);
          }
        } else if (event.touches.length === 2 && routeProfileTouchDistance) {
          event.preventDefault();
          const nextDistance = Math.abs(event.touches[0].clientX - event.touches[1].clientX);
          if (nextDistance > 8) {
            const rect = profileCanvas.getBoundingClientRect();
            const centerX = (event.touches[0].clientX + event.touches[1].clientX) / 2;
            const focusPct = Math.max(0, Math.min(1, (centerX - rect.left) / Math.max(1, rect.width)));
            zoomRouteProfileByFactor(routeProfileTouchDistance / nextDistance, focusPct);
            routeProfileTouchDistance = nextDistance;
          }
        }
      }, { passive: false });
      profileCanvas.addEventListener('touchend', () => {
        routeProfileTouchDistance = null;
        routeProfilePanStart = null;
      });
    }
    window.addEventListener('resize', renderRouteProfileChart);

        // Map reduce / extend controls
    const mapBox = document.querySelector('.map-box');
    const reduceBtn = document.getElementById('map-reduce-btn');
    const extendBtn = document.getElementById('map-extend-btn');
    if (reduceBtn && mapBox) {
      reduceBtn.addEventListener('click', () => {
        mapBox.classList.add('reduced');
      });
    }
    if (extendBtn && mapBox) {
      extendBtn.addEventListener('click', () => {
        mapBox.classList.remove('reduced');
        refreshPhoneAltitude();
      });
    }
    startPhoneAltitudePolling();
  });
})();
