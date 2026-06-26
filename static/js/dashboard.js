// Dashboard-only behavior layered on top of the shared app.js. None of this
// touches app.js itself except by calling its newly-parameterized render
// helpers (renderTodayCardInto/renderSpeiTrendChart/renderTropicalChart) —
// index.html never loads this file, so it can't be affected by anything here.

const DASH_MAX_LOCS = 3;

// One shared "viewed date" for all 3 Today cards (mirrors app.js's own
// _todayOffset/_todayViewDate/_todayServerDate, but kept separately here
// since this page never uses app.js's single-card #today-status path).
let _dashTodayOffset     = 0;
let _dashTodayViewDate   = null; // null = today
let _dashTodayServerDate = null; // 'YYYY-MM-DD' for "today" as resolved by the server

// ── Default location selection ──────────────────────────────────────────────
// Mirrors init()'s own "default_location + highest-trend station" logic
// (app.js:2371-2375), extended to 3: default_location ("Skopje") + the 2
// remaining locations with the largest temperature_max trend10 (°C/decade).
// Only runs when no valid saved 1-3 location preference exists — checked
// directly against the shared mk_prefs localStorage entry (the same one
// init() itself already consulted via loadPrefs()), so a returning visitor's
// saved selection is never overridden.
async function _dashPickDefaultLocations() {
  let hasSavedLocs = false;
  try {
    const p = JSON.parse(localStorage.getItem(_PREFS_KEY) || 'null');
    if (p && Array.isArray(p.selLocs) && (!p.ts || Date.now() - p.ts <= _PREFS_TTL_MS)) {
      const valid = p.selLocs.filter(l => state.locations.includes(l));
      if (valid.length >= 1 && valid.length <= DASH_MAX_LOCS) hasSavedLocs = true;
    }
  } catch { /* ignore malformed prefs, fall through to auto-pick */ }
  if (hasSavedLocs) return;

  const defLoc = _metaConfig?.default_location || "Skopje";
  try {
    const todayDoy = getTodayDOY();
    const r = await fetch(`api/trends?var=temperature_max&doy=${todayDoy}&window=7&method=theilsen&corr=raw`)
      .then(res => res.json());
    if (!r?.points?.length) { state.selLocs = [defLoc]; return; }
    const sorted = [...r.points].sort((a, b) =>
      b.trend10 - a.trend10 || a.loc.localeCompare(b.loc));
    const extras = sorted.filter(p => p.loc !== defLoc).slice(0, DASH_MAX_LOCS - 1).map(p => p.loc);
    state.selLocs = [defLoc, ...extras];
  } catch {
    state.selLocs = [defLoc];
  }
}

// Clamp to 1-3 (was: always exactly 3) — trims if a restored preference had
// more than 3; never force-fills, since 1 or 2 deliberately-chosen locations
// is now valid.
function _dashEnforceLocCount() {
  if (state.selLocs.length > DASH_MAX_LOCS) {
    state.selLocs = state.selLocs.slice(0, DASH_MAX_LOCS);
    syncLocationCheckboxes();
    updateMapSelection();
    refreshRegression();
    refreshCalendar();
  }
}

// Apply `.dash-row-solo` (full-width span) to a row's only card when N=1;
// clear it otherwise. N=2 needs no class — cards keep their normal 1-column
// width with the 3rd column left empty, which is the grid's default.
function _dashSizeRow(rowEl) {
  const cards = rowEl.children;
  for (const card of cards) card.classList.remove("dash-row-solo");
  if (cards.length === 1) cards[0].classList.add("dash-row-solo");
}

// ── Today row: one card per selected location ───────────────────────────────
// Title/subtitle live in a separate .dash-card-head sibling, not inside the
// body renderTodayCardInto() overwrites each render — so the head survives
// every re-render (date step, location change) untouched.
async function _dashRenderTodayRow() {
  const row = document.getElementById("today-row");
  if (!row || !isEnabled("today_section")) return;
  const locs = state.selLocs;
  row.innerHTML = locs.map((loc, i) => `<div class="dash-card">
      <div class="dash-card-head">
        <div class="dash-card-title">${locName(loc)}</div>
        <div class="dash-card-sub">Is it hot today?</div>
      </div>
      <div id="today-card-${i}" class="dash-card-body"></div>
    </div>`).join("");
  _dashSizeRow(row);

  await Promise.all(locs.map(async (loc, i) => {
    try {
      const params = new URLSearchParams({ loc });
      if (_dashTodayViewDate) params.set('date', _dashTodayViewDate);
      const r = await fetch(`api/today_status?${params}`).then(res => res.json());
      if (!r.available) return;
      if (_dashTodayOffset === 0 && r.date) _dashTodayServerDate = r.date;
      renderTodayCardInto(`today-card-${i}`, r, `today-${i}`);
    } catch { /* leave this card empty on network error */ }
  }));
}

// Step all 3 Today cards' date together (called from the shared DOY
// prev/next/play controls via the setDoy wrapper below).
async function _dashNavigateTodayTo(newOffset) {
  if (newOffset > 0) return;
  _dashTodayOffset   = newOffset;
  _dashTodayViewDate = newOffset === 0 ? null : _addDaysToDateStr(_dashTodayServerDate, newOffset);
  await _dashRenderTodayRow();
}

