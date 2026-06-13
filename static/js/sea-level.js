// sea-level.js — zoomable sea level rise widget for podnebje.kesma.wtf
// Base map: Leaflet + OpenTopoMap (izohypse / contour lines)
// Flood overlay: pre-generated DEM PNG masks projected onto screen via canvas

(function SeaLevel() {
  'use strict';

  // ── Projection data ────────────────────────────────────────────────────────

  const DATA = {
    datum: 218,
    projections: {
      ssp245: {
        median: { 2030:9,  2040:15, 2050:23, 2060:31, 2070:39, 2080:47, 2090:54, 2100:60 },
        low:    { 2030:6,  2040:10, 2050:16, 2060:22, 2070:28, 2080:34, 2090:39, 2100:45 },
        high:   { 2030:12, 2040:20, 2050:30, 2060:40, 2070:50, 2080:60, 2090:68, 2100:75 },
      },
      ssp585: {
        median: { 2030:11, 2040:19, 2050:28, 2060:40, 2070:53, 2080:66, 2090:75, 2100:84  },
        low:    { 2030:8,  2040:14, 2050:21, 2060:30, 2070:40, 2080:50, 2090:58, 2100:66  },
        high:   { 2030:14, 2040:24, 2050:36, 2060:52, 2070:70, 2080:88, 2090:100,2100:108 },
      },
    },
    surcharge: { p70: 58, p20: 76, p01: 98 },
    impact: {
      base: 40,
      perCm: { ha: 20.77, build: 14.13, ppl: 63.3 },
    },
  };

  // Bounding box of the pre-generated flood PNGs (WGS84)
  const FLOOD_BOUNDS = L.latLngBounds(
    L.latLng(45.425, 13.535),   // SW
    L.latLng(45.605, 13.795)    // NE
  );

  // Two distinct coastal lowland zones (elevation < ~5 m), no hills included:
  //   Zone A — Sečovlje saltpans (entirely flat, near sea level)
  //   Zone B — Koper harbour / Semedela coastal fringe
  const FLOOD_RISK_ZONES = [
    L.latLngBounds(L.latLng(45.462, 13.582), L.latLng(45.491, 13.636)),  // A: saltpans
    L.latLngBounds(L.latLng(45.538, 13.716), L.latLng(45.552, 13.752)),  // B: Koper coast
  ];

  // ── Math helpers ───────────────────────────────────────────────────────────

  function interp(obj, year) {
    const keys = Object.keys(obj).map(Number).sort((a, b) => a - b);
    if (year <= keys[0]) return obj[keys[0]];
    if (year >= keys[keys.length - 1]) return obj[keys[keys.length - 1]];
    let lo = keys[0], hi = keys[1];
    for (let i = 0; i < keys.length - 1; i++) {
      if (year >= keys[i] && year <= keys[i + 1]) { lo = keys[i]; hi = keys[i + 1]; break; }
    }
    return obj[lo] + ((year - lo) / (hi - lo)) * (obj[hi] - obj[lo]);
  }

  function getMeanRise(scn, year)  { return interp(DATA.projections[scn].median, year); }
  function getRange(scn, year) {
    return { lo: interp(DATA.projections[scn].low, year), hi: interp(DATA.projections[scn].high, year) };
  }
  function calcImpacts(cm) {
    const over = Math.max(0, cm - DATA.impact.base);
    return {
      ha:    Math.round(over * DATA.impact.perCm.ha),
      build: Math.round(over * DATA.impact.perCm.build),
      ppl:   Math.round(over * DATA.impact.perCm.ppl),
    };
  }
  function fmt(n) { return n.toLocaleString('sl-SI'); }
  function gaugeY(cm) { return Math.max(10, 175 - Math.min(cm, 206) * 0.8); }

  // ── State ──────────────────────────────────────────────────────────────────

  const state = { scn: 'ssp245', prob: 'p20', year: 2050, play: false, divPct: 50 };
  let rafId = null;

  // ── Flood PNG loading + tinting ────────────────────────────────────────────

  const imgCache = {};

  function snapLevel(cm) { return Math.max(10, Math.min(250, Math.round(cm / 10) * 10)); }
  function floodUrl(lvl)  { return `/data/flood/flood-${String(lvl).padStart(3,'0')}cm.png`; }

  function loadImg(lvl) {
    if (imgCache[lvl]) return Promise.resolve(imgCache[lvl]);
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload  = () => { imgCache[lvl] = img; resolve(img); };
      img.onerror = reject;
      img.src = floodUrl(lvl);
    });
  }

  // Tint a white flood-mask image to a given CSS colour using source-in composite
  function tintImg(src, colour) {
    const tmp = document.createElement('canvas');
    tmp.width = src.naturalWidth || src.width;
    tmp.height = src.naturalHeight || src.height;
    const c = tmp.getContext('2d');
    c.drawImage(src, 0, 0);
    c.globalCompositeOperation = 'source-in';
    c.fillStyle = colour;
    c.fillRect(0, 0, tmp.width, tmp.height);
    return tmp;
  }

  // ── Flood canvas rendering ─────────────────────────────────────────────────

  let floodCanvas, floodCtx, leafletMap;

  function getFloodScreenRect() {
    // NW = top-left, SE = bottom-right in screen coordinates → both w and h are positive
    const nw = leafletMap.latLngToContainerPoint(L.latLng(45.605, 13.535));
    const se = leafletMap.latLngToContainerPoint(L.latLng(45.425, 13.795));
    return { x: nw.x, y: nw.y, w: se.x - nw.x, h: se.y - nw.y };
  }

  // Leaflet polygon layers for schematic flood zones (used when DEM PNGs unavailable).
  // Stored so we can remove + re-add when colors change.
  let schematicLayers = [];

  // Approximate coastal lowland polygons (WGS84).
  // Zone A: Sečovlje saltpans / Zone B: Koper harbour + Semedela flat
  const SCHEMATIC_POLYS = [
    [[45.462, 13.582],[45.491, 13.582],[45.491, 13.636],[45.462, 13.636]],
    [[45.538, 13.716],[45.552, 13.716],[45.552, 13.752],[45.538, 13.752]],
  ];

  function clearSchematic() {
    schematicLayers.forEach(l => leafletMap.removeLayer(l));
    schematicLayers = [];
  }

  function drawSchematicLeaflet(fillColour, borderColour) {
    SCHEMATIC_POLYS.forEach(coords => {
      const layer = L.polygon(coords, {
        color:       borderColour,
        weight:      1.5,
        fillColor:   fillColour,
        fillOpacity: 0.62,
        opacity:     0.85,
        interactive: false,
      }).addTo(leafletMap);
      schematicLayers.push(layer);
    });
  }

  async function renderFloodCanvas() {
    if (!leafletMap || !floodCanvas) return;

    const container = leafletMap.getContainer();
    floodCanvas.width  = container.offsetWidth;
    floodCanvas.height = container.offsetHeight;

    const ctx = floodCtx;
    ctx.clearRect(0, 0, floodCanvas.width, floodCanvas.height);
    const divX = floodCanvas.width * state.divPct / 100;

    const todayCm  = DATA.surcharge[state.prob];
    const futureCm = getMeanRise(state.scn, state.year) + DATA.surcharge[state.prob];
    const tL = snapLevel(todayCm);
    const fL = snapLevel(futureCm);

    // Try pre-generated DEM PNGs
    let todayImg, futureImg;
    try {
      [todayImg, futureImg] = await Promise.all([loadImg(tL), loadImg(fL)]);
    } catch (_) {
      todayImg = futureImg = null;
    }

    if (todayImg && futureImg) {
      // ── Precise DEM-based rendering ───────────────────────────────────────
      clearSchematic();
      const { x, y, w, h } = getFloodScreenRect();
      if (w <= 0 || h <= 0) return;

      const todayCyan   = tintImg(todayImg,  'rgba(60,30,200,0.82)');
      const futureCoral = tintImg(futureImg, 'rgba(210,30,45,0.83)');

      ctx.save();
      ctx.beginPath(); ctx.rect(0, 0, divX, floodCanvas.height); ctx.clip();
      ctx.drawImage(todayCyan, x, y, w, h);
      ctx.restore();

      ctx.save();
      ctx.beginPath(); ctx.rect(divX, 0, floodCanvas.width - divX, floodCanvas.height); ctx.clip();
      ctx.drawImage(futureCoral, x, y, w, h);
      ctx.globalCompositeOperation = 'destination-out';
      ctx.drawImage(todayImg, x, y, w, h);
      ctx.globalCompositeOperation = 'source-over';
      ctx.drawImage(todayCyan, x, y, w, h);
      ctx.restore();
      ctx.globalCompositeOperation = 'source-over';
    } else {
      // ── Schematic fallback via Leaflet vector layers ───────────────────────
      // Divider LEFT = TODAY view (indigo), RIGHT = FUTURE view (coral).
      // We re-draw based on where the divider sits relative to each zone.
      clearSchematic();

      // Determine divider longitude (map pixel divX → geographic lng)
      const divLng = leafletMap.containerPointToLatLng(L.point(divX, floodCanvas.height / 2)).lng;

      SCHEMATIC_POLYS.forEach(coords => {
        // bounding lng of this polygon
        const lngs   = coords.map(c => c[1]);
        const zoneW  = Math.max(...lngs);   // easternmost lng of zone
        const zoneE  = Math.min(...lngs);   // westernmost

        // Choose colour by majority side: if zone center is left of divider → today, else future
        const centerLng = (zoneW + zoneE) / 2;
        const isToday   = centerLng < divLng;
        const fill      = isToday ? '#3c1ec8' : (futureCm > todayCm ? '#d21e2d' : '#3c1ec8');
        const border    = isToday ? '#6655ff' : (futureCm > todayCm ? '#ff4455' : '#6655ff');

        const layer = L.polygon(coords, {
          color: border, weight: 1.5,
          fillColor: fill, fillOpacity: 0.65,
          opacity: 0.9, interactive: false,
        }).addTo(leafletMap);
        schematicLayers.push(layer);
      });

      // Draw divider line on canvas (the Leaflet layers handle the coloring)
      ctx.save();
      ctx.strokeStyle = 'rgba(255,255,255,0.85)';
      ctx.lineWidth   = 2;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(divX, 0);
      ctx.lineTo(divX, floodCanvas.height);
      ctx.stroke();
      ctx.restore();
    }
  }

  // ── Flood stats (DEM ha + OSM buildings) ──────────────────────────────────

  let floodStats = null;   // loaded async; falls back to linear formula if absent

  async function loadFloodStats() {
    try {
      floodStats = await fetch('/data/flood-stats.json').then(r => r.json());
      console.log('[sea-level] flood-stats loaded', Object.keys(floodStats.levels).length, 'levels');
    } catch (e) {
      console.warn('[sea-level] flood-stats.json unavailable, using linear formula', e);
    }
  }

  function getStats(totalCm) {
    if (floodStats) {
      const lvl = String(snapLevel(totalCm));
      const row = floodStats.levels[lvl];
      if (row) return { ha: row.ha, buildings: row.buildings };
    }
    // Linear fallback anchored on Kovačič et al. 2016/2019
    const over = Math.max(0, totalCm - 40);
    return { ha: Math.round(over * 20.77), buildings: Math.round(over * 14.13) };
  }

  // Fraction of the two coastal lowland zones visible in the current viewport (0–1).
  // Sums intersections across both zones, normalised by their combined area.
  // Returns 0 when the view doesn't overlap either zone (hills, open sea, etc.).
  function viewFraction() {
    if (!leafletMap) return 1;
    const vb = leafletMap.getBounds();
    let visArea = 0, totalArea = 0;
    for (const fz of FLOOD_RISK_ZONES) {
      const latLo = Math.max(vb.getSouth(), fz.getSouth());
      const latHi = Math.min(vb.getNorth(), fz.getNorth());
      const lngLo = Math.max(vb.getWest(),  fz.getWest());
      const lngHi = Math.min(vb.getEast(),  fz.getEast());
      totalArea += (fz.getNorth() - fz.getSouth()) * (fz.getEast() - fz.getWest());
      if (latHi > latLo && lngHi > lngLo)
        visArea += (latHi - latLo) * (lngHi - lngLo);
    }
    return totalArea > 0 ? Math.min(1, visArea / totalArea) : 0;
  }

  // Pre-load most-used PNG levels in background
  function preload() {
    [58,60,70,76,80,90,98,100,110,120,130,140,150,160,170,180,190].forEach(cm => {
      loadImg(snapLevel(cm)).catch(() => {});
    });
  }

  // ── Divider drag ───────────────────────────────────────────────────────────

  let elDivLine, elDivHandle, elFutureLbl;

  function updateDividerPos() {
    const pct = state.divPct;
    elDivLine.style.left   = pct + '%';
    elDivHandle.style.left = pct + '%';
    // Future label: right side when divider is right, left side when it crosses over
    elFutureLbl.style.right = (100 - pct) < 12 ? 'auto' : (100 - Math.min(pct + 1, 98)) + '%';
    elFutureLbl.style.left  = (100 - pct) < 12 ? (pct + 1) + '%' : 'auto';
  }

  function wireDivider(wrap) {
    elDivHandle.addEventListener('pointerdown', e => {
      e.preventDefault();
      elDivHandle.setPointerCapture(e.pointerId);
      const onMove = me => {
        const rect = wrap.getBoundingClientRect();
        state.divPct = Math.max(5, Math.min(95,
          (me.clientX - rect.left) / rect.width * 100));
        updateDividerPos();
        renderFloodCanvas();
      };
      elDivHandle.addEventListener('pointermove', onMove);
      elDivHandle.addEventListener('pointerup', () => {
        elDivHandle.removeEventListener('pointermove', onMove);
      }, { once: true });
    });
  }

  // ── Gauge & stats ──────────────────────────────────────────────────────────

  let elRiseVal, elScnLbl, elGaugeBand, elGaugeFill, elImpHa, elImpBuild,
      elInlineHa, elInlineBuild, elGaugeHFill, elGaugeHBand, elGaugeHVal;

  const GAUGE_MAX = 200;   // cm shown on gauge axis

  function renderGauge() {
    const meanRise = getMeanRise(state.scn, state.year);
    const range    = getRange(state.scn, state.year);
    const sc       = DATA.surcharge[state.prob];
    const total    = meanRise + sc;

    // Vertical SVG gauge (desktop)
    const fillY  = gaugeY(total);
    const bandHi = gaugeY(range.hi + sc);
    const bandLo = gaugeY(range.lo + sc);
    elGaugeFill.setAttribute('y', fillY);
    elGaugeFill.setAttribute('height', Math.max(0, 175 - fillY));
    elGaugeBand.setAttribute('y', bandHi);
    elGaugeBand.setAttribute('height', Math.max(0, bandLo - bandHi));

    // Horizontal gauge (mobile)
    const pct     = Math.min(100, total / GAUGE_MAX * 100);
    const bandL   = Math.min(100, (range.lo + sc) / GAUGE_MAX * 100);
    const bandW   = Math.min(100 - bandL, (range.hi - range.lo) / GAUGE_MAX * 100);
    elGaugeHFill.style.width = pct + '%';
    elGaugeHBand.style.left  = bandL + '%';
    elGaugeHBand.style.width = bandW + '%';
    elGaugeHVal.textContent  = '+' + Math.round(total) + ' cm';
  }

  function renderStats() {
    const meanRise = getMeanRise(state.scn, state.year);
    const futureCm = meanRise + DATA.surcharge[state.prob];
    elRiseVal.textContent = '+' + Math.round(meanRise);
    elScnLbl.textContent  = state.scn === 'ssp245' ? 'SSP2-4.5' : 'SSP5-8.5';
    const fi      = getStats(futureCm);
    const frac    = viewFraction();
    const visHa    = Math.round(fi.ha        * frac);
    const visBuild = Math.round(fi.buildings * frac);
    const noData   = frac === 0;
    elImpHa.textContent    = noData ? '—' : fmt(visHa);
    elImpBuild.textContent = noData ? '—' : fmt(visBuild);
    // Inline stats near year slider
    elInlineHa.textContent    = noData ? '—' : fmt(visHa);
    elInlineBuild.textContent = noData ? '—' : fmt(visBuild);
  }

  function renderAll() {
    renderGauge();
    renderStats();
    const yr = Math.round(state.year);
    document.getElementById('sl-year-lbl').textContent = yr;
    document.getElementById('sl-year').value           = yr;
    elFutureLbl.textContent = yr;
    renderFloodCanvas();
  }

  // ── Play animation ─────────────────────────────────────────────────────────

  function startPlay() {
    if (state.year >= 2100) state.year = 2024;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      state.year = 2100; renderAll(); stopPlay(); return;
    }
    const SPEED = 13;
    let last = performance.now();
    const tick = now => {
      state.year = Math.min(state.year + (now - last) / 1000 * SPEED, 2100);
      last = now;
      renderAll();
      if (state.year >= 2100) { stopPlay(); return; }
      rafId = requestAnimationFrame(tick);
    };
    rafId = requestAnimationFrame(tick);
    const btn = document.getElementById('sl-play');
    btn.textContent = '⏸'; btn.setAttribute('aria-label', 'Ustavi');
    state.play = true;
  }

  function stopPlay() {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    const btn = document.getElementById('sl-play');
    btn.textContent = '▶'; btn.setAttribute('aria-label', 'Predvajaj');
    state.play = false;
  }

  // ── Wire controls ──────────────────────────────────────────────────────────

  function wireControls() {
    document.getElementById('sl-scn-btns').addEventListener('click', e => {
      const btn = e.target.closest('[data-scn]'); if (!btn) return;
      state.scn = btn.dataset.scn;
      document.querySelectorAll('#sl-scn-btns .sl-btn').forEach(b =>
        b.classList.toggle('sl-btn--active', b.dataset.scn === state.scn));
      renderAll();
    });

    document.getElementById('sl-prob-btns').addEventListener('click', e => {
      const btn = e.target.closest('[data-prob]'); if (!btn) return;
      state.prob = btn.dataset.prob;
      document.querySelectorAll('#sl-prob-btns .sl-btn').forEach(b =>
        b.classList.toggle('sl-btn--active', b.dataset.prob === state.prob));
      renderAll();
    });

    document.getElementById('sl-year').addEventListener('input', e => {
      if (state.play) stopPlay();
      state.year = +e.currentTarget.value;
      renderAll();
    });

    document.getElementById('sl-play').addEventListener('click', () =>
      state.play ? stopPlay() : startPlay());
  }

  // ── Init ───────────────────────────────────────────────────────────────────

  function init() {
    const section = document.getElementById('sea-level-section');
    if (!section) return;
    section.hidden = false;

    // Grab refs
    elDivLine   = document.getElementById('sl-div-line');
    elDivHandle = document.getElementById('sl-div-handle');
    elFutureLbl = document.getElementById('sl-future-lbl');
    elRiseVal   = document.getElementById('sl-rise-val');
    elScnLbl    = document.getElementById('sl-scn-lbl');
    elGaugeBand = document.getElementById('sl-gauge-band');
    elGaugeFill = document.getElementById('sl-gauge-fill');
    elImpHa        = document.getElementById('sl-imp-ha');
    elImpBuild     = document.getElementById('sl-imp-build');
    elInlineHa     = document.getElementById('sl-inline-ha');
    elInlineBuild  = document.getElementById('sl-inline-build');
    elGaugeHFill   = document.getElementById('sl-gauge-h-fill');
    elGaugeHBand   = document.getElementById('sl-gauge-h-band');
    elGaugeHVal    = document.getElementById('sl-gauge-h-val');

    floodCanvas = document.getElementById('sl-flood-canvas');
    floodCtx    = floodCanvas.getContext('2d');

    const wrap = document.getElementById('sl-map-wrap');

    // ── Leaflet map ──────────────────────────────────────────────────────────
    leafletMap = L.map('sl-leaflet', {
      center:          [45.51, 13.645],
      zoom:            11,
      minZoom:         9,
      maxZoom:         16,
      zoomControl:     true,
      scrollWheelZoom: true,
      attributionControl: true,
    });

    // OpenTopoMap — contour lines (izohypse) at all zoom levels, greyscale via CSS filter
    L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
      attribution: '© <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA) | © <a href="https://www.openstreetmap.org">OSM</a>',
      maxZoom: 17,
      opacity: 1,
    }).addTo(leafletMap);

    // Monochrome — desaturate tiles, keep topo relief readable
    leafletMap.getPane('tilePane').style.filter = 'grayscale(1) contrast(0.88) brightness(0.82)';

    // Re-render flood canvas on any map movement or resize, also update viewport stats
    const onMapUpdate = () => { renderFloodCanvas(); renderStats(); };
    leafletMap.on('move zoom moveend zoomend viewreset resize', onMapUpdate);
    new ResizeObserver(onMapUpdate).observe(wrap);

    // ── Divider ──────────────────────────────────────────────────────────────
    wireDivider(wrap);
    updateDividerPos();

    // ── Controls ─────────────────────────────────────────────────────────────
    wireControls();

    // ── Initial render ───────────────────────────────────────────────────────
    loadFloodStats().then(() => renderStats());  // update stats once loaded
    renderAll();
    preload();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
