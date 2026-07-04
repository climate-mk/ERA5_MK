/* fires.js — Wildfire Tracker page (/fires)
 *
 * Standalone page logic: loads /api/meta (fires block + languages), builds a
 * Leaflet map with base layers (OSM / satellite) and WMS overlays (EFFIS fire
 * danger, Sentinel-3 hotspots, GHSL settlement), plots FIRMS detection points
 * for a date range, and renders a year-over-year totals chart.
 *
 * Reuses the same JSON locale system as app.js (t() over /locales/<lang>_default.json).
 */

let META = null;
let FIRES = null;          // meta.fires block
let _locale = null;
let map = null;
let pointsLayer = null;    // Leaflet layer group for FIRMS points
let _debounce = null;
let _layerControl = null;  // Leaflet layers control (to add/remove the danger entry)
let _dangerLayer = null;   // EFFIS fire-danger WMS layer
let _dangerLabel = '';     // its label in the layer control
let _burntLayer = null;    // EFFIS burnt-area WMS layer (time follows the date selection)
let _protectedLayer = null; // WDPA protected-areas WMS layer

// ── i18n (mirrors app.js) ──────────────────────────────────────────────────────
async function loadLocale(name) {
  try {
    const r = await fetch(`/locales/${name}.json`);
    if (!r.ok) throw new Error(r.status);
    _locale = await r.json();
  } catch (e) { _locale = null; }
}
function t(key, vars = {}) {
  if (!_locale) return null;
  let val = _locale;
  for (const p of key.split('.')) { val = val?.[p]; if (val === undefined) return null; }
  if (typeof val !== 'string') return null;
  return val.replace(/\{(\w+)\}/g, (_, k) => (vars[k] !== undefined ? vars[k] : `{${k}}`));
}
// Text helper: use locale value if present, else the hardcoded English fallback.
function tx(key, fallback, vars) { return t(key, vars) || fallback; }

// Persisted language (same localStorage key family as app.js uses for lang)
function currentLang() {
  const saved = localStorage.getItem('lang');
  const langs = META?.languages || ['en'];
  return langs.includes(saved) ? saved : (META?.default_language || 'en');
}

// ── FRP → colour ramp ──────────────────────────────────────────────────────────
const FIRE_RAMP = ['#F2C14E', '#E8940A', '#D9662A', '#C2341C', '#8A1A10'];
function frpColor(frp) {
  if (frp == null) return FIRE_RAMP[1];
  if (frp < 5)   return FIRE_RAMP[0];
  if (frp < 20)  return FIRE_RAMP[1];
  if (frp < 50)  return FIRE_RAMP[2];
  if (frp < 120) return FIRE_RAMP[3];
  return FIRE_RAMP[4];
}

