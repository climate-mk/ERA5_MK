"""
Flask API backend for MK Climate Explorer.
Run:  source venv/bin/activate && python3 mk_api.py
Open: http://127.0.0.1:5050
"""

import os, glob, time, hashlib, json, threading, ipaddress, sqlite3, csv, io
import numpy as np
import pandas as pd
import requests as http_requests
from flask import Flask, jsonify, request, send_from_directory, session, Response
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

from chat_config import (
    DIRECT_LINE_SECRET, DL_GENERATE_URL, DL_REFRESH_URL,
    TOKEN_CACHE_BUFFER, TOKEN_LIMIT_MINUTE, TOKEN_LIMIT_HOUR,
    CHAT_ERROR_RATE_LIMIT, CHAT_ERROR_GENERIC, CHAT_ERROR_GLOBAL_LIMIT,
    CHAT_GLOBAL_HOURLY_LIMIT, CHAT_GLOBAL_DAILY_LIMIT,
)

_global_chat_counter = {"hour": -1, "hour_count": 0, "day": -1, "day_count": 0}

# Analytics export key — set ANALYTICS_EXPORT_KEY in .env (and as a GitHub secret)
_ANALYTICS_EXPORT_KEY = os.getenv("ANALYTICS_EXPORT_KEY", "")

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
PALETTE     = ["#e07b00","#9b4dca","#c9880a","#d0408a",
               "#20aab0","#b06830"]

# ── Variable style ─────────────────────────────────────────────────────────────

