/* MK Climate Explorer — Highcharts frontend */

"use strict";

// ── Helpers ───────────────────────────────────────────────────────────────────

const wait = ms => new Promise(res => setTimeout(res, ms));

// ── Locale system ─────────────────────────────────────────────────────────────
// JSON files live in static/locales/{lang}_{style}.json
// t(key, vars)  — interpolate a string, e.g. t('today.explain1')
// tArr(key)     — return an array, or null if missing / not an array
// loadLocale()  — called once in init(); page reloads on locale switch

let _locale = null;

async function loadLocale(name) {
  try {
    const r = await fetch(`locales/${name}.json`);
    if (!r.ok) throw new Error(r.status);
    _locale = await r.json();
  } catch (e) {
    console.warn(`Locale '${name}' failed to load — using hardcoded defaults`);
    _locale = null;
  }
}

function t(key, vars = {}) {
  if (!_locale) return key;
  const parts = key.split('.');
  let val = _locale;
  for (const p of parts) { val = val?.[p]; if (val === undefined) return key; }
  if (typeof val !== 'string') return key;
  return val.replace(/\{(\w+)\}/g, (_, k) => (vars[k] !== undefined ? vars[k] : `{${k}}`));
}

function tArr(key) {
  if (!_locale) return null;
  const parts = key.split('.');
  let val = _locale;
  for (const p of parts) { val = val?.[p]; if (val === undefined) return null; }
  return Array.isArray(val) ? val : null;
}

// Return localised display name for a location (falls back to the canonical key)
function locName(name) {
  return _locale?.locations?.[name] || name;
}

// ── Color constants ───────────────────────────────────────────────────────────

const ACCENT   = "#C25A2C";
const COOL     = "#3a5a8a";
const INK      = "#0E0E0C";
const INK_SOFT = "#6B655B";

// ── State ─────────────────────────────────────────────────────────────────────

const state = {
  locations:  [],
  variables:  {},
  monthNames: [],
  palette:    [],
  selLocs:    ["Skopje"],
  selVar:     "temperature_max",
  method:     "theilsen",
  corr:       "raw",
  doy:        105,
  window:     7,
  playing:    false,
};

// ── Preferences (localStorage persistence) ───────────────────────────────────

const _PREFS_KEY        = 'mk_prefs';
const _PREFS_ENABLED_KEY = 'mk_save_prefs';
const _PREFS_TTL_MS     = 3 * 24 * 60 * 60 * 1000;   // 3 days of inactivity → reset to defaults

// Whether the user has opted in to saving preferences (default: off)
let _savePrefsEnabled = localStorage.getItem(_PREFS_ENABLED_KEY) === 'true';

function savePrefs() {
  if (!_savePrefsEnabled) return;
  try {
    localStorage.setItem(_PREFS_KEY, JSON.stringify({
      ts:      Date.now(),
      selLocs: state.selLocs,
      selVar:  state.selVar,
      method:  state.method,
      corr:    state.corr,
      window:  state.window,
      doy:     state.doy,
    }));
  } catch (_) {}
}

/**
 * Restore saved prefs into state and sync all UI controls.
 * Must be called after meta is loaded so we can validate locations/variables.
 * Returns true if selLocs were restored (so init() can skip auto-select).
 */
function loadPrefs(validLocs, validVars) {
  try {
    const p = JSON.parse(localStorage.getItem(_PREFS_KEY) || 'null');
    if (!p) return false;
    // Expired — treat as new user, clear stored prefs
    if (p.ts && Date.now() - p.ts > _PREFS_TTL_MS) {
      localStorage.removeItem(_PREFS_KEY);
      return false;
    }
    let locsRestored = false;
    if (Array.isArray(p.selLocs)) {
      const valid = p.selLocs.filter(l => validLocs.includes(l));
      if (valid.length) { state.selLocs = valid; locsRestored = true; }
    }
    if (p.selVar && validVars[p.selVar])                           state.selVar = p.selVar;
    if (p.method && ['theilsen','ols'].includes(p.method))         state.method = p.method;
    if (p.corr   && ['raw','corr'].includes(p.corr))               state.corr   = p.corr;
    if (Number.isInteger(p.window) && p.window >= 1)               state.window = p.window;
    if (Number.isInteger(p.doy)    && p.doy >= 1 && p.doy <= 365) state.doy    = p.doy;

    // Sync UI controls to restored state
    const varSel = document.getElementById('var-select');
    if (varSel) varSel.value = state.selVar;

    const methodEl = document.querySelector(`input[name='method'][value='${state.method}']`);
    if (methodEl) {
      methodEl.checked = true;
      document.querySelectorAll('.pill-radio').forEach(p => p.classList.remove('active'));
      methodEl.closest('.pill-radio')?.classList.add('active');
      const checkTheilsen = document.getElementById('check-theilsen');
      const checkOls      = document.getElementById('check-ols');
      if (checkTheilsen) checkTheilsen.style.display = state.method === 'theilsen' ? '' : 'none';
      if (checkOls)      checkOls.style.display      = state.method === 'ols'      ? '' : 'none';
    }

    const corrToggle = document.getElementById('corr-toggle');
    if (corrToggle) corrToggle.checked = state.corr === 'corr';

    const corrSection = document.getElementById('corr-section');
    if (corrSection) corrSection.style.display = isTemp(state.selVar) ? '' : 'none';

    const winInput = document.getElementById('window-input');
    if (winInput) winInput.value = state.window;

    return locsRestored;
  } catch (_) { return false; }
}

// Calendar cache: key → data (avoids re-fetching)
const calCache = {};

// ── Highcharts global theme ────────────────────────────────────────────────────

Highcharts.setOptions({
  chart: {
    backgroundColor: "transparent",
    style: { fontFamily: "'Space Grotesk', system-ui, sans-serif" },
    animation: false,
  },
  colors: [ACCENT, COOL, "#2a9d5c", "#e07b00", "#9b4dca"],
  title:    { style: { color: INK, fontSize: "13px", fontWeight: "600", fontFamily: "'Space Grotesk', sans-serif" } },
  subtitle: { style: { color: INK_SOFT, fontSize: "11px", fontFamily: "'JetBrains Mono', monospace" } },
  xAxis: {
    labels:        { style: { color: INK_SOFT, fontSize: "10px", fontFamily: "'JetBrains Mono', monospace" } },
    lineColor:     "rgba(14,14,12,0.1)",
    tickColor:     "rgba(14,14,12,0.1)",
    gridLineColor: "rgba(14,14,12,0.06)",
  },
  yAxis: {
    labels:        { style: { color: INK_SOFT, fontSize: "10px", fontFamily: "'JetBrains Mono', monospace" } },
    lineColor:     "rgba(14,14,12,0.1)",
    tickColor:     "rgba(14,14,12,0.1)",
    gridLineColor: "rgba(14,14,12,0.06)",
    title:         { style: { color: INK_SOFT, fontSize: "10px", fontFamily: "'JetBrains Mono', monospace" } },
  },
  legend: {
    itemStyle:       { color: INK, fontSize: "12px", fontWeight: "400", fontFamily: "'Space Grotesk', sans-serif" },
    itemHoverStyle:  { color: "#000" },
    itemHiddenStyle: { color: "#aaa" },
  },
  tooltip: {
    backgroundColor: "#ffffff",
    borderColor:     "rgba(14,14,12,0.14)",
    style:           { color: INK, fontSize: "12px", fontFamily: "'Space Grotesk', sans-serif" },
    shadow:          { color: "rgba(0,0,0,0.10)", offsetX: 0, offsetY: 2, opacity: 1, width: 8 },
  },
  credits: { enabled: false },
  exporting: { enabled: false },
});

// ── Charts ────────────────────────────────────────────────────────────────────

let regChart  = null;
let calCharts = [];
let mapChart      = null;
let _mapHoveredLoc = null;   // last station confirmed by nearest-center hover
let _mkTopo  = null;
let _mapUnit  = "";

function initRegChart() {
  regChart = Highcharts.chart("reg-chart", {
    chart: { type: "scatter", zoomType: "x", marginTop: 40, backgroundColor: "transparent" },
    title:    { text: "Loading…" },
    subtitle: { text: "" },
    xAxis:  { title: { text: "Year" }, crosshair: true },
    yAxis:  { title: { text: "" } },
    legend: { enabled: true },
    series: [],
    plotOptions: {
      series:  { animation: false },
      scatter: { marker: { radius: 5, symbol: "circle" }, enableMouseTracking: true },
    },
    tooltip: {
      formatter() {
        const pt = this.point;
        if (pt.anomaly !== undefined) {
          return `<b>${this.series.name}</b><br>Year: ${pt.x}<br>Value: ${pt.y}<br>Anomaly: ${pt.anomaly > 0 ? "+" : ""}${pt.anomaly}`;
        }
        return `<b>${this.series.name}</b><br>${Math.round(this.x)}: ${this.y}`;
      },
    },
  });
  requestAnimationFrame(() => regChart?.reflow());
}


// ── Map helpers ───────────────────────────────────────────────────────────────

function lerp(a, b, t) { return Math.round(a + (b - a) * t); }

function buildScales(points) {
  const pos = points.filter(p => p.trend10 >= 0).map(p => p.trend10);
  const neg = points.filter(p => p.trend10 <  0).map(p => Math.abs(p.trend10));
  return {
    maxPos: pos.length ? Math.max(...pos) : 0.001,
    maxNeg: neg.length ? Math.max(...neg) : 0.001,
    maxAbs: Math.max(...points.map(p => Math.abs(p.trend10)), 0.001),
    minAbs: Math.min(...points.map(p => Math.abs(p.trend10)), 0),
  };
}

function pointColor(trend10, scales) {
  const neutral  = [220, 220, 228];
  const pos      = isPrecipLike(state.selVar) ? [ 26,  95, 200] : [160,   0,   0];
  const neg      = isPrecipLike(state.selVar) ? [160,  92,  32] : [  0,  45, 175];
  if (trend10 >= 0) {
    const t = Math.pow(trend10 / scales.maxPos, 0.65);
    return `rgb(${lerp(neutral[0],pos[0],t)},${lerp(neutral[1],pos[1],t)},${lerp(neutral[2],pos[2],t)})`;
  }
  const t = Math.pow(Math.abs(trend10) / scales.maxNeg, 0.65);
  return `rgb(${lerp(neutral[0],neg[0],t)},${lerp(neutral[1],neg[1],t)},${lerp(neutral[2],neg[2],t)})`;
}

function pointZ(trend10, scales) {
  const abs  = Math.abs(trend10);
  const norm = (abs - scales.minAbs) / (scales.maxAbs - scales.minAbs || 0.001);
  return Math.pow(norm, 0.6) + 0.05;
}

// ── Map API + render ──────────────────────────────────────────────────────────

async function fetchTrends() {
  const params = new URLSearchParams({
    var:    state.selVar,
    doy:    state.doy,
    window: state.window,
    corr:   state.corr,
    method: state.method,
  });
  const r = await fetch("api/trends?" + params);
  if (!r.ok) throw new Error("trends " + r.status);
  return r.json();
}

function syncLocationCheckboxes() {
  document.querySelectorAll("#loc-list input[type='checkbox']").forEach(cb => {
    cb.checked = state.selLocs.includes(cb.value);
  });
}

function updateMapSelection() {
  if (!mapChart) return;
  mapChart.series[1].data.forEach(pt => {
    pt.update({
      marker: {
        lineWidth: state.selLocs.includes(pt.name) ? 3 : 0,
        lineColor: INK,
      }
    }, false);
  });
  mapChart.redraw(false);
}

function renderMap(data) {
  const points = data.points;
  _mapUnit = data.unit;
  const scales = buildScales(points);

  const mapPoints = points.map(p => ({
    name:  p.loc,
    lat:   p.lat,
    lon:   p.lon,
    value: p.trend10,
    z:     pointZ(p.trend10, scales),
    p_val: p.p_val,
    sig:   p.sig_label,
    color: pointColor(p.trend10, scales),
    marker: {
      lineWidth: state.selLocs.includes(p.loc) ? 3 : 0,
      lineColor: INK,
    },
  }));

  // Update station count + variable label
  const countEl = document.getElementById("map-station-count");
  const mapVarLbl = (state.variables[state.selVar] || state.selVar).split("(")[0].trim();
  if (countEl) countEl.textContent = `${mapVarLbl} · ${points.length} STATIONS`;

  const mapLeg = document.getElementById("map-legend");
  if (mapLeg) mapLeg.classList.toggle("precip", isPrecipLike(state.selVar));

  if (mapChart) {
    mapChart.series[1].setData(mapPoints, true);
    return;
  }

  mapChart = Highcharts.mapChart("map-chart", {
    chart: {
      backgroundColor: "#F5F2EC",
      style: { fontFamily: "'Space Grotesk', system-ui, sans-serif" },
      animation: false,
      margin: [10, 10, 10, 10],
    },
    title:    { text: null },
    subtitle: { text: null },
    credits:  { enabled: false },
    legend:   { enabled: false },
    mapNavigation: { enabled: true, buttonOptions: { verticalAlign: "bottom" } },
    plotOptions: {
      series: {
        states: { inactive: { opacity: 1 } },
      },
    },
    tooltip: {
      useHTML: true,
      backgroundColor: "#ffffff",
      borderColor: "rgba(14,14,12,0.14)",
      style: { fontSize: "13px", fontFamily: "'Space Grotesk', sans-serif" },
      formatter() {
        const sign = this.point.value >= 0 ? "+" : "";
        const col  = this.point.color;
        return `<span style="font-weight:600">${locName(this.point.name)}</span><br>
                <span style="color:${col};font-weight:700">${sign}${this.point.value.toFixed(3)} ${_mapUnit}/dec</span><br>
                <span style="color:${INK_SOFT};font-size:11px">${this.point.sig}</span>`;
      },
    },
    series: [
      {
        type: "map",
        mapData: _mkTopo,
        color: "#EFEBE2",
        borderColor: "rgba(14,14,12,0.18)",
        borderWidth: 1.25,
        enableMouseTracking: false,
        nullColor: "#EFEBE2",
        states: { hover: { enabled: false }, inactive: { opacity: 1 } },
      },
      {
        type: "mapbubble",
        name: "Locations",
        data: mapPoints,
        cursor: "pointer",
        minSize: 10,
        maxSize: 80,
        // Use nearest center for hover/tooltip instead of bubble-radius containment
        findNearestPointBy: "xy",
        stickyTracking: false,
        dataLabels: {
          enabled: true,
          formatter() { return locName(this.point.name); },
          style: { fontSize: "9px", fontWeight: "400", color: INK, textOutline: "2px #fff", fontFamily: "'JetBrains Mono', monospace" },
          y: 0,
        },
        point: {
          events: {
            // findNearestPointBy:'xy' ensures mouseOver fires on the correct
            // nearest-center station — record it so click can use it reliably.
            mouseOver() { _mapHoveredLoc = this.name; },
            click() {
              // Use the last hovered station (nearest-center) rather than
              // 'this', which Highcharts resolves via bubble-radius hit area.
              const loc = _mapHoveredLoc || this.name;
              if (state.selLocs.includes(loc)) {
                if (state.selLocs.length > 1)
                  state.selLocs = state.selLocs.filter(l => l !== loc);
              } else {
                if (state.selLocs.length < 6)
                  state.selLocs.push(loc);
              }
              syncLocationCheckboxes();
              updateLocCheckboxStates();
              updateLocDisplay();
              updateMapSelection();
              savePrefs();
              refreshRegression();
              refreshCalendar();
            },
          },
        },
      },
    ],
  });
  requestAnimationFrame(() => mapChart?.reflow());
}

