"""
Minimal Flask sidecar for live Open-Meteo endpoints.
Reads pre-computed cutoffs from var/sqlite/era5-slovenia.db.

Run:
    COUNTRY=si python3 mk_sidecar.py
Serves on http://127.0.0.1:5052
"""

import datetime, json, os, sqlite3, warnings, glob
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import requests as http_requests
import yaml
from flask import Flask, jsonify, request
from flask_cors import CORS
from scipy import stats
from scipy.stats import theilslopes
import pymannkendall as mk_test

warnings.filterwarnings("ignore")

# ── Config ────────────────────────────────────────────────────────────────────

COUNTRY = os.environ.get("COUNTRY", "si")
with open(f"countries/{COUNTRY}.yaml") as f:
    CONFIG = yaml.safe_load(f)

DB_PATH    = Path("var") / "sqlite" / "era5-slovenia.db"
PORT       = int(os.environ.get("SIDECAR_PORT", 5052))
MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun",
               "Jul","Aug","Sep","Oct","Nov","Dec"]

LOC_COORDS = {s["name"]: {"lat": s["lat"], "lon": s["lon"]}
              for s in CONFIG["stations"]}

_TODAY_CATEGORIES = [
    (10,  "freezing", "#3a5a8a"),
    (20,  "cold",     "#6c8fb6"),
    (80,  "nope",     "#e7d9b8"),
    (95,  "hot",      "#c25a2c"),
    (101, "hell",     "#962c1a"),
]

LAPSE_RATE = 0.0065
VARIABLES = {
    "temperature_max":        "Temperature Max (°C)",
    "temperature_min":        "Temperature Min (°C)",
    "temperature_mean":       "Temperature Mean (°C)",
    "precipitation_sum":      "Precipitation (mm)",
    "et0_evapotranspiration": "ET₀ Evapotranspiration (mm)",
}
PALETTE = ["#e07b00","#9b4dca","#c9880a","#d0408a","#20aab0","#b06830"]
MONTH_NAMES_REG = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

_VSTYLE = {
    "precipitation_sum": {
        "pos_rgb":(26,95,200), "neg_rgb":(160,92,32),
        "pos_label":"wetter ↑", "neg_label":"drier ↓", "chg_unit":"mm",
    },
    "et0_evapotranspiration": {
        "pos_rgb":(26,95,200), "neg_rgb":(160,92,32),
        "pos_label":"higher ET₀ ↑", "neg_label":"lower ET₀ ↓", "chg_unit":"mm",
    },
}
_TEMP_STYLE = {
    "pos_rgb":(204,34,34), "neg_rgb":(26,95,200),
    "pos_label":"warming ↑", "neg_label":"cooling ↓", "chg_unit":"°C",
}

def _vstyle(var: str) -> dict:
    return _VSTYLE.get(var, _TEMP_STYLE)

def _sig_label(p: float) -> str:
    if p < 0.001: return "p < 0.001  ★★★"
    if p < 0.01:  return "p < 0.01  ★★"
    if p < 0.05:  return "p < 0.05  ★"
    return "not significant"

def _resolve_col(var: str, corr: str) -> str:
    if corr == "corr" and var in ("temperature_max","temperature_min","temperature_mean"):
        return var + "_corr"
    return var

def _doy_to_md(doy: int) -> tuple[int,int]:
    ref = pd.Timestamp("2001-01-01") + pd.Timedelta(days=int(doy) - 1)
    return ref.month, ref.day

def _window_filter(loc_data: pd.DataFrame, month: int, day: int, half_window: int) -> pd.DataFrame:
    try:    target_doy = pd.Timestamp(2001, month, day).dayofyear
    except: target_doy = pd.Timestamp(2001, month, 28).dayofyear
    row_doy  = loc_data["date"].dt.dayofyear.to_numpy()
    raw_diff = (row_doy - target_doy).astype(int)
    circ_diff = ((raw_diff + 182) % 365) - 182
    out = loc_data[np.abs(circ_diff) <= half_window].copy()
    rd_out = raw_diff[np.abs(circ_diff) <= half_window]
    year_adj = np.where(rd_out > 182, 1, np.where(rd_out < -182, -1, 0))
    out["_window_year"] = out["year"].to_numpy() + year_adj
    return out