// ── Map ──────────────────────────────────────────────────────────────────────
function buildMap() {
  const bbox = FIRES.bbox;                 // [W,S,E,N]
  const center = [ (bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2 ];
  map = L.map('fires-map', { zoomControl: true }).setView(center, 8);

  // ── Base layers ──
  const streets = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18,
    attribution: '© OpenStreetMap contributors',
  });
  const baseLayers = {};
  baseLayers[tx('fires.base_streets', 'Streets')] = streets;

  if (FIRES.satellite_tiles) {
    baseLayers[tx('fires.base_satellite', 'Satellite')] = L.tileLayer(FIRES.satellite_tiles, {
      maxZoom: 18,
      attribution: 'Imagery © Esri, Maxar, Earthstar Geographics',
    });
  }
  streets.addTo(map);

  // ── Overlay layers (WMS) ──
  const overlays = {};
  const wmsOpts = (layer, extra, more) => Object.assign({
    layers: layer, format: 'image/png', transparent: true,
    version: '1.3.0', opacity: 0.65, attribution: extra || '',
  }, more || {});

  // Layer-control label with a tap/hover "(i)" info affordance. Leaflet renders
  // overlay/base names as HTML, so we can embed a small info span with the
  // description both as a native title (hover, desktop) and as data for the
  // tap-to-expand row wired up after the control is built (see wireLayerInfo()).
  const labelWithInfo = (text, desc) =>
    `<span class="layer-label">${text}` +
    `<span class="layer-info" title="${desc.replace(/"/g, '&quot;')}" data-desc="${desc.replace(/"/g, '&quot;')}">ⓘ</span>` +
    `</span>`;

  // Built-up areas (GHSL) listed first — grouped right under the Streets/Satellite
  // base maps in the layer control, above the fire overlays.
  if (FIRES.settlement) {
    overlays[labelWithInfo(tx('fires.layer_settlement', 'Built-up areas (GHSL)'),
      tx('fires.desc_settlement', 'Built-up land (buildings, urban fabric) from the Global Human Settlement Layer — shows where fires are near populated areas.'))] =
      L.tileLayer.wms(FIRES.settlement.wms, wmsOpts(FIRES.settlement.builtup_layer,
        '© EC JRC — GHSL'));
  }

  // Burnt-area (fire footprint) polygons — OFF by default (opt-in overlay). Its
  // time window follows the date/period selection (see burntTimeRange / updateBurntTime).
  if (FIRES.burnt_area) {
    _burntLayer = L.tileLayer.wms(FIRES.burnt_area.wms,
      wmsOpts(FIRES.burnt_area.layer, '© Copernicus EFFIS',
              { opacity: 0.55, time: burntTimeRange() }));
    overlays[labelWithInfo(tx('fires.layer_burnt', 'Burnt areas (EFFIS)'),
      tx('fires.desc_burnt', 'Mapped extent of land already burned (fire footprint), not live fire location. Follows the selected date/period — from EFFIS/Copernicus.'))] = _burntLayer;
  }

  // Protected areas (WDPA) — OFF by default (opt-in overlay).
  if (FIRES.protected_areas) {
    _protectedLayer = L.tileLayer.wms(FIRES.protected_areas.wms,
      wmsOpts(FIRES.protected_areas.layer, '© WDPA / Copernicus EFFIS', { opacity: 0.5 }));
    overlays[labelWithInfo(tx('fires.layer_protected', 'Protected areas (WDPA)'),
      tx('fires.desc_protected', 'National parks, nature reserves and other legally protected areas — helps assess fire risk to protected ecosystems. From the World Database on Protected Areas.'))] = _protectedLayer;
  }

  // FIRMS points overlay (always on by default) — clustered so a big fire's many
  // adjacent detections read as one group until you zoom in.
  pointsLayer = (typeof L.markerClusterGroup === 'function')
    ? L.markerClusterGroup({
        maxClusterRadius: 40, spiderfyOnMaxZoom: true,
        showCoverageOnHover: false, chunkedLoading: true,
        // Cluster colour = average FRP of its detections (same fire-intensity ramp
        // and meaning as individual dots); cluster SIZE = detection count. Two
        // distinct, honestly-labelled channels — colour never means "count".
        iconCreateFunction(cluster) {
          const n = cluster.getChildCount();
          const frps = cluster.getAllChildMarkers()
            .map(m => m._frp).filter(v => v != null);
          const avgFrp = frps.length ? frps.reduce((a, b) => a + b, 0) / frps.length : null;
          const size = n < 10 ? 32 : n < 50 ? 38 : n < 200 ? 44 : 50;
          return L.divIcon({
            html: `<div style="background:${frpColor(avgFrp)};width:${size}px;height:${size}px;border-radius:50%;` +
                  `display:flex;align-items:center;justify-content:center;color:#fff;font-weight:600;` +
                  `font-family:'Space Grotesk',sans-serif;font-size:${size > 40 ? 13 : 12}px;` +
                  `border:2px solid rgba(255,255,255,0.85);box-shadow:0 1px 4px rgba(0,0,0,0.3)">${n}</div>`,
            className: 'fire-cluster-icon',
            iconSize: L.point(size, size),
          });
        },
      })
    : L.layerGroup();
  pointsLayer.addTo(map);
  overlays[labelWithInfo(tx('fires.layer_points', 'Fire detections'),
    tx('fires.desc_points', 'Individual satellite hot-spot detections (NASA FIRMS: MODIS + VIIRS, and Sentinel-3). Each dot is one detection; a big fire shows as a cluster.'))] = pointsLayer;

  if (FIRES.s3_wms) {
    overlays[labelWithInfo(tx('fires.layer_s3', 'Sentinel-3 hotspots'),
      tx('fires.desc_s3', 'Live hotspot overlay from the Sentinel-3 satellite (EFFIS), shown as a map layer in addition to the individual detection dots above.'))] =
      L.tileLayer.wms(FIRES.s3_wms.wms, wmsOpts(FIRES.s3_wms.layer,
        '© Copernicus EFFIS'));
  }
  if (FIRES.danger) {
    // The EFFIS FWI layer is time-dimensioned and defaults to an old date
    // (empty tile) — request today so the forecast actually renders. Fire danger
    // is a live forecast, so it's only meaningful for "today" (see period mode).
    _dangerLabel = labelWithInfo(tx('fires.layer_danger', 'Fire danger (EFFIS)'),
      tx('fires.desc_danger', "Today's Fire Weather Index forecast — how favourable conditions are for a fire to start and spread, not actual fires. Only shown for today."));
    _dangerLayer = L.tileLayer.wms(FIRES.danger.wms,
      wmsOpts(FIRES.danger.layer, '© Copernicus EFFIS / GWIS',
              { time: localToday() }));
    overlays[_dangerLabel] = _dangerLayer;
  }

  _layerControl = L.control.layers(baseLayers, overlays, { collapsed: false }).addTo(map);
  wireLayerInfo();

  // Legend
  const legend = L.control({ position: 'bottomright' });
  legend.onAdd = function () {
    const div = L.DomUtil.create('div', 'fire-legend');
    const lbl = tx('fires.legend_intensity', 'Fire intensity (FRP)');
    const lo = tx('fires.legend_low', 'low'), hi = tx('fires.legend_high', 'high');
    div.innerHTML = `<div style="margin-bottom:4px">${lbl}</div>` +
      FIRE_RAMP.map((c, i) => `<div><i style="background:${c}"></i>${i === 0 ? lo : (i === FIRE_RAMP.length - 1 ? hi : '')}</div>`).join('');
    return div;
  };
  legend.addTo(map);
}