async function refreshMap() {
  showLoading("map-loading", true);
  try {
    const data = await fetchTrends();
    renderMap(data);
  } catch(e) {
    console.error("Map error:", e);
  } finally {
    showLoading("map-loading", false);
  }
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function doyToDate(doy) {
  const d = new Date(2001, 0, doy);
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

function doyBadge(doy) {
  return doyToDate(doy);
}



function getTodayDOY() {
  const now = new Date();
  // Use UTC arithmetic to avoid DST skew (a spring-forward day is 23 h,
  // which makes floor(ms/86400000) land one day early).
  const start = Date.UTC(now.getFullYear(), 0, 0);
  const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((today - start) / 86400000);
}


function isTemp(v) {
  return ["temperature_max","temperature_min","temperature_mean"].includes(v);
}

function isPrecipLike(v) {
  return v === "precipitation_sum" || v === "et0_evapotranspiration";
}

function showLoading(id, show) {
  document.getElementById(id).classList.toggle("show", show);
}

// ── API calls ─────────────────────────────────────────────────────────────────

async function fetchRegression() {
  const params = new URLSearchParams();
  state.selLocs.forEach(l => params.append("loc", l));
  params.set("var",    state.selVar);
  params.set("doy",    state.doy);
  params.set("window", state.window);
  params.set("corr",   state.corr);
  params.set("method", state.method);

  const r = await fetch("api/regression?" + params);
  if (!r.ok) throw new Error("regression " + r.status);
  return r.json();
}

async function fetchCalendar(loc) {
  const key = `${loc}|${state.selVar}|${state.corr}|${state.window}|${state.method}`;
  if (calCache[key]) return calCache[key];

  const params = new URLSearchParams({
    loc:    loc,
    var:    state.selVar,
    window: state.window,
    corr:   state.corr,
    method: state.method,
  });
  const r = await fetch("api/calendar?" + params);
  if (!r.ok) throw new Error("calendar " + r.status);
  const data = await r.json();
  calCache[key] = data;
  return data;
}

// ── Render regression ─────────────────────────────────────────────────────────

function renderRegression(data) {
  const { results, date_label, ylabel } = data;

  // Remove all existing series
  while (regChart.series.length) regChart.series[0].remove(false);

  // Build series array
  const series = [];

  results.forEach(res => {
    const color = res.color;

    // CI band (arearange)
    const bandData = res.line.x.map((x, i) => [x, res.line.lower[i], res.line.upper[i]]);
    series.push({
      type: "arearange",
      name: res.loc + " CI",
      data: bandData,
      color: color,
      fillOpacity: 0.12,
      lineWidth: 0,
      marker: { enabled: false },
      enableMouseTracking: false,
      showInLegend: false,
      zIndex: 1,
    });

    // Trend line
    const lineData = res.line.x.map((x, i) => [x, res.line.y[i]]);
    series.push({
      type: "line",
      name: res.loc,
      data: lineData,
      color: res.color,
      lineWidth: 2,
      marker: { enabled: false },
      zIndex: 2,
      showInLegend: true,
    });

    // Scatter dots
    const scatterData = res.scatter.map(pt => ({
      x: pt.x,
      y: pt.y,
      anomaly: pt.anomaly,
      color: pt.color,
      marker: { fillColor: pt.color },
    }));
    series.push({
      type: "scatter",
      name: res.loc + " data",
      data: scatterData,
      color: color,
      zIndex: 3,
      showInLegend: false,
      marker: { radius: 4, symbol: "circle" },
    });
  });

  // Add all series
  series.forEach(s => regChart.addSeries(s, false));

  regChart.setTitle(
    { text: "" },
    { text: "" },
    false
  );
  regChart.yAxis[0].setTitle({ text: ylabel }, false);
  regChart.redraw(false);

  // Update legend swatch colors from actual scatter data (varies by variable)
  const allScatter = results.flatMap(r => r.scatter || []);
  const posPoint = allScatter.filter(p => p.anomaly > 0).sort((a, b) => b.anomaly - a.anomaly)[0];
  const negPoint = allScatter.filter(p => p.anomaly < 0).sort((a, b) => a.anomaly - b.anomaly)[0];
  const swPos = document.getElementById("swatch-pos");
  const swNeg = document.getElementById("swatch-neg");
  if (swPos && posPoint) swPos.style.background = posPoint.color;
  if (swNeg && negPoint) swNeg.style.background = negPoint.color;

  // Panel header + annotations
  if (results.length) {
    const r0   = results[0];
    const st0  = r0.stats;
    const ymin = r0.year_min || "";
    const ymax = r0.year_max || "";
    const varLbl = (state.variables[state.selVar] || state.selVar).split("(")[0].trim();

    const titleEl = document.getElementById("chart-title");
    if (titleEl) titleEl.textContent = `${varLbl} · ${results.map(r => locName(r.loc)).join(", ")}`;

    const subEl = document.getElementById("chart-sub");
    if (subEl) subEl.textContent = `${date_label}${ymin ? " · " + ymin + " – " + ymax : ""}`;

    const obsEl = document.getElementById("chart-obs");
    if (obsEl) obsEl.textContent = st0.n_years ? `${st0.n_years} YRS · ${(st0.n_values || "").toLocaleString()} OBS` : "";

    const yrEl = document.getElementById("chart-year-range");
    if (yrEl) yrEl.textContent = ymin && ymax ? `${ymin} – ${ymax}` : "";

    // Change over record annotation
    const ly  = r0.line.y;
    const chg = ly[ly.length - 1] - ly[0];
    const csg = chg >= 0 ? "+" : "−";
    const chgEl = document.getElementById("chart-change");
    if (chgEl) chgEl.textContent = `${csg}${Math.abs(chg).toFixed(2)} ${data.unit || ""}`;
    if (chgEl) chgEl.style.color = isPrecipLike(state.selVar) ? (chg >= 0 ? COOL : ACCENT) : (chg >= 0 ? ACCENT : COOL);

    // Baseline plotlines — one per location, colored to match their series
    // Remove all existing baseline plotlines before re-adding (handles deselected locations)
    (regChart.yAxis[0].plotLinesAndBands || [])
      .filter(pl => pl.id && pl.id.startsWith("baseline-"))
      .forEach(pl => regChart.yAxis[0].removePlotLine(pl.id));
    results.forEach(res => {
      regChart.yAxis[0].addPlotLine({
        id:        `baseline-${res.loc}`,
        value:     res.baseline,
        color:     res.color,
        width:     1,
        dashStyle: "Dash",
        zIndex:    2,
        label: {
          text:  `${res.stats.n_years}-YR MEAN ${res.baseline.toFixed(1)}`,
          align: "right",
          x:     -4,
          style: { color: res.color, fontSize: "9px", fontFamily: "'JetBrains Mono', monospace" },
        },
      });
    });
  }
}

// ── Render hero cards (hero + sig merged, one card per location) ──────────────

function renderHeroCards(data) {
  if (!data.results.length) return;

  const unit       = data.unit || "";
  const hasDegreeSym = unit.startsWith("°");
  const unitSuffix   = hasDegreeSym ? unit.slice(1) : unit;
  const degSpan      = hasDegreeSym ? `<span class="stat-deg">°</span>` : "";
  const n       = data.results.length;
  const isMulti = n > 1;
  const hero    = document.getElementById("hero");
  if (!hero) return;

  const doyLabel = doyToDate(state.doy);
  const varLabel = (state.variables[state.selVar] || state.selVar).toLowerCase().replace(/\s*\(.*\)/, "");

  hero.className = `hero-cards-${n}`;

  function stars(p) {
    if (p < 0.001) return `<span class="sig-stars">★★★</span>&nbsp;p &lt; 0.001`;
    if (p < 0.01)  return `<span class="sig-stars">★★</span>&nbsp;p &lt; 0.01`;
    if (p < 0.05)  return `<span class="sig-stars">★</span>&nbsp;p &lt; 0.05`;
    return `<span class="sig-stars" style="opacity:0.25">★</span>&nbsp;p = ${typeof p === "number" ? p.toFixed(3) : p}`;
  }
  function sampleHtml(st) {
    return st.n_values
      ? `${st.n_values.toLocaleString()} <span class="sig-muted">obs · ${st.n_years} yrs</span>`
      : `${st.n_years} <span class="sig-muted">years</span>`;
  }
  function ar1Html(st) {
    const v = st.ar1 != null ? st.ar1 : "—";
    const d = st.ar1 != null
      ? (Math.abs(st.ar1) < 0.1 ? "negligible" : Math.abs(st.ar1) < 0.3 ? "weak" : "moderate")
      : "";
    return `AR(1) = ${v}${d ? `<span class="sig-muted"> · ${d}</span>` : ""}`;
  }

  hero.innerHTML = data.results.map(res => {
    const st    = res.stats;
    const isPos = st.trend10 >= 0;
    const sg    = isPos ? "+" : "−";
    const col   = isPrecipLike(state.selVar) ? (isPos ? COOL : ACCENT) : (isPos ? ACCENT : COOL);

    // Verdict block — temperature variables only
    const tempVar = isTemp(state.selVar);
    const verdictText = tempVar
      ? (st.trend10 !== 0
          ? (() => {
              const t100 = Math.abs(st.trend10 * 10).toFixed(2);
              const yrs  = Math.abs(10 / st.trend10).toFixed(1);
              const key  = isPos ? "hero.verdict_warming" : "hero.verdict_cooling";
              return t(key, {sign: sg, t100, unit, yrs});
            })()
          : t("hero.verdict_none"))
      : null;
    const methodText = st.method === "OLS" ? t("hero.method_ols") : t("hero.method_theilsen");

    return `<div class="loc-hero-card">
      <div class="loc-hero-main">
        <div class="hero-left">
          <div class="eyebrow">
            <span class="eyebrow-city">${locName(res.loc)}</span>
            <span class="pip"></span>
            <span>${doyLabel} ±${state.window} days · ${varLabel}</span>
          </div>
          <div class="stat">
            <span class="stat-num"><span class="stat-sign" style="color:${col}">${sg}</span>${Math.abs(st.trend10)}</span>
            <span class="stat-unit">${degSpan}${unitSuffix} / decade</span>
          </div>
        </div>
        ${verdictText !== null ? `<div class="hero-right">
          <div class="verdict">${verdictText}</div>
          <div class="verdict-sub">${methodText}</div>
        </div>` : ""}
      </div>
      ${state.selVar === 'temperature_max' ? (() => {
        const cat    = trendCategory(st.trend10);
        const label  = t(`hero_category.${cat}`) || cat;
        const ctx    = t(`hero_context.${res.loc}.${cat}`) || t(`hero_context.${res.loc}.baseline`);
        const catTip = '< 0.05 °C/dec — Baseline\n0.05–0.10 °C/dec — Moderate\n0.10–0.20 °C/dec — Bad\n0.20–0.30 °C/dec — Extreme\n> 0.30 °C/dec — Catastrophic';
        return `<div class="hero-context-block">
          <div style="display:flex;align-items:center;gap:0">
            <span class="trend-badge trend-badge--${cat}">${label}</span><span class="tip-icon" data-tooltip="${catTip}"><svg width="12" height="12" viewBox="0 0 12 12" fill="none"><circle cx="6" cy="6" r="5" stroke="currentColor" stroke-width="1.1"/><path d="M6 5.2v3M6 3.8h.01" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg></span>
          </div>
          ${ctx ? `<p class="hero-context-text">${ctx}</p>` : ''}
        </div>`;
      })() : ''}
      ${(() => {
        const metricTip = st.method === 'OLS'
          ? 'R²: proportion of variance\nexplained by the linear trend\n(0 = no fit, 1 = perfect fit)'
          : "Kendall's τ: rank correlation\nbetween time and values\n(−1 to +1, sign = direction)";
        return `<div class="sig-row${state.selVar === 'temperature_max' ? ' sig-row--compact' : ''}">
          <div class="sig-item" data-tooltip="Mann-Kendall p-value\nProbability the trend is random.\n★★★ p<0.001 · ★★ p<0.01 · ★ p<0.05"><span class="sig-k">Significance</span><span class="sig-v">${stars(st.p_val)}</span></div>
          <div class="sig-item" data-tooltip="${metricTip}"><span class="sig-k">${st.metric_lbl}</span><span class="sig-v">${st.metric}</span></div>
          <div class="sig-item" data-tooltip="Annual observations and years\nof ERA5-Land data used\nfor the trend calculation"><span class="sig-k">Sample</span><span class="sig-v">${sampleHtml(st)}</span></div>
          <div class="sig-item" data-tooltip="AR(1) serial correlation:\nyear-to-year auto-correlation.\nHigh values reduce effective\nsample size; TFPW corrects for this"><span class="sig-k">Autocorrelation</span><span class="sig-v">${ar1Html(st)}</span></div>
        </div>`;
      })()}
    </div>`;
  }).join("");

}

let _todayOffset   = 0;
let _todayViewDate = null; // null = today, 'YYYY-MM-DD' = browsed past date

// ── Today flag SVG builder ────────────────────────────────────────────────────
const _TF_SUN = "m-140 14v-28l280 28v-28zm126-84h28L0-15zM14 70h-28L0 15zM-140-70h42L12.86 7.72zm0 140h42L12.86-7.72zM140-70H98L-12.86 7.72zm0 140H98L-12.86-7.72z";
function _tfSnowPath(r) {
  const f = n => n.toFixed(2); let d = '';
  for (let i = 0; i < 6; i++) {
    const a = i*Math.PI/3, ax = Math.cos(a), ay = Math.sin(a);
    d += `M0,0L${f(r*ax)},${f(r*ay)}`;
    const bx1=0.4*r*ax, by1=0.4*r*ay, b1=0.3*r;
    d += `M${f(bx1)},${f(by1)}L${f(bx1+b1*Math.cos(a+Math.PI/3))},${f(by1+b1*Math.sin(a+Math.PI/3))}`;
    d += `M${f(bx1)},${f(by1)}L${f(bx1+b1*Math.cos(a-Math.PI/3))},${f(by1+b1*Math.sin(a-Math.PI/3))}`;
    const bx2=0.7*r*ax, by2=0.7*r*ay, b2=0.2*r;
    d += `M${f(bx2)},${f(by2)}L${f(bx2+b2*Math.cos(a+Math.PI/3))},${f(by2+b2*Math.sin(a+Math.PI/3))}`;
    d += `M${f(bx2)},${f(by2)}L${f(bx2+b2*Math.cos(a-Math.PI/3))},${f(by2+b2*Math.sin(a-Math.PI/3))}`;
  }
  return d;
}
const _TF_SNOW_POS = [
  [-118,52,8,1.9,0.00],[-60,24,6,2.3,0.45],[-20,60,9,1.6,0.90],[28,-52,7,2.1,0.25],
  [82,44,8,1.8,1.10],[118,-32,7,2.4,0.60],[132,56,6,1.7,1.50],[-82,-50,9,2.2,0.35],
  [-6,-62,7,2.0,0.95],[62,-60,8,1.5,0.50],[-132,-22,7,1.9,1.20],[102,65,9,2.1,0.05],
  [-38,-28,6,1.7,1.40],[52,66,7,2.3,0.75],[-2,40,6,1.6,1.65],[138,-58,7,2.0,0.30],
];
const _TF_CLOUD_DEF = {
  s: '<ellipse cx="6" cy="0" rx="8" ry="6"/><ellipse cx="16" cy="-4" rx="10" ry="8"/><ellipse cx="27" cy="0" rx="8" ry="6"/><rect x="-2" y="3" width="38" height="7" rx="2"/>',
  m: '<ellipse cx="8" cy="1" rx="9" ry="7"/><ellipse cx="20" cy="-5" rx="13" ry="10"/><ellipse cx="34" cy="-3" rx="11" ry="9"/><ellipse cx="46" cy="1" rx="9" ry="7"/><rect x="-1" y="4" width="57" height="8" rx="2"/>',
  l: '<ellipse cx="9" cy="2" rx="10" ry="8"/><ellipse cx="22" cy="-5" rx="14" ry="11"/><ellipse cx="38" cy="-7" rx="16" ry="12"/><ellipse cx="54" cy="-3" rx="13" ry="10"/><ellipse cx="68" cy="2" rx="10" ry="8"/><rect x="-1" y="4" width="80" height="9" rx="2"/>',
};
function _buildTodayFlag(catKey) {
  const P = _TF_SUN;
  const snow = catKey === 'freezing' ? _TF_SNOW_POS.map(([x,y,r,dur,del]) =>
    `<g transform="translate(${x},${y})"><path class="tf-snowflake" d="${_tfSnowPath(r)}" fill="none" stroke="rgba(210,235,255,0.95)" stroke-width="0.9" stroke-linecap="round" style="--dur:${dur}s;--delay:${del}s"/></g>`
  ).join('') : '';
  const clouds = catKey === 'cold' ? [
    [-48,1.0,0.72,38,0,'m'],[-22,0.65,0.52,52,8,'s'],[8,1.25,0.62,30,18,'l'],
    [38,0.80,0.48,44,4,'m'],[-60,0.55,0.38,58,26,'s'],[58,1.0,0.55,34,13,'l'],
  ].map(([y,sc,op,dur,del,sh]) =>
    `<g opacity="${op}" fill="rgba(255,248,248,0.82)"><g transform="scale(${sc})">${_TF_CLOUD_DEF[sh]}</g><animateTransform attributeName="transform" type="translate" from="-220 ${y}" to="220 ${y}" dur="${dur}s" begin="${del}s" repeatCount="indefinite"/></g>`
  ).join('') : '';
  const C = {
    freezing: {
      bg: '#0b1926',
      defs: '<filter id="tf-cg" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur in="SourceGraphic" stdDeviation="3.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>',
      body: `<g class="tf-cold-sun" filter="url(#tf-cg)"><path d="${P}" fill="#8ec4e0"/><circle r="22.5" fill="#b4d8f0" stroke="#0b1926" stroke-width="5"/></g>`,
    },
    cold: {
      bg: '#1c3460', defs: '',
      body: `<g class="tf-cool-sun"><path d="${P}" fill="#f0d830"/><circle r="22.5" fill="#f0d830" stroke="#1c3460" stroke-width="5"/></g>`,
    },
    nope: {
      bg: '#d82126', defs: '',
      body: `<g><path d="${P}" fill="#f8e92e"/><circle class="tf-avg-circle" r="22.5" fill="#f8e92e" stroke="#d82126" stroke-width="5"/></g>`,
    },
    hot: {
      bg: '#7d1000',
      defs: '<filter id="tf-hg" x="-45%" y="-45%" width="190%" height="190%"><feGaussianBlur in="SourceGraphic" stdDeviation="6.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter><filter id="tf-hh" x="-5%" y="-5%" width="110%" height="110%"><feTurbulence type="turbulence" baseFrequency="0.010 0.022" numOctaves="2" result="t"><animate attributeName="seed" from="0" to="40" dur="4s" repeatCount="indefinite"/></feTurbulence><feDisplacementMap in="SourceGraphic" in2="t" scale="2.2" xChannelSelector="R" yChannelSelector="G"/></filter>',
      body: `<g filter="url(#tf-hh)"><g class="tf-hot-sun" filter="url(#tf-hg)"><path d="${P}" fill="#ff8020"/><circle r="22.5" fill="#ffa030" stroke="#7d1000" stroke-width="5"/></g></g>`,
    },
    hell: {
      bg: 'url(#tf-hellg)',
      defs: '<radialGradient id="tf-hellg" cx="50%" cy="50%" r="58%"><stop offset="0%" stop-color="#420700"/><stop offset="100%" stop-color="#0d0100"/></radialGradient><filter id="tf-hhg" x="-55%" y="-55%" width="210%" height="210%"><feGaussianBlur in="SourceGraphic" stdDeviation="12" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter><filter id="tf-hhh" x="-7%" y="-7%" width="114%" height="114%"><feTurbulence type="turbulence" baseFrequency="0.022 0.046" numOctaves="3" result="t"><animate attributeName="seed" from="0" to="80" dur="1.6s" repeatCount="indefinite"/></feTurbulence><feDisplacementMap in="SourceGraphic" in2="t" scale="7" xChannelSelector="R" yChannelSelector="G"/></filter>',
      body: `<g filter="url(#tf-hhh)"><g class="tf-hell-sun" filter="url(#tf-hhg)"><path d="${P}" fill="#ff4c00"/><circle r="22.5" fill="#ffbe30" stroke="#160300" stroke-width="5"/><circle r="28" fill="rgba(255,100,0,0.18)" class="tf-hell-ember" style="animation-delay:0s"/><circle r="18" fill="rgba(255,140,0,0.22)" class="tf-hell-ember" style="animation-delay:.4s"/><circle fill="#fff" opacity=".92"><animate attributeName="r" values="7;13;7" dur="1.1s" repeatCount="indefinite"/><animate attributeName="opacity" values=".7;1;.7" dur="1.1s" repeatCount="indefinite"/></circle></g></g>`,
    },
  };
  const cfg = C[catKey] || C.nope;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="-140 -70 280 140"><defs>${cfg.defs}</defs><rect x="-140" y="-70" width="280" height="140" fill="${cfg.bg}"/>${cfg.body}${clouds}${snow}</svg>`;
}

// ── Render "Is it Hot in Macedonia Today?" ────────────────────────────────────

function _buildTodayCardInner(r) {
  return `
    <div class="today-h-row">
      <div class="today-h">${t('ui.title_today')}</div>
      <div class="today-temp-badge" style="background:${r.color};color:${r.category_key === 'nope' ? 'var(--ink)' : '#fff'}">${r.today_temp.toFixed(1)}°C</div>
    </div>
    <div class="today-body">
      <div class="today-flag-wrap">${_buildTodayFlag(r.category_key)}</div>
      <div class="today-text">
        <span class="today-cat">${_locale?.categories?.[r.category_key || r.category.toLowerCase()]?.name || r.category}</span><span class="today-sep-dot" style="background:${r.color}"></span><span class="today-desc">${(_locale?.categories?.[r.category_key || r.category.toLowerCase()]?.desc || r.description).replace('{d}', _fmtDay(r.month_num, r.day_num, r.day_label))}</span>
      </div>
    </div>
    <p class="today-explain">${t('today.explain1')}</p>
    ${t('today.climate_context') ? `<p class="today-context">${t('today.climate_context')}</p>` : ''}
    <div class="today-foot">
      ${_locale?.today?.foot
        ? t('today.foot', {temp: r.today_temp.toFixed(1), pct: r.percentile.toFixed(0), samples: r.n_samples.toLocaleString(), year_min: r.year_min, year_max: r.year_max})
        : `${r.today_temp.toFixed(1)} °C · ${r.percentile.toFixed(0)}th percentile · ${r.n_samples.toLocaleString()} samples (${r.year_min}–${r.year_max})`}
    </div>`;
}

function _updateTodayCard(r) {
  const dateEl = document.querySelector('#today-status .sec-heading-date');
  if (dateEl) dateEl.textContent = ` · ${_fmtDay(r.month_num, r.day_num, r.day_label)}`;

  const card = document.getElementById('today-main-card');
  if (card) card.innerHTML = _buildTodayCardInner(r);

  const distCard = document.getElementById('today-dist-card');
  if (distCard) {
    distCard.innerHTML = `<div class="today-chart-title">${t('today.chart_title', {day_label: _fmtDay(r.month_num, r.day_num, r.day_label), year_min: r.year_min})}</div><div id="today-dist-chart"></div>`;
    renderTodayChart(r);
  }

  const trendCard = document.getElementById('today-trend-card');
  if (trendCard) {
    trendCard.innerHTML = `<div class="today-chart-title" id="today-trend-title">Macedonia annual peak temperature · loading…</div><div id="today-trend-chart"></div>`;
    renderTodayTrendChart(_todayViewDate);
  }

  const prevBtn = document.getElementById('today-prev');
  const nextBtn = document.getElementById('today-next');
  if (prevBtn) prevBtn.disabled = false;
  if (nextBtn) nextBtn.disabled = _todayOffset === 0;
}

async function _navigateTodayTo(newOffset) {
  if (newOffset > 0) return;
  const prevBtn = document.getElementById('today-prev');
  const nextBtn = document.getElementById('today-next');
  if (prevBtn) prevBtn.disabled = true;
  if (nextBtn) nextBtn.disabled = true;
  try {
    const d = new Date();
    d.setDate(d.getDate() + newOffset);
    const dateStr = d.toISOString().slice(0, 10);
    const r = await fetch(`api/today_status?date=${dateStr}`).then(res => res.json());
    if (!r.available) {
      if (prevBtn) prevBtn.disabled = false;
      if (nextBtn) nextBtn.disabled = _todayOffset === 0;
      return;
    }
    _todayOffset   = newOffset;
    _todayViewDate = newOffset === 0 ? null : dateStr;
    _updateTodayCard(r);
  } catch {
    if (prevBtn) prevBtn.disabled = false;
    if (nextBtn) nextBtn.disabled = _todayOffset === 0;
  }
}

async function renderTodayStatus() {
  const el = document.getElementById("today-status");
  if (!el) return;
  try {
    const r = await fetch("api/today_status").then(res => res.json());
    if (!r.available) return;
    el.innerHTML = `
      <div class="sec-heading">
        <div class="today-heading-left">
          <span>${t('ui.heading_today')}<span class="sec-heading-date"></span></span>
          <div class="today-nav">
            <button id="today-prev" class="today-nav-btn" aria-label="Previous day"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg></button>
            <button id="today-next" class="today-nav-btn" aria-label="Next day" disabled><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg></button>
          </div>
        </div>
        <div id="share-widget">
          <button id="share-toggle" aria-label="Share this site">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>
          </button>
          <div id="share-popover" hidden>
            <button id="share-copy">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
              <span id="share-copy-lbl">Copy link</span>
            </button>
            <a href="https://x.com/intent/tweet?url=https%3A%2F%2Fclimate.mk&text=Explore+climate+trends+for+North+Macedonia" target="_blank" rel="noopener">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-4.714-6.231-5.401 6.231H2.744l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>
              <span>X / Twitter</span>
            </a>
            <a href="https://www.facebook.com/sharer/sharer.php?u=https%3A%2F%2Fclimate.mk" target="_blank" rel="noopener">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z"/></svg>
              <span>Facebook</span>
            </a>
            <a href="https://www.linkedin.com/sharing/share-offsite/?url=https%3A%2F%2Fclimate.mk" target="_blank" rel="noopener">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433c-1.144 0-2.063-.926-2.063-2.065 0-1.138.92-2.063 2.063-2.063 1.14 0 2.064.925 2.064 2.063 0 1.139-.925 2.065-2.064 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/></svg>
              <span>LinkedIn</span>
            </a>
            <a href="https://bsky.app/intent/compose?text=Explore+climate+trends+for+North+Macedonia+https%3A%2F%2Fclimate.mk" target="_blank" rel="noopener">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M12 10.8c-1.087-2.114-4.046-6.053-6.798-7.995C2.566.944 1.561 1.266.902 1.565.139 1.908 0 3.08 0 3.768c0 .69.378 5.65.624 6.479.815 2.736 3.713 3.66 6.383 3.364.136-.02.275-.039.415-.056-.138.022-.276.04-.415.056-3.912.58-7.387 2.005-2.83 7.078 5.013 5.19 6.87-1.113 7.823-4.308.953 3.195 2.05 9.271 7.733 4.308 4.267-4.308 1.172-6.498-2.74-7.078a8.741 8.741 0 0 1-.415-.056c.14.017.279.036.415.056 2.67.297 5.568-.628 6.383-3.364.246-.828.624-5.79.624-6.478 0-.69-.139-1.861-.902-2.204-.659-.299-1.664-.62-4.3 1.24C16.046 4.748 13.087 8.687 12 10.8z"/></svg>
              <span>Bluesky</span>
            </a>
          </div>
        </div>
      </div>
      <div class="today-grid">
        <div class="today-card" id="today-main-card"></div>
        <div class="today-chart" id="today-dist-card"></div>
        <div class="today-chart" id="today-trend-card">
          <div class="today-chart-title" id="today-trend-title">Macedonia annual peak temperature · loading…</div>
          <div id="today-trend-chart"></div>
        </div>
      </div>`;
    el.hidden = false;
    _todayOffset   = 0;
    _todayViewDate = null;
    _updateTodayCard(r);
    document.getElementById('today-prev').addEventListener('click', () => _navigateTodayTo(_todayOffset - 1));
    document.getElementById('today-next').addEventListener('click', () => _navigateTodayTo(_todayOffset + 1));
  } catch {
    /* network error — section stays hidden */
  }
}

function renderTodayChart(r) {
  const c = r.cutoffs;
  const labelStyle = {
    color: INK, fontSize: "10px", fontWeight: "600",
    fontFamily: "'JetBrains Mono', monospace", letterSpacing: "0.04em",
  };
  const zoneLabelStyle = {
    color: INK_SOFT, fontSize: "9px", fontWeight: "600",
    fontFamily: "'JetBrains Mono', monospace", letterSpacing: "0.04em",
  };
  const distMin = r.distribution[0][0];
  const distMax = r.distribution[r.distribution.length - 1][0];
  Highcharts.chart("today-dist-chart", {
    chart: { type: "areaspline", height: 220, margin: [28, 16, 32, 16], backgroundColor: "transparent", animation: false },
    title:   { text: null },
    credits: { enabled: false },
    legend:  { enabled: false },
    tooltip: {
      formatter() {
        const temp = this.x;
        let zone;
        if      (temp < c.p10) zone = t('today.zone_cold');
        else if (temp < c.p20) zone = t('today.zone_cool');
        else if (temp < c.p80) zone = t('today.zone_normal');
        else if (temp < c.p95) zone = t('today.zone_hot');
        else                   zone = t('today.zone_extreme');
        return `${temp.toFixed(1)}°C · ${zone}`;
      },
    },
    xAxis: {
      title:     { text: null },
      labels:    { format: "{value}°C", style: { color: INK_SOFT, fontSize: "10px", fontFamily: "'JetBrains Mono', monospace" } },
      lineColor:     "rgba(14,14,12,0.1)",
      tickColor:     "rgba(14,14,12,0.1)",
      gridLineWidth: 0,
      crosshair: { color: "rgba(14,14,12,0.15)", width: 1 },
      plotLines: [
        { value: r.today_temp, color: INK, width: 3, zIndex: 5,
          label: { text: `${t('today.today_label')}: ${r.today_temp.toFixed(1)}°C`, rotation: -90, x: -4, y: 40, align: "right", style: { ...labelStyle, fontSize: "13px", textOutline: "3px var(--card)" } } },
      ],
      plotBands: [
        { from: distMin, to: c.p10,
          color: "transparent",
          label: { text: `< ${c.p10.toFixed(1)}°C`, align: "center", verticalAlign: "top", y: 18, style: zoneLabelStyle } },
        { from: c.p10, to: c.p20,
          color: "transparent",
          label: { text: `${c.p10.toFixed(1)}–${c.p20.toFixed(1)}°C`, align: "center", verticalAlign: "top", y: 18, style: zoneLabelStyle } },
        { from: c.p20, to: c.p80,
          color: "transparent",
          label: { text: `${c.p20.toFixed(1)}–${c.p80.toFixed(1)}°C`, align: "center", verticalAlign: "top", y: 18, style: zoneLabelStyle } },
        { from: c.p80, to: c.p95,
          color: "transparent",
          label: { text: `${c.p80.toFixed(1)}–${c.p95.toFixed(1)}°C`, align: "center", verticalAlign: "top", y: 18, style: zoneLabelStyle } },
        { from: c.p95, to: distMax,
          color: "transparent",
          label: { text: `> ${c.p95.toFixed(1)}°C`, align: "center", verticalAlign: "top", y: 18, style: zoneLabelStyle } },
      ],
    },
    yAxis: {
      title:    { text: null },
      labels:   { enabled: false },
      gridLineWidth: 0,
      lineWidth: 0,
      tickWidth: 0,
    },
    plotOptions: {
      areaspline: {
        marker: { enabled: false },
        lineWidth: 0,
        fillOpacity: 1,
        zoneAxis: "x",
        zones: [
          { value: c.p10, color: "transparent", fillColor: "#3a5a8a" },
          { value: c.p20, color: "transparent", fillColor: "#6c8fb6" },
          { value: c.p80, color: "transparent", fillColor: "#e7d9b8" },
          { value: c.p95, color: "transparent", fillColor: "#c25a2c" },
          {               color: "transparent", fillColor: "#962c1a" },
        ],
      },
    },
    series: [{ name: "Density", data: r.distribution }],
  });

  const legend = document.createElement("div");
  legend.className = "today-chart-legend";
  legend.innerHTML = `
    <span class="tcl-item"><span class="tcl-sw" style="background:#3a5a8a"></span>${t('today.zone_cold')}</span>
    <span class="tcl-item"><span class="tcl-sw" style="background:#6c8fb6"></span>${t('today.zone_cool')}</span>
    <span class="tcl-item"><span class="tcl-sw" style="background:#e7d9b8"></span>${t('today.zone_normal')}</span>
    <span class="tcl-item"><span class="tcl-sw" style="background:#c25a2c"></span>${t('today.zone_hot')}</span>
    <span class="tcl-item"><span class="tcl-sw" style="background:#962c1a"></span>${t('today.zone_extreme')}</span>`;
  document.getElementById("today-dist-chart").after(legend);

  // Move title to after legend, before explanation
  const distTitle = document.querySelector(".today-chart .today-chart-title");
  if (distTitle) legend.after(distTitle);

  const explain2 = document.createElement("p");
  explain2.className = "today-explain";
  explain2.style.padding = "6px 0 4px";
  explain2.textContent = t('today.explain2');
  document.querySelector(".today-chart").appendChild(explain2);

  const foot2 = document.createElement("div");
  foot2.className = "today-foot";
  foot2.textContent = _locale?.today?.foot2
    ? t('today.foot2', {temp: r.today_temp.toFixed(1), pct: r.percentile.toFixed(0), median: r.cutoffs.p50.toFixed(1), samples: r.n_samples, year_min: r.year_min, year_max: r.year_max})
    : `Today: ${r.today_temp.toFixed(1)} °C · ${r.percentile.toFixed(0)}th percentile · median ${r.cutoffs.p50.toFixed(1)} °C · ${r.n_samples} observations · ${r.year_min}–${r.year_max}`;
  document.querySelector(".today-chart").appendChild(foot2);
}

async function renderTodayTrendChart(dateStr = null) {
  try {
    const url = dateStr ? `api/annual_trend?date=${dateStr}` : 'api/annual_trend';
    const d = await fetch(url).then(r => r.json());
    const currentYear = new Date().getFullYear();

    // Update title with actual year range
    const titleEl = document.getElementById("today-trend-title");
    if (titleEl) titleEl.textContent = t('today.trend_title', {day_label: _fmtDay(d.month_num, d.day_num, d.day_label), year_min: d.year_min, year_max: d.year_max});

    const histBand = d.hist_line.x.map((x, i) => [x, d.hist_line.lower[i], d.hist_line.upper[i]]);
    const fcBand   = d.projection_line.x.map((x, i) => [x, d.projection_line.lower[i], d.projection_line.upper[i]]);
    const histLine = d.hist_line.x.map((x, i) => [x, d.hist_line.y[i]]);
    const fcLine   = d.projection_line.x.map((x, i) => [x, d.projection_line.y[i]]);

    const milestoneYears = [2030, 2035, 2040, 2045, 2050];
    const fcMilestones = milestoneYears.map(yr => {
      let best = 0, bestDiff = Infinity;
      d.projection_line.x.forEach((x, i) => {
        const diff = Math.abs(x - yr);
        if (diff < bestDiff) { bestDiff = diff; best = i; }
      });
      return { x: yr, y: d.projection_line.y[best] };
    });

    const trendLabelStyle = {
      color: INK, fontSize: "9px", fontWeight: "600",
      fontFamily: "'JetBrains Mono', monospace",
    };

    Highcharts.chart("today-trend-chart", {
      chart: { type: "line", height: 240, margin: [16, 16, 40, 54], backgroundColor: "transparent", animation: false },
      title:   { text: null },
      credits: { enabled: false },
      legend:  { enabled: false },
      tooltip: {
        formatter() {
          if (this.series.name === "Annual max") return `<b>${Math.round(this.x)}</b>: ${this.y.toFixed(1)} °C`;
          if (this.series.name === "Projection milestones") return `<b>${this.x}</b>: ${this.y.toFixed(1)} °C <span style="opacity:0.6">(linear projection)</span>`;
          return false;
        },
      },
      xAxis: {
        title:         { text: null },
        labels:        { style: { color: INK_SOFT, fontSize: "10px", fontFamily: "'JetBrains Mono', monospace" } },
        lineColor:     "rgba(14,14,12,0.1)",
        tickColor:     "rgba(14,14,12,0.1)",
        gridLineWidth: 0,
        plotLines: [
          { value: currentYear, color: INK, width: 1.5, dashStyle: "Dot", zIndex: 5,
            label: { text: String(currentYear), rotation: 0, align: "center", y: -4, style: trendLabelStyle } },
        ],
      },
      yAxis: {
        title:         { text: null },
        labels:        { format: "{value}°C", style: { color: INK_SOFT, fontSize: "10px", fontFamily: "'JetBrains Mono', monospace" } },
        gridLineColor: "rgba(14,14,12,0.06)",
      },
      series: [
        { name: "CI (hist)",     type: "arearange", data: histBand, fillOpacity: 0.10, lineWidth: 0, color: "#962c1a", enableMouseTracking: false, marker: { enabled: false } },
        { name: "CI (projection)", type: "arearange", data: fcBand,   fillOpacity: 0.06, lineWidth: 0, color: "#962c1a", enableMouseTracking: false, marker: { enabled: false } },
        { name: "Trend (hist)",       type: "line", data: histLine, color: "#962c1a", lineWidth: 1.5, enableMouseTracking: false, marker: { enabled: false } },
        { name: "Trend (projection)", type: "line", data: fcLine,   color: "#962c1a", lineWidth: 1.5, dashStyle: "Dash", enableMouseTracking: false, marker: { enabled: false } },
        { name: "Annual max", type: "scatter", data: d.scatter,
          color: "rgba(150,44,26,0.6)", marker: { enabled: true, radius: 3, symbol: "circle" }, zIndex: 5 },
        { name: "Projection milestones", type: "scatter", data: fcMilestones,
          marker: { enabled: true, radius: 3, symbol: "circle", fillColor: "var(--paper)", lineColor: "#962c1a", lineWidth: 1.5 },
          zIndex: 6, enableMouseTracking: true,
          tooltip: { pointFormat: "<b>{point.x}</b>: {point.y:.1f} °C (linear projection)" } },
      ],
    });

    const s = d.stats;
    const sign = s.trend10 >= 0 ? "+" : "";
    const sig  = s.p_val < 0.01 ? "p < 0.01" : s.p_val < 0.05 ? `p = ${s.p_val}` : `p = ${s.p_val} (ns)`;
    // Move title to after chart, before explanation
    const trendTitle = document.getElementById("today-trend-title");
    if (trendTitle) document.getElementById("today-trend-card").appendChild(trendTitle);

    const explain3 = document.createElement("p");
    explain3.className = "today-explain";
    explain3.style.padding = "4px 0 2px";
    explain3.textContent = t('today.explain3', {year_min: d.year_min});
    document.getElementById("today-trend-card").appendChild(explain3);

    const foot = document.createElement("div");
    foot.className = "today-foot";
    foot.textContent = _locale?.today?.trend_foot
      ? t('today.trend_foot', {trend: `${sign}${s.trend10.toFixed(2)}`, sig, tau: s.tau, n_years: s.n_years})
      : `Theil-Sen + TFPW MK: ${sign}${s.trend10.toFixed(2)} °C/decade · ${sig} · τ = ${s.tau} · ${s.n_years} yrs`;
    document.getElementById("today-trend-card").appendChild(foot);
  } catch {
    /* silently skip if endpoint unavailable */
  }
}

// ── Render calendar ───────────────────────────────────────────────────────────

function _calChartOptions() {
  return {
    chart: { type: "column", marginTop: 10, animation: false, backgroundColor: "transparent" },
    title:    { text: null },
    subtitle: { text: null },
    xAxis: {
      title: { text: null },
      // Exact 365-unit range so bar width auto-fills: plotWidth ÷ 365 per bar
      min: 0.5, max: 365.5,
      tickPositions: [0,31,59,90,120,151,181,212,243,273,304,334].map(d => d + 15),
      labels: {
        formatter() {
          const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
          const bounds = [0,31,59,90,120,151,181,212,243,273,304,334,365];
          for (let i = 0; i < 12; i++) {
            if (this.value >= bounds[i] && this.value < bounds[i+1]) return months[i];
          }
          return "";
        },
      },
    },
    yAxis:  { title: { text: null }, plotLines: [{ value: 0, color: "rgba(14,14,12,0.2)", width: 1 }] },
    legend: { enabled: false },
    plotOptions: {
      // No pointWidth — Highcharts auto-sizes to fill plotWidth ÷ 365, resizes on reflow
      column: { animation: false, grouping: false, borderWidth: 0, pointPadding: 0, groupPadding: 0 },
    },
    series: [],
    tooltip: {
      formatter() {
        const ref = doyToDate(this.x);
        return `<b>${ref}</b><br>Trend: ${this.y > 0 ? "+" : ""}${this.y}/decade<br>p=${this.point.p}`;
      },
    },
    credits: { enabled: false },
  };
}

function renderCalendarPanel(calData, containerId) {
  const unit    = calData.unit || "";
  const colData = calData.days.map(d => ({ x: d.doy, y: d.slope10, color: d.color, p: d.p }));

  const chart = Highcharts.chart(containerId, _calChartOptions());
  chart.addSeries({ type: "column", name: "trend/decade", data: colData, borderWidth: 0 }, false);
  chart.xAxis[0].addPlotLine({ id: "doy-line", value: state.doy, color: ACCENT, width: 2, zIndex: 5, dashStyle: "ShortDash" });
  chart.yAxis[0].setTitle({ text: `${unit}/decade` }, false);
  chart.redraw(false);
  return chart;
}

// ── Refresh functions ─────────────────────────────────────────────────────────

async function refreshRegression() {
  showLoading("reg-loading", true);
  try {
    const data = await fetchRegression();
    renderRegression(data);
    renderHeroCards(data);
  } catch(e) {
    console.error("Regression error:", e);
  } finally {
    showLoading("reg-loading", false);
  }
}

async function refreshCalendar() {
  const locs = state.selLocs;
  if (!locs.length) return;

  const calSection = document.getElementById("cal-section");

  // Destroy old chart instances
  calCharts.forEach(c => { try { c.destroy(); } catch(e) {} });
  calCharts = [];

  // Build one panel per location
  calSection.innerHTML = locs.map((loc, i) => `
    <div class="cal-panel">
      <div class="panel-h">
        <div>
          <div class="panel-title" id="cal-title-${i}">—</div>
          <div class="panel-sub" id="cal-sub-${i}">—</div>
        </div>
      </div>
      <div class="chart-wrap">
        <div id="cal-chart-${i}" class="cal-panel-chart"></div>
        <div class="loading-overlay" id="cal-loading-${i}">
          <div class="spinner"></div> Calculating…
        </div>
      </div>
      ${t('hero.explain_cal') ? `<p class="panel-explain">${t('hero.explain_cal')}</p>` : ''}
      <div class="cal-legend${isPrecipLike(state.selVar) ? ' precip' : ''}">
        <span class="leg-cool">${isTemp(state.selVar) ? t('charts.cal_cooling') || 'Cooling' : t('charts.cal_decreasing') || 'Decreasing'}</span>
        <span class="cal-leg-ramp"></span>
        <span class="leg-warm">${isTemp(state.selVar) ? t('charts.cal_warming') || 'Warming' : t('charts.cal_increasing') || 'Increasing'}</span>
      </div>
    </div>`).join("");

  // Fetch and render each location (in parallel)
  await Promise.all(locs.map(async (loc, i) => {
    showLoading(`cal-loading-${i}`, true);
    try {
      const data = await fetchCalendar(loc);
      const chart = renderCalendarPanel(data, `cal-chart-${i}`);
      calCharts[i] = chart;
      const varLbl = (state.variables[state.selVar] || state.selVar).split("(")[0].trim();
      const titleEl = document.getElementById(`cal-title-${i}`);
      const subEl   = document.getElementById(`cal-sub-${i}`);
      if (titleEl) titleEl.textContent = `Year-round trend · ${locName(loc)}`;
      if (subEl)   subEl.textContent   = `${varLbl} · ${data.method_label} · ±${state.window} d`;
    } catch(e) {
      console.error("Calendar error:", e);
    } finally {
      showLoading(`cal-loading-${i}`, false);
    }
  }));
}

// Update DOY plotline on all calendar charts without re-fetching
function updateCalDoyLine() {
  calCharts.forEach(chart => {
    if (!chart) return;
    chart.xAxis[0].removePlotLine("doy-line");
    chart.xAxis[0].addPlotLine({
      id: "doy-line",
      value: state.doy,
      color: ACCENT,
      width: 2,
      zIndex: 5,
      dashStyle: "ShortDash",
    });
  });
}

// ── DOY slider & play ─────────────────────────────────────────────────────────

const slider  = document.getElementById("doy-slider");
const badge   = document.getElementById("doy-badge");
const btnPlay = document.getElementById("btn-play");
const btnPrev = document.getElementById("btn-prev");
const btnNext = document.getElementById("btn-next");

let regDebounce = null;

function setDoy(val) {
  state.doy = Math.max(1, Math.min(365, val));
  slider.value = state.doy;
  badge.textContent = doyBadge(state.doy);
  updateCalDoyLine();

  clearTimeout(regDebounce);
  regDebounce = setTimeout(() => { savePrefs(); refreshRegression(); refreshMap(); }, 180);
}

slider.addEventListener("input", () => setDoy(parseInt(slider.value)));

badge.addEventListener("click", e => {
  e.stopPropagation();
  document.getElementById("doy-popup")?.remove();

  const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const MAX_DAYS = [31,28,31,30,31,30,31,31,30,31,30,31];

  const ref = new Date(2001, 0, state.doy);
  let curMonth = ref.getMonth();
  let curDay   = ref.getDate();

  const popup = document.createElement("div");
  popup.id = "doy-popup";
  popup.className = "doy-popup";

  const mSel = document.createElement("select");
  MONTHS.forEach((m, i) => {
    const opt = document.createElement("option");
    opt.value = i; opt.textContent = m;
    if (i === curMonth) opt.selected = true;
    mSel.appendChild(opt);
  });

  const dSel = document.createElement("select");
  function populateDays(max, selected) {
    dSel.innerHTML = "";
    for (let d = 1; d <= max; d++) {
      const opt = document.createElement("option");
      opt.value = d; opt.textContent = d;
      if (d === selected) opt.selected = true;
      dSel.appendChild(opt);
    }
  }
  populateDays(MAX_DAYS[curMonth], curDay);

  mSel.addEventListener("change", () => {
    curMonth = parseInt(mSel.value);
    curDay = Math.min(curDay, MAX_DAYS[curMonth]);
    populateDays(MAX_DAYS[curMonth], curDay);
  });

  dSel.addEventListener("change", () => {
    curDay = parseInt(dSel.value);
  });

  const setBtn = document.createElement("button");
  setBtn.className = "doy-popup-set";
  setBtn.textContent = "Set";
  setBtn.addEventListener("click", () => {
    const d = new Date(2001, curMonth, curDay);
    const doy = Math.round((d - new Date(2001, 0, 0)) / 86400000);
    setDoy(Math.max(1, Math.min(365, doy)));
    close();
  });

  popup.appendChild(mSel);
  popup.appendChild(dSel);
  popup.appendChild(setBtn);
  document.body.appendChild(popup);

  const rect = badge.getBoundingClientRect();
  popup.style.top  = (rect.bottom + 6) + "px";
  popup.style.left = rect.left + "px";
  requestAnimationFrame(() => {
    const pw = popup.offsetWidth;
    const ph = popup.offsetHeight;
    if (rect.left + pw > window.innerWidth - 10)
      popup.style.left = Math.max(10, window.innerWidth - pw - 10) + "px";
    if (rect.bottom + 6 + ph > window.innerHeight - 10)
      popup.style.top = Math.max(10, rect.top - ph - 6) + "px";
  });

  function close() {
    popup.remove();
    document.removeEventListener("click", onOutside);
    document.removeEventListener("keydown", onKey);
  }
  const onOutside = ev => { if (!popup.contains(ev.target)) close(); };
  const onKey     = ev => { if (ev.key === "Escape") close(); };
  setTimeout(() => {
    document.addEventListener("click", onOutside);
    document.addEventListener("keydown", onKey);
  }, 0);
});

btnPrev.addEventListener("click", () => {
  stopPlay();
  setDoy(state.doy - 1 < 1 ? 365 : state.doy - 1);
});

btnNext.addEventListener("click", () => {
  stopPlay();
  setDoy(state.doy + 1 > 365 ? 1 : state.doy + 1);
});

btnPlay.addEventListener("click", () => {
  state.playing ? stopPlay() : startPlay();
});

function startPlay() {
  state.playing = true;
  btnPlay.textContent = "⏸";
  btnPlay.classList.add("active");

  async function loop() {
    if (!state.playing) return;
    state.doy = state.doy >= 365 ? 1 : state.doy + 1;
    slider.value = state.doy;
    badge.textContent = doyBadge(state.doy);
    updateCalDoyLine();
    await Promise.all([refreshRegression(), refreshMap()]);
    if (!state.playing) return;
    await wait(700);  // dwell so the user can see the loaded state before advancing
    if (state.playing) setTimeout(loop, 0);
  }
  loop();
}

function stopPlay() {
  state.playing = false;
  btnPlay.textContent = "▶";
  btnPlay.classList.remove("active");
}

// ── Location dropdown (multi-select, mobile-optimized) ──────────────────────

const locBtn = document.getElementById("loc-btn");
const locMenu = document.getElementById("loc-menu");
const locDisplay = document.getElementById("loc-display");
const locList = document.getElementById("loc-list");
const locBackdrop = document.getElementById("loc-backdrop");
const locCloseBtn = document.getElementById("loc-close-btn");

function buildLocationList(locations) {
  locList.innerHTML = "";
  locations.forEach((loc, idx) => {
    const div = document.createElement("div");
    div.className = "loc-item";
    
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = loc;
    checkbox.checked = state.selLocs.includes(loc);
    checkbox.addEventListener("change", handleLocCheckboxChange);
    checkbox.addEventListener("click", (e) => e.stopPropagation());
    
    const dot = document.createElement("div");
    dot.className = "loc-dot";
    dot.style.backgroundColor = state.palette[idx % state.palette.length];
    
    const name = document.createElement("div");
    name.className = "loc-name";
    name.textContent = locName(loc);
    
    div.appendChild(checkbox);
    div.appendChild(dot);
    div.appendChild(name);
    div.addEventListener("click", () => checkbox.click());
    
    locList.appendChild(div);
  });
  
  updateLocDisplay();
}

function updateLocDisplay() {
  if (state.selLocs.length === 0) {
    locDisplay.textContent = "None selected";
  } else if (state.selLocs.length === 1) {
    locDisplay.textContent = locName(state.selLocs[0]);
  } else {
    locDisplay.textContent = `${state.selLocs.length} selected`;
  }
}

function handleLocCheckboxChange(e) {
  const loc = e.target.value;
  if (e.target.checked) {
    if (!state.selLocs.includes(loc)) {
      if (state.selLocs.length >= 6) {
        e.target.checked = false;
        return;
      }
      state.selLocs.push(loc);
    }
  } else {
    if (state.selLocs.length <= 1) {
      e.target.checked = true;
      return;
    }
    state.selLocs = state.selLocs.filter(l => l !== loc);
  }
  updateLocCheckboxStates();
  updateLocDisplay();
  updateMapSelection();
  savePrefs();
  refreshRegression();
  refreshCalendar();
}

function updateLocCheckboxStates() {
  const atMax = state.selLocs.length >= 6;
  document.querySelectorAll("#loc-list input[type='checkbox']").forEach(cb => {
    cb.disabled = atMax && !cb.checked;
  });
}

function openLocMenu() {
  locMenu.classList.add("open");
  locBtn.classList.add("open");
  locBackdrop.classList.add("show");
}

function closeLocMenu() {
  locMenu.classList.remove("open");
  locBtn.classList.remove("open");
  locBackdrop.classList.remove("show");
}

locBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  if (locMenu.classList.contains("open")) {
    closeLocMenu();
  } else {
    openLocMenu();
  }
});

locCloseBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  closeLocMenu();
});

locBackdrop.addEventListener("click", closeLocMenu);

document.addEventListener("click", (e) => {
  if (!e.target.closest(".loc-dropdown") && locMenu.classList.contains("open")) {
    closeLocMenu();
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && locMenu.classList.contains("open")) {
    closeLocMenu();
  }
});

document.getElementById("var-select").addEventListener("change", function() {
  state.selVar = this.value;
  // Show/hide lapse-rate toggle
  document.getElementById("corr-section").style.display = isTemp(state.selVar) ? "" : "none";
  if (!isTemp(state.selVar)) state.corr = "raw";
  Object.keys(calCache).forEach(k => delete calCache[k]);
  savePrefs();
  refreshRegression();
  refreshCalendar();
  refreshMap();
});

document.querySelectorAll("input[name='method']").forEach(el => {
  el.addEventListener("change", function() {
    state.method = this.value;
    // Toggle pill active state
    document.querySelectorAll(".pill-radio").forEach(pill => pill.classList.remove("active"));
    this.closest(".pill-radio")?.classList.add("active");
    // Toggle check-box visibility (only show on selected method)
    const checkTheilsen = document.getElementById("check-theilsen");
    const checkOls      = document.getElementById("check-ols");
    if (checkTheilsen) checkTheilsen.style.display = state.method === "theilsen" ? "" : "none";
    if (checkOls)      checkOls.style.display      = state.method === "ols"      ? "" : "none";
    Object.keys(calCache).forEach(k => delete calCache[k]);
    savePrefs();
    refreshRegression();
    refreshCalendar();
    refreshMap();
  });
});

document.getElementById("corr-toggle").addEventListener("change", function() {
  state.corr = this.checked ? "corr" : "raw";
  Object.keys(calCache).forEach(k => delete calCache[k]);
  savePrefs();
  refreshRegression();
  refreshCalendar();
  refreshMap();
});

document.getElementById("window-input").addEventListener("change", function() {
  const v = parseInt(this.value);
  if (isNaN(v) || v < 1) return;
  state.window = v;
  Object.keys(calCache).forEach(k => delete calCache[k]);
  savePrefs();
  refreshRegression();
  refreshCalendar();
  refreshMap();
});

// ── Chat (BotFramework-WebChat + Direct Line token) ───────────────────────────

let _chatDirectLine  = null;
let _chatRefreshTimer = null;
let _conversationStarted = false;
let _analyticsConvId = "";

/**
 * Fire-and-forget: send one chat event to the analytics endpoint.
 * Errors are silently swallowed — analytics must never break the chat UX.
 * @param {"user"|"bot"} direction
 * @param {string} message  The raw text (capped server-side at 2000 chars)
 * @param {string} convId   Direct Line conversation ID (used for session grouping)
 */
function _logChatEvent(direction, message, convId) {
  try {
    fetch("/api/analytics/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ direction, message, conv_id: convId || "" }),
    }).catch(() => {});   // ignore network errors
  } catch (_) {}
}
let _chatErrorRateLimit   = "Chat is temporarily unavailable — too many requests. Please try again in a few minutes.";
let _chatErrorGeneric     = "Chat is temporarily unavailable. Please try again later.";
let _chatErrorGlobalLimit = "The chat assistant has reached its limit for now. Please try again in a little while.";

function _fmtDay(month, day, fallback) {
  if (_locale?.meta?.lang === 'mk') return `${day}.${String(month).padStart(2, '0')}`;
  return fallback;
}

function trendCategory(trend10) {
  if (trend10 >= 0.30) return 'catastrophic';
  if (trend10 >= 0.20) return 'extreme';
  if (trend10 >= 0.10) return 'bad';
  if (trend10 >= 0.05) return 'moderate';
  return 'baseline';
}

async function openChat() {
  document.getElementById("chat-modal").classList.add("open");
  document.body.classList.add("chat-modal-open");
  const errEl    = document.getElementById("chat-error");
  const chatEl   = document.getElementById("webchat-container");
  const creditEl = document.querySelector(".chat-credit");
  errEl.hidden = true;
  chatEl.style.display = "";
  if (creditEl) creditEl.hidden = false;
  if (_chatDirectLine) return;   // already initialised — reuse existing session
  try {
    const res = await fetch("/api/token");
    if (!res.ok) {
      let errorCode = null;
      if (res.status === 429) {
        try { const d = await res.json(); errorCode = d.error; } catch {}
      }
      const msg = errorCode === "chat_limit_reached" ? _chatErrorGlobalLimit
                : res.status === 429                 ? _chatErrorRateLimit
                : _chatErrorGeneric;
      throw Object.assign(new Error("Token error"), { status: res.status, msg });
    }
    const data = await res.json();
    _initWebChat(data.token, data.expires_in, data.conversationId || "");
  } catch (e) {
    console.error("Chat init failed:", e);
    const msg = e.msg || _chatErrorGeneric;
    chatEl.style.display = "none";
    if (creditEl) creditEl.hidden = true;
    errEl.innerHTML = `<strong>Chat unavailable</strong><span>${msg}</span>`;
    errEl.hidden = false;
  }
}

function _initWebChat(token, expiresIn, convId) {
  _analyticsConvId = convId || "";
  _chatDirectLine = window.WebChat.createDirectLine({
    token,
    domain: "https://europe.directline.botframework.com/v3/directline",
  });

  const store = window.WebChat.createStore({}, ({ dispatch }) => next => action => {
    if (action.type === "DIRECT_LINE/CONNECT_FULFILLED" && !_conversationStarted) {
      _conversationStarted = true;
      dispatch({
        type: "WEB_CHAT/SEND_EVENT",
        payload: { name: "startConversation", value: "" },
      });
    }

    // ── Analytics: log user prompts only ───────────────────────────────────
    if (action.type === "DIRECT_LINE/POST_ACTIVITY") {
      const act = action.payload?.activity;
      if (act?.type === "message" && act?.text) {
        // conversationId lives on the directLine instance, not on the activity
        // at POST time (it gets assigned by the service after posting).
        _logChatEvent("user", act.text, _analyticsConvId);
      }
    }
    // ───────────────────────────────────────────────────────────────────────

    return next(action);
  });

  window.WebChat.renderWebChat(
    {
      directLine: _chatDirectLine,
      store,
      locale: "en-US",
      styleOptions: {
        primaryFont:                "system-ui, -apple-system, 'Segoe UI', sans-serif",
        backgroundColor:            "#f0ebe2",
        bubbleBackground:           "#e8e3da",
        bubbleBorderRadius:         10,
        bubbleFromUserBackground:   "#c4622d",
        bubbleFromUserBorderRadius: 10,
        bubbleFromUserTextColor:    "#ffffff",
        bubbleTextColor:            "#1c1814",
        sendBoxBackground:          "#ffffff",
        sendBoxTextColor:           "#1c1814",
        sendBoxBorderTop:           "1px solid #ddd8d0",
        timestampColor:             "#8a7f74",
        botAvatarImage:             "/Ognen100.png",
        botAvatarInitials:          "O",
        hideUserAvatar:             true,
        hideUploadButton:           true,
      },
    },
    document.getElementById("webchat-container")
  );
  _scheduleTokenRefresh(expiresIn);
}

function _scheduleTokenRefresh(expiresIn) {
  clearTimeout(_chatRefreshTimer);
  const refreshAfterMs = Math.max((expiresIn - 300) * 1000, 60_000);
  _chatRefreshTimer = setTimeout(async () => {
    try {
      const res  = await fetch("/api/token/refresh", { method: "POST" });
      const data = await res.json();
      if (res.ok && _chatDirectLine) {
        _chatDirectLine.reconnect({ token: data.token });
        _scheduleTokenRefresh(data.expires_in);
      }
    } catch (e) {
      console.error("Token refresh failed:", e);
    }
  }, refreshAfterMs);
}

// ── About section renderer ────────────────────────────────────────────────────
// Updates static HTML about section from locale data when a non-default locale
// is loaded. Elements are identified by ID added in index.html.

function renderAbout() {
  if (!_locale?.about) return;
  const a = _locale.about;
  const setHtml = (id, val) => { const el = document.getElementById(id); if (el && val) el.innerHTML = val; };
  setHtml('about-heading',   a.heading);
  setHtml('about-col1-title', a.col1_title);
  setHtml('about-col1-text',  a.col1_text);
  setHtml('about-col2-title', a.col2_title);
  setHtml('about-col2-text1', a.col2_text1);
  setHtml('about-col2-text2', a.col2_text2);
  setHtml('about-col3-title', a.col3_title);
  setHtml('about-col3-text',  a.col3_text);
}

// Updates static headings in the toolbar area from locale
function renderStaticLabels() {
  if (!_locale?.ui) return;
  const u = _locale.ui;
  const ch = _locale.charts || {};
  const setTxt = (id, val) => { const el = document.getElementById(id); if (el && val) el.textContent = val; };
  setTxt('heading-location', u.heading_location);
  setTxt('heading-controls', u.heading_controls);
  setTxt('heading-controls-sub', u.heading_controls_sub);
  // Map legend
  const legCool = document.querySelector('.leg-cool');
  const legWarm = document.querySelector('.leg-warm');
  if (legCool && u.map_falling) legCool.textContent = u.map_falling;
  if (legWarm && u.map_rising)  legWarm.textContent = u.map_rising;
  // Chart legend labels
  setTxt('lbl-under-mean', ch.under_mean);
  setTxt('lbl-over-mean',  ch.over_mean);
  setTxt('lbl-trend-line', ch.trend_line);
  setTxt('lbl-ci95',       ch.ci95);
  setTxt('chart-change-lbl', ch.change_record);
  const regExplain = document.getElementById('reg-explain');
  if (regExplain) regExplain.textContent = t('hero.explain_reg') || '';
}

// Build the composite locale key from the two separate localStorage values
function _localeKey() {
  return (localStorage.getItem('mk_lang') || 'en') + '_' + (localStorage.getItem('mk_content') || 'default');
}

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {

  // Load locale before rendering anything
  await loadLocale(_localeKey());

  // Fetch metadata + topo + initial trends in parallel
  const todayDoy = getTodayDOY();
  const [meta, topo, initTrends] = await Promise.all([
    fetch("api/meta").then(r => r.json()),
    fetch("https://code.highcharts.com/mapdata/countries/mk/mk-all.topo.json").then(r => r.json()),
    fetch(`api/trends?var=temperature_max&doy=${todayDoy}&window=7&method=theilsen&corr=raw`)
      .then(r => r.json()).catch(() => null),
  ]);
  _mkTopo = topo;
  state.locations  = meta.locations;
  state.variables  = meta.variables;
  state.monthNames = meta.month_names;
  state.palette    = meta.palette;

  // Restore saved preferences (locations, variable, method, corr, window, doy).
  // loadPrefs() returns true when saved locations were found — skip auto-select in that case.
  const _prefsHadLocs = loadPrefs(meta.locations, Object.keys(meta.variables));

  // Auto-select Skopje + the non-Skopje station with the highest max-temp warming trend
  // — only when no saved prefs exist (first visit / cleared storage).
  if (!_prefsHadLocs && initTrends && Array.isArray(initTrends.points) && initTrends.points.length > 1) {
    const sorted = [...initTrends.points].sort((a, b) => b.trend10 - a.trend10);
    const second = sorted.find(p => p.loc !== "Skopje");
    if (second) state.selLocs = ["Skopje", second.loc];
  }

  // Set DOY to today only if no saved DOY preference was found
  if (!JSON.parse(localStorage.getItem(_PREFS_KEY) || 'null')?.doy) {
    state.doy = getTodayDOY();
  }
  slider.value = state.doy;

  // Populate variable select with server labels
  const sel = document.getElementById("var-select");
  sel.innerHTML = "";
  Object.entries(meta.variables).forEach(([k, v]) => {
    const opt = document.createElement("option");
    opt.value = k;
    opt.textContent = v;
    if (k === state.selVar) opt.selected = true;
    sel.appendChild(opt);
  });

  if (meta.chat_error_rate_limit)   _chatErrorRateLimit   = meta.chat_error_rate_limit;
  if (meta.chat_error_generic)      _chatErrorGeneric     = meta.chat_error_generic;
  if (meta.chat_error_global_limit) _chatErrorGlobalLimit = meta.chat_error_global_limit;

  // Show/hide chat button based on server config
  if (!meta.chat_enabled) {
    document.getElementById("chat-toggle-btn").style.display = "none";
  }

  // Build location list (desktop dropdown)
  buildLocationList(meta.locations);

  // Hide mobile chat section if chat is disabled
  if (!meta.chat_enabled) {
    const mdrChat = document.getElementById("mdr-chat-section");
    if (mdrChat) mdrChat.style.display = "none";
  }

  // Mark initially-selected method pill as active
  const initPill = document.querySelector("input[name='method']:checked")?.closest(".pill-radio");
  if (initPill) initPill.classList.add("active");

  // Initial DOY badge
  badge.textContent = doyBadge(state.doy);

  // Apply locale to static page elements
  renderAbout();
  renderStaticLabels();

  // Init charts
  initRegChart();

  // First load
  await refreshRegression();
  refreshCalendar();    // async, don't await
  refreshMap();         // async, don't await
  renderTodayStatus();  // async, don't await — country-wide, doesn't depend on selection
  renderPrecipHeatmap();    // async, don't await
  renderSeasonHeatmap();    // async, don't await
  renderSpeiTrendChart();   // async, don't await

  // Quote + effects use locale data — must run after loadLocale() resolves
  loadQuote();
  loadEffects();
}

// ── Season heatmap ────────────────────────────────────────────────────────────

async function renderSeasonHeatmap() {
  const section = document.getElementById("season-heatmap-section");
  if (!section) return;
  try {
    const d = await fetch("api/season_heatmap").then(r => r.json());
    if (!d.available || !d.data?.length) return;

    // ── helpers ────────────────────────────────────────────────────────────
    function ordinal(n) {
      const s = ["th","st","nd","rd"], v = n % 100;
      return n + (s[(v - 20) % 10] || s[v] || s[0]);
    }
    const CAT_LABELS = {
      cold:    "Cold (<10th pct)",
      cool:    "Cool (10–20th pct)",
      normal:  "Normal (20–80th pct)",
      hot:     "Hot (80–95th pct)",
      extreme: "Extreme (>95th pct)",
    };
    const CAT_COLORS = {
      cold:"#3a5a8a", cool:"#6c8fb6", normal:"#e7d9b8", hot:"#c25a2c", extreme:"#962c1a",
    };

    // Display order: Autumn → Summer → Spring → Winter (top to bottom)
    const SEASON_ORDER = ["Autumn", "Summer", "Spring", "Winter"];

    // Index data by "Season|year"
    const lookup = {};
    d.data.forEach(p => { lookup[`${p.season}|${p.y}`] = p; });

    const allYears = [];
    for (let y = d.year_min; y <= d.year_max; y++) allYears.push(y);

    // ── state ──────────────────────────────────────────────────────────────
    let currentMode = "all";
    let revealedYears = new Set(allYears);
    let animRunning = false, animYear = d.year_min, animTimer = null;

    // ── subtitle ───────────────────────────────────────────────────────────
    const sub = document.getElementById("shm-sub");
    const baselineLabel = d.baseline ? `1950–1980 baseline` : `all years since ${d.year_min}`;
    if (sub) sub.textContent =
      `Percentile rank vs ${baselineLabel} · ERA5-Land · data to ${d.era5_last}`;

    // ── controls ───────────────────────────────────────────────────────────
    const ctrlEl = document.getElementById("shm-controls");
    const MODES = [
      { key:"all",      label:"All seasons" },
      { key:"extremes", label:"Extremes only" },
      { key:"Autumn",   label:"Autumn" },
      { key:"Summer",   label:"Summer" },
      { key:"Spring",   label:"Spring" },
      { key:"Winter",   label:"Winter" },
    ];
    ctrlEl.innerHTML = MODES.map(m =>
      `<button class="shm-btn${m.key==='all'?' shm-btn--active':''}" data-shm-mode="${m.key}">${m.label}</button>`
    ).join("") +
      `<button class="shm-btn shm-btn--anim" id="shm-anim-btn">▶ Animate</button>`;

    ctrlEl.addEventListener("click", e => {
      const btn = e.target.closest(".shm-btn[data-shm-mode]");
      if (btn) {
        currentMode = btn.dataset.shmMode;
        ctrlEl.querySelectorAll(".shm-btn[data-shm-mode]").forEach(b =>
          b.classList.toggle("shm-btn--active", b.dataset.shmMode === currentMode));
        reapply();
      }
      if (e.target.closest("#shm-anim-btn")) toggleAnimate();
    });

    // ── build grid ─────────────────────────────────────────────────────────
    const outer = document.getElementById("shm-chart-outer");
    outer.innerHTML = `
      <div class="shm-grid" id="shm-grid"></div>
      <div class="shm-year-axis">
        <div class="shm-lbl-spacer"></div>
        <div class="shm-year-ticks" id="shm-year-ticks"></div>
      </div>
      <div class="shm-legend">
        ${Object.entries(CAT_COLORS).map(([k,c]) =>
          `<span class="shm-leg-item"><span class="shm-leg-sw" style="background:${c}${k==='normal'?';border:1px solid var(--rule-2)':''}"></span>${CAT_LABELS[k]}</span>`
        ).join("")}
      </div>`;

    buildGrid();
    buildTicks();

    // ── stats ──────────────────────────────────────────────────────────────
    updateStats();

    section.hidden = false;
    window.addEventListener("resize", buildTicks);

    // ── grid builder ───────────────────────────────────────────────────────
    function buildGrid() {
      const grid = document.getElementById("shm-grid");
      grid.innerHTML = "";
      SEASON_ORDER.forEach(sName => {
        const lbl = document.createElement("div");
        lbl.className = "shm-season-lbl";
        lbl.textContent = sName;
        grid.appendChild(lbl);

        const row = document.createElement("div");
        row.className = "shm-row";
        row.dataset.season = sName;

        allYears.forEach(y => {
          const p = lookup[`${sName}|${y}`];
          if (!p) {
            const empty = document.createElement("div");
            empty.className = "shm-cell shm-cell--empty";
            row.appendChild(empty);
            return;
          }
          const cell = document.createElement("div");
          cell.className = "shm-cell";
          cell.style.background = p.color;
          cell.dataset.year   = y;
          cell.dataset.season = sName;
          cell.dataset.cat    = p.cat;
          applyMode(cell, sName, p.cat, y);
          cell.addEventListener("mouseenter", ev => showTip(ev, p));
          cell.addEventListener("mousemove",  moveTip);
          cell.addEventListener("mouseleave", hideTip);
          row.appendChild(cell);
        });
        grid.appendChild(row);
      });
    }

    function buildTicks() {
      const tickEl = document.getElementById("shm-year-ticks");
      if (!tickEl) return;
      const row = document.querySelector(".shm-row");
      if (!row) return;
      tickEl.innerHTML = "";
      const n = allYears.length;
      allYears.forEach((y, i) => {
        if (y % 10 !== 0) return;
        const span = document.createElement("span");
        span.className = "shm-tick";
        span.textContent = y;
        span.style.left = ((i / n) * 100) + "%";
        tickEl.appendChild(span);
      });
    }

    // ── mode application ───────────────────────────────────────────────────
    function applyMode(cell, season, cat, year) {
      cell.classList.remove("shm-cell--dim", "shm-cell--hl", "shm-cell--pulse");
      if (!revealedYears.has(year)) { cell.classList.add("shm-cell--dim"); return; }
      if (currentMode === "all") return;
      if (currentMode === "extremes") {
        if (cat === "extreme") cell.classList.add("shm-cell--pulse");
        else                   cell.classList.add("shm-cell--dim");
        return;
      }
      // single-season filter
      if (season !== currentMode) cell.classList.add("shm-cell--dim");
      else                        cell.classList.add("shm-cell--hl");
    }

    function reapply() {
      document.querySelectorAll(".shm-cell:not(.shm-cell--empty)").forEach(c =>
        applyMode(c, c.dataset.season, c.dataset.cat, +c.dataset.year));
    }

    // ── animate ────────────────────────────────────────────────────────────
    function toggleAnimate() {
      animRunning ? stopAnimate() : startAnimate();
    }
    function startAnimate() {
      animRunning = true; animYear = d.year_min; revealedYears = new Set();
      document.getElementById("shm-anim-btn").textContent = "⏹ Stop";
      document.querySelectorAll(".shm-cell:not(.shm-cell--empty)").forEach(c =>
        c.classList.add("shm-cell--dim"));
      updateStats(); step();
    }
    function step() {
      if (!animRunning) return;
      revealedYears.add(animYear);
      document.querySelectorAll(`.shm-cell[data-year="${animYear}"]`).forEach(c =>
        applyMode(c, c.dataset.season, c.dataset.cat, animYear));
      updateStats();
      if (animYear >= d.year_max) { stopAnimate(); return; }
      animYear++;
      const delay = animYear > 2005 ? 55 : animYear > 1985 ? 80 : 110;
      animTimer = setTimeout(step, delay);
    }
    function stopAnimate() {
      animRunning = false; clearTimeout(animTimer);
      revealedYears = new Set(allYears);
      document.getElementById("shm-anim-btn").textContent = "▶ Animate";
      reapply(); updateStats();
    }

    // ── stats ──────────────────────────────────────────────────────────────
    function updateStats() {
      let ext = 0, cold = 0, extSince2010 = 0, hotRecent = 0;
      const recentFrom = d.year_max - 9;
      revealedYears.forEach(y => {
        SEASON_ORDER.forEach(s => {
          const p = lookup[`${s}|${y}`];
          if (!p) return;
          if (p.cat === "extreme") ext++;
          if (p.cat === "cold")    cold++;
          if (p.cat === "extreme" && y >= 2010) extSince2010++;
          if ((p.cat === "extreme" || p.cat === "hot") && y >= recentFrom) hotRecent++;
        });
      });
      document.getElementById("shm-stats").innerHTML = [
        [ext,         "Extreme seasons"],
        [cold,        "Cold seasons"],
        [extSince2010,"Extreme since 2010"],
        [hotRecent,   `Hot or extreme (${recentFrom}–${d.year_max})`],
      ].map(([n, lbl]) => `
        <div class="shm-stat">
          <div class="shm-stat-num">${n}</div>
          <div class="shm-stat-lbl">${lbl}</div>
        </div>`).join("");
    }

    // ── tooltip ────────────────────────────────────────────────────────────
    const tip = document.getElementById("shm-tip");
    function showTip(ev, p) {
      tip.innerHTML = `
        <strong>${p.season} ${p.y}</strong>
        <div class="shm-tip-row">
          <span class="shm-tip-sw" style="background:${p.color}"></span>
          ${CAT_LABELS[p.cat]}
        </div>
        Avg national max: <b>${p.avg.toFixed(1)} °C</b><br>
        ${ordinal(p.rank)} hottest ${p.season} in ${p.total} years`;
      tip.hidden = false;
      moveTip(ev);
    }
    function moveTip(ev) {
      const x = ev.clientX + 16, y = ev.clientY - 52;
      tip.style.left = Math.min(x, window.innerWidth - 210) + "px";
      tip.style.top  = Math.max(8, y) + "px";
    }
    function hideTip() { tip.hidden = true; }

  } catch(e) {
    console.warn("Season heatmap error:", e);
  }
}

async function renderPrecipHeatmap() {
  const section = document.getElementById("precip-heatmap-section");
  if (!section) return;
  try {
    const d = await fetch("api/spei_heatmap").then(r => r.json());
    if (!d.available || !d.data?.length) return;

    function ordinal(n) {
      const s = ["th","st","nd","rd"], v = n % 100;
      return n + (s[(v - 20) % 10] || s[v] || s[0]);
    }
    const CAT_LABELS = {
      extreme_dry: "Extreme drought (SPEI < −1.5)",
      dry:         "Dry (SPEI −1.5 to −1.0)",
      normal:      "Normal (SPEI −1.0 to 1.0)",
      wet:         "Wet (SPEI 1.0 to 1.5)",
      extreme_wet: "Extremely wet (SPEI > 1.5)",
    };
    const CAT_COLORS = {
      extreme_dry: "#8b3a0f",
      dry:         "#c2713a",
      normal:      "#e7e0d0",
      wet:         "#4a80b0",
      extreme_wet: "#1e4d78",
    };

    const SEASON_ORDER = ["Autumn", "Summer", "Spring", "Winter"];
    const lookup = {};
    d.data.forEach(p => { lookup[`${p.season}|${p.y}`] = p; });

    const allYears = [];
    for (let y = d.year_min; y <= d.year_max; y++) allYears.push(y);

    let currentMode = "all";
    let revealedYears = new Set(allYears);
    let animRunning = false, animYear = d.year_min, animTimer = null;

    // subtitle
    const sub = document.getElementById("phm-sub");
    const baselineLabel = d.baseline ? `1950–1980 baseline` : `all years since ${d.year_min}`;
    if (sub) sub.textContent =
      `P − ET₀ water balance, log-logistic standardised vs ${baselineLabel} · ERA5-Land · data to ${d.era5_last}`;

    // controls
    const ctrlEl = document.getElementById("phm-controls");
    const MODES = [
      { key:"all",      label:"All seasons" },
      { key:"extremes", label:"Extremes only" },
      { key:"Autumn",   label:"Autumn" },
      { key:"Summer",   label:"Summer" },
      { key:"Spring",   label:"Spring" },
      { key:"Winter",   label:"Winter" },
    ];
    ctrlEl.innerHTML = MODES.map(m =>
      `<button class="shm-btn${m.key==='all'?' shm-btn--active':''}" data-phm-mode="${m.key}">${m.label}</button>`
    ).join("") +
      `<button class="shm-btn shm-btn--anim" id="phm-anim-btn">▶ Animate</button>`;

    ctrlEl.addEventListener("click", e => {
      const btn = e.target.closest(".shm-btn[data-phm-mode]");
      if (btn) {
        currentMode = btn.dataset.phmMode;
        ctrlEl.querySelectorAll(".shm-btn[data-phm-mode]").forEach(b =>
          b.classList.toggle("shm-btn--active", b.dataset.phmMode === currentMode));
        reapply();
      }
      if (e.target.closest("#phm-anim-btn")) toggleAnimate();
    });

    // grid
    const outer = document.getElementById("phm-chart-outer");
    outer.innerHTML = `
      <div class="shm-grid" id="phm-grid"></div>
      <div class="shm-year-axis">
        <div class="shm-lbl-spacer"></div>
        <div class="shm-year-ticks" id="phm-year-ticks"></div>
      </div>
      <div class="shm-legend">
        ${Object.entries(CAT_COLORS).map(([k,c]) =>
          `<span class="shm-leg-item"><span class="shm-leg-sw" style="background:${c}${k==='normal'?';border:1px solid var(--rule-2)':''}"></span>${CAT_LABELS[k]}</span>`
        ).join("")}
      </div>`;

    buildGrid();
    buildTicks();
    updateStats();
    section.hidden = false;
    window.addEventListener("resize", buildTicks);

    function buildGrid() {
      const grid = document.getElementById("phm-grid");
      grid.innerHTML = "";
      SEASON_ORDER.forEach(sName => {
        const lbl = document.createElement("div");
        lbl.className = "shm-season-lbl";
        lbl.textContent = sName;
        grid.appendChild(lbl);

        const row = document.createElement("div");
        row.className = "shm-row";
        row.dataset.season = sName;

        allYears.forEach(y => {
          const p = lookup[`${sName}|${y}`];
          if (!p) {
            const empty = document.createElement("div");
            empty.className = "shm-cell shm-cell--empty";
            row.appendChild(empty);
            return;
          }
          const cell = document.createElement("div");
          cell.className = "shm-cell";
          cell.style.background = p.color;
          cell.dataset.year   = y;
          cell.dataset.season = sName;
          cell.dataset.cat    = p.cat;
          applyMode(cell, sName, p.cat, y);
          cell.addEventListener("mouseenter", ev => showTip(ev, p));
          cell.addEventListener("mousemove",  moveTip);
          cell.addEventListener("mouseleave", hideTip);
          row.appendChild(cell);
        });
        grid.appendChild(row);
      });
    }

    function buildTicks() {
      const tickEl = document.getElementById("phm-year-ticks");
      if (!tickEl) return;
      const row = document.querySelector("#phm-grid .shm-row");
      if (!row) return;
      tickEl.innerHTML = "";
      const n = allYears.length;
      allYears.forEach((y, i) => {
        if (y % 10 !== 0) return;
        const span = document.createElement("span");
        span.className = "shm-tick";
        span.textContent = y;
        span.style.left = ((i / n) * 100) + "%";
        tickEl.appendChild(span);
      });
    }

    function applyMode(cell, season, cat, year) {
      cell.classList.remove("shm-cell--dim", "shm-cell--hl", "shm-cell--pulse");
      if (!revealedYears.has(year)) { cell.classList.add("shm-cell--dim"); return; }
      if (currentMode === "all") return;
      if (currentMode === "extremes") {
        if (cat === "extreme_dry" || cat === "extreme_wet") cell.classList.add("shm-cell--pulse");
        else                                                cell.classList.add("shm-cell--dim");
        return;
      }
      if (season !== currentMode) cell.classList.add("shm-cell--dim");
      else                        cell.classList.add("shm-cell--hl");
    }

    function reapply() {
      document.querySelectorAll("#phm-grid .shm-cell:not(.shm-cell--empty)").forEach(c =>
        applyMode(c, c.dataset.season, c.dataset.cat, +c.dataset.year));
    }

    function toggleAnimate() { animRunning ? stopAnimate() : startAnimate(); }
    function startAnimate() {
      animRunning = true; animYear = d.year_min; revealedYears = new Set();
      document.getElementById("phm-anim-btn").textContent = "⏹ Stop";
      document.querySelectorAll("#phm-grid .shm-cell:not(.shm-cell--empty)").forEach(c =>
        c.classList.add("shm-cell--dim"));
      updateStats(); step();
    }
    function step() {
      if (!animRunning) return;
      revealedYears.add(animYear);
      document.querySelectorAll(`#phm-grid .shm-cell[data-year="${animYear}"]`).forEach(c =>
        applyMode(c, c.dataset.season, c.dataset.cat, animYear));
      updateStats();
      if (animYear >= d.year_max) { stopAnimate(); return; }
      animYear++;
      const delay = animYear > 2005 ? 55 : animYear > 1985 ? 80 : 110;
      animTimer = setTimeout(step, delay);
    }
    function stopAnimate() {
      animRunning = false; clearTimeout(animTimer);
      revealedYears = new Set(allYears);
      document.getElementById("phm-anim-btn").textContent = "▶ Animate";
      reapply(); updateStats();
    }

    function updateStats() {
      let extDry = 0, extWet = 0, extDrySince2000 = 0, dryRecent = 0;
      const recentFrom = d.year_max - 9;
      revealedYears.forEach(y => {
        SEASON_ORDER.forEach(s => {
          const p = lookup[`${s}|${y}`];
          if (!p) return;
          if (p.cat === "extreme_dry") extDry++;
          if (p.cat === "extreme_wet") extWet++;
          if (p.cat === "extreme_dry" && y >= 2000) extDrySince2000++;
          if ((p.cat === "extreme_dry" || p.cat === "dry") && y >= recentFrom) dryRecent++;
        });
      });
      document.getElementById("phm-stats").innerHTML = [
        [extDry,          "Extreme drought seasons (SPEI < −1.5)"],
        [extWet,          "Extremely wet seasons (SPEI > 1.5)"],
        [extDrySince2000, "Extreme drought seasons since 2000"],
        [dryRecent,       `Dry or drought (${recentFrom}–${d.year_max})`],
      ].map(([n, lbl]) => `
        <div class="shm-stat">
          <div class="shm-stat-num">${n}</div>
          <div class="shm-stat-lbl">${lbl}</div>
        </div>`).join("");
    }

    const tip = document.getElementById("phm-tip");
    function showTip(ev, p) {
      const speiSign = p.spei >= 0 ? "+" : "";
      tip.innerHTML = `
        <strong>${p.season} ${p.y}</strong>
        <div class="shm-tip-row">
          <span class="shm-tip-sw" style="background:${p.color}"></span>
          ${CAT_LABELS[p.cat]}
        </div>
        SPEI: <b>${speiSign}${p.spei.toFixed(2)}</b><br>
        Water balance: <b>${p.balance.toFixed(0)} mm P−ET₀</b><br>
        ${ordinal(p.rank)} driest ${p.season} in ${p.total} years`;
      tip.hidden = false;
      moveTip(ev);
    }
    function moveTip(ev) {
      const x = ev.clientX + 16, y = ev.clientY - 52;
      tip.style.left = Math.min(x, window.innerWidth - 210) + "px";
      tip.style.top  = Math.max(8, y) + "px";
    }
    function hideTip() { tip.hidden = true; }

  } catch(e) {
    console.warn("Precip heatmap error:", e);
  }
}