def _window_series(loc_data: pd.DataFrame, month: int, day: int, half_window: int, col: str) -> pd.Series:
    sub    = _window_filter(loc_data, month, day, half_window)
    agg_fn = "sum" if col in ("precipitation_sum","et0_evapotranspiration") else "mean"
    return sub.groupby("_window_year")[col].agg(agg_fn).dropna()

# ── Load CSV data ─────────────────────────────────────────────────────────────

def _load_csv(filepath: str) -> pd.DataFrame:
    df = pd.read_csv(filepath)
    df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d")
    return df

_DATA_DIR  = Path("data") / CONFIG["code"]
_csv_files = sorted(glob.glob(str(_DATA_DIR / "*.csv")))
if _csv_files:
    _frames = [_load_csv(f) for f in _csv_files]
    _data   = pd.concat(_frames, ignore_index=True)
    _data   = _data[_data["date"] <= pd.Timestamp.today()]
    _data["year"]  = _data["date"].dt.year
    _data["month"] = _data["date"].dt.month
    for _c in ("temperature_max","temperature_min","temperature_mean"):
        _data[_c + "_corr"] = _data[_c] + _data["elevation_diff_m"] * LAPSE_RATE
else:
    _data = pd.DataFrame()

_LOCATIONS = sorted(_data["location"].unique().tolist()) if not _data.empty else []

# ── Regression computation ────────────────────────────────────────────────────

_REG_CACHE: dict = {}

def _compute_regression(loc: str, var: str, month: int, day: int,
                        half_window: int, col: str, method: str) -> dict | None:
    key = (loc, var, month, day, half_window, col, method)
    if key in _REG_CACHE:
        return _REG_CACHE[key]
    if _data.empty:
        return None
    ld     = _data[_data["location"] == loc]
    series = _window_series(ld, month, day, half_window, col)
    if len(series) < 5:
        return None

    x_arr    = series.index.to_numpy(float)
    y_arr    = series.values
    baseline = float(series.mean())
    vs       = _vstyle(var)

    anomalies = y_arr - baseline
    max_abs   = max(float(np.abs(anomalies).max()), 1e-6)
    scatter   = []
    for yr, v, a in zip(x_arr, y_arr, anomalies):
        alpha = 0.45 + 0.50 * abs(a) / max_abs
        r, g, b = vs["pos_rgb"] if a >= 0 else vs["neg_rgb"]
        scatter.append({
            "x": int(yr), "y": round(float(v), 3),
            "color": f"rgba({r},{g},{b},{alpha:.2f})",
            "anomaly": round(float(a), 3),
        })

    x_line = np.linspace(x_arr.min(), x_arr.max(), 300)
    is_sum = col in ("precipitation_sum","et0_evapotranspiration")

    if method == "ols":
        slope, intercept, r_ann, p_val, _ = stats.linregress(x_arr, y_arr)
        y_line    = slope * x_line + intercept
        residuals = y_arr - (slope * x_arr + intercept)
        se_res    = np.sqrt(np.sum(residuals**2) / max(len(x_arr) - 2, 1))
        ss_x      = np.sum((x_arr - x_arr.mean())**2)
        t_crit    = stats.t.ppf(0.975, df=max(len(x_arr) - 2, 1))
        se_ln     = se_res * np.sqrt(1/len(x_arr) + (x_line - x_arr.mean())**2 / max(ss_x, 1e-12))
        upper, lower = y_line + t_crit * se_ln, y_line - t_crit * se_ln
        metric, metric_lbl, ar1 = r_ann**2, "R²", None
    else:
        res    = theilslopes(y_arr, x_arr, 0.95)
        slope  = res.slope
        mk_r   = mk_test.yue_wang_modification_test(y_arr)
        p_val, tau = mk_r.p, mk_r.Tau
        x_med, y_med = float(np.median(x_arr)), float(np.median(y_arr))
        ic    = y_med - slope          * x_med
        ic_hi = y_med - res.high_slope * x_med
        ic_lo = y_med - res.low_slope  * x_med
        y_line  = slope          * x_line + ic
        upper   = res.high_slope * x_line + ic_hi
        lower   = res.low_slope  * x_line + ic_lo
        metric, metric_lbl = tau**2, "τ²"
        ar1 = round(float(np.corrcoef(y_arr[:-1], y_arr[1:])[0,1]), 3) if len(y_arr) > 2 else 0.0

    trend10  = float(slope * 10)
    slope_ab = abs(slope)
    chg_unit = vs["chg_unit"]
    yrs_per  = 1.0 / slope_ab if slope_ab > 1e-9 else None
    chg_str  = f"1 {chg_unit} change every {yrs_per:.1f} yrs" if yrs_per else "No trend"
    agg_lbl  = "annual sums" if is_sum else "annual means"
    fit_desc = f"Fitted on {len(x_arr)} {agg_lbl} ({len(x_arr)} years)"
    if ar1 is not None:
        fit_desc += f"  ·  AR(1)={ar1:.2f}"

    result = {
        "loc": loc,
        "year_min": int(x_arr.min()),
        "year_max": int(x_arr.max()),
        "scatter": scatter,
        "line": {
            "x":     x_line.tolist(),
            "y":     [round(v, 4) for v in y_line],
            "upper": [round(v, 4) for v in upper],
            "lower": [round(v, 4) for v in lower],
        },
        "baseline": round(baseline, 4),
        "stats": {
            "method":     "OLS" if method == "ols" else "Theil-Sen+MK(TFPW)",
            "trend10":    round(trend10, 3),
            "metric":     round(float(metric), 4),
            "metric_lbl": metric_lbl,
            "p_val":      round(float(p_val), 5),
            "direction":  vs["pos_label"] if trend10 > 0 else vs["neg_label"],
            "chg_str":    chg_str,
            "fit_desc":   fit_desc,
            "sig_label":  _sig_label(float(p_val)),
            "n_years":    int(len(x_arr)),
            "ar1":        ar1,
        },
    }
    _REG_CACHE[key] = result
    return result

