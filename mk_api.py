"""
Flask API backend for MK Climate Explorer.
Run:  source venv/bin/activate && python3 mk_api.py
Open: http://127.0.0.1:5050
"""

import os, glob, time, hashlib
import numpy as np
import pandas as pd
import requests as http_requests
from flask import Flask, jsonify, request, send_from_directory, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from scipy import stats
from scipy.stats import theilslopes, gaussian_kde
import pymannkendall as mk_test
import warnings
warnings.filterwarnings("ignore")
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

# ── Location coordinates (for map endpoint) ────────────────────────────────────

LOC_COORDS = {
    "Berovo":       {"lat": 41.7047, "lon": 22.8556},
    "Bitola":       {"lat": 41.0314, "lon": 21.3347},
    "Debar":        {"lat": 41.5239, "lon": 20.5239},
    "Demir_Kapija": {"lat": 41.4042, "lon": 22.2458},
    "Gevgelija":    {"lat": 41.1414, "lon": 22.5011},
    "Gostivar":     {"lat": 41.7956, "lon": 20.9089},
    "Kavadarci":    {"lat": 41.4331, "lon": 22.0119},
    "Kicevo":       {"lat": 41.5131, "lon": 20.9589},
    "Kochani":      {"lat": 41.9167, "lon": 22.4167},
    "Kumanovo":     {"lat": 42.1322, "lon": 21.7144},
    "Lazaropole":   {"lat": 41.5394, "lon": 20.6956},
    "Negotino":     {"lat": 41.4831, "lon": 22.0894},
    "Ohrid":        {"lat": 41.1231, "lon": 20.8016},
    "Prilep":       {"lat": 41.3453, "lon": 21.5550},
    "Radovis":      {"lat": 41.6386, "lon": 22.4647},
    "Skopje":       {"lat": 41.9965, "lon": 21.4314},
    "Stip":         {"lat": 41.7457, "lon": 22.1961},
    "Strumica":     {"lat": 41.4378, "lon": 22.6431},
    "Tetovo":       {"lat": 42.0092, "lon": 20.9714},
    "Veles":        {"lat": 41.7153, "lon": 21.7753},
}

# ── Direct Line config ─────────────────────────────────────────────────────────

DIRECT_LINE_SECRET   = os.getenv("DIRECT_LINE_SECRET", "")
_DL_GENERATE_URL     = "https://europe.directline.botframework.com/v3/directline/tokens/generate"
_DL_REFRESH_URL      = "https://europe.directline.botframework.com/v3/directline/tokens/refresh"
_TOKEN_CACHE_BUFFER  = 300   # treat token as expired if < 5 min remaining

# Rate limits — change these two strings to tune the /api/token endpoints
TOKEN_LIMIT_MINUTE = "3 per minute"
TOKEN_LIMIT_HOUR   = "20 per hour"

# ── Load data ──────────────────────────────────────────────────────────────────

DATA_DIR = "./data"
def _load_csv(filepath):
    df = pd.read_csv(filepath)
    try:
        df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d")
    except (ValueError, TypeError):
        # Fallback for legacy DD-MM-YY format (e.g. old Gevgelija exports).
        # All CSVs were migrated to YYYY-MM-DD by mk_collect.py (2026-05-25) so
        # this branch no longer triggers, but is kept as a safety net.
        # dayfirst=True parses day/month correctly, but dateutil maps 2-digit years
        # 50-68 → 2050-2068 instead of 1950-1968, so subtract 100 years to fix.
        df["date"] = pd.to_datetime(df["date"], dayfirst=True)
        mask = df["date"].dt.year > pd.Timestamp.today().year
        df.loc[mask, "date"] = df.loc[mask, "date"] - pd.DateOffset(years=100)
    return df

dfs = [_load_csv(f) for f in sorted(glob.glob(os.path.join(DATA_DIR, "*.csv")))]
data = pd.concat(dfs, ignore_index=True)
data = data[data["date"] <= pd.Timestamp.today()]
data["year"]  = data["date"].dt.year
data["month"] = data["date"].dt.month

LAPSE_RATE = 0.0065
for _c in ["temperature_max", "temperature_min", "temperature_mean"]:
    data[_c + "_corr"] = data[_c] + data["elevation_diff_m"] * LAPSE_RATE