async function renderSpeiTrendChart() {
  const section = document.getElementById("spei-trend-section");
  if (!section) return;

  // show section immediately so chart div has dimensions when Highcharts renders
  section.hidden = false;

  // show loading state while computing (can take ~15s on first run)
  const chartDiv = document.getElementById("spei-trend-chart");
  if (chartDiv) chartDiv.innerHTML =
    `<div style="display:flex;align-items:center;justify-content:center;height:280px;color:var(--ink-soft);font-family:'JetBrains Mono',monospace;font-size:11px;gap:10px">
      <div class="spinner"></div> Computing drought index for all stations…
    </div>`;

  try {
    const d = await fetch("/api/spei_station_seasonal").then(r => r.json());
    if (!d.available) { section.hidden = true; return; }
    if (chartDiv) chartDiv.innerHTML = "";

    const SEASONS = ["Annual", "Winter", "Spring", "Summer", "Autumn"];
    const MONTHS  = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    const stations = Object.keys(d.stations).sort();
    let currentStation = stations.includes("Skopje") ? "Skopje" : stations[0];
    let currentSeason  = "Summer";
    let chart          = null;

    // subtitle
    document.getElementById("spei-trend-sub").textContent =
      `Seasonal (SPEI-3) and monthly (SPEI-30) water balance standardised vs ${d.baseline} baseline · ERA5-Land · data to ${d.era5_last}`;

    // controls
    const ctrlEl = document.getElementById("spei-trend-controls");

    function buildControls() {
      ctrlEl.innerHTML =
        // row 1: locations
        `<div class="spei-ctrl-row">` +
        stations.map(s =>
          `<button class="shm-btn spei-loc-btn${s===currentStation?' shm-btn--active':''}" data-spei-loc="${s}">${s.replace(/_/g," ")}</button>`
        ).join("") +
        `</div>` +
        // row 2: seasons + months
        `<div class="spei-ctrl-row" style="margin-top:6px">` +
        SEASONS.map(s =>
          `<button class="shm-btn spei-sea-btn${s===currentSeason?' shm-btn--active':''}" data-spei-sea="${s}">${s}</button>`
        ).join("") +
        `<span class="spei-ctrl-sep"></span>` +
        MONTHS.map(m =>
          `<button class="shm-btn spei-sea-btn${m===currentSeason?' shm-btn--active':''}" data-spei-sea="${m}">${m}</button>`
        ).join("") +
        `</div>`;
    }
    buildControls();

    ctrlEl.addEventListener("click", e => {
      const locBtn = e.target.closest(".spei-loc-btn");
      const seaBtn = e.target.closest(".spei-sea-btn");
      if (locBtn) {
        currentStation = locBtn.dataset.speiLoc;
        ctrlEl.querySelectorAll(".spei-loc-btn").forEach(b =>
          b.classList.toggle("shm-btn--active", b.dataset.speiLoc === currentStation));
        renderChart();
      }
      if (seaBtn) {
        currentSeason = seaBtn.dataset.speiSea;
        ctrlEl.querySelectorAll(".spei-sea-btn").forEach(b =>
          b.classList.toggle("shm-btn--active", b.dataset.speiSea === currentSeason));
        renderChart();
      }
    });

    renderChart();

    function speiColor(v) {
      if (v < -1.5) return "#8b3a0f";
      if (v < -1.0) return "#c2713a";
      if (v <  1.0) return "#aaa49a";
      if (v <  1.5) return "#4a80b0";
      return "#1e4d78";
    }

    function renderChart() {
      const series = d.stations[currentStation]?.[currentSeason];
      if (!series) return;

      const { years, spei, trend } = series;
      const n = years.length;

      // stats box
      const slopeEl = document.getElementById("spei-trend-slope");
      const titleEl = document.getElementById("spei-trend-title");
      const obsEl   = document.getElementById("spei-trend-obs");
      const explEl  = document.getElementById("spei-trend-explain");

      const isMonth = MONTHS.includes(currentSeason);
      const scaleLabel = isMonth ? "SPEI-30" : "SPEI-3";
      titleEl.textContent = `${currentStation.replace(/_/g," ")} — ${currentSeason} ${scaleLabel}`;
      obsEl.textContent   = `${n} ${isMonth ? "months" : "seasons"} · ${years[0]}–${years[n-1]}`;

      if (trend?.slope_per_decade != null) {
        const s = trend.slope_per_decade;
        slopeEl.textContent = (s >= 0 ? "+" : "") + s.toFixed(2);
        slopeEl.style.color = s < 0 ? "var(--accent)" : "#4a80b0";
        const sig = trend.p_value < 0.05 ? "statistically significant (p < 0.05)" : `not significant (p = ${trend.p_value})`;

        // Extrapolate when trend line crosses ±1.5 threshold
        let thresholdLine = "";
        const slopePerYear = s / 10;
        if (slopePerYear !== 0) {
          const ic        = trend.intercept;
          const lastYear  = years[n - 1];
          const curVal    = slopePerYear * lastYear + ic;

          if (slopePerYear < 0) {
            // Drying — heading toward extreme drought (−1.5)
            if (curVal <= -1.5) {
              thresholdLine = "The trend line has already crossed the extreme drought threshold (SPEI −1.5).";
            } else {
              const targetYear = Math.round((-1.5 - ic) / slopePerYear);
              if (targetYear > lastYear && targetYear < 2200) {
                thresholdLine = `At this rate the trend reaches extreme drought (SPEI −1.5) around ${targetYear}.`;
              }
            }
          } else {
            // Wetting — heading toward extremely wet (+1.5)
            if (curVal >= 1.5) {
              thresholdLine = "The trend line has already crossed the extremely wet threshold (SPEI +1.5).";
            } else {
              const targetYear = Math.round((1.5 - ic) / slopePerYear);
              if (targetYear > lastYear && targetYear < 2200) {
                thresholdLine = `At this rate the trend reaches extremely wet (SPEI +1.5) around ${targetYear}.`;
              }
            }
          }
        }

        explEl.textContent =
          `Theil-Sen slope: ${(s>=0?"+":"")}${s.toFixed(3)} SPEI/decade · Mann-Kendall: ${trend.mk_trend} · ${sig}. ` +
          `Negative trend means conditions are becoming drier relative to the 1950–1980 baseline.` +
          (thresholdLine ? ` ${thresholdLine}` : "");
      } else {
        slopeEl.textContent = "—";
        explEl.textContent  = "";
      }

      // build trend line points
      const trendPoints = trend?.slope_per_decade != null ? (() => {
        const sl = trend.slope_per_decade / 10;
        const ic = trend.intercept;
        return [[years[0], +(sl * years[0] + ic).toFixed(2)],
                [years[n-1], +(sl * years[n-1] + ic).toFixed(2)]];
      })() : [];

      // scatter data
      const scatter = years.map((y, i) => ({
        x: y, y: spei[i],
        color: speiColor(spei[i]),
        marker: { radius: 4 },
      }));

      const opts = {
        chart: {
          type: "scatter",
          height: 280,
          backgroundColor: "transparent",
          style: { fontFamily: "'Space Grotesk', sans-serif" },
          animation: false,
        },
        title:    { text: "" },
        credits:  { enabled: false },
        legend:   { enabled: false },
        tooltip: {
          formatter() {
            const cat = this.y < -1.5 ? "Extreme drought" : this.y < -1.0 ? "Dry" :
                        this.y <  1.0 ? "Normal" : this.y < 1.5 ? "Wet" : "Extremely wet";
            return `<b>${currentSeason} ${this.x}</b><br>SPEI: <b>${this.y >= 0 ? "+" : ""}${this.y.toFixed(2)}</b><br>${cat}`;
          },
        },
        xAxis: {
          title: { text: "" },
          labels: { style: { fontSize: "10px", color: "var(--ink-soft)" } },
          gridLineWidth: 0,
          tickColor: "var(--rule)",
        },
        yAxis: {
          title: { text: "SPEI", style: { fontSize: "10px", color: "var(--ink-soft)" } },
          min: -3, max: 3,
          plotLines: [
            { value: 0,    color: "var(--ink)", width: 1, dashStyle: "Solid", zIndex: 3 },
            { value: -1.5, color: "#8b3a0f", width: 1, dashStyle: "Dash", zIndex: 3,
              label: { text: "extreme drought", style: { fontSize: "9px", color: "#8b3a0f" } } },
            { value:  1.5, color: "#1e4d78", width: 1, dashStyle: "Dash", zIndex: 3,
              label: { text: "extremely wet", style: { fontSize: "9px", color: "#1e4d78" }, align: "right" } },
          ],
          gridLineColor: "var(--rule)",
          labels: { style: { fontSize: "10px", color: "var(--ink-soft)" } },
        },
        series: [
          { type: "scatter", data: scatter, zIndex: 4 },
          ...(trendPoints.length ? [{
            type: "line",
            data: trendPoints,
            color: "var(--ink)",
            lineWidth: 2,
            dashStyle: "Solid",
            marker: { enabled: false },
            enableMouseTracking: false,
            zIndex: 5,
          }] : []),
        ],
      };

      if (chart) { chart.destroy(); chart = null; }
      chart = Highcharts.chart("spei-trend-chart", opts);
    }

  } catch(e) {
    console.warn("SPEI trend chart error:", e);
  }
}