# ── Calendar computation ──────────────────────────────────────────────────────

_CAL_CACHE: dict = {}

def _compute_calendar(loc: str, col: str, var: str, half_window: int, method: str) -> dict:
    key = (loc, col, half_window, method)
    if key in _CAL_CACHE:
        return _CAL_CACHE[key]
    if _data.empty:
        return {}
    ld = _data[_data["location"] == loc]

    rows = []
    for m in range(1, 13):
        for d in range(1, 32):
            try: pd.Timestamp(2001, m, d)
            except: continue
            ser = _window_series(ld, m, d, half_window, col)
            if len(ser) < 5:
                rows.append({"month": m, "day": d, "trend10": None, "p_val": None})
                continue
            x_arr, y_arr = ser.index.to_numpy(float), ser.values
            if method == "ols":
                slope, _, _, p_val, _ = stats.linregress(x_arr, y_arr)
            else:
                res   = theilslopes(y_arr, x_arr, 0.95)
                slope = res.slope
                p_val = mk_test.yue_wang_modification_test(y_arr).p
            rows.append({
                "month": m, "day": d,
                "trend10": round(float(slope * 10), 4),
                "p_val":   round(float(p_val), 5),
            })

    _CAL_CACHE[key] = {"loc": loc, "var": var, "rows": rows}
    return _CAL_CACHE[key]

# ── Hero / trends ─────────────────────────────────────────────────────────────

def _compute_trends(month: int, day: int, var: str,
                    half_window: int, col: str, method: str) -> list[dict]:
    if _data.empty:
        return []
    vs = _vstyle(var)
    out = []
    for loc in _LOCATIONS:
        r = _compute_regression(loc, var, month, day, half_window, col, method)
        if r:
            r2 = {**r, "loc": loc}
            out.append(r2)
    return out

# ── SQLite helper ─────────────────────────────────────────────────────────────