_VSTYLE = {
    "precipitation_sum": {
        "pos_rgb": (26, 95, 200), "neg_rgb": (160, 92, 32),
        "pos_label": "wetter ↑",  "neg_label": "drier ↓",
        "chg_unit": "mm",
        "cal_pos": (35, 100, 210), "cal_neg": (180, 105, 25),
    },
    "et0_evapotranspiration": {
        "pos_rgb": (26, 95, 200),  "neg_rgb": (160, 92, 32),
        "pos_label": "higher ET₀ ↑", "neg_label": "lower ET₀ ↓",
        "chg_unit": "mm",
        "cal_pos": (35, 100, 210), "cal_neg": (180, 105, 25),
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

# ── Regression computation (in-memory cached) ─────────────────────────────────

_REGRESSION_CACHE = {}

def compute_regression(loc, var, month, day, half_window, col, method):
    _key = (loc, var, month, day, half_window, col, method)
    if _key in _REGRESSION_CACHE:
        return _REGRESSION_CACHE[_key]
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
    _REGRESSION_CACHE[_key] = result
    return result

# ── Calendar computation (in-memory + filesystem cached) ──────────────────────

_CAL_CACHE = {}

def compute_calendar(loc, col, var, half_window, method):
    key = (loc, col, half_window, method)
    if key in _CAL_CACHE:
        return _CAL_CACHE[key]

    # FS cache: survives service restarts (data only changes once/day via cron)
    today_str   = _today_mk().date().isoformat()
    fs_filename = f"cal_{loc}_{col}_{half_window}_{method}_{today_str}.json"
    fs_path     = os.path.join(_CACHE_DIR, fs_filename)
    cached      = _fs_load(fs_path)
    if cached is not None:
        _CAL_CACHE[key] = cached
        return cached

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
    _fs_save(fs_path, result,
             glob_pattern=os.path.join(_CACHE_DIR, f"cal_{loc}_{col}_{half_window}_{method}_*.json"),
             anchor_date=today_str)
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

    # FS cache: survives restarts
    fs_path = os.path.join(_CACHE_DIR, f"annual_trend_{cache_key}.json")
    cached  = _fs_load(fs_path)
    if cached is not None:
        _ANNUAL_TREND_CACHE[cache_key] = cached
        return cached

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
        "month_num":     month,
        "day_num":       day,
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
    _fs_save(fs_path, result,
             glob_pattern=os.path.join(_CACHE_DIR, "annual_trend_*.json"),
             anchor_date=cache_key)
    return result

# ── Generic filesystem cache helpers ──────────────────────────────────────────

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")

def _fs_load(path):
    """Load a JSON cache file; return None on any error."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None

def _fs_save(path, data, glob_pattern=None, keep_days=3, anchor_date=None):
    """Write data as JSON; optionally prune old sibling files by date suffix."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)
        if glob_pattern and anchor_date:
            cutoff = (pd.Timestamp(anchor_date) - pd.Timedelta(days=keep_days)).date().isoformat()
            for p in glob.glob(glob_pattern):
                # Filenames end with _YYYY-MM-DD.json
                stem = os.path.basename(p)
                date_part = stem[-len("YYYY-MM-DD.json"):-len(".json")]
                if date_part < cutoff:
                    try: os.remove(p)
                    except Exception: pass
    except Exception:
        pass  # disk failure is non-fatal

# ── Today status ("Is it Hot in Macedonia Today?") ─────────────────────────────

_TODAY_CACHE     = {}
_TODAY_CACHE_DIR = _CACHE_DIR

_TODAY_CATEGORIES = [
    # (max_percentile_exclusive, key, name, hex, description_template)
    (10,  "freezing", "Freezing", "#3a5a8a", "Among the coldest {d}s in our 76-year record."),
    (20,  "cold",     "Cold",     "#6c8fb6", "Cooler than most {d}s we've measured."),
    (80,  "nope",     "Nope",     "#e7d9b8", "Right around what {d} usually feels like in Macedonia."),
    (95,  "hot",      "Hot",      "#c25a2c", "Among the hottest {d}s in our record."),
    (101, "hell",     "Hell",     "#962c1a", "Exceptional heat — top 5% of all {d}s since 1950."),
]

def _categorize_today(pct, dlabel):
    for cutoff, key, name, color, tpl in _TODAY_CATEGORIES:
        if pct < cutoff:
            return key, name, color, tpl.format(d=dlabel)
    last = _TODAY_CATEGORIES[-1]
    return last[1], last[2], last[3], last[4].format(d=dlabel)

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
    cat_key, name, color, desc = _categorize_today(pct, dlabel)

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
        "category_key": cat_key,
        "category":     name,
        "color":        color,
        "description":  desc,
        "n_samples":    int(len(samples)),
        "year_min":     int(data["year"].min()),
        "year_max":     int(data["year"].max()),
        "distribution": distribution,
        "cutoffs":      cutoffs,
        "day_label":    dlabel,
        "month_num":    month,
        "day_num":      day,
    }
    _TODAY_CACHE[cache_key] = result
    _save_today_to_disk(cache_key, result)   # persist so restarts don't re-fetch
    return result

# ── Chat analytics ────────────────────────────────────────────────────────────
#
# Privacy design:
#   • IPs are NEVER stored — not even hashed.  The IP is used only to derive a
#     2-letter ISO country code (via GeoLite2-Country.mmdb if present, or
#     ip-api.com as fallback), then immediately discarded.
#   • The country lookup result is cached in-memory (ip → country) so the
#     external fallback is called at most once per unique IP per process lifetime.
#   • Message text is stored as typed (capped at 2000 chars) — it is the chat
#     prompt the user intentionally sent to the bot.
#   • Conversation IDs are stored as-is for session grouping; they are opaque
#     tokens assigned by Direct Line and carry no personal information.
#   • The database file (chat_analytics.db) is excluded from git.
#   • There is no public-facing API endpoint exposing this data.

_ANALYTICS_DB  = os.path.join(os.path.dirname(__file__), "chat_analytics.db")
_GEO_DB_PATH   = os.path.join(os.path.dirname(__file__), "GeoLite2-Country.mmdb")
_COUNTRY_CACHE = {}          # ip → country code (in-memory, resets on restart)
_analytics_lock = threading.Lock()

def _ip_to_country(ip: str) -> str:
    """Return ISO 3166-1 alpha-2 country code, 'LO' for private/local, 'XX' for unknown."""
    try:
        addr = ipaddress.ip_address(ip)
        if addr.is_private or addr.is_loopback:
            return "LO"
    except ValueError:
        return "XX"
    # 1. Local GeoLite2-Country database (fast, offline, most accurate)
    #    Download from https://dev.maxmind.com/geoip/geolite2-free-geolocation-data
    #    and place GeoLite2-Country.mmdb in the app root directory.
    if os.path.exists(_GEO_DB_PATH):
        try:
            import maxminddb
            with maxminddb.open_database(_GEO_DB_PATH) as reader:
                rec = reader.get(ip)
                return (rec or {}).get("country", {}).get("iso_code") or "XX"
        except Exception:
            pass
    # 2. Fallback: ip-api.com (free, no key, ~45 req/min limit; cached per IP)
    try:
        resp = http_requests.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "countryCode"},
            timeout=3,
        )
        if resp.ok:
            return resp.json().get("countryCode") or "XX"
    except Exception:
        pass
    return "XX"