init().catch(console.error);

// ── Climate quote card ────────────────────────────────────────────────────────

async function loadQuote() {
  try {
    const localeQuotes = tArr('quotes');
    let rows;
    if (localeQuotes && localeQuotes.length) {
      // Author names always shown in English.
      // To enable translated authors in the future, replace enQuotes with localeQuotes below.
      const style    = localStorage.getItem('mk_content') || 'default';
      const lang     = localStorage.getItem('mk_lang') || 'en';
      const enQuotes = lang === 'en' ? localeQuotes :
        await fetch(`locales/en_${style}.json`)
          .then(r => r.json()).then(d => d.quotes || localeQuotes).catch(() => localeQuotes);
      const len = Math.min(localeQuotes.length, enQuotes.length);
      const idx = Math.floor(Math.random() * len);
      const card = document.getElementById('quote-card');
      card.innerHTML = `<div><p class="quote-text">${localeQuotes[idx].quote}</p><span class="quote-author">${enQuotes[idx].author}</span></div>`;
      card.removeAttribute('hidden');
      return;
    }
    // CSV fallback — already English
    const resp = await fetch('climate_quotes.csv');
    const text = await resp.text();
    const lines = text.trim().split(/\r?\n/).slice(1);
    rows = lines.map(line => {
      const m = line.match(/^"(.+)","(.+)"$/) ||
                line.match(/^(.+?),"(.+)"$/) ||
                line.match(/^"(.+)",(.+)$/) ||
                line.match(/^(.+?),(.+)$/);
      return m ? { author: m[1].trim(), quote: m[2].trim() } : null;
    }).filter(Boolean);
    const row = rows[Math.floor(Math.random() * rows.length)];
    const card = document.getElementById('quote-card');
    card.innerHTML = `<div><p class="quote-text">${row.quote}</p><span class="quote-author">${row.author}</span></div>`;
    card.removeAttribute('hidden');
  } catch (e) { /* silently skip */ }
}