// Tap-to-expand info rows for the layer control. Leaflet's control shows only the
// label + a native `title` tooltip (hover), which doesn't work on touch — so a tap
// on the "ⓘ" also toggles a description line under that row. Delegated on the
// control's root element so it keeps working as Leaflet rebuilds rows (e.g. when
// the fire-danger entry is added/removed by updateDangerAvailability()).
function wireLayerInfo() {
  const root = _layerControl?.getContainer?.();
  if (!root || root._infoWired) return;
  root._infoWired = true;
  root.addEventListener('click', (e) => {
    const info = e.target.closest('.layer-info');
    if (!info) return;
    e.preventDefault();
    e.stopPropagation();
    const row = info.closest('label');
    let desc = row?.querySelector('.layer-desc');
    if (desc) { desc.remove(); return; }          // already open → collapse
    desc = document.createElement('div');
    desc.className = 'layer-desc';
    desc.textContent = info.getAttribute('data-desc') || '';
    row?.appendChild(desc);
  });
}

// Fire danger is a live forecast → only meaningful for "today". When a custom
// historical period is active, remove it from the map and the layer control; when
// back on "today", offer it again.
let _dangerInControl = true;   // danger overlay starts listed (today is default)
function updateDangerAvailability() {
  if (!_dangerLayer || !_layerControl) return;
  // Fire danger is a live forecast → only offered when viewing today.
  if (!viewingToday()) {
    if (map.hasLayer(_dangerLayer)) map.removeLayer(_dangerLayer);
    if (_dangerInControl) { _layerControl.removeLayer(_dangerLayer); _dangerInControl = false; }
  } else if (!_dangerInControl) {
    _layerControl.addOverlay(_dangerLayer, _dangerLabel);
    _dangerInControl = true;
  }
}

// ── FIRMS points ───────────────────────────────────────────────────────────────
function selectedSensors() {
  return Array.from(document.querySelectorAll('#sensor-chips input:checked')).map(el => el.value);
}

async function refreshPoints(manual) {
  const { start, end } = activeRange();
  const sensors = selectedSensors();
  const params = new URLSearchParams({ start, end });
  if (sensors.length) params.set('sensor', sensors.join(','));

  const countEl = document.getElementById('fire-count');
  countEl.textContent = tx('fires.loading', 'loading…');
  const btn = document.getElementById('refresh-now');
  if (manual && btn) btn.disabled = true;
  let data;
  try {
    const r = await fetch('/api/fires/points?' + params);
    if (r.status === 204) { pointsLayer.clearLayers(); countEl.textContent = '0'; if (btn) btn.disabled = false; return; }
    data = await r.json();
  } catch (e) {
    countEl.textContent = tx('fires.load_error', 'could not load detections');
    return;
  }

  pointsLayer.clearLayers();
  (data.points || []).forEach(p => {
    const marker = L.circleMarker([p.lat, p.lon], {
      radius: 4, weight: 0.5, color: '#5a1a10',
      fillColor: frpColor(p.frp), fillOpacity: 0.8,
    }).bindPopup(
      `<b>${p.sensor}</b><br>${p.date}` +
      (p.frp != null ? `<br>FRP: ${p.frp.toFixed(1)} MW` : '') +
      (p.conf ? `<br>${tx('fires.confidence', 'confidence')}: ${p.conf}` : '')
    );
    marker._frp = p.frp;   // stashed for cluster icon aggregation (see buildMap)
    marker.addTo(pointsLayer);
  });

  const n = data.count || 0;
  countEl.innerHTML = `<b>${n.toLocaleString()}</b>` +
    (data.capped ? ' (' + tx('fires.showing_top', 'showing strongest {n}', { n: (data.points || []).length.toLocaleString() }) + ')' : '');

  // Show when the SERVER last collected the data (true freshness), not the
  // browser fetch time. Falls back to now only if the server didn't report it.
  _lastRefreshAt = data.collected_at ? new Date(data.collected_at) : null;
  _latestDetection = data.latest_detection ? new Date(data.latest_detection) : null;
  if (viewingToday()) renderLastRefresh();
  if (btn) btn.disabled = false;
}