LOCATIONS   = sorted(data["location"].unique().tolist())
VARIABLES   = {
    "temperature_max":        "Temperature Max (°C)",
    "temperature_min":        "Temperature Min (°C)",
    "temperature_mean":       "Temperature Mean (°C)",
    "precipitation_sum":      "Precipitation (mm)",
    "et0_evapotranspiration": "ET₀ Evapotranspiration (mm)",
}
MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun",
               "Jul","Aug","Sep","Oct","Nov","Dec"]
PALETTE     = ["#4c52c9","#d94f4f","#2a9d5c","#e07b00",
               "#0099bb","#9b4dca","#c9880a","#3a7a3a"]

# ── Variable style ─────────────────────────────────────────────────────────────

_VSTYLE = {
    "precipitation_sum": {
        "pos_rgb": (26, 95, 200), "neg_rgb": (160, 92, 32),
        "pos_label": "wetter ↑",  "neg_label": "drier ↓",
        "chg_unit": "mm",
        "cal_pos": (35, 100, 210), "cal_neg": (180, 105, 25),
    },
    "et0_evapotranspiration": {
        "pos_rgb": (224, 123, 0),  "neg_rgb": (42, 157, 92),
        "pos_label": "higher ET₀ ↑", "neg_label": "lower ET₀ ↓",
        "chg_unit": "mm",
        "cal_pos": (210, 120, 0),  "cal_neg": (42, 157, 92),
    },
}
_TEMP = {
    "pos_rgb": (204, 34, 34), "neg_rgb": (26, 95, 200),
    "pos_label": "warming ↑",  "neg_label": "cooling ↓",
    "chg_unit": "°C",
    "cal_pos": (210, 55, 35),  "cal_neg": (35, 90, 210),
}

def vstyle(var):
    return _VSTYLE.get(var, _TEMP)

# ── Helpers ────────────────────────────────────────────────────────────────────

def sig_stars(p):
    return "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else "ns"

def sig_label(p):
    return {"***": "p < 0.001  ★★★", "**": "p < 0.01  ★★",
            "*":   "p < 0.05  ★",    "ns": "not significant"}[sig_stars(p)]

def resolve_col(var, corr):
    if corr == "corr" and var in ["temperature_max","temperature_min","temperature_mean"]:
        return var + "_corr"
    return var

def doy_to_md(doy):
    ref = pd.Timestamp("2001-01-01") + pd.Timedelta(days=int(doy) - 1)
    return ref.month, ref.day

def window_filter(loc_data, month, day, half_window):
    try:    target_doy = pd.Timestamp(2001, month, day).dayofyear
    except: target_doy = pd.Timestamp(2001, month, 28).dayofyear
    row_doy   = loc_data["date"].dt.dayofyear.to_numpy()
    raw_diff  = (row_doy - target_doy).astype(int)
    circ_diff = ((raw_diff + 182) % 365) - 182
    in_win    = np.abs(circ_diff) <= half_window
    out       = loc_data[in_win].copy()
    rd_out    = raw_diff[in_win]
    year_adj  = np.where(rd_out >  182,  1, np.where(rd_out < -182, -1, 0))
    out["_window_year"] = out["year"].to_numpy() + year_adj
    return out

def window_series(loc_data, month, day, half_window, col):
    sub    = window_filter(loc_data, month, day, half_window)
    agg_fn = "sum" if col in ["precipitation_sum","et0_evapotranspiration"] else "mean"
    return sub.groupby("_window_year")[col].agg(agg_fn).dropna()

def window_raw(loc_data, month, day, half_window, col):
    sub = window_filter(loc_data, month, day, half_window).dropna(subset=[col])
    sub["x"] = sub["year"] + (sub["date"].dt.dayofyear - 1) / 365.0
    return sub["x"].to_numpy(float), sub[col].to_numpy(float)

# ── Regression computation ─────────────────────────────────────────────────────