def _db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _get_cutoffs(station: str | None, month: int, day: int) -> dict | None:
    """Fetch pre-computed percentile cutoffs for (station or national aggregate, month, day)."""
    if not DB_PATH.exists():
        return None
    try:
        with _db_conn() as conn:
            if station:
                row = conn.execute(
                    "SELECT * FROM si_daily_window WHERE station=? AND month=? AND day=?",
                    (station, month, day),
                ).fetchone()
            else:
                # National aggregate: average cutoffs across all stations;
                # pick distribution_json from the station closest to median p50
                agg = conn.execute(
                    """SELECT
                         AVG(p5) AS p5, AVG(p10) AS p10, AVG(p20) AS p20,
                         AVG(p50) AS p50, AVG(p80) AS p80, AVG(p95) AS p95,
                         SUM(n_samples) AS n_samples,
                         MIN(year_min) AS year_min, MAX(year_max) AS year_max
                       FROM si_daily_window WHERE month=? AND day=?""",
                    (month, day),
                ).fetchone()
                if agg is None:
                    return None
                median_p50 = dict(agg).get("p50") or 0
                rep = conn.execute(
                    """SELECT distribution_json FROM si_daily_window
                       WHERE month=? AND day=?
                       ORDER BY ABS(p50 - ?) LIMIT 1""",
                    (month, day, median_p50),
                ).fetchone()
                row = {**dict(agg), "distribution_json": (rep[0] if rep else None)}
            if row is None:
                return None
            return dict(row)
    except Exception:
        return None

# ── Live Open-Meteo fetch ─────────────────────────────────────────────────────

_RAW_CACHE: dict[str, dict[str, float]] = {}


def _fetch_om(date_str: str) -> dict[str, float]:
    if date_str in _RAW_CACHE:
        return _RAW_CACHE[date_str]

    today_str = datetime.date.today().isoformat()
    if date_str == today_str:
        url, extra = "https://api.open-meteo.com/v1/forecast", {"forecast_days": 1}
    else:
        url, extra = "https://archive-api.open-meteo.com/v1/archive", {
            "start_date": date_str, "end_date": date_str
        }

    def _one(name: str, lat: float, lon: float) -> tuple[str, float | None]:
        try:
            resp = http_requests.get(url, params={
                "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
                "daily": "temperature_2m_max",
                "timezone": CONFIG["timezone"], **extra,
            }, timeout=10)
            resp.raise_for_status()
            arr = resp.json().get("daily", {}).get("temperature_2m_max", [])
            return name, float(arr[0]) if arr and arr[0] is not None else None
        except Exception:
            return name, None

    result: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(_one, n, c["lat"], c["lon"]): n
                   for n, c in LOC_COORDS.items()}
        for fut in as_completed(futures):
            name, val = fut.result()
            if val is not None:
                result[name] = val

    _RAW_CACHE[date_str] = result
    return result

# ── Category helper ───────────────────────────────────────────────────────────

def _categorize(pct: float) -> tuple[str, str]:
    for cutoff, key, color in _TODAY_CATEGORIES:
        if pct < cutoff:
            return key, color
    last = _TODAY_CATEGORIES[-1]
    return last[1], last[2]

# ── Same-date rank ────────────────────────────────────────────────────────────

# Map station name → SQLite table name
_STATION_TABLES = {s["name"]: f"si_{s['name']}" for s in CONFIG["stations"]}


def _compute_rank(loc: str | None, month: int, day: int,
                  date_str: str, today_temp: float) -> dict | None:
    """Return rank_info dict if today lands in top/bottom 5, else None."""
    if not DB_PATH.exists():
        return None
    try:
        with _db_conn() as conn:
            if loc and loc in _STATION_TABLES:
                table = _STATION_TABLES[loc]
                rows = conn.execute(
                    f'SELECT date, temperature_max FROM "{table}" '
                    "WHERE strftime('%m', date)=? AND strftime('%d', date)=? "
                    "AND date != ?",
                    (f"{month:02d}", f"{day:02d}", date_str),
                ).fetchall()
                # One row per year for a single station
                by_date = {r["date"]: r["temperature_max"] for r in rows if r["temperature_max"] is not None}
            else:
                # National: max across all station tables for each calendar date
                parts = []
                for tbl in _STATION_TABLES.values():
                    parts.append(
                        f'SELECT date, temperature_max FROM "{tbl}" '
                        f"WHERE strftime('%m', date)=? AND strftime('%d', date)=? AND date != ?"
                    )
                union = " UNION ALL ".join(parts)
                params = []
                for _ in _STATION_TABLES:
                    params += [f"{month:02d}", f"{day:02d}", date_str]
                rows = conn.execute(
                    f"SELECT date, MAX(temperature_max) AS tmax FROM ({union}) GROUP BY date",
                    params,
                ).fetchall()
                by_date = {r["date"]: r["tmax"] for r in rows if r["tmax"] is not None}

        if len(by_date) < 10:
            return None

        vals = sorted(by_date.items(), key=lambda kv: kv[1], reverse=True)
        rank_total = len(vals) + 1
        rank_hot  = sum(1 for _, v in vals if v > today_temp) + 1
        rank_cold = sum(1 for _, v in vals if v < today_temp) + 1

        direction = None
        if rank_hot  <= 5: direction = "hot"
        elif rank_cold <= 5: direction = "cold"
        if not direction:
            return None

        ascending = direction == "cold"
        top4 = sorted(vals, key=lambda kv: kv[1], reverse=not ascending)[:4]
        top5_list = [{"year": int(d[:4]), "date": d, "temp": round(float(v), 1)}
                     for d, v in top4]
        top5_list.append({"year": int(date_str[:4]), "date": date_str,
                           "temp": round(float(today_temp), 1), "is_today": True})
        top5_list.sort(key=lambda x: x["temp"], reverse=(direction == "hot"))

        rank = rank_hot if direction == "hot" else rank_cold
        return {"rank": rank, "total": rank_total, "direction": direction, "top5": top5_list}
    except Exception:
        return None