async function loadEffects() {
  try {
    // Use locale effects if available, otherwise fall back to CSV
    const localeEffects = tArr('effects');
    let items;
    if (localeEffects && localeEffects.length) {
      items = localeEffects;
    } else {
      const resp = await fetch('effects.csv');
      const text = await resp.text();
      items = text.trim().split(/\r?\n/).map(l => l.trim()).filter(Boolean);
    }
    const shuffled = [...items].sort(() => Math.random() - 0.5).slice(0, 4);
    const card = document.getElementById('effects-card');
    card.innerHTML = `<p class="action-card-label">${t('ui.label_effects')}</p>
      <ul class="effects-list">${shuffled.map(i => `<li>${i}</li>`).join('')}</ul>`;
  } catch (e) { /* silently skip */ }
}

// ── Welcome modal ─────────────────────────────────────────────────────────────

function closeWelcome() {
  document.getElementById("welcome-modal").classList.remove("open");
  localStorage.setItem("welcome_dismissed", "1");
}

if (!localStorage.getItem("welcome_dismissed")) {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    document.getElementById("welcome-modal").classList.add("open");
  }));
}

// ── Mobile drawer ─────────────────────────────────────────────────────────────

function openMobileDrawer() {
  document.getElementById("mobile-drawer").classList.add("open");
  document.getElementById("mobile-backdrop").classList.add("show");
  document.body.classList.add("drawer-open");
}