function debouncedRefresh() {
  clearTimeout(_debounce);
  _debounce = setTimeout(() => refreshPoints(false), 200);
}

// ── Year chart ───────────────────────────────────────────────────────────────
async function renderYearChart() {
  if (!META.features.fires_year_chart) { document.getElementById('year-block').hidden = true; return; }
  let d;
  try {
    const r = await fetch('/api/fires/yearly');
    if (r.status === 204) { document.getElementById('year-block').hidden = true; return; }
    d = await r.json();
  } catch (e) { return; }

  if (!d.years || !d.years.length) {
    document.getElementById('fires-year-chart').innerHTML =
      `<p style="padding:24px;color:var(--ink-soft)">${tx('fires.no_data', 'No fire data collected yet.')}</p>`;
    return;
  }

  // Years before GFW's VIIRS coverage (2012) are filled in from local MODIS
  // counts — mark those bars with a muted colour so the sensor change is visible,
  // not just mentioned in a caption below.
  const preGfw = new Set(d.pre_gfw_years || []);
  const points = d.years.map((y, i) => preGfw.has(y)
    ? { y: d.counts[i], color: '#B5AFA3' }
    : d.counts[i]);

  Highcharts.chart('fires-year-chart', {
    chart: { type: 'column', backgroundColor: 'transparent', style: { fontFamily: "'Space Grotesk', sans-serif" } },
    title: { text: null }, credits: { enabled: false }, legend: { enabled: false },
    xAxis: { categories: d.years.map(String), title: { text: null }, labels: { style: { fontSize: '11px' } } },
    yAxis: { title: { text: tx('fires.axis_detections', 'Detections') }, gridLineColor: 'rgba(14,14,12,0.06)' },
    tooltip: {
      formatter() {
        const suffix = preGfw.has(+this.x) ? ' (' + tx('fires.modis_only', 'MODIS only') + ')' : '';
        return `<b>${this.y}</b> ${tx('fires.axis_detections', 'detections')}${suffix}`;
      },
    },
    plotOptions: { column: { color: '#D9662A', borderRadius: 2, pointPadding: 0.05, groupPadding: 0.08 } },
    series: [{ name: tx('fires.axis_detections', 'Detections'), data: points }],
  });

  // Source + sensor caveat note
  const srcLabel = d.source === 'gfw'
    ? tx('fires.src_gfw', 'Yearly totals from Global Forest Watch (VIIRS fire alerts).')
    : tx('fires.src_local', 'Yearly totals counted from collected FIRMS detections.');
  let note = srcLabel + ' ' + tx('fires.sensor_caveat',
      'Counts are not directly comparable across years when different satellites were active (more satellites detect more fires).');
  if (preGfw.size) {
    const range = d.years.filter(y => preGfw.has(y));
    note += ' ' + tx('fires.pre_gfw_note',
      'Years {from}–{to} (shown in grey) are from local MODIS detections, before VIIRS/Global Forest Watch coverage begins in 2012 — not directly comparable to the VIIRS-based years.',
      { from: range[0], to: range[range.length - 1] });
  }
  document.getElementById('year-note').textContent = note;
}