# ── Core computation ──────────────────────────────────────────────────────────

def _today_status(date_str: str, loc: str | None) -> dict:
    target = datetime.date.fromisoformat(date_str)
    today  = datetime.date.today()
    if target > today:
        return {"available": False}

    month, day = target.month, target.day
    dlabel     = f"{MONTH_NAMES[month - 1]} {day}"

    # Fetch live temperature
    temps = _fetch_om(date_str)
    if not temps:
        return {"available": False}
    if loc:
        if loc not in temps:
            return {"available": False}
        today_temp = temps[loc]
    else:
        today_temp = max(temps.values())

    # Pre-computed cutoffs from SQLite
    cutoffs = _get_cutoffs(loc, month, day)
    if cutoffs is None:
        return {"available": False}

    # Rank today_temp against cutoffs
    p5,  p10 = cutoffs["p5"],  cutoffs["p10"]
    p20, p50 = cutoffs["p20"], cutoffs["p50"]
    p80, p95 = cutoffs["p80"], cutoffs["p95"]

    # Linear interpolation to estimate percentile
    boundaries = [(0, p5), (5, p5), (10, p10), (20, p20),
                  (50, p50), (80, p80), (95, p95), (100, p95 + (p95 - p80))]
    pct: float = 0.0
    for i in range(len(boundaries) - 1):
        pct_lo, t_lo = boundaries[i]
        pct_hi, t_hi = boundaries[i + 1]
        if t_lo <= today_temp <= t_hi:
            span = t_hi - t_lo
            pct  = pct_lo + (pct_hi - pct_lo) * ((today_temp - t_lo) / span if span else 0)
            break
    else:
        if today_temp >= p95:
            pct = 95.0 + 5.0 * min((today_temp - p95) / max(p95 - p80, 0.1), 1.0)
        else:
            pct = 0.0
    pct = round(float(np.clip(pct, 0, 100)), 1)

    cat_key, color = _categorize(pct)

    dist_raw = cutoffs.get("distribution_json")
    distribution = json.loads(dist_raw) if dist_raw else []

    rank_info = _compute_rank(loc, month, day, date_str, today_temp)

    return {
        "available":    True,
        "date":         date_str,
        "today_temp":   round(float(today_temp), 1),
        "percentile":   pct,
        "category_key": cat_key,
        "color":        color,
        "n_samples":    int(cutoffs.get("n_samples") or 0),
        "year_min":     int(cutoffs.get("year_min") or 0),
        "year_max":     int(cutoffs.get("year_max") or 0),
        "distribution": distribution,
        "cutoffs":      {"p5": p5, "p10": p10, "p20": p20,
                         "p50": p50, "p80": p80, "p95": p95},
        "day_label":    dlabel,
        "month_num":    month,
        "day_num":      day,
        "rank_info":    rank_info,
        "loc":          loc,
    }

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)