def compute_regression(loc, var, month, day, half_window, col, method):
    ld     = data[data["location"] == loc]
    series = window_series(ld, month, day, half_window, col)
    n_raw  = int(window_filter(ld, month, day, half_window)[col].notna().sum())
    if len(series) < 5:
        return None

    x_arr    = series.index.to_numpy(float)
    y_arr    = series.values
    baseline = float(series.mean())
    vs       = vstyle(var)

    # Dot colours
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

    # Always fit on annual aggregates (means or sums) — using raw daily values
    # contaminates the slope with within-year seasonal variation: a wider window
    # around a rising/falling season includes more intra-year pairs whose slope
    # reflects seasonality, not long-term trend. Annual aggregates are immune to
    # this because the seasonal component cancels within each year's window.
    is_sum = col in ["precipitation_sum","et0_evapotranspiration"]
    x_fit, y_fit = x_arr, y_arr   # annual means (temp) or annual sums (precip/ET0)

    x_line = np.linspace(x_arr.min(), x_arr.max(), 300)

    if method == "ols":
        slope, intercept, r_ann, p_val, _ = stats.linregress(x_fit, y_fit)
        y_line    = slope * x_line + intercept
        residuals = y_fit - (slope * x_fit + intercept)
        se_res    = np.sqrt(np.sum(residuals**2) / max(len(x_fit) - 2, 1))
        ss_x      = np.sum((x_fit - x_fit.mean())**2)
        t_crit    = stats.t.ppf(0.975, df=max(len(x_fit) - 2, 1))
        se_ln     = se_res * np.sqrt(1/len(x_fit) + (x_line - x_fit.mean())**2 / max(ss_x, 1e-12))
        upper, lower = y_line + t_crit * se_ln, y_line - t_crit * se_ln
        metric, metric_lbl, ar1 = r_ann**2, "R²", None
    else:
        res    = theilslopes(y_fit, x_fit, 0.95)
        slope  = res.slope
        mk_r   = mk_test.yue_wang_modification_test(y_arr)
        p_val, tau = mk_r.p, mk_r.Tau
        x_med, y_med = float(np.median(x_fit)), float(np.median(y_fit))
        ic      = y_med - slope          * x_med
        ic_hi   = y_med - res.high_slope * x_med
        ic_lo   = y_med - res.low_slope  * x_med
        y_line  = slope          * x_line + ic
        upper   = res.high_slope * x_line + ic_hi
        lower   = res.low_slope  * x_line + ic_lo
        metric, metric_lbl = tau**2, "τ²"
        ar1 = round(float(np.corrcoef(y_arr[:-1], y_arr[1:])[0, 1]), 3) if len(y_arr) > 2 else 0.0

    trend10   = float(slope * 10)
    slope_abs = abs(slope)
    chg_unit  = vs["chg_unit"]
    yrs_per   = 1.0 / slope_abs if slope_abs > 1e-9 else None
    chg_str   = f"1 {chg_unit} change every {yrs_per:.1f} yrs" if yrs_per else "No trend"
    agg_label = "annual sums" if is_sum else "annual means"
    fit_desc  = f"Fitted on {len(x_arr)} {agg_label} ({len(x_arr)} years)"
    if ar1 is not None:
        fit_desc += f"  ·  AR(1)={ar1:.2f}"

    return {
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
            "method":       "OLS" if method == "ols" else "Theil-Sen+MK(TFPW)",
            "trend10":      round(trend10, 3),
            "metric":       round(float(metric), 4),
            "metric_lbl":   metric_lbl,
            "p_val":        round(float(p_val), 5),
            "direction":    vs["pos_label"] if trend10 > 0 else vs["neg_label"],
            "chg_str":      chg_str,
            "fit_desc":     fit_desc,
            "sig_label":    sig_label(float(p_val)),
            "n_years":      int(len(x_arr)),
            "n_values":     n_raw,
            "ar1":          ar1,
        },
    }

# ── Calendar computation (cached) ──────────────────────────────────────────────

_CAL_CACHE = {}