function closeMobileDrawer() {
  document.getElementById("mobile-drawer").classList.remove("open");
  document.getElementById("mobile-backdrop").classList.remove("show");
  document.body.classList.remove("drawer-open");
}

// Open / close
document.getElementById("hamburger-btn").addEventListener("click", openMobileDrawer);
document.getElementById("mdr-close").addEventListener("click", closeMobileDrawer);
document.getElementById("mobile-backdrop").addEventListener("click", closeMobileDrawer);
document.querySelectorAll(".mdr-nav a").forEach(a => a.addEventListener("click", closeMobileDrawer));
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && document.getElementById("mobile-drawer").classList.contains("open"))
    closeMobileDrawer();
});

// Style switcher (placeholder — applies data-theme, wires up real styles later)
const _savedTheme = localStorage.getItem("mk_theme");
if (_savedTheme && _savedTheme !== "default") {
  document.documentElement.setAttribute("data-theme", _savedTheme);
  const _sel = document.getElementById("mdr-style-select");
  if (_sel) _sel.value = _savedTheme;
}
document.getElementById("mdr-style-select").addEventListener("change", function() {
  const theme = this.value;
  document.documentElement.setAttribute("data-theme", theme === "default" ? "" : theme);
  localStorage.setItem("mk_theme", theme);
});

