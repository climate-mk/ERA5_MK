/* MK Climate Explorer — Highcharts frontend */

"use strict";

// ── State ─────────────────────────────────────────────────────────────────────

const state = {
  locations:  [],
  variables:  {},
  monthNames: [],
  palette:    [],
  selLocs:    ["Skopje"],
  selVar:     "temperature_mean",
  method:     "theilsen",
  corr:       "raw",
  doy:        105,
  window:     7,
  playing:    false,
  playTimer:  null,
};

// Calendar cache: key → data (avoids re-fetching)
const calCache = {};

// ── Highcharts global theme ────────────────────────────────────────────────────

Highcharts.setOptions({
  chart: {
    backgroundColor: "transparent",
    style: { fontFamily: "system-ui, -apple-system, 'Segoe UI', sans-serif" },
    animation: false,
  },
  title:    { style: { color: "#1a1d2e", fontSize: "13px", fontWeight: "600" } },
  subtitle: { style: { color: "#6b7190", fontSize: "11px" } },
  xAxis: {
    labels:    { style: { color: "#6b7190", fontSize: "11px" } },
    lineColor: "#dde1ee",
    tickColor: "#dde1ee",
    gridLineColor: "rgba(0,0,0,0.06)",
  },
  yAxis: {
    labels:    { style: { color: "#6b7190", fontSize: "11px" } },
    lineColor: "#dde1ee",
    tickColor: "#dde1ee",
    gridLineColor: "rgba(0,0,0,0.06)",
    title:     { style: { color: "#6b7190", fontSize: "11px" } },
  },
  legend: {
    itemStyle:       { color: "#1a1d2e", fontSize: "12px", fontWeight: "400" },
    itemHoverStyle:  { color: "#000" },
    itemHiddenStyle: { color: "#aaa" },
  },
  tooltip: {
    backgroundColor: "#ffffff",
    borderColor:     "#dde1ee",
    style:           { color: "#1a1d2e", fontSize: "12px" },
    shadow:          { color: "rgba(0,0,0,0.10)", offsetX: 0, offsetY: 2, opacity: 1, width: 8 },
  },
  credits: { enabled: false },
  exporting: { enabled: false },
});

// ── Charts ────────────────────────────────────────────────────────────────────

let regChart = null;
let calChart = null;

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
        return `<b>${this.series.name}</b><br>${this.x}: ${this.y}`;
      },
    },
  });
}

function initCalChart() {
  calChart = Highcharts.chart("cal-chart", {
    chart: { type: "column", marginTop: 40 },
    title:    { text: "Year-round trend calendar" },
    subtitle: { text: "Calculating…" },
    xAxis: {
      title: { text: "Month" },
      categories: ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"],
      tickPositions: [0,31,59,90,120,151,181,212,243,273,304,334].map(d => d + 15),
      labels: {
        formatter() {
          const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
          const doy = this.value;
          const bounds = [0,31,59,90,120,151,181,212,243,273,304,334,365];
          for (let i = 0; i < 12; i++) {
            if (doy >= bounds[i] && doy < bounds[i+1]) return months[i];
          }
          return "";
        },
      },
    },
    yAxis:  { title: { text: "Trend / decade" }, plotLines: [{ value: 0, color: "#555", width: 1 }] },
    legend: { enabled: false },
    plotOptions: {
      column: {
        animation: false,
        grouping: false,
        borderWidth: 0,
        pointPadding: 0,
        groupPadding: 0,
        pointWidth: 1.5,
      },
    },
    series: [],
    tooltip: {
      formatter() {
        const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
        const doy = this.x;
        const ref = doyToDate(doy);
        return `<b>${ref}</b><br>Trend: ${this.y > 0 ? "+" : ""}${this.y}/decade<br>p=${this.point.p}`;
      },
    },
  });
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

function calCacheKey() {
  return `${state.selLocs[0]}|${state.selVar}|${state.corr}|${state.window}|${state.method}`;
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
  const key = calCacheKey();
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
      color: color,
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
    { text: `${ylabel}  ·  ${date_label}` },
    { text: results.length === 0 ? "No data" : "" },
    false
  );
  regChart.yAxis[0].setTitle({ text: ylabel }, false);
  regChart.redraw(false);
}

// ── Render stats ──────────────────────────────────────────────────────────────

function renderStats(data) {
  const row = document.getElementById("stats-row");
  row.innerHTML = "";

  data.results.forEach(res => {
    const st = res.stats;
    const sign = st.trend10 >= 0 ? "+" : "";
    const unit = data.unit || "";
    const card = document.createElement("div");
    card.className = "stat-card";
    card.innerHTML = `
      <div class="stat-loc">
        <div class="dot" style="background:${res.color}"></div>
        ${res.loc}
      </div>
      <div class="stat-trend" style="color:${res.color}">${sign}${st.trend10} ${unit}/dec</div>
      <div class="stat-detail">
        ${st.metric_lbl}=${st.metric} · ${st.sig_label}<br>
        ${st.chg_str}<br>
        <span style="color:#7a7f9a">${st.fit_desc}</span>
      </div>`;
    row.appendChild(card);
  });
}