@app.route("/api/live/today_status")
def api_today_status():
    date_str = request.args.get("date", datetime.date.today().isoformat())
    loc      = request.args.get("loc") or None
    return jsonify(_today_status(date_str, loc))


@app.route("/api/live/today_status/last7")
def api_today_last7():
    loc = request.args.get("loc") or None
    end = datetime.date.fromisoformat(
        request.args.get("date", datetime.date.today().isoformat())
    )
    days = []
    for offset in range(6, -1, -1):
        d = end - datetime.timedelta(days=offset)
        r = _today_status(d.isoformat(), loc)
        if r.get("available"):
            days.append({
                "date":         r["date"],
                "day_label":    r["day_label"],
                "today_temp":   r["today_temp"],
                "percentile":   r["percentile"],
                "category_key": r["category_key"],
                "color":        r["color"],
            })
    return jsonify({"available": bool(days), "days": days})


@app.route("/api/live/meta")
def api_meta():
    return jsonify({
        "country":          CONFIG["code"],
        "name":             CONFIG["name"],
        "default_location": CONFIG["default_location"],
        "languages":        CONFIG["languages"],
        "default_language": CONFIG["default_language"],
        "features":         CONFIG["features"],
        "map":              CONFIG["map"],
        "branding":         CONFIG["branding"],
        "stations": [
            {"name": s["name"], "lat": s["lat"], "lon": s["lon"],
             "elevation": s["elevation"]}
            for s in CONFIG["stations"]
        ],
    })


@app.route("/health")
def health():
    return "ok"


@app.route("/api/live/regression")
def api_regression():
    locs   = request.args.getlist("loc") or [CONFIG["default_location"]]
    var    = request.args.get("var",    "temperature_max")
    doy    = int(request.args.get("doy",    184))
    window = int(request.args.get("window",  30))
    corr   = request.args.get("corr",   "raw")
    method = request.args.get("method", "theilsen")

    month, day = _doy_to_md(doy)
    col    = _resolve_col(var, corr)
    ylabel = VARIABLES.get(var, var)
    unit   = ylabel.split("(")[-1].rstrip(")") if "(" in ylabel else ""
    ref    = pd.Timestamp("2001-01-01") + pd.Timedelta(days=doy - 1)
    date_label = f"{ref.day} {MONTH_NAMES_REG[ref.month - 1]}  ±{window} d"

    results = []
    for i, loc in enumerate(locs[:8]):
        try:
            res = _compute_regression(loc, var, month, day, window, col, method)
            if res:
                res = {**res, "color": PALETTE[i % len(PALETTE)]}
                results.append(res)
        except Exception:
            pass

    return jsonify({
        "results":    results,
        "date_label": date_label,
        "ylabel":     ylabel,
        "unit":       unit,
    })


@app.route("/api/live/calendar")
def api_calendar():
    loc    = request.args.get("loc",    CONFIG["default_location"])
    var    = request.args.get("var",    "temperature_max")
    window = int(request.args.get("window",  30))
    corr   = request.args.get("corr",   "raw")
    method = request.args.get("method", "theilsen")
    col    = _resolve_col(var, corr)
    ylabel = VARIABLES.get(var, var)
    unit   = ylabel.split("(")[-1].rstrip(")") if "(" in ylabel else ""
    result = _compute_calendar(loc, col, var, window, method)
    return jsonify({**result, "unit": unit,
                   "method_label": "OLS · R²" if method == "ols" else "Theil-Sen · TFPW MK · τ"})


@app.route("/api/live/trends")
def api_trends():
    doy    = int(request.args.get("doy",    184))
    var    = request.args.get("var",    "temperature_max")
    window = int(request.args.get("window",  30))
    corr   = request.args.get("corr",   "raw")
    method = request.args.get("method", "theilsen")
    month, day = _doy_to_md(doy)
    col    = _resolve_col(var, corr)
    results = _compute_trends(month, day, var, window, col, method)
    return jsonify({"results": results, "locations": _LOCATIONS})


@app.route("/api/live/variables")
def api_variables():
    return jsonify({
        "variables": VARIABLES,
        "locations": _LOCATIONS,
    })


if __name__ == "__main__":
    print(f"Sidecar listening on http://127.0.0.1:{PORT}")
    app.run(host="127.0.0.1", port=PORT, debug=False)