// Remember-settings toggle
const _savePrefsToggle = document.getElementById('mdr-save-prefs-toggle');
if (_savePrefsToggle) {
  _savePrefsToggle.checked = _savePrefsEnabled;
  _savePrefsToggle.addEventListener('change', function() {
    _savePrefsEnabled = this.checked;
    localStorage.setItem(_PREFS_ENABLED_KEY, _savePrefsEnabled);
    if (_savePrefsEnabled) {
      savePrefs();   // immediately save current state
    } else {
      localStorage.removeItem(_PREFS_KEY);   // wipe saved prefs when opting out
    }
  });
}

// Language + content-style switchers — each reloads page so all text updates
const _langSel    = document.getElementById("mdr-lang-select");
const _contentSel = document.getElementById("mdr-content-select");
if (_langSel) {
  _langSel.value = localStorage.getItem("mk_lang") || "en";
  _langSel.addEventListener("change", function() {
    localStorage.setItem("mk_lang", this.value);
    window.location.reload();
  });
}
if (_contentSel) {
  _contentSel.value = localStorage.getItem("mk_content") || "default";
  _contentSel.addEventListener("change", function() {
    localStorage.setItem("mk_content", this.value);
    window.location.reload();
  });
}

if (document.fonts && document.fonts.ready) {
  document.fonts.ready.then(() => {
    mapChart?.reflow();
    regChart?.reflow();
    calCharts.forEach(c => c?.reflow());
  });
}

