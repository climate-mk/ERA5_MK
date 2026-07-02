import type { TodayStatus, Last7, AnnualTrendRow, AnnualTrend, SiteMeta, SeasonHeatmapRow, RegressionResult, RegressionResponse } from "../types/index.ts";

// In dev: empty string = same-origin proxy (see eleventy.config.mjs proxyConfig)
// In prod: set VITE_SIDECAR_URL / VITE_DATASETTE_URL at build time
const SIDECAR   = (import.meta.env.VITE_SIDECAR_URL   as string | undefined) ?? "";
const DATASETTE = (import.meta.env.VITE_DATASETTE_URL  as string | undefined) ?? "/datasette";

async function get<T>(url: string): Promise<T> {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${resp.status} ${url}`);
  return resp.json() as Promise<T>;
}

export function fetchMeta(): Promise<SiteMeta> {
  return get(`${SIDECAR}/api/live/meta`);
}

export function fetchTodayStatus(date: string, loc: string | null): Promise<TodayStatus> {
  const params = new URLSearchParams({ date });
  if (loc) params.set("loc", loc);
  return get(`${SIDECAR}/api/live/today_status?${params}`);
}

export function fetchLast7(date: string, loc: string | null): Promise<Last7> {
  const params = new URLSearchParams({ date });
  if (loc) params.set("loc", loc);
  return get(`${SIDECAR}/api/live/today_status/last7?${params}`);
}

export async function fetchDailyWindow(station: string | null, month: number, day: number) {
  const s = station ?? "Ljubljana";
  return get<import("../types/index.ts").DailyWindowRow[]>(
    `${DATASETTE}/era5-slovenia/si_daily_window.json?station=${encodeURIComponent(s)}&month=${month}&day=${day}&_shape=array&_size=1`
  );
}

export function fetchSeasonHeatmap(): Promise<SeasonHeatmapRow[]> {
  return get<SeasonHeatmapRow[]>(
    `${DATASETTE}/era5-slovenia/si_season_heatmap.json?_shape=array&_size=2000`
  );
}

export interface RegressionParams {
  locs:   string[];
  var:    string;
  doy:    number;
  window: number;
  corr:   "raw" | "corr";
  method: "theilsen" | "ols";
}

export function fetchRegression(p: RegressionParams): Promise<RegressionResponse> {
  const params = new URLSearchParams({ var: p.var, doy: String(p.doy), window: String(p.window), corr: p.corr, method: p.method });
  p.locs.forEach(l => params.append("loc", l));
  return get<RegressionResponse>(`${SIDECAR}/api/live/regression?${params}`);
}

export async function fetchAnnualTrend(month: number, day: number): Promise<AnnualTrend> {
  const url = `${DATASETTE}/era5-slovenia/si_annual_trend.json`
    + `?month=${month}&day=${day}&_shape=array&_size=1`;
  const rows = await get<AnnualTrendRow[]>(url);
  if (!rows.length) throw new Error("No annual trend row");
  const r = rows[0]!;
  return {
    dayLabel:  r.day_label,
    monthNum:  r.month,
    dayNum:    r.day,
    yearMin:   r.year_min,
    yearMax:   r.year_max,
    trend10:   r.trend10,
    pVal:      r.p_val,
    tau:       r.tau,
    nYears:    r.n_years,
    scatter:   JSON.parse(r.scatter_json) as Array<{ x: number; y: number }>,
    histLine: {
      x:     JSON.parse(r.hist_x_json) as number[],
      y:     JSON.parse(r.hist_y_json) as number[],
      upper: JSON.parse(r.hist_upper_json) as number[],
      lower: JSON.parse(r.hist_lower_json) as number[],
    },
    projLine: {
      x:     JSON.parse(r.proj_x_json) as number[],
      y:     JSON.parse(r.proj_y_json) as number[],
      upper: JSON.parse(r.proj_upper_json) as number[],
      lower: JSON.parse(r.proj_lower_json) as number[],
    },
  };
}