def compute_calendar(loc, col, var, half_window, method):
    key = (loc, col, half_window, method)
    if key in _CAL_CACHE:
        return _CAL_CACHE[key]

    vs     = vstyle(var)
    ld     = data[data["location"] == loc]
    agg_fn = "sum" if col in ["precipitation_sum","et0_evapotranspiration"] else "mean"
    days   = []

    for doy in range(1, 366):
        ref = pd.Timestamp("2001-01-01") + pd.Timedelta(days=doy - 1)
        sub = window_filter(ld, ref.month, ref.day, half_window)
        ser = sub.groupby("_window_year")[col].agg(agg_fn).dropna()
        if len(ser) < 10:
            continue
        x, y = ser.index.to_numpy(float), ser.values
        try:
            if method == "ols":
                sv, _, rv, pv, _ = stats.linregress(x, y)
                metric = rv ** 2
            else:
                ts  = theilslopes(y, x, 0.95)
                sv  = ts.slope
                mkr = mk_test.yue_wang_modification_test(y)
                pv, metric = mkr.p, mkr.Tau
            alpha = 0.95 if pv < 0.001 else 0.70 if pv < 0.01 else 0.40 if pv < 0.05 else 0.12
            # Use slope sign for colour direction — metric is R² for OLS (always ≥0)
            # and τ for Theil-Sen (signed), so sv is the reliable direction indicator
            r, g, b = vs["cal_pos"] if sv >= 0 else vs["cal_neg"]
            days.append({
                "doy":     doy,
                "slope10": round(float(sv * 10), 4),
                "p":       round(float(pv), 5),
                "metric":  round(float(metric), 4),
                "color":   f"rgba({r},{g},{b},{alpha})",
            })
        except Exception:
            pass

    result = {"days": days}
    _CAL_CACHE[key] = result
    return result

# ── Timezone helper ────────────────────────────────────────────────────────────

def _today_mk():
    """Current date in Macedonia local time (CET/CEST = UTC+1/+2).
    The server runs UTC; without this, dates drift during the 22:00–00:00 UTC
    window (midnight–2am Skopje) causing a mismatch between Open-Meteo's
    timezone-aware forecast and the historical distribution month/day lookup."""
    return pd.Timestamp.now(tz="Europe/Skopje").normalize().tz_localize(None)

# ── Annual trend (cached) ──────────────────────────────────────────────────────

_ANNUAL_TREND_CACHE = {}

def compute_annual_trend():
    today     = _today_mk()
    cache_key = today.date().isoformat()
    if cache_key in _ANNUAL_TREND_CACHE:
        return _ANNUAL_TREND_CACHE[cache_key]

    month, day = today.month, today.day
    dlabel     = f"{MONTH_NAMES[month - 1]} {day}"

    # Daily max across all stations, then mean of top-15 days per year
    window = window_filter(data, month, day, 7)
    daily_max = (
        window.groupby(["_window_year", "date"])["temperature_max"]
        .max()
        .reset_index()
    )
    annual = (
        daily_max.groupby("_window_year")["temperature_max"]
        .apply(lambda x: x.nlargest(15).mean())
        .dropna()
    )

    # Last 30 years
    cutoff = int(annual.index.max()) - 30
    annual = annual[annual.index >= cutoff]
    x_arr  = annual.index.to_numpy(float)
    y_arr  = annual.values

    # Theil-Sen fit
    res   = theilslopes(y_arr, x_arr, 0.95)
    slope = res.slope
    x_med, y_med = float(np.median(x_arr)), float(np.median(y_arr))
    ic    = y_med - slope          * x_med
    ic_hi = y_med - res.high_slope * x_med
    ic_lo = y_med - res.low_slope  * x_med

    # Mann-Kendall significance
    mk_r  = mk_test.yue_wang_modification_test(y_arr)

    # Historical trend line (dense)
    x_hist = np.linspace(x_arr.min(), x_arr.max(), 300)
    y_hist = slope          * x_hist + ic
    u_hist = res.high_slope * x_hist + ic_hi
    l_hist = res.low_slope  * x_hist + ic_lo

    # Linear projection: last observed year → 2050
    last_yr = int(x_arr.max())
    x_fc    = np.linspace(last_yr, 2050, 200)
    y_fc    = slope          * x_fc + ic
    u_fc    = res.high_slope * x_fc + ic_hi
    l_fc    = res.low_slope  * x_fc + ic_lo

    scatter = [{"x": int(yr), "y": round(float(v), 2)} for yr, v in zip(x_arr, y_arr)]

    result = {
        "scatter":       scatter,
        "year_min":      int(x_arr.min()),
        "year_max":      last_yr,
        "day_label":     dlabel,
        "hist_line":     {"x": x_hist.tolist(),
                          "y":     [round(v, 3) for v in y_hist],
                          "upper": [round(v, 3) for v in u_hist],
                          "lower": [round(v, 3) for v in l_hist]},
        "projection_line": {"x": x_fc.tolist(),
                          "y":     [round(v, 3) for v in y_fc],
                          "upper": [round(v, 3) for v in u_fc],
                          "lower": [round(v, 3) for v in l_fc]},
        "stats": {
            "trend10": round(float(slope * 10), 3),
            "p_val":   round(float(mk_r.p), 5),
            "tau":     round(float(mk_r.Tau), 3),
            "n_years": int(len(x_arr)),
        },
    }
    _ANNUAL_TREND_CACHE[cache_key] = result
    return result