// ── "About this data" block (below the map) — what each layer/control shows ────
function renderDataExplain() {
  const el = document.getElementById('data-explain-list');
  if (!el) return;
  const rows = [
    [tx('fires.layer_points', 'Fire detections'),
      tx('fires.desc_points', 'Individual satellite hot-spot detections (NASA FIRMS: MODIS + VIIRS, and Sentinel-3). Each dot is one detection; a big fire shows as a cluster.')],
    [tx('fires.recent', 'Recent (48h)'),
      tx('fires.explain_recent', 'The default view: the last 48 hours of available detections. Satellites pass over only a few times a day and take a few hours to process, so this — not the literal calendar day — is what "current" fires look like.')],
  ];
  if (FIRES.danger) rows.push([tx('fires.layer_danger', 'Fire danger (EFFIS)'),
    tx('fires.desc_danger', "Today's Fire Weather Index forecast — how favourable conditions are for a fire to start and spread, not actual fires. Only shown for today.")]);
  if (FIRES.s3_wms) rows.push([tx('fires.layer_s3', 'Sentinel-3 hotspots'),
    tx('fires.desc_s3', 'Live hotspot overlay from the Sentinel-3 satellite (EFFIS), shown as a map layer in addition to the individual detection dots above.')]);
  if (FIRES.burnt_area) rows.push([tx('fires.layer_burnt', 'Burnt areas (EFFIS)'),
    tx('fires.desc_burnt', 'Mapped extent of land already burned (fire footprint), not live fire location. Follows the selected date/period — from EFFIS/Copernicus.')]);
  if (FIRES.protected_areas) rows.push([tx('fires.layer_protected', 'Protected areas (WDPA)'),
    tx('fires.desc_protected', 'National parks, nature reserves and other legally protected areas — helps assess fire risk to protected ecosystems. From the World Database on Protected Areas.')]);
  if (FIRES.settlement) rows.push([tx('fires.layer_settlement', 'Built-up areas (GHSL)'),
    tx('fires.desc_settlement', 'Built-up land (buildings, urban fabric) from the Global Human Settlement Layer — shows where fires are near populated areas.')]);
  if (FIRES.satellite_tiles) rows.push([tx('fires.base_satellite', 'Satellite'),
    tx('fires.explain_satellite', 'Satellite imagery basemap (Esri), as an alternative to the default street map.')]);
  rows.push([tx('fires.per_year', 'Fires per year'),
    tx('fires.explain_yearly', 'Total detections per year, for comparing fire activity across years. Counts are not directly comparable across years when different satellites were active.')]);

  el.innerHTML = rows.map(([term, desc]) =>
    `<dt>${term}</dt><dd>${desc}</dd>`).join('');
}

// ── Data sources block (below the charts) ──────────────────────────────────────
function renderDataSources() {
  const rows = [
    ['NASA FIRMS', 'https://firms.modaps.eosdis.nasa.gov/',
      tx('fires.src_firms_desc', 'Active-fire detections (MODIS + VIIRS satellites) — the map points and history.')],
    ['Copernicus EFFIS / GWIS', 'https://forest-fire.emergency.copernicus.eu/',
      tx('fires.src_effis_desc', 'Fire Weather Index (fire-danger forecast) and Sentinel-3 hotspot overlays.')],
    ['Global Forest Watch', 'https://www.globalforestwatch.org/',
      tx('fires.src_gfw_desc', 'Aggregated yearly fire-alert totals for the comparison chart.')],
    ['EC JRC — GHSL', 'https://ghsl.jrc.ec.europa.eu/',
      tx('fires.src_ghsl_desc', 'Global Human Settlement Layer — built-up areas overlay.')],
    ['OpenStreetMap / Esri', 'https://www.openstreetmap.org/copyright',
      tx('fires.src_base_desc', 'Street and satellite base maps.')],
  ];
  const heading = tx('fires.sources_heading', 'Data sources');
  const html =
    `<div style="font-weight:600;text-transform:uppercase;letter-spacing:0.06em;color:var(--ink-soft);margin-bottom:8px">${heading}</div>` +
    rows.map(([name, url, desc]) =>
      `<div style="margin-bottom:4px"><a href="${url}" target="_blank" rel="noopener">${name}</a> — ${desc}</div>`
    ).join('');
  document.getElementById('credits').innerHTML = html;
}