// ── Render calendar ───────────────────────────────────────────────────────────

function renderCalendar(calData) {
  while (calChart.series.length) calChart.series[0].remove(false);

  const loc   = state.selLocs[0] || "—";
  const days  = calData.days;
  const unit  = calData.unit || "";

  // Single column series, one point per DOY, colour from server
  const colData = days.map(d => ({
    x: d.doy,
    y: d.slope10,
    color: d.color,
    p: d.p,
  }));

  calChart.addSeries({
    type: "column",
    name: "trend/decade",
    data: colData,
    borderWidth: 0,
    pointWidth: 1,
  }, false);

  // Mark current DOY with a plotLine
  calChart.xAxis[0].removePlotLine("doy-line");
  calChart.xAxis[0].addPlotLine({
    id: "doy-line",
    value: state.doy,
    color: "#6b72e8",
    width: 2,
    zIndex: 5,
    dashStyle: "ShortDash",
  });

  calChart.setTitle(
    { text: `Year-round trend · ${loc}` },
    { text: `${calData.method_label} · window ±${state.window} d · ${unit}/decade` },
    false
  );
  calChart.yAxis[0].setTitle({ text: `${unit}/decade` }, false);
  calChart.redraw(false);
}

// ── Refresh functions ─────────────────────────────────────────────────────────

async function refreshRegression() {
  showLoading("reg-loading", true);
  try {
    const data = await fetchRegression();
    renderRegression(data);
    renderStats(data);
  } catch(e) {
    console.error("Regression error:", e);
  } finally {
    showLoading("reg-loading", false);
  }
}

async function refreshCalendar() {
  const loc = state.selLocs[0];
  if (!loc) return;

  showLoading("cal-loading", true);
  try {
    const data = await fetchCalendar(loc);
    renderCalendar(data);
  } catch(e) {
    console.error("Calendar error:", e);
  } finally {
    showLoading("cal-loading", false);
  }
}

// Update DOY plotline on calendar without re-fetching
function updateCalDoyLine() {
  if (!calChart) return;
  calChart.xAxis[0].removePlotLine("doy-line");
  calChart.xAxis[0].addPlotLine({
    id: "doy-line",
    value: state.doy,
    color: "#6b72e8",
    width: 2,
    zIndex: 5,
    dashStyle: "ShortDash",
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
  regDebounce = setTimeout(refreshRegression, 180);
}

slider.addEventListener("input", () => setDoy(parseInt(slider.value)));

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
  btnPlay.textContent = "⏸ Pause";
  btnPlay.classList.add("active");
  state.playTimer = setInterval(() => {
    const next = state.doy >= 365 ? 1 : state.doy + 1;
    setDoy(next);
  }, 400);
}

function stopPlay() {
  state.playing = false;
  clearInterval(state.playTimer);
  btnPlay.textContent = "▶ Play";
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
      state.selLocs.push(loc);
    }
  } else {
    state.selLocs = state.selLocs.filter(l => l !== loc);
  }
  updateLocDisplay();
  refreshRegression();
  refreshCalendar();
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
});

document.querySelectorAll("input[name='method']").forEach(el => {
  el.addEventListener("change", function() {
    state.method = this.value;
    Object.keys(calCache).forEach(k => delete calCache[k]);
    refreshRegression();
    refreshCalendar();
  });
});

document.getElementById("corr-toggle").addEventListener("change", function() {
  state.corr = this.checked ? "corr" : "raw";
  Object.keys(calCache).forEach(k => delete calCache[k]);
  refreshRegression();
  refreshCalendar();
});

document.getElementById("window-input").addEventListener("change", function() {
  const v = parseInt(this.value);
  if (isNaN(v) || v < 1) return;
  state.window = v;
  Object.keys(calCache).forEach(k => delete calCache[k]);
  refreshRegression();
  refreshCalendar();
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
        fontSizeSmall:            "12px",
        primaryFont:              "system-ui, -apple-system, 'Segoe UI', sans-serif",
        backgroundColor:          "#ffffff",
        bubbleBackground:         "#f0f2f8",
        bubbleBorderRadius:       14,
        bubbleFromUserBackground: "#3d44b8",
        bubbleFromUserBorderRadius: 14,
        bubbleFromUserTextColor:  "#ffffff",
        bubbleTextColor:          "#1a1d2e",
        sendBoxBackground:        "#ffffff",
        sendBoxTextColor:         "#1a1d2e",
        sendBoxBorderTop:         "1px solid #dde1ee",
        timestampColor:           "#6b7190",
        botAvatarImage:           "/Ognen100.png",
        botAvatarInitials:        "O",
        hideUserAvatar:           true,
        hideUploadButton:         true,
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

  // Fetch metadata
  const meta = await fetch("api/meta").then(r => r.json());
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

  // Build location list
  buildLocationList(meta.locations);

  // Initial DOY badge
  badge.textContent = doyBadge(state.doy);

  // Init charts
  initRegChart();
  initCalChart();

  // First load
  await refreshRegression();
  refreshCalendar();  // async, don't await — show immediately
}

init().catch(console.error);