# ── Today status ("Is it Hot in Macedonia Today?") ─────────────────────────────

_TODAY_CACHE     = {}
_TODAY_CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")

_TODAY_CATEGORIES = [
    # (max_percentile_exclusive, name, hex, description_template)
    (10,  "Freezing", "#3a5a8a", "Among the coldest {d}s in our 76-year record."),
    (20,  "Cold",     "#6c8fb6", "Cooler than most {d}s we've measured."),
    (80,  "Nope",     "#e7d9b8", "Right around what {d} usually feels like in Macedonia."),
    (95,  "Hot",      "#c25a2c", "Among the hottest {d}s in our record."),
    (101, "Hell",     "#962c1a", "Exceptional heat — top 5% of all {d}s since 1950."),
]

def _categorize_today(pct, dlabel):
    for cutoff, name, color, tpl in _TODAY_CATEGORIES:
        if pct < cutoff:
            return name, color, tpl.format(d=dlabel)
    return _TODAY_CATEGORIES[-1][1], _TODAY_CATEGORIES[-1][2], _TODAY_CATEGORIES[-1][3].format(d=dlabel)

def _today_cache_path(date_str):
    return os.path.join(_TODAY_CACHE_DIR, f"today_{date_str}.json")

def _load_today_from_disk(date_str):
    """Return cached dict if today's file exists and is valid, else None."""
    import json as _json
    path = _today_cache_path(date_str)
    try:
        with open(path) as f:
            return _json.load(f)
    except Exception:
        return None

def _save_today_to_disk(date_str, result):
    """Persist a successful today_status result to disk."""
    import json as _json
    try:
        os.makedirs(_TODAY_CACHE_DIR, exist_ok=True)
        with open(_today_cache_path(date_str), "w") as f:
            _json.dump(result, f)
        # Remove cache files older than 3 days (filename sort works: today_YYYY-MM-DD.json)
        cutoff = (pd.Timestamp(date_str) - pd.Timedelta(days=3)).date().isoformat()
        for p in glob.glob(os.path.join(_TODAY_CACHE_DIR, "today_*.json")):
            file_date = os.path.basename(p)[len("today_"):-len(".json")]
            if file_date < cutoff:
                try: os.remove(p)
                except Exception: pass
    except Exception:
        pass  # disk write failure is non-fatal