// ── Static labels + language selector ──────────────────────────────────────────
function applyLabels() {
  const set = (id, key, fb) => { const el = document.getElementById(id); if (el) el.textContent = tx(key, fb); };
  set('page-title', 'fires.title', 'Wildfire Tracker');
  // fires-h1 has a "work in progress" badge <span> inside it — set only the text
  // node, not innerHTML/textContent, so the badge survives relabeling.
  const h1 = document.getElementById('fires-h1');
  if (h1?.firstChild) h1.firstChild.textContent = tx('fires.title', 'Wildfire Tracker') + ' ';
  set('wip-badge', 'fires.wip_badge', 'work in progress');
  set('fires-intro', 'fires.intro', 'Satellite-detected active fires and fire-danger forecast across the country.');
  set('lbl-range', 'fires.date', 'Date');
  set('lbl-recent', 'fires.recent', 'Recent (48h)');
  set('lag-note', 'fires.lag_note', 'How current is this? Polar-orbiting satellites pass over only a few times a day, and detections take ~1–3h more to process — so fire data is typically 3–12 hours old and can approach ~24h overnight. The “Recent” view shows the last 48 hours of available data (matching how the NASA FIRMS and EFFIS “today” views behave). Use the date controls to browse a specific day or period.');
  set('lbl-explain-heading', 'fires.explain_heading', 'About this data');
  set('lbl-period', 'fires.custom_period', 'Custom period');
  set('lbl-start', 'fires.from', 'From');
  set('lbl-end', 'fires.to', 'To');
  set('refresh-now-lbl', 'fires.refresh_now', 'Refresh now');
  const dp = document.getElementById('day-prev'); if (dp) dp.title = tx('fires.prev_day', 'Previous day');
  const dn = document.getElementById('day-next'); if (dn) dn.title = tx('fires.next_day', 'Next day');
  set('lbl-sensors', 'fires.satellites', 'Satellites');
  set('lbl-count', 'fires.detections', 'Detections');
  set('lbl-yearchart', 'fires.per_year', 'Fires per year');
  set('footer-text', 'fires.footer', 'climate.mk — wildfire data from NASA FIRMS, Copernicus EFFIS & Global Forest Watch');
  document.title = tx('fires.title', 'Wildfire Tracker') + ' — climate.mk';
}

function buildLangSelector() {
  const sel = document.getElementById('lang-select');
  const langs = META.languages || ['en'];
  const cur = currentLang();
  sel.innerHTML = langs.map(l => `<option value="${l}" ${l === cur ? 'selected' : ''}>${l.toUpperCase()}</option>`).join('');
  sel.addEventListener('change', () => {
    localStorage.setItem('lang', sel.value);
    location.reload();
  });
}

function buildSensorChips() {
  const chips = document.getElementById('sensor-chips');
  const sensors = FIRES.sensors || [];
  const labels = { MODIS: 'MODIS', VIIRS_SNPP: 'VIIRS SNPP', VIIRS_NOAA20: 'NOAA-20', VIIRS_NOAA21: 'NOAA-21', SENTINEL3: 'Sentinel-3' };
  chips.innerHTML = sensors.map(s =>
    `<label><input type="checkbox" value="${s}" checked>${labels[s] || s}</label>`
  ).join('');
  chips.addEventListener('change', () => { updateDateBounds(); debouncedRefresh(); });
}

// Earliest date actually queryable: the earliest date any *currently selected*
// sensor has real collected data for (from /api/meta, reflecting what's really in
// the CSVs — not a sensor's theoretical launch date). Falls back to the earliest
// across all sensors if none are selected, or null if we don't know yet.
function earliestSelectableDate() {
  const starts = FIRES.sensor_start || {};
  const selected = selectedSensors();
  const keys = selected.length ? selected : Object.keys(starts);
  const dates = keys.map(k => starts[k]).filter(Boolean);
  return dates.length ? dates.sort()[0] : null;
}

// Keep the date pickers' `min` in sync with what's actually queryable, so users
// can't pick a date we have no data for (before any sensor's earliest record).
// Also clamps any current value that would now fall before that floor.
function updateDateBounds() {
  const min = earliestSelectableDate();
  if (!min) return;
  ['date-day', 'date-start', 'date-end'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.min = min;
    if (el.value && el.value < min) el.value = min;
  });
}

function isoDay(d) { return d.toISOString().slice(0, 10); }

// "Today" in the country's timezone (from /api/meta), so the default day matches
// the server's notion of today rather than the visitor's local date.
function localToday() {
  const tz = META?.timezone;
  try {
    if (tz) {
      // en-CA gives YYYY-MM-DD
      return new Intl.DateTimeFormat('en-CA', { timeZone: tz }).format(new Date());
    }
  } catch (e) { /* fall through */ }
  return isoDay(new Date());
}

// Rolling window (days) for the default "Recent" view. FIRMS/EFFIS lag calendar
// dates by up to ~a day (satellite passes + NRT processing), so a strict "today"
// query is often empty even when current fires exist — a rolling window mirrors
// how the FIRMS/EFFIS "today" views actually behave.
const RECENT_DAYS = 2;

// True when the user has switched on a custom historical period.
function periodMode() {
  return document.getElementById('period-toggle')?.checked;
}

// True in the default rolling "Recent" view (last RECENT_DAYS).
function recentMode() {
  return !periodMode() && document.getElementById('recent-toggle')?.checked;
}

