/* MK Climate Explorer — Highcharts frontend */

"use strict";

// ── Helpers ───────────────────────────────────────────────────────────────────

const wait = ms => new Promise(res => setTimeout(res, ms));

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
let mapChart  = null;
let _mkTopo  = null;
let _mapUnit  = "";

function initRegChart() {
  regChart = Highcharts.chart("reg-chart", {
    chart: { type: "scatter", zoomType: "x", marginTop: 40 },
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
  const neutral = [220, 220, 228];
  const pos     = [160,   0,   0];
  const neg     = [  0,  45, 175];
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
        return `<span style="font-weight:600">${this.point.name}</span><br>
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
        dataLabels: {
          enabled: true,
          format: "{point.name}",
          style: { fontSize: "9px", fontWeight: "400", color: INK, textOutline: "2px #fff", fontFamily: "'JetBrains Mono', monospace" },
          y: 0,
        },
        point: {
          events: {
            click() {
              const loc = this.name;
              if (state.selLocs.includes(loc)) {
                if (state.selLocs.length > 1) {
                  state.selLocs = state.selLocs.filter(l => l !== loc);
                }
              } else {
                if (state.selLocs.length < 6) {
                  state.selLocs.push(loc);
                }
              }
              syncLocationCheckboxes();
              updateLocCheckboxStates();
              updateLocDisplay();
              updateMapSelection();
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
  const today = new Date();
  const start = new Date(today.getFullYear(), 0, 0);
  const diff = today - start;
  const oneDay = 1000 * 60 * 60 * 24;
  return Math.floor(diff / oneDay);
}


function isTemp(v) {
  return ["temperature_max","temperature_min","temperature_mean"].includes(v);
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
    if (titleEl) titleEl.textContent = `${varLbl} · ${results.map(r => r.loc).join(", ")}`;

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
    if (chgEl) chgEl.style.color = chg >= 0 ? ACCENT : COOL;

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
    const col   = isPos ? ACCENT : COOL;

    // Verdict block — temperature variables only
    const tempVar = isTemp(state.selVar);
    const verdictText = tempVar
      ? (st.trend10 !== 0
          ? (() => {
              const t100 = Math.abs(st.trend10 * 10).toFixed(2);
              const yrs  = Math.abs(10 / st.trend10).toFixed(1);
              const dir  = isPos ? "warm" : "cool";
              return `A century to ${dir} by <em>${sg}${t100} ${unit}</em> – at current pace, every <em>${yrs} years</em> adds another degree.`;
            })()
          : "No detectable trend at this day of year.")
      : null;
    const methodText = st.method === "OLS" ? "OLS linear regression" : "Theil-Sen + TFPW Mann-Kendall";

    return `<div class="loc-hero-card">
      <div class="loc-hero-main">
        <div class="hero-left">
          <div class="eyebrow">
            <span class="eyebrow-city">${res.loc}</span>
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
      <div class="sig-row">
        <div class="sig-item"><span class="sig-k">Significance</span><span class="sig-v">${stars(st.p_val)}</span></div>
        <div class="sig-item"><span class="sig-k">${st.metric_lbl}</span><span class="sig-v">${st.metric}</span></div>
        <div class="sig-item"><span class="sig-k">Sample</span><span class="sig-v">${sampleHtml(st)}</span></div>
        <div class="sig-item"><span class="sig-k">Autocorrelation</span><span class="sig-v">${ar1Html(st)}</span></div>
      </div>
    </div>`;
  }).join("");

}

// ── Render calendar ───────────────────────────────────────────────────────────

function _calChartOptions() {
  return {
    chart: { type: "column", marginTop: 10, animation: false, backgroundColor: "transparent" },
    title:    { text: null },
    subtitle: { text: null },
    xAxis: {
      title: { text: null },
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
      column: { animation: false, grouping: false, borderWidth: 0, pointPadding: 0, groupPadding: 0, pointWidth: 1.5 },
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
  chart.addSeries({ type: "column", name: "trend/decade", data: colData, borderWidth: 0, pointWidth: 1 }, false);
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
      if (titleEl) titleEl.textContent = `Year-round trend · ${loc}`;
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
  regDebounce = setTimeout(() => { refreshRegression(); refreshMap(); }, 180);
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
    name.textContent = loc;
    
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
    locDisplay.textContent = state.selLocs[0];
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
    refreshRegression();
    refreshCalendar();
    refreshMap();
  });
});

document.getElementById("corr-toggle").addEventListener("change", function() {
  state.corr = this.checked ? "corr" : "raw";
  Object.keys(calCache).forEach(k => delete calCache[k]);
  refreshRegression();
  refreshCalendar();
  refreshMap();
});

document.getElementById("window-input").addEventListener("change", function() {
  const v = parseInt(this.value);
  if (isNaN(v) || v < 1) return;
  state.window = v;
  Object.keys(calCache).forEach(k => delete calCache[k]);
  refreshRegression();
  refreshCalendar();
  refreshMap();
});

// ── Chat (BotFramework-WebChat + Direct Line token) ───────────────────────────

let _chatDirectLine  = null;
let _chatRefreshTimer = null;
let _conversationStarted = false;

async function openChat() {
  document.getElementById("chat-modal").classList.add("open");
  document.body.classList.add("chat-modal-open");
  if (_chatDirectLine) return;   // already initialised — reuse existing session
  try {
    const res  = await fetch("/api/token");
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Token error");
    _initWebChat(data.token, data.expires_in);
  } catch (e) {
    console.error("Chat init failed:", e);
  }
}

function _initWebChat(token, expiresIn) {
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

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {

  // Fetch metadata + topo in parallel
  const [meta, topo] = await Promise.all([
    fetch("api/meta").then(r => r.json()),
    fetch("https://code.highcharts.com/mapdata/countries/mk/mk-all.topo.json").then(r => r.json()),
  ]);
  _mkTopo = topo;
  state.locations  = meta.locations;
  state.variables  = meta.variables;
  state.monthNames = meta.month_names;
  state.palette    = meta.palette;

  // Set DOY to today
  state.doy = getTodayDOY();
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

  // Show/hide chat button based on server config
  if (!meta.chat_enabled) {
    document.getElementById("chat-toggle-btn").style.display = "none";
  }

  // Build location list
  buildLocationList(meta.locations);

  // Mark initially-selected method pill as active
  const initPill = document.querySelector("input[name='method']:checked")?.closest(".pill-radio");
  if (initPill) initPill.classList.add("active");

  // Initial DOY badge
  badge.textContent = doyBadge(state.doy);

  // Init charts
  initRegChart();

  // First load
  await refreshRegression();
  refreshCalendar();  // async, don't await
  refreshMap();       // async, don't await
}

init().catch(console.error);

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