def _log_chat_event(ip: str, message: str, conv_id: str = "") -> None:
    """Write one user-prompt row. Never raises — logging must not break the API."""
    try:
        with _analytics_lock:
            if ip not in _COUNTRY_CACHE:
                _COUNTRY_CACHE[ip] = _ip_to_country(ip)
            country = _COUNTRY_CACHE[ip]
        with _analytics_lock:
            with sqlite3.connect(_ANALYTICS_DB) as con:
                con.execute(
                    "INSERT INTO chat_events(country, message, sess) VALUES(?, ?, ?)",
                    (country, message[:2000], conv_id),
                )
    except Exception as e:
        print(f"[analytics] log_chat_event failed: {e}")

def _init_analytics_db() -> None:
    try:
        with sqlite3.connect(_ANALYTICS_DB) as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS chat_events (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts      TEXT    NOT NULL
                                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    country TEXT    NOT NULL DEFAULT 'XX',
                    message TEXT    NOT NULL,
                    sess    TEXT    NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_ts      ON chat_events(ts);
                CREATE INDEX IF NOT EXISTS idx_country ON chat_events(country);
                CREATE INDEX IF NOT EXISTS idx_sess    ON chat_events(sess);
            """)
    except Exception as e:
        print(f"[analytics] DB init failed: {e}")

_init_analytics_db()

# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder="static", static_url_path="")

# Session secret derived from the Direct Line secret — stable across restarts,
# invalidated automatically if the secret is rotated (correct behaviour).
app.secret_key = hashlib.sha256(DIRECT_LINE_SECRET.encode()).digest() if DIRECT_LINE_SECRET else os.urandom(24)

limiter = Limiter(get_remote_address, app=app, default_limits=[])

@app.after_request
def set_cache_headers(response):
    """
    Prevent stale static assets after deploys.
    - HTML: no-store (always fetch fresh — tiny file, worth it)
    - JS / CSS / JSON: no-cache (revalidate via ETag; 304 if unchanged = free)
    - API JSON responses: already ephemeral, leave as-is
    """
    path = request.path
    if path == "/" or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-store"
    elif path.endswith((".js", ".css", ".json")):
        response.headers["Cache-Control"] = "no-cache"
    return response

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
        "chat_enabled":          bool(DIRECT_LINE_SECRET),
        "chat_error_rate_limit":   CHAT_ERROR_RATE_LIMIT,
        "chat_error_generic":      CHAT_ERROR_GENERIC,
        "chat_error_global_limit": CHAT_ERROR_GLOBAL_LIMIT,
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


# ── Season heatmap ─────────────────────────────────────────────────────────────

def _is_leap(y: int) -> bool:
    return (y % 4 == 0 and y % 100 != 0) or (y % 400 == 0)


def compute_season_heatmap():
    """
    For each completed (year, meteorological season) compute the mean of the
    national daily-maximum temperature (max across all ERA5 stations per day).
    Percentile-rank each season within its own season type across all years.

    Seasons:
      Winter YYYY  = Dec(YYYY-1) + Jan + Feb(YYYY)   ends last day of Feb
      Spring YYYY  = Mar + Apr + May                  ends May 31
      Summer YYYY  = Jun + Jul + Aug                  ends Aug 31
      Autumn YYYY  = Sep + Oct + Nov                  ends Nov 30

    A season is included only when its end date ≤ last ERA5 date in the dataset.
    100 % ERA5-Land — no Open-Meteo mixing.
    """
    BASELINE_START, BASELINE_END = 1950, 1980
    cache_key = "season_heatmap_baseline_1950_1980"
    if cache_key in _TODAY_CACHE:
        return _TODAY_CACHE[cache_key]

    last_era5  = data["date"].max()
    fs_path    = os.path.join(_CACHE_DIR, f"season_heatmap_{last_era5.date().isoformat()}.json")
    fs_cached  = _fs_load(fs_path)
    if fs_cached is not None:
        _TODAY_CACHE[cache_key] = fs_cached
        return fs_cached

    # National daily max (max across all stations) — compute once
    daily_nat = (
        data.groupby("date")["temperature_max"]
        .max()
        .reset_index(name="tmax")
    )
    daily_nat["year"]  = daily_nat["date"].dt.year
    daily_nat["month"] = daily_nat["date"].dt.month

    year_min = int(daily_nat["year"].min())
    year_max = int(daily_nat["year"].max())

    # Season definitions: (label, x-index, start_month, end_month, end_day_fn)
    SEASONS = [
        ("Winter", 0, None, 2,  lambda y: pd.Timestamp(y, 2, 29 if _is_leap(y) else 28)),
        ("Spring", 1, 3,    5,  lambda y: pd.Timestamp(y, 5, 31)),
        ("Summer", 2, 6,    8,  lambda y: pd.Timestamp(y, 8, 31)),
        ("Autumn", 3, 9,    11, lambda y: pd.Timestamp(y, 11, 30)),
    ]

    records = []
    for yr in range(year_min, year_max + 1):
        for s_name, s_xi, s_start, s_end_m, end_fn in SEASONS:
            season_end = end_fn(yr)
            if season_end > last_era5:
                continue  # not yet fully present in ERA5

            if s_name == "Winter":
                chunk = daily_nat[
                    ((daily_nat["year"] == yr - 1) & (daily_nat["month"] == 12)) |
                    ((daily_nat["year"] == yr)     & (daily_nat["month"].isin([1, 2])))
                ]
            else:
                chunk = daily_nat[
                    (daily_nat["year"] == yr) &
                    (daily_nat["month"] >= s_start) &
                    (daily_nat["month"] <= s_end_m)
                ]

            if len(chunk) < 30:  # skip seasons with too many missing days
                continue

            records.append({
                "year":   yr,
                "xi":     s_xi,       # x position in heatmap
                "season": s_name,
                "avg":    round(float(chunk["tmax"].mean()), 2),
                "n_days": len(chunk),
            })

    if not records:
        result = {"available": False}
        _TODAY_CACHE[cache_key] = result
        return result

    rec_df = pd.DataFrame(records)

    def _pct_cat(pct):
        if   pct < 10: return "cold"
        elif pct < 20: return "cool"
        elif pct < 80: return "normal"
        elif pct < 95: return "hot"
        else:          return "extreme"

    def _pct_color(pct):
        return {"cold":"#3a5a8a","cool":"#6c8fb6","normal":"#e7d9b8",
                "hot":"#c25a2c","extreme":"#962c1a"}[_pct_cat(pct)]

    out = []
    for xi in range(4):
        sub   = rec_df[rec_df["xi"] == xi].copy()
        if sub.empty:
            continue
        all_avgs    = sub["avg"].values
        total       = len(all_avgs)
        # Baseline: 1950–1980 only — fixed reference period to show warming trend
        baseline_sub  = sub[(sub["year"] >= BASELINE_START) & (sub["year"] <= BASELINE_END)]
        baseline_avgs = baseline_sub["avg"].values
        # Descending rank: 1 = hottest (ranked against all years)
        sorted_desc = np.sort(all_avgs)[::-1]

        for _, row in sub.iterrows():
            if len(baseline_avgs) > 0:
                pct = float((baseline_avgs < row["avg"]).mean() * 100)
            else:
                # Fallback if no baseline data for this season
                pct = float((all_avgs < row["avg"]).mean() * 100)
            rank = int(np.searchsorted(-sorted_desc, -row["avg"])) + 1
            cat  = _pct_cat(pct)
            out.append({
                "x":          int(row["xi"]),
                "y":          int(row["year"]),
                "avg":        row["avg"],
                "percentile": round(pct, 1),
                "cat":        cat,
                "rank":       rank,
                "total":      total,
                "color":      _pct_color(pct),
                "season":     row["season"],
                "n_days":     int(row["n_days"]),
            })

    result = {
        "available":      True,
        "data":           out,
        "year_min":       year_min,
        "year_max":       year_max,
        "seasons":        ["Winter", "Spring", "Summer", "Autumn"],
        "era5_last":      last_era5.date().isoformat(),
        "baseline":       f"{BASELINE_START}–{BASELINE_END}",
        "baseline_start": BASELINE_START,
        "baseline_end":   BASELINE_END,
    }
    _TODAY_CACHE[cache_key] = result
    _fs_save(fs_path, result)
    return result


@app.route("/api/season_heatmap")
def api_season_heatmap():
    return jsonify(compute_season_heatmap())


def compute_spei_heatmap():
    """
    Seasonal SPEI (Standardized Precipitation-Evapotranspiration Index).

    Method:
      1. National daily water balance D = mean(P) − mean(ET₀) across all stations
      2. Seasonal D sum (mm) for each completed season
      3. Fit a 3-parameter log-logistic distribution to the 1950–1980 baseline
         values for each season type (shift γ so all values are positive, then
         fit scipy.stats.fisk with floc=0)
      4. Transform via the fitted CDF → standard normal (SPEI score)
      5. Colour by WMO drought thresholds: SPEI < −1.5 extreme drought,
         −1.5–−1.0 severe, −1.0–1.0 normal, 1.0–1.5 wet, > 1.5 extremely wet

    Positive SPEI = wetter than 1950–1980; negative = drier.
    """
    BASELINE_START, BASELINE_END = 1950, 1980
    cache_key = "spei_heatmap_v1"
    if cache_key in _TODAY_CACHE:
        return _TODAY_CACHE[cache_key]

    last_era5  = data["date"].max()
    fs_path    = os.path.join(_CACHE_DIR, f"spei_heatmap_{last_era5.date().isoformat()}.json")
    fs_cached  = _fs_load(fs_path)
    if fs_cached is not None:
        _TODAY_CACHE[cache_key] = fs_cached
        return fs_cached

    # National daily water balance: mean P − mean ET0 across all stations
    daily_p   = data.groupby("date")["precipitation_sum"].mean()
    daily_et0 = data.groupby("date")["et0_evapotranspiration"].mean()
    daily_bal = (daily_p - daily_et0).reset_index()
    daily_bal.columns = ["date", "balance"]
    daily_bal["year"]  = daily_bal["date"].dt.year
    daily_bal["month"] = daily_bal["date"].dt.month

    year_min = int(daily_bal["year"].min())
    year_max = int(daily_bal["year"].max())

    SEASONS = [
        ("Winter", 0, None, 2,  lambda y: pd.Timestamp(y, 2, 29 if _is_leap(y) else 28)),
        ("Spring", 1, 3,    5,  lambda y: pd.Timestamp(y, 5, 31)),
        ("Summer", 2, 6,    8,  lambda y: pd.Timestamp(y, 8, 31)),
        ("Autumn", 3, 9,    11, lambda y: pd.Timestamp(y, 11, 30)),
    ]

    records = []
    for yr in range(year_min, year_max + 1):
        for s_name, s_xi, s_start, s_end_m, end_fn in SEASONS:
            season_end = end_fn(yr)
            if season_end > last_era5:
                continue

            if s_name == "Winter":
                chunk = daily_bal[
                    ((daily_bal["year"] == yr - 1) & (daily_bal["month"] == 12)) |
                    ((daily_bal["year"] == yr)     & (daily_bal["month"].isin([1, 2])))
                ]
            else:
                chunk = daily_bal[
                    (daily_bal["year"] == yr) &
                    (daily_bal["month"] >= s_start) &
                    (daily_bal["month"] <= s_end_m)
                ]

            if len(chunk) < 30:
                continue

            records.append({
                "year":    yr,
                "xi":      s_xi,
                "season":  s_name,
                "balance": round(float(chunk["balance"].sum()), 1),  # mm P-ET0
                "n_days":  len(chunk),
            })

    if not records:
        result = {"available": False}
        _TODAY_CACHE[cache_key] = result
        return result

    rec_df = pd.DataFrame(records)

    def _spei_cat(spei):
        if   spei < -1.5: return "extreme_dry"
        elif spei < -1.0: return "dry"
        elif spei <  1.0: return "normal"
        elif spei <  1.5: return "wet"
        else:             return "extreme_wet"

    def _spei_color(spei):
        return {
            "extreme_dry": "#8b3a0f",
            "dry":         "#c2713a",
            "normal":      "#e7e0d0",
            "wet":         "#4a80b0",
            "extreme_wet": "#1e4d78",
        }[_spei_cat(spei)]

    out = []
    for xi in range(4):
        sub = rec_df[rec_df["xi"] == xi].copy()
        if sub.empty:
            continue

        all_vals     = sub["balance"].values
        n_total      = len(all_vals)
        baseline_sub = sub[(sub["year"] >= BASELINE_START) & (sub["year"] <= BASELINE_END)]
        b_vals       = baseline_sub["balance"].values

        if len(b_vals) < 5:
            b_vals = all_vals  # fallback to all years if baseline too short

        # 3-parameter log-logistic: shift so all values positive, fit fisk(floc=0)
        gamma_shift = float(b_vals.min()) - 1e-6
        b_shifted   = b_vals - gamma_shift

        try:
            c_par, _, scale_par = stats.fisk.fit(b_shifted, floc=0)
        except Exception:
            c_par, scale_par = 1.0, float(b_shifted.mean())

        # Rank (1 = driest) against all years
        sorted_asc = np.sort(all_vals)

        for _, row in sub.iterrows():
            shifted_val = float(row["balance"]) - gamma_shift
            shifted_val = max(shifted_val, 1e-9)  # guard against ≤0 after shift
            p = float(stats.fisk.cdf(shifted_val, c_par, loc=0, scale=scale_par))
            p = float(np.clip(p, 1e-6, 1 - 1e-6))
            spei_val = float(stats.norm.ppf(p))
            spei_val = float(np.clip(spei_val, -3.0, 3.0))

            rank = int(np.searchsorted(sorted_asc, row["balance"])) + 1
            cat  = _spei_cat(spei_val)
            out.append({
                "x":       int(row["xi"]),
                "y":       int(row["year"]),
                "spei":    round(spei_val, 2),
                "balance": row["balance"],
                "cat":     cat,
                "rank":    rank,
                "total":   n_total,
                "color":   _spei_color(spei_val),
                "season":  row["season"],
                "n_days":  int(row["n_days"]),
            })

    result = {
        "available":      True,
        "data":           out,
        "year_min":       year_min,
        "year_max":       year_max,
        "seasons":        ["Winter", "Spring", "Summer", "Autumn"],
        "era5_last":      last_era5.date().isoformat(),
        "baseline":       f"{BASELINE_START}–{BASELINE_END}",
        "baseline_start": BASELINE_START,
        "baseline_end":   BASELINE_END,
    }
    _TODAY_CACHE[cache_key] = result
    _fs_save(fs_path, result)
    return result


@app.route("/api/spei_heatmap")
def api_spei_heatmap():
    return jsonify(compute_spei_heatmap())


def compute_spei_station_seasonal():
    """
    Per-station seasonal SPEI.
    For each station × meteorological season:
      - sum daily (P − ET₀) over the season
      - fit 3-parameter log-logistic to 1950–1980 baseline values
      - transform all years → SPEI score
      - Theil-Sen slope + Mann-Kendall significance on annual series
    Also computes an "Annual" series = mean of the 4 seasonal SPEI values per year.
    Result is cached to disk keyed by era5_last date.
    """
    BASELINE_START, BASELINE_END = 1950, 1980
    cache_key = "spei_station_seasonal_v2"   # bumped: adds monthly SPEI-30
    if cache_key in _TODAY_CACHE:
        return _TODAY_CACHE[cache_key]

    last_era5 = data["date"].max()
    fs_path   = os.path.join(_CACHE_DIR, f"spei_station_seasonal_v2_{last_era5.date().isoformat()}.json")
    fs_cached = _fs_load(fs_path)
    if fs_cached is not None:
        _TODAY_CACHE[cache_key] = fs_cached
        return fs_cached

    year_min = int(data["year"].min())
    year_max = int(data["year"].max())

    SEASONS = [
        ("Winter", None, 2,  lambda y: pd.Timestamp(y, 2, 29 if _is_leap(y) else 28)),
        ("Spring", 3,    5,  lambda y: pd.Timestamp(y, 5, 31)),
        ("Summer", 6,    8,  lambda y: pd.Timestamp(y, 8, 31)),
        ("Autumn", 9,    11, lambda y: pd.Timestamp(y, 11, 30)),
    ]

    stations = sorted(data["location"].unique())
    result_stations = {}

    for station in stations:
        sd = data[data["location"] == station].copy()
        sd["balance"] = sd["precipitation_sum"] - sd["et0_evapotranspiration"]

        season_series = {}

        for s_name, s_start, s_end_m, end_fn in SEASONS:
            records = []
            for yr in range(year_min, year_max + 1):
                if end_fn(yr) > last_era5:
                    continue

                if s_name == "Winter":
                    chunk = sd[
                        ((sd["year"] == yr - 1) & (sd["month"] == 12)) |
                        ((sd["year"] == yr)     & (sd["month"].isin([1, 2])))
                    ]
                else:
                    chunk = sd[
                        (sd["year"] == yr) &
                        (sd["month"] >= s_start) &
                        (sd["month"] <= s_end_m)
                    ]

                if len(chunk) < 30:
                    continue

                records.append({"year": yr, "balance": float(chunk["balance"].sum())})

            if len(records) < 10:
                continue

            rec_df       = pd.DataFrame(records)
            baseline_df  = rec_df[(rec_df["year"] >= BASELINE_START) & (rec_df["year"] <= BASELINE_END)]
            b_vals       = baseline_df["balance"].values if len(baseline_df) >= 5 else rec_df["balance"].values

            gamma_shift = float(b_vals.min()) - 1e-6
            try:
                c_par, _, scale_par = stats.fisk.fit(b_vals - gamma_shift, floc=0)
            except Exception:
                c_par, scale_par = 1.0, max(float((b_vals - gamma_shift).mean()), 1e-6)

            spei_vals = []
            for bal in rec_df["balance"].values:
                sv = max(float(bal) - gamma_shift, 1e-9)
                p  = float(np.clip(stats.fisk.cdf(sv, c_par, loc=0, scale=scale_par), 1e-6, 1 - 1e-6))
                spei_vals.append(round(float(np.clip(stats.norm.ppf(p), -3.0, 3.0)), 2))

            years = [int(y) for y in rec_df["year"].tolist()]

            # Theil-Sen + Mann-Kendall
            trend = {}
            if len(spei_vals) >= 10:
                try:
                    ts       = theilslopes(spei_vals, years)
                    mk_res   = mk_test.original_test(np.array(spei_vals))
                    trend    = {
                        "slope_per_decade": round(float(ts.slope) * 10, 3),
                        "p_value":          round(float(mk_res.p), 3),
                        "mk_trend":         mk_res.trend,
                        "intercept":        round(float(ts.intercept), 3),
                    }
                except Exception:
                    pass

            season_series[s_name] = {"years": years, "spei": spei_vals, "trend": trend}

        # Annual = mean of the available seasonal SPEI values per year
        by_year = {}
        for s in season_series.values():
            for yr, sp in zip(s["years"], s["spei"]):
                by_year.setdefault(yr, []).append(sp)

        ann_years = sorted(yr for yr, vals in by_year.items() if len(vals) >= 2)
        ann_spei  = [round(float(np.mean(by_year[yr])), 2) for yr in ann_years]

        ann_trend = {}
        if len(ann_spei) >= 10:
            try:
                ts     = theilslopes(ann_spei, ann_years)
                mk_res = mk_test.original_test(np.array(ann_spei))
                ann_trend = {
                    "slope_per_decade": round(float(ts.slope) * 10, 3),
                    "p_value":          round(float(mk_res.p), 3),
                    "mk_trend":         mk_res.trend,
                    "intercept":        round(float(ts.intercept), 3),
                }
            except Exception:
                pass

        season_series["Annual"] = {"years": ann_years, "spei": ann_spei, "trend": ann_trend}

        # ── SPEI-30: monthly (calendar month water balance) ────────────────────
        MONTH_NAMES_SHORT = ["Jan","Feb","Mar","Apr","May","Jun",
                             "Jul","Aug","Sep","Oct","Nov","Dec"]
        for m_idx, m_name in enumerate(MONTH_NAMES_SHORT, start=1):
            records = []
            for yr in range(year_min, year_max + 1):
                # last day of this month
                if m_idx == 12:
                    m_end = pd.Timestamp(yr, 12, 31)
                else:
                    m_end = pd.Timestamp(yr, m_idx + 1, 1) - pd.Timedelta(days=1)
                if m_end > last_era5:
                    continue
                chunk = sd[(sd["year"] == yr) & (sd["month"] == m_idx)]
                if len(chunk) < 20:   # allow slightly short months (Feb)
                    continue
                records.append({"year": yr, "balance": float(chunk["balance"].sum())})

            if len(records) < 10:
                continue

            rec_df      = pd.DataFrame(records)
            baseline_df = rec_df[(rec_df["year"] >= BASELINE_START) & (rec_df["year"] <= BASELINE_END)]
            b_vals      = baseline_df["balance"].values if len(baseline_df) >= 5 else rec_df["balance"].values

            gamma_shift = float(b_vals.min()) - 1e-6
            try:
                c_par, _, scale_par = stats.fisk.fit(b_vals - gamma_shift, floc=0)
            except Exception:
                c_par, scale_par = 1.0, max(float((b_vals - gamma_shift).mean()), 1e-6)

            spei_vals = []
            for bal in rec_df["balance"].values:
                sv = max(float(bal) - gamma_shift, 1e-9)
                p  = float(np.clip(stats.fisk.cdf(sv, c_par, loc=0, scale=scale_par), 1e-6, 1 - 1e-6))
                spei_vals.append(round(float(np.clip(stats.norm.ppf(p), -3.0, 3.0)), 2))

            years = [int(y) for y in rec_df["year"].tolist()]

            trend = {}
            if len(spei_vals) >= 10:
                try:
                    ts     = theilslopes(spei_vals, years)
                    mk_res = mk_test.original_test(np.array(spei_vals))
                    trend  = {
                        "slope_per_decade": round(float(ts.slope) * 10, 3),
                        "p_value":          round(float(mk_res.p), 3),
                        "mk_trend":         mk_res.trend,
                        "intercept":        round(float(ts.intercept), 3),
                    }
                except Exception:
                    pass

            season_series[m_name] = {"years": years, "spei": spei_vals, "trend": trend}

        result_stations[station] = season_series

    result = {
        "available":  True,
        "stations":   result_stations,
        "era5_last":  last_era5.date().isoformat(),
        "baseline":   f"{BASELINE_START}–{BASELINE_END}",
        "year_min":   year_min,
        "year_max":   year_max,
    }
    _TODAY_CACHE[cache_key] = result
    _fs_save(fs_path, result)
    return result


@app.route("/api/spei_station_seasonal")
def api_spei_station_seasonal():
    return jsonify(compute_spei_station_seasonal())


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
    if cached and time.time() < cached["expires_at"] - TOKEN_CACHE_BUFFER:
        return jsonify({
            "token":          cached["token"],
            "conversationId": cached["conversationId"],
            "expires_in":     int(cached["expires_at"] - time.time()),
        })

    # Global hourly / daily cap (new sessions only — cache hits bypass this)
    if CHAT_GLOBAL_HOURLY_LIMIT > 0 or CHAT_GLOBAL_DAILY_LIMIT > 0:
        current_hour = int(time.time() // 3600)
        current_day  = int(time.time() // 86400)
        if _global_chat_counter["hour"] != current_hour:
            _global_chat_counter.update({"hour": current_hour, "hour_count": 0})
        if _global_chat_counter["day"] != current_day:
            _global_chat_counter.update({"day": current_day, "day_count": 0})
        if CHAT_GLOBAL_HOURLY_LIMIT > 0 and _global_chat_counter["hour_count"] >= CHAT_GLOBAL_HOURLY_LIMIT:
            return jsonify({"error": "chat_limit_reached"}), 429
        if CHAT_GLOBAL_DAILY_LIMIT > 0 and _global_chat_counter["day_count"] >= CHAT_GLOBAL_DAILY_LIMIT:
            return jsonify({"error": "chat_limit_reached"}), 429
        _global_chat_counter["hour_count"] += 1
        _global_chat_counter["day_count"] += 1

    try:
        resp = http_requests.post(
            DL_GENERATE_URL,
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
            DL_REFRESH_URL,
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

# ── Analytics route ───────────────────────────────────────────────────────────

@app.route("/api/analytics/chat", methods=["POST"])
@limiter.limit("60 per minute")
def api_analytics_chat():
    """
    Internal endpoint — receives one chat event from the browser.
    Body JSON: { direction: "user"|"bot", message: "...", conv_id: "..." }
    The server resolves the real IP, looks up the country code, then discards
    the IP immediately.  Only the country code, direction, message, and an
    opaque conversation ID are written to SQLite.
    """
    body = request.get_json(silent=True) or {}
    direction = body.get("direction", "")
    message   = body.get("message", "")
    conv_id   = body.get("conv_id", "")
    if direction != "user":
        return jsonify({"ok": True})   # silently ignore bot messages
    if not message:
        return jsonify({"ok": False}), 400
    # X-Real-IP is set by Nginx to the true client IP (after Cloudflare processing).
    # Fall back to get_remote_address() when running locally without a proxy.
    ip = (request.headers.get("X-Real-IP") or
          request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or
          get_remote_address())
    threading.Thread(
        target=_log_chat_event,
        args=(ip, message, conv_id),
        daemon=True,
    ).start()
    return jsonify({"ok": True})

@app.route("/api/analytics/export")
def api_analytics_export():
    """
    Private CSV export of the full chat_events table.
    Access: GET /api/analytics/export?key=<ANALYTICS_EXPORT_KEY>
    The key is set via the ANALYTICS_EXPORT_KEY environment variable (.env on
    the server, GitHub repository secret for reference).  Returns 403 if the
    key is missing or wrong.  Safe to share the URL with colleagues — knowing
    the key is the only requirement, no server access needed.
    """
    key = request.args.get("key", "")
    if not _ANALYTICS_EXPORT_KEY or key != _ANALYTICS_EXPORT_KEY:
        return Response("Forbidden", status=403)

    def generate():
        with _analytics_lock:
            with sqlite3.connect(_ANALYTICS_DB) as con:
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    "SELECT ts, country, message, sess FROM chat_events ORDER BY ts"
                ).fetchall()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["ts", "country", "message", "conv_id"])
        for row in rows:
            writer.writerow(list(row))
        yield buf.getvalue()

    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=chat_analytics.csv"},
    )

# ── Background pre-warm ────────────────────────────────────────────────────────
# After every restart, silently pre-compute the most expensive entries so the
# first real visitor doesn't wait.  Runs in a daemon thread; errors are ignored.

def _prewarm():
    time.sleep(3)  # let gunicorn/Flask finish binding before we start heavy work
    try: compute_annual_trend()
    except Exception: pass
    try: compute_today_status()
    except Exception: pass
    # Calendar for all locations with default params (temperature_max, w=7, theilsen)
    for _loc in list(LOC_COORDS.keys()):
        try: compute_calendar(_loc, "temperature_max", "temperature_max", 7, "theilsen")
        except Exception: pass

threading.Thread(target=_prewarm, daemon=True).start()

if __name__ == "__main__":
    print("API running at http://127.0.0.1:5050")
    app.run(debug=False, host="0.0.0.0", port=5050, threaded=True)