// Danger + auto-refresh apply to the "current" view: Recent mode, or a single day
// pointing at today.
function viewingToday() {
  if (periodMode()) return false;
  if (recentMode()) return true;
  return document.getElementById('date-day')?.value === localToday();
}

// Resolve the active [start, end] window.
function activeRange() {
  if (periodMode()) {
    return {
      start: document.getElementById('date-start').value,
      end:   document.getElementById('date-end').value,
    };
  }
  if (recentMode()) {
    const end = localToday();
    const d = new Date(end + 'T00:00:00');
    d.setDate(d.getDate() - (RECENT_DAYS - 1));
    return { start: isoDay(d), end };
  }
  const d = document.getElementById('date-day').value || localToday();
  return { start: d, end: d };
}

// Time window for the burnt-area WMS overlay. Burnt areas accumulate over a
// season, so a single-day filter would be nearly empty — show everything burnt
// from the year's start up to the end of the active window.
function burntTimeRange() {
  const { start, end } = activeRange();
  if (periodMode()) return `${start}/${end}`;
  const yearStart = `${end.slice(0, 4)}-01-01`;
  return `${yearStart}/${end}`;
}

function updateBurntTime() {
  if (!_burntLayer) return;
  _burntLayer.setParams({ time: burntTimeRange() });
}

function shiftDay(deltaDays) {
  // Stepping the date leaves Recent mode.
  const rt = document.getElementById('recent-toggle');
  if (rt && rt.checked) rt.checked = false;
  const cur = document.getElementById('date-day').value || localToday();
  const d = new Date(cur + 'T00:00:00');
  d.setDate(d.getDate() + deltaDays);
  const next = isoDay(d);
  if (next > localToday()) return;          // never go past today
  document.getElementById('date-day').value = next;
  onDayChanged();
}

function setDayNavEnabled() {
  const on = recentMode();
  ['day-prev', 'day-next', 'date-day'].forEach(id => {
    const el = document.getElementById(id); if (el) el.disabled = on;
  });
  if (!on) document.getElementById('day-next').disabled =
    (document.getElementById('date-day').value >= localToday());
}

function onDayChanged() {
  // Picking a specific day leaves Recent mode.
  const rt = document.getElementById('recent-toggle');
  if (rt && rt.checked) rt.checked = false;
  setDayNavEnabled();
  updateDangerAvailability();
  updateBurntTime();
  updateRefreshUI();
  debouncedRefresh();
}

function setupDateControls() {
  const today = localToday();
  const monthAgo = new Date(); monthAgo.setDate(monthAgo.getDate() - 30);
  document.getElementById('date-day').value = today;
  document.getElementById('date-day').max = today;   // no future dates
  document.getElementById('date-end').value = today;
  document.getElementById('date-end').max = today;
  document.getElementById('date-start').value = isoDay(monthAgo);
  document.getElementById('date-start').max = today;
  updateDateBounds();   // don't let users pick a date before any sensor has data

  document.getElementById('day-prev').addEventListener('click', () => shiftDay(-1));
  document.getElementById('day-next').addEventListener('click', () => shiftDay(1));
  document.getElementById('date-day').addEventListener('change', onDayChanged);

  // Recent (rolling 48h) toggle — default on. Turning it off enables day picking;
  // turning it back on returns to the rolling window AND resets the day picker
  // back to today (otherwise it's left showing whatever day was picked while
  // Recent was off, which breaks "next"/disabled-state and looks stuck).
  document.getElementById('recent-toggle').addEventListener('change', (e) => {
    if (e.target.checked) {
      const dayInput = document.getElementById('date-day');
      dayInput.value = localToday();
      document.getElementById('day-next').disabled = true;
    }
    setDayNavEnabled();
    updateDangerAvailability();
    updateBurntTime();
    updateRefreshUI();
    debouncedRefresh();
  });

  const toggle = document.getElementById('period-toggle');
  const dayNav = document.getElementById('day-nav');
  const fields = [document.getElementById('period-fields'),
                  document.getElementById('period-fields-to')];
  toggle.addEventListener('change', () => {
    const on = toggle.checked;
    dayNav.hidden = on;                    // swap the single-date nav for the range
    fields.forEach(f => { if (f) f.hidden = !on; });
    updateDangerAvailability();
    updateBurntTime();
    updateRefreshUI();
    debouncedRefresh();
  });
  const onPeriodEdit = () => { updateBurntTime(); debouncedRefresh(); };
  document.getElementById('date-start').addEventListener('change', onPeriodEdit);
  document.getElementById('date-end').addEventListener('change', onPeriodEdit);

  document.getElementById('refresh-now').addEventListener('click', serverRefreshToday);
  setDayNavEnabled();          // Recent is on by default → day nav disabled initially
}

