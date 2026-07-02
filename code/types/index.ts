/** Sidecar /api/live/today_status response */
export interface TodayStatus {
  available:    boolean;
  date?:        string;
  today_temp?:  number;
  percentile?:  number;
  category_key?: string;
  color?:       string;
  n_samples?:   number;
  year_min?:    number;
  year_max?:    number;
  distribution?: [number, number][];
  cutoffs?: {
    p5: number; p10: number; p20: number;
    p50: number; p80: number; p95: number;
  };
  day_label?:   string;
  month_num?:   number;
  day_num?:     number;
  rank_info?:   RankInfo | null;
  loc?:         string | null;
}

export interface RankInfo {
  rank:      number;
  total:     number;
  direction: "hot" | "cold";
  top5:      Array<{ year: number; date: string; temp: number; is_today?: boolean }>;
}

/** Sidecar /api/live/today_status/last7 response */
export interface Last7 {
  available: boolean;
  days: Array<{
    date:         string;
    day_label:    string;
    today_temp:   number;
    percentile:   number;
    category_key: string;
    color:        string;
  }>;
}

/** Datasette si_annual_trend row */
export interface AnnualTrendRow {
  month:           number;
  day:             number;
  day_label:       string;
  year_min:        number;
  year_max:        number;
  trend10:         number;
  p_val:           number;
  tau:             number;
  n_years:         number;
  scatter_json:    string;
  hist_x_json:     string;
  hist_y_json:     string;
  hist_upper_json: string;
  hist_lower_json: string;
  proj_x_json:     string;
  proj_y_json:     string;
  proj_upper_json: string;
  proj_lower_json: string;
}

/** Datasette si_daily_window row */
export interface DailyWindowRow {
  station:           string;
  month:             number;
  day:               number;
  p5:                number;
  p10:               number;
  p20:               number;
  p50:               number;
  p80:               number;
  p95:               number;
  n_samples:         number;
  year_min:          number;
  year_max:          number;
  distribution_json: string;
}

/** Sidecar /api/live/meta response */
export interface SiteMeta {
  country:          string;
  name:             string;
  default_location: string;
  languages:        string[];
  default_language: string;
  features:         Record<string, boolean>;
  map:              { center_lat: number; center_lon: number; zoom: number };
  branding:         { site_title: string; domain: string };
  stations: Array<{ name: string; lat: number; lon: number; elevation: number }>;
}

/** Parsed annual trend with arrays decoded from JSON columns */
export interface AnnualTrend {
  dayLabel:  string;
  monthNum:  number;
  dayNum:    number;
  yearMin:   number;
  yearMax:   number;
  trend10:   number;
  pVal:      number;
  tau:       number;
  nYears:    number;
  scatter:   Array<{ x: number; y: number }>;
  histLine:  { x: number[]; y: number[]; upper: number[]; lower: number[] };
  projLine:  { x: number[]; y: number[]; upper: number[]; lower: number[] };
}