// ── SPEI trend / tropical days / tropical nights rows: one card per
// selected location, each pinned to its own location (no station-picker). ──
// `label` is a static per-row title ("Summer SPEI trend" / "Tropical days" /
// "Tropical nights") — app.js's own dynamic #<idp>-title (e.g. "Skopje —
// Summer SPEI-3" or just "Skopje") already names the location, so it's
// shown as the subtitle underneath instead of changing what app.js writes
// (keeps app.js's title text identical to index.html's single-card usage).
// `withSub` adds the #<idp>-sub placeholder renderSpeiTrendChart() needs
// (app.js sets its .textContent unconditionally) — renderTropicalChart()
// has no such element, so tropical cards omit it.
function _dashCardMarkup(idp, label, withSub) {
  return `<div class="dash-card" id="${idp}-section">
    <div class="dash-card-head">
      <div class="dash-card-title">${label}</div>
      <div class="dash-card-sub" id="${idp}-title">—</div>
    </div>
    ${withSub ? `<div id="${idp}-sub" hidden></div>` : ""}
    <div id="${idp}-obs" hidden></div>
    <div id="${idp}-slope" hidden></div>
    <p id="${idp}-explain" hidden></p>
    <div id="${idp}-controls" class="shm-controls"></div>
    <div class="dash-chart-wrap"><div id="${idp}-chart" class="dash-chart"></div></div>
  </div>`;
}

function _dashRenderSpeiTrendRow() {
  const row = document.getElementById("spei-trend-row");
  if (!row || !isEnabled("drought_trend_chart")) return;
  const locs = state.selLocs;
  row.innerHTML = locs.map((_, i) => _dashCardMarkup(`spei-trend-${i}`, "Drought index (SPEI) trend", true)).join("");
  _dashSizeRow(row);
  locs.forEach((loc, i) => renderSpeiTrendChart(`spei-trend-${i}`, loc));
}

function _dashRenderTropicalRow(kind) {
  const cfg = TROP_CONFIGS[kind];
  const row = document.getElementById(`${cfg.prefix}-row`);
  if (!row || !isEnabled(cfg.featureFlag)) return;
  const locs = state.selLocs;
  row.innerHTML = locs.map((_, i) => _dashCardMarkup(`${cfg.prefix}-${i}`, cfg.tooltipNoun, false)).join("");
  _dashSizeRow(row);
  locs.forEach((loc, i) => renderTropicalChart(kind, `${cfg.prefix}-${i}`, loc));
}

// ── Re-render every per-location row when the selection changes ────────────
function _dashRenderLocationRows() {
  _dashRenderTodayRow();
  _dashRenderSpeiTrendRow();
  _dashRenderTropicalRow("days");
  _dashRenderTropicalRow("nights");
}

const _origUpdateLocDisplay = window.updateLocDisplay;
window.updateLocDisplay = function() {
  _dashEnforceLocCount();
  _origUpdateLocDisplay();
  _dashRenderLocationRows();
};

// Page Today's real date alongside the DOY prev/next/play buttons — each
// click steps state.doy AND all 3 Today cards' date by one day, same direction.
const _origSetDoy = window.setDoy;
window.setDoy = function(val) {
  const oldDoy = state.doy;
  _origSetDoy(val);
  const step = (state.doy > oldDoy || (oldDoy === 365 && state.doy === 1)) ? 1 : -1;
  _dashNavigateTodayTo(_dashTodayOffset + step);
};

// Season/SPEI heatmap tooltips (#shm-tip/#phm-tip) only hide on the grid
// cell's mouseleave inside app.js — no outside-click dismissal exists there.
// showTip/hideTip are closures private to renderSeasonHeatmap()/
// renderPrecipHeatmap() with no exported hook, but setting tip.hidden
// directly reproduces exactly what those closures' hideTip() does.
document.addEventListener("click", (e) => {
  ["shm-tip", "phm-tip"].forEach((id) => {
    const tip = document.getElementById(id);
    if (tip && !tip.hidden && !e.target.closest(".shm-cell, .shm-tip")) {
      tip.hidden = true;
    }
  });
});

// Apply once on load, after state.locations is known (set inside app.js's
// init(), which runs before this IIFE settles since app.js is loaded first
// and its init() is awaited internally, but this file still polls since
// there's no exported "ready" signal).
(function _waitForInitialState() {
  if (state.locations?.length) {
    _dashPickDefaultLocations().then(() => {
      // _dashPickDefaultLocations() may have replaced state.selLocs (e.g.
      // init()'s own 2-location auto-select → our 3-location Skopje+top2
      // pick) — refresh every location-driven part of the page, not just
      // the rows this file owns, otherwise the map/hero/calendar are left
      // showing init()'s earlier 2-location selection.
      syncLocationCheckboxes();
      updateMapSelection();
      refreshRegression();
      refreshCalendar();
      _dashEnforceLocCount();
      _origUpdateLocDisplay();
      _dashRenderLocationRows();
    });
    return;
  }
  setTimeout(_waitForInitialState, 200);
})();