// "Refresh now": ask the server to pull fresh FIRMS data for today, wait for it,
// then reload the points. Shows a "please wait" state on the button meanwhile.
let _refreshing = false;
async function serverRefreshToday() {
  if (_refreshing) return;
  _refreshing = true;
  const btn = document.getElementById('refresh-now');
  const lbl = document.getElementById('refresh-now-lbl');
  const info = document.getElementById('refresh-info');
  const origLbl = lbl.textContent;
  btn.disabled = true;
  lbl.textContent = tx('fires.refreshing', 'Fetching latest…');
  if (info) info.textContent = tx('fires.please_wait', 'Please wait — pulling the latest detections…');
  try {
    const r = await fetch('/api/fires/refresh', { method: 'POST' });
    if (r.status === 429) {
      if (info) info.textContent = tx('fires.rate_limited', 'Too many refreshes — please wait a moment.');
    } else if (r.status === 409) {
      if (info) info.textContent = tx('fires.refresh_busy', 'A refresh is already running…');
    } else if (!r.ok) {
      if (info) info.textContent = tx('fires.refresh_failed', 'Refresh failed — showing existing data.');
    }
  } catch (e) {
    if (info) info.textContent = tx('fires.refresh_failed', 'Refresh failed — showing existing data.');
  } finally {
    lbl.textContent = origLbl;
    btn.disabled = false;
    _refreshing = false;
    await refreshPoints(true);              // reload points + update "last updated"
  }
}

// ── Refresh status + hourly auto-refresh (today only) ──────────────────────────
let _autoTimer = null;
let _lastRefreshAt = null;
let _latestDetection = null;   // newest detection time (UTC) reported by the server

function updateRefreshUI() {
  const row = document.getElementById('refresh-row');
  const show = viewingToday();
  row.hidden = !show;
  if (_autoTimer) { clearInterval(_autoTimer); _autoTimer = null; }
  if (show) {
    renderLastRefresh();
    // Auto-refresh today's detections every hour.
    _autoTimer = setInterval(() => refreshPoints(true), 60 * 60 * 1000);
  }
}

function fmtLocal(d) {
  try {
    return new Intl.DateTimeFormat(currentLang() || 'en',
      { timeZone: META.timezone, hour: '2-digit', minute: '2-digit',
        day: '2-digit', month: 'short' }).format(d);
  } catch (e) { return d.toLocaleString(); }
}

// Human "~Nh ago" / "~Nm ago" for the newest detection.
function agoText(d) {
  const mins = Math.max(0, Math.round((Date.now() - d.getTime()) / 60000));
  if (mins < 90) return tx('fires.ago_min', '~{n}m ago', { n: mins });
  return tx('fires.ago_hr', '~{n}h ago', { n: Math.round(mins / 60) });
}

function renderLastRefresh() {
  const el = document.getElementById('refresh-info');
  if (!el) return;
  const parts = [];
  // Primary: how fresh the actual fire data is (newest detection).
  if (_latestDetection && !isNaN(_latestDetection)) {
    parts.push(tx('fires.latest_detection', 'Latest detection: {t} ({ago})',
      { t: fmtLocal(_latestDetection), ago: agoText(_latestDetection) }));
  }
  // Secondary: when the server last pulled from the sources.
  if (_lastRefreshAt && !isNaN(_lastRefreshAt)) {
    parts.push(tx('fires.checked', 'checked {t}', { t: fmtLocal(_lastRefreshAt) }));
  }
  el.textContent = parts.join(' · ');
}

// ── Init ───────────────────────────────────────────────────────────────────────
(async function init() {
  META = await fetch('/api/meta').then(r => r.json());
  if (!META.features?.fires_map || !META.fires) {
    document.body.innerHTML = '<p style="padding:60px;text-align:center;color:#6B655B">The wildfire page is not enabled.</p>';
    return;
  }
  FIRES = META.fires;
  await loadLocale(`${currentLang()}_default`);

  applyLabels();
  buildLangSelector();
  buildSensorChips();
  setupDateControls();
  buildMap();
  updateDangerAvailability();   // danger available on load (defaults to "today")
  updateRefreshUI();            // show last-refresh + start hourly auto-refresh
  renderDataExplain();
  renderDataSources();
  await refreshPoints();
  await renderYearChart();
})();