window.addEventListener("resize", () => {
  mapChart?.reflow();
  regChart?.reflow();
  calCharts.forEach(c => c?.reflow());
});

// ── Share widget (event delegation — widget is injected by renderTodayStatus) ──
document.addEventListener('click', e => {
  const popover = document.getElementById('share-popover');
  if (!popover) return;
  if (e.target.closest('#share-toggle')) {
    e.stopPropagation();
    popover.hidden = !popover.hidden;
    return;
  }
  if (e.target.closest('#share-copy')) {
    navigator.clipboard.writeText(window.location.href).then(() => {
      const lbl = document.getElementById('share-copy-lbl');
      if (!lbl) return;
      const orig = lbl.textContent;
      lbl.textContent = 'Copied!';
      setTimeout(() => { lbl.textContent = orig; }, 1500);
    });
    return;
  }
  if (!e.target.closest('#share-popover')) popover.hidden = true;
});

// ── Touch tooltip handler ─────────────────────────────────────────────────────
// Hover-based tooltips don't work on touch. On touchstart, toggle .tip-active
// on the tapped element; any other touchstart dismisses the open one.
let _activeTip = null;
document.addEventListener("touchstart", e => {
  const el = e.target.closest("[data-tooltip]");
  if (el && el !== _activeTip) {
    if (_activeTip) _activeTip.classList.remove("tip-active");
    el.classList.add("tip-active");
    _activeTip = el;
    e.preventDefault();
  } else {
    if (_activeTip) _activeTip.classList.remove("tip-active");
    _activeTip = null;
  }
}, { passive: false });