def compute_today_status():
    today = _today_mk()
    cache_key = today.date().isoformat()

    # 1. Check in-memory cache (fast path — survives within a single process lifetime)
    if cache_key in _TODAY_CACHE:
        return _TODAY_CACHE[cache_key]

    # 2. Check filesystem cache (survives service restarts — written after first
    #    successful Open-Meteo fetch, so the 20-station call only happens once/day)
    cached = _load_today_from_disk(cache_key)
    if cached is not None:
        _TODAY_CACHE[cache_key] = cached
        return cached

    # 3. Fetch from Open-Meteo for all 20 stations — fetched in
    #    parallel individual requests (one per station) to avoid 502s that
    #    Open-Meteo's proxy returns for long multi-coordinate query strings.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch_one(loc_name, lat, lon):
        try:
            r = http_requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude":      f"{lat:.4f}",
                    "longitude":     f"{lon:.4f}",
                    "daily":         "temperature_2m_max",
                    "timezone":      "Europe/Skopje",
                    "forecast_days": 1,
                },
                timeout=10,
            )
            r.raise_for_status()
            arr = r.json().get("daily", {}).get("temperature_2m_max", [])
            return float(arr[0]) if arr and arr[0] is not None else None
        except Exception:
            return None

    today_temps = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {
            pool.submit(_fetch_one, name, c["lat"], c["lon"]): name
            for name, c in LOC_COORDS.items()
        }
        for fut in as_completed(futures):
            v = fut.result()
            if v is not None:
                today_temps.append(v)

    if not today_temps:
        _TODAY_CACHE[cache_key] = {"available": False}
        return _TODAY_CACHE[cache_key]
    today_temp = max(today_temps)

    # 2. Historical distribution: ±7-day window across all years, averaged across stations per date
    month, day = today.month, today.day
    window = window_filter(data, month, day, 7)
    daily_max = window.groupby("date")["temperature_max"].max().dropna()
    samples = daily_max.to_numpy()
    if len(samples) < 50:
        _TODAY_CACHE[cache_key] = {"available": False}
        return _TODAY_CACHE[cache_key]

    # 3. Percentile + category
    pct = float((samples < today_temp).mean() * 100)
    dlabel = f"{MONTH_NAMES[month - 1]} {day}"
    name, color, desc = _categorize_today(pct, dlabel)

    # 4. KDE curve + percentile cutoffs for the distribution chart
    cutoffs = {
        "p5":  round(float(np.percentile(samples,  5)), 2),
        "p10": round(float(np.percentile(samples, 10)), 2),
        "p20": round(float(np.percentile(samples, 20)), 2),
        "p50": round(float(np.percentile(samples, 50)), 2),
        "p80": round(float(np.percentile(samples, 80)), 2),
        "p95": round(float(np.percentile(samples, 95)), 2),
    }
    smin, smax = float(samples.min()), float(samples.max())
    pad = max((smax - smin) * 0.05, 0.5)
    x_grid = np.linspace(smin - pad, smax + pad, 200)
    try:
        kde     = gaussian_kde(samples)
        density = kde(x_grid)
    except Exception:
        density = np.zeros_like(x_grid)
    distribution = [[round(float(x), 3), round(float(d), 6)] for x, d in zip(x_grid, density)]

    result = {
        "available":    True,
        "today_temp":   round(today_temp, 1),
        "percentile":   round(pct, 1),
        "category":     name,
        "color":        color,
        "description":  desc,
        "n_samples":    int(len(samples)),
        "year_min":     int(data["year"].min()),
        "year_max":     int(data["year"].max()),
        "distribution": distribution,
        "cutoffs":      cutoffs,
        "day_label":    dlabel,
    }
    _TODAY_CACHE[cache_key] = result
    _save_today_to_disk(cache_key, result)   # persist so restarts don't re-fetch
    return result

# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder="static", static_url_path="")

# Session secret derived from the Direct Line secret — stable across restarts,
# invalidated automatically if the secret is rotated (correct behaviour).
app.secret_key = hashlib.sha256(DIRECT_LINE_SECRET.encode()).digest() if DIRECT_LINE_SECRET else os.urandom(24)

limiter = Limiter(get_remote_address, app=app, default_limits=[])

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/meta")
def api_meta():
    return jsonify({
        "locations":   LOCATIONS,
        "variables":   VARIABLES,
        "month_names": MONTH_NAMES,
        "palette":     PALETTE,
        "chat_enabled": bool(DIRECT_LINE_SECRET),
    })

@app.route("/api/regression")
def api_regression():
    locs   = request.args.getlist("loc") or ["Skopje"]
    var    = request.args.get("var",    "temperature_mean")
    doy    = int(request.args.get("doy",    105))
    window = int(request.args.get("window",   7))
    corr   = request.args.get("corr",   "raw")
    method = request.args.get("method", "theilsen")

    month, day = doy_to_md(doy)
    col    = resolve_col(var, corr)
    ylabel = VARIABLES.get(var, var)
    unit   = ylabel.split("(")[-1].rstrip(")") if "(" in ylabel else ""
    ref    = pd.Timestamp("2001-01-01") + pd.Timedelta(days=doy - 1)
    date_label = f"{ref.day} {MONTH_NAMES[ref.month - 1]}  ±{window} d"

    results = []
    for i, loc in enumerate(locs[:8]):
        try:
            res = compute_regression(loc, var, month, day, window, col, method)
            if res:
                res["color"] = PALETTE[i % len(PALETTE)]
                results.append(res)
        except Exception:
            pass

    return jsonify({
        "results":    results,
        "date_label": date_label,
        "ylabel":     ylabel,
        "unit":       unit,
    })

@app.route("/api/calendar")
def api_calendar():
    loc    = request.args.get("loc",    "Skopje")
    var    = request.args.get("var",    "temperature_mean")
    window = int(request.args.get("window",   7))
    corr   = request.args.get("corr",   "raw")
    method = request.args.get("method", "theilsen")

    col    = resolve_col(var, corr)
    ylabel = VARIABLES.get(var, var)
    unit   = ylabel.split("(")[-1].rstrip(")") if "(" in ylabel else ""

    result = compute_calendar(loc, col, var, window, method)
    return jsonify({
        **result,
        "unit":         unit,
        "loc":          loc,
        "method_label": "OLS · R²" if method == "ols" else "Theil-Sen · TFPW MK · τ",
    })

@app.route("/api/trends")
def api_trends():
    var    = request.args.get("var",    "temperature_mean")
    doy    = int(request.args.get("doy",    105))
    window = int(request.args.get("window",   7))
    corr   = request.args.get("corr",   "raw")
    method = request.args.get("method", "theilsen")

    month, day = doy_to_md(doy)
    col = resolve_col(var, corr)
    vs  = vstyle(var)

    points = []
    for loc, coords in LOC_COORDS.items():
        try:
            res = compute_regression(loc, var, month, day, window, col, method)
            if res:
                points.append({
                    "loc":       loc,
                    "lat":       coords["lat"],
                    "lon":       coords["lon"],
                    "trend10":   res["stats"]["trend10"],
                    "p_val":     res["stats"]["p_val"],
                    "direction": res["stats"]["direction"],
                    "sig_label": res["stats"]["sig_label"],
                })
        except Exception:
            pass

    return jsonify({"points": points, "unit": vs["chg_unit"]})


@app.route("/api/today_status")
def api_today_status():
    return jsonify(compute_today_status())


@app.route("/api/annual_trend")
def api_annual_trend():
    return jsonify(compute_annual_trend())


@app.route("/api/token")
@limiter.limit(TOKEN_LIMIT_MINUTE)
@limiter.limit(TOKEN_LIMIT_HOUR)
def get_token():
    if not DIRECT_LINE_SECRET:
        return jsonify({"error": "Chat service not configured"}), 503

    # Return cached token if it still has more than TOKEN_CACHE_BUFFER seconds left
    cached = session.get("dl_token")
    if cached and time.time() < cached["expires_at"] - _TOKEN_CACHE_BUFFER:
        return jsonify({
            "token":          cached["token"],
            "conversationId": cached["conversationId"],
            "expires_in":     int(cached["expires_at"] - time.time()),
        })

    try:
        resp = http_requests.post(
            _DL_GENERATE_URL,
            headers={"Authorization": f"Bearer {DIRECT_LINE_SECRET}"},
            timeout=10,
        )
        resp.raise_for_status()
    except http_requests.HTTPError as e:
        print(f"[token] Direct Line HTTP {e.response.status_code}: {e.response.text[:300]}")
        return jsonify({"error": "Failed to generate token", "detail": e.response.text[:200]}), 502
    except http_requests.RequestException as e:
        print(f"[token] Direct Line request failed: {e}")
        return jsonify({"error": "Failed to generate token"}), 502

    data = resp.json()
    session["dl_token"] = {
        "token":          data["token"],
        "conversationId": data["conversationId"],
        "expires_at":     time.time() + data["expires_in"],
    }
    return jsonify({
        "token":          data["token"],
        "conversationId": data["conversationId"],
        "expires_in":     data["expires_in"],
    })


@app.route("/api/token/refresh", methods=["POST"])
@limiter.limit(TOKEN_LIMIT_MINUTE)
@limiter.limit(TOKEN_LIMIT_HOUR)
def refresh_token():
    cached = session.get("dl_token")
    if not cached:
        return jsonify({"error": "No active session"}), 400

    try:
        resp = http_requests.post(
            _DL_REFRESH_URL,
            headers={"Authorization": f"Bearer {cached['token']}"},
            timeout=10,
        )
        resp.raise_for_status()
    except http_requests.RequestException:
        return jsonify({"error": "Failed to refresh token"}), 502

    data = resp.json()
    session["dl_token"] = {
        "token":          data["token"],
        "conversationId": data["conversationId"],
        "expires_at":     time.time() + data["expires_in"],
    }
    return jsonify({
        "token":          data["token"],
        "conversationId": data["conversationId"],
        "expires_in":     data["expires_in"],
    })

if __name__ == "__main__":
    print("API running at http://127.0.0.1:5050")
    app.run(debug=False, host="0.0.0.0", port=5050, threaded=True)
