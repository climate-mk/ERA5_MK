"""
Flask API backend for MK Climate Explorer.
Run:  source venv/bin/activate && python3 mk_api.py
Open: http://127.0.0.1:5050
"""

import os, glob, time, hashlib, json, threading, ipaddress, sqlite3, csv, io, datetime, shutil
import sys, traceback
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import pandas as pd
import requests as http_requests

from climate_news import compute_climate_news, build_rss_xml
from flask import Flask, jsonify, request, send_from_directory, send_file, session, Response
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from scipy import stats
from scipy.stats import theilslopes, gaussian_kde
import pymannkendall as mk_test
import statsmodels.api as sm
import warnings
warnings.filterwarnings("ignore")
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

from config import CONFIG

# ── Location coordinates (for map endpoint) — derived from CONFIG, no duplication ──
LOC_COORDS = {s["name"]: {"lat": s["lat"], "lon": s["lon"]}
              for s in sorted(CONFIG["stations"], key=lambda s: s["name"])}

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

# Today-status refresh key — set TODAY_REFRESH_KEY in .env (and as a GitHub secret).
# Lets cron re-pull today's live forecast a few times a day (see /api/today_status/refresh).
_TODAY_REFRESH_KEY = os.getenv("TODAY_REFRESH_KEY", "")

# ── Load data ──────────────────────────────────────────────────────────────────

DATA_DIR = os.path.join("data", CONFIG["code"])
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

_CSV_MAX_DATE    = data["date"].max().date()
_RECORD_YEARS    = int(data["year"].max() - data["year"].min() + 1)
_DATA_START_YEAR = int(data["year"].min())

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

# ── Wildfire data (for the /fires page) ────────────────────────────────────────
# Loaded lazily & independently from the climate DataFrame. Fire CSVs live under
# data/<code>/fires/ (one per sensor), so the climate glob above never sees them.
FIRES_CFG      = CONFIG.get("fires") or {}
_FIRES_DIR     = os.path.join(DATA_DIR, "fires")
GFW_API_KEY    = os.getenv("GFW_API_KEY", "").strip()
_FIRES_DF      = None   # cached concat of all sensor CSVs (or empty frame)

def _fires_any_enabled():
    return any(_feature_enabled(f) for f in
               ("fires_map", "fires_year_chart", "fires_danger",
                "fires_satellite", "fires_settlement", "fires_burnt_area",
                "fires_protected_areas"))

def _load_fires():
    """Concat all per-sensor fire CSVs into one DataFrame (cached).
    Returns an empty DataFrame with the expected columns if none exist yet."""
    global _FIRES_DF
    if _FIRES_DF is not None:
        return _FIRES_DF
    cols = ["sensor", "acq_date", "acq_time", "latitude", "longitude",
            "confidence", "frp", "daynight"]
    frames = []
    for f in sorted(glob.glob(os.path.join(_FIRES_DIR, "*.csv"))):
        try:
            frames.append(pd.read_csv(f))
        except Exception:
            pass
    if frames:
        df = pd.concat(frames, ignore_index=True)
        df["acq_date"] = pd.to_datetime(df["acq_date"], errors="coerce")
        df = df.dropna(subset=["acq_date", "latitude", "longitude"])
        df["year"] = df["acq_date"].dt.year
        # Combined UTC acquisition datetime (acq_time is HHMM UTC, or HH:MM:SS from
        # the Sentinel-3 WFS) — used to report true data freshness.
        df["acq_dt"] = _combine_acq_datetime(df)
    else:
        df = pd.DataFrame(columns=cols + ["year", "acq_dt"])
    _FIRES_DF = df
    return df

def _combine_acq_datetime(df):
    """Build a UTC acquisition timestamp from acq_date + acq_time.
    Handles both FIRMS 'HHMM' (e.g. 1204) and 'HH:MM:SS' (Sentinel-3 WFS)."""
    def to_minutes(v):
        s = str(v).strip()
        if not s or s.lower() == "nan":
            return 0
        if ":" in s:                       # HH:MM:SS
            h, m = s.split(":")[:2]
            return int(h) * 60 + int(m)
        s = s.split(".")[0].zfill(4)       # HHMM
        return int(s[:-2]) * 60 + int(s[-2:])
    mins = df["acq_time"].map(to_minutes)
    return (df["acq_date"] + pd.to_timedelta(mins, unit="m")).dt.tz_localize("UTC")

def _fires_latest_detection():
    """ISO-UTC timestamp of the newest detection in the data, or None."""
    df = _load_fires()
    if df.empty or "acq_dt" not in df or df["acq_dt"].isna().all():
        return None
    return df["acq_dt"].max().isoformat()

def _fires_sensor_start_dates():
    """Earliest acq_date actually present per sensor (YYYY-MM-DD), from the real
    collected data — not the sensor's theoretical launch date — so the frontend
    date pickers never let a user pick a date we can't actually query."""
    df = _load_fires()
    if df.empty:
        return {}
    return {sensor: d.date().isoformat()
            for sensor, d in df.groupby("sensor")["acq_date"].min().items()}

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
    today_str   = _today_local().date().isoformat()
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

def _today_local():
    """Current date in the country's local timezone (from CONFIG).
    The server runs UTC; without this, dates drift during the late-evening UTC
    window causing a mismatch between Open-Meteo's timezone-aware forecast and
    the historical distribution month/day lookup."""
    return pd.Timestamp.now(tz=CONFIG["timezone"]).normalize().tz_localize(None)

# ── Annual trend (cached) ──────────────────────────────────────────────────────

_ANNUAL_TREND_CACHE = {}

def compute_annual_trend(target_date=None, loc=None):
    if target_date is None:
        target_date = _today_local().date()

    month, day = target_date.month, target_date.day
    # Keyed by day-of-year (MM-DD) — the trend window is identical for any
    # year's May 28, so we don't re-compute when the year changes.
    cache_loc = loc or "national"
    cache_key = f"{month:02d}-{day:02d}|{cache_loc}"

    if cache_key in _ANNUAL_TREND_CACHE:
        return _ANNUAL_TREND_CACHE[cache_key]

    # FS cache: survives restarts; day-of-year files are permanent (no cleanup needed)
    fs_path = os.path.join(_CACHE_DIR, f"annual_trend_v12_{month:02d}-{day:02d}_{cache_loc}.json")
    cached  = _fs_load(fs_path)
    if cached is not None:
        _ANNUAL_TREND_CACHE[cache_key] = cached
        return cached

    dlabel = f"{MONTH_NAMES[month - 1]} {day}"

    # National daily MEAN temperature_max across all stations, ±30-day window
    # (or a single station's own daily max when `loc` is given — no cross-station
    # averaging needed since there's only one station's series).
    # Mean (not max) gives equal weight to all stations, removes single-station spikes.
    # Annual value = 90th percentile of those ~61 daily means per year.
    # Excludes years where fewer than 50 days are available in the window
    # (guards against the current year being incomplete at the window edges).
    WINDOW_HALF = 30   # ±days around today's date

    loc_data = data[data["location"] == loc] if loc else data
    window = window_filter(loc_data, month, day, WINDOW_HALF)
    daily_mean = (
        window.groupby(["_window_year", "date"])["temperature_max"]
        .mean()
        .reset_index(name="tmax")
    )
    annual_raw = (
        daily_mean.groupby("_window_year")["tmax"]
        .apply(lambda x: np.percentile(x.dropna(), 90) if len(x.dropna()) >= 50 else np.nan)
        .dropna()
    )
    annual = annual_raw

    # ── Configurable start year ───────────────────────────────────────────────
    annual = annual[annual.index >= CONFIG["trend_start_year"]]
    x_arr  = annual.index.to_numpy(float)
    y_arr  = annual.values

    last_yr  = int(x_arr.max())
    first_yr = int(x_arr.min())

    # ── Theil-Sen + Mann-Kendall (TFPW) ──────────────────────────────────────
    # Robust non-parametric slope for continuous temperature data.
    # 99% CI on the slope via Theil-Sen's Kendall confidence interval.
    res   = theilslopes(y_arr, x_arr, 0.95)
    slope = res.slope
    x_med, y_med = float(np.median(x_arr)), float(np.median(y_arr))
    ic    = y_med - slope          * x_med
    ic_hi = y_med - res.high_slope * x_med
    ic_lo = y_med - res.low_slope  * x_med
    mk_r  = mk_test.yue_wang_modification_test(y_arr)

    # Trend line + CI band over observed period
    x_hist = np.linspace(x_arr.min(), x_arr.max(), 300)
    y_hist = slope          * x_hist + ic
    u_hist = res.high_slope * x_hist + ic_hi
    l_hist = res.low_slope  * x_hist + ic_lo

    # Projection last_yr → projection_end_year
    x_fc = np.linspace(last_yr, CONFIG["projection_end_year"], 200)
    y_fc = slope          * x_fc + ic
    u_fc = res.high_slope * x_fc + ic_hi
    l_fc = res.low_slope  * x_fc + ic_lo

    scatter = [{"x": int(yr), "y": round(float(v), 2)} for yr, v in zip(x_arr, y_arr)]

    result = {
        "scatter":        scatter,
        "year_min":       first_yr,
        "year_max":       last_yr,
        "day_label":      dlabel,
        "month_num":      month,
        "day_num":        day,
        "hist_line":      {"x": x_hist.tolist(),
                           "y":     [round(v, 3) for v in y_hist],
                           "upper": [round(v, 3) for v in u_hist],
                           "lower": [round(v, 3) for v in l_hist]},
        "projection_line":{"x": x_fc.tolist(),
                           "y":     [round(v, 3) for v in y_fc],
                           "upper": [round(v, 3) for v in u_fc],
                           "lower": [round(v, 3) for v in l_fc]},
        "stats": {
            "trend10": round(float(slope * 10), 3),
            "p_val":   round(float(mk_r.p), 5),
            "tau":     round(float(mk_r.Tau), 3),
            "n_years": int(len(x_arr)),
        },
        "loc": loc,
    }
    _ANNUAL_TREND_CACHE[cache_key] = result
    _fs_save(fs_path, result)  # no cleanup — day-of-year files are permanent
    return result

# ── Generic filesystem cache helpers ──────────────────────────────────────────

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache", CONFIG["code"])

def _fs_load(path):
    """Load a JSON cache file; return None on any error."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _fs_save(path, data, glob_pattern=None, keep_days=3, anchor_date=None):
    """Write data as JSON; optionally prune old sibling files by date suffix."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
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

def _fs_load_bytes(path, max_age_s=None):
    """Load raw bytes from a cache file; None on any error or if older than
    max_age_s (mtime-based TTL). Binary sibling of _fs_load, for cached tiles."""
    try:
        if max_age_s is not None and (time.time() - os.path.getmtime(path)) > max_age_s:
            return None
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None

def _fs_save_bytes(path, data):
    """Write raw bytes to a cache file (creating dirs). Non-fatal on failure."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    except Exception:
        pass

# ── Today status ("Is it Hot in Macedonia Today?") ─────────────────────────────

_TODAY_CACHE     = {}
_TODAY_CACHE_DIR = _CACHE_DIR

# Raw {station: temp} fetches, keyed by date — shared across all locations so
# switching the location filter for the same date doesn't re-hit Open-Meteo for
# all 20 stations again (only the final per-location result is cached in
# _TODAY_CACHE / on disk; this caches the underlying network call itself).
_TODAY_RAW_CACHE = {}

# Thresholds and colours only — text loaded from locale at runtime.
_TODAY_CATEGORIES = [
    # (max_percentile_exclusive, key, colour)
    (10,  "freezing", "#3a5a8a"),
    (20,  "cold",     "#6c8fb6"),
    (80,  "nope",     "#e7d9b8"),
    (95,  "hot",      "#c25a2c"),
    (101, "hell",     "#962c1a"),
]

def _load_en_locale() -> dict:
    path = os.path.join(os.path.dirname(__file__), "static", "locales", "en_default.json")
    try:
        with open(path, encoding="utf-8") as _f:
            return json.load(_f)
    except Exception:
        return {}

_EN_LOCALE = _load_en_locale()

def _categorize_today(pct, dlabel, country=None):
    """Return (key, name, color, description) for a given percentile.
    Text comes from en_default.json so no country name or year is hardcoded here.
    Variables interpolated: {d} day-label, {country}, {record_years}, {data_start_year}.
    `country` defaults to CONFIG["name"] (national); pass a station's display name
    when describing a single location instead of the whole country.
    """
    cats   = _EN_LOCALE.get("categories", {})
    interp = dict(d=dlabel, country=country or CONFIG["name"],
                  record_years=_RECORD_YEARS, data_start_year=_DATA_START_YEAR)
    for cutoff, key, color in _TODAY_CATEGORIES:
        if pct < cutoff:
            cat  = cats.get(key, {})
            name = cat.get("name", key)
            desc = cat.get("desc", "").format_map(interp)
            return key, name, color, desc
    last = _TODAY_CATEGORIES[-1]
    cat  = cats.get(last[1], {})
    name = cat.get("name", last[1])
    desc = cat.get("desc", "").format_map(interp)
    return last[1], name, last[2], desc

def _today_cache_path(date_str, loc):
    return os.path.join(_TODAY_CACHE_DIR, f"today_{date_str}_{loc}.json")

def _load_today_from_disk(date_str, loc):
    """Return cached dict if today's file exists and is valid, else None."""
    import json as _json
    path = _today_cache_path(date_str, loc)
    try:
        with open(path) as f:
            return _json.load(f)
    except Exception:
        return None

def _save_today_to_disk(date_str, loc, result):
    """Persist a successful today_status result to disk."""
    import json as _json
    try:
        os.makedirs(_TODAY_CACHE_DIR, exist_ok=True)
        with open(_today_cache_path(date_str, loc), "w") as f:
            _json.dump(result, f)
        # Remove cache files older than 3 days.
        # Filename shape: today_YYYY-MM-DD_<loc>.json — date is always the first
        # segment after the "today_" prefix, so slice on a fixed-width date instead
        # of assuming the date runs up to the extension.
        cutoff = (pd.Timestamp(date_str) - pd.Timedelta(days=3)).date().isoformat()
        for p in glob.glob(os.path.join(_TODAY_CACHE_DIR, "today_*.json")):
            stem = os.path.basename(p)[len("today_"):-len(".json")]
            file_date = stem[:10]  # "YYYY-MM-DD"
            if file_date < cutoff:
                try: os.remove(p)
                except Exception: pass
    except Exception:
        pass  # disk write failure is non-fatal

def compute_today_status(target_date=None, loc=None):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    today_ts   = _today_local()
    today_date = today_ts.date()

    if target_date is None:
        target_date = today_date
    is_today = (target_date == today_date)

    # Reject future dates
    if target_date > today_date:
        return {"available": False}

    cache_loc = loc or "national"
    cache_key = target_date.isoformat()

    # 1. In-memory cache
    mem_key = f"{cache_key}|{cache_loc}"
    if mem_key in _TODAY_CACHE:
        return _TODAY_CACHE[mem_key]

    # 2. Filesystem cache — only for gap/past dates, which are immutable once
    # collected. "Today" is never persisted here: its value is expected to
    # change through the day, so a disk hit would just serve a stale forecast
    # across restarts instead of refetching it.
    if not is_today:
        cached = _load_today_from_disk(cache_key, cache_loc)
        if cached is not None:
            _TODAY_CACHE[mem_key] = cached
            return cached

    # Shared helper: parallel 20-station fetch from any Open-Meteo endpoint.
    # extra_params is merged into the per-station request (e.g. forecast_days or start/end_date).
    # Returns {station_name: temp} so callers can pick a single station or take the
    # national max — one batched fetch serves both cases.
    def _fetch_om(url, extra_params):
        def _one(lat, lon):
            try:
                resp = http_requests.get(url, params={
                    "latitude":  f"{lat:.4f}",
                    "longitude": f"{lon:.4f}",
                    "daily":     "temperature_2m_max",
                    "timezone":  CONFIG["timezone"],
                    **extra_params,
                }, timeout=10)
                resp.raise_for_status()
                arr = resp.json().get("daily", {}).get("temperature_2m_max", [])
                return float(arr[0]) if arr and arr[0] is not None else None
            except Exception:
                return None
        temps_by_station = {}
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = {pool.submit(_one, c["lat"], c["lon"]): n for n, c in LOC_COORDS.items()}
            for fut in as_completed(futures):
                name = futures[fut]
                v = fut.result()
                if v is not None:
                    temps_by_station[name] = v
        return temps_by_station

    # 3. Get today_temp via the right source
    if is_today or target_date > _CSV_MAX_DATE:
        if is_today:
            url, extra = "https://api.open-meteo.com/v1/forecast", {"forecast_days": 1}
        else:
            # Gap between CSV coverage and today — use Open-Meteo archive (ERA5-T near-real-time)
            ds = cache_key
            url, extra = "https://archive-api.open-meteo.com/v1/archive", {"start_date": ds, "end_date": ds}

        # Reuse the same day's 20-station fetch across every location filter —
        # only the first request of the day actually calls Open-Meteo.
        temps_by_station = _TODAY_RAW_CACHE.get(cache_key) or {}
        if loc and loc not in temps_by_station:
            # Either nothing cached yet, or the cached fetch missed this one
            # station transiently — (re)fetch and merge in the result.
            temps_by_station.update(_fetch_om(url, extra))
            _TODAY_RAW_CACHE[cache_key] = temps_by_station
        elif not temps_by_station:
            temps_by_station = _fetch_om(url, extra)
            _TODAY_RAW_CACHE[cache_key] = temps_by_station
        if not temps_by_station or (loc and loc not in temps_by_station):
            return {"available": False}  # not cached — a later retry may succeed
        today_temp = temps_by_station[loc] if loc else max(temps_by_station.values())
    else:
        # Within CSV coverage — read directly from ERA5 in-memory data
        day_rows = data[data["date"] == pd.Timestamp(target_date)]
        if loc:
            day_rows = day_rows[day_rows["location"] == loc]
        if day_rows.empty or day_rows["temperature_max"].isna().all():
            return {"available": False}
        today_temp = float(day_rows["temperature_max"].max())

    # 4. Historical distribution: ±7-day window across all years
    month, day = target_date.month, target_date.day
    loc_data = data[data["location"] == loc] if loc else data
    window = window_filter(loc_data, month, day, 7)
    daily_max = window.groupby("date")["temperature_max"].max().dropna()
    samples = daily_max.to_numpy()
    if len(samples) < 50:
        _TODAY_CACHE[mem_key] = {"available": False}
        return _TODAY_CACHE[mem_key]

    # 5. Percentile + category
    pct = float((samples < today_temp).mean() * 100)
    dlabel = f"{MONTH_NAMES[month - 1]} {day}"
    cat_key, name, color, desc = _categorize_today(pct, dlabel, country=loc)

    # 5b. Same-date rank: how today_temp compares to this exact calendar date
    # (month+day, no ±window) across every prior year on record. Surfaced on
    # the card only when today lands in the top/bottom 5 — a true "Nth hottest/
    # coldest day on record", distinct from the ±7-day percentile above.
    same_date = loc_data[(loc_data["date"].dt.month == month) & (loc_data["date"].dt.day == day)
                          & (loc_data["date"] != pd.Timestamp(target_date))]
    same_date_max = same_date.groupby("date")["temperature_max"].max().dropna()
    rank_total = int(len(same_date_max)) + 1  # + the date being ranked
    rank_hot = int((same_date_max > today_temp).sum()) + 1
    rank_cold = int((same_date_max < today_temp).sum()) + 1
    rank_info = None
    if rank_total >= 10:
        direction = None
        if rank_hot <= 5:
            direction = "hot"
        elif rank_cold <= 5:
            direction = "cold"
        if direction:
            top5 = same_date_max.sort_values(ascending=(direction == "cold")).head(4)
            top5_list = [{"year": int(d.year), "date": d.strftime("%Y-%m-%d"), "temp": round(float(v), 1)}
                         for d, v in top5.items()]
            top5_list.append({"year": int(target_date.year), "date": target_date.isoformat(),
                               "temp": round(today_temp, 1), "is_today": True})
            top5_list.sort(key=lambda x: x["temp"], reverse=(direction == "hot"))
            rank_info = {
                "rank": rank_hot if direction == "hot" else rank_cold,
                "total": rank_total,
                "direction": direction,
                "top5": top5_list,
            }

    # 6. KDE curve + percentile cutoffs
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
        "date":         cache_key,
        "computed_at":  pd.Timestamp.now(tz=CONFIG["timezone"]).isoformat(),
        "computed_at_tz": CONFIG["timezone"],
        "today_temp":   round(today_temp, 1),
        "percentile":   round(pct, 1),
        "category_key": cat_key,
        "category":     name,
        "color":        color,
        "description":  desc,
        "n_samples":    int(len(samples)),
        "rank_info":    rank_info,
        "year_min":     int(loc_data["year"].min()),
        "year_max":     int(loc_data["year"].max()),
        "distribution": distribution,
        "cutoffs":      cutoffs,
        "day_label":    dlabel,
        "month_num":    month,
        "day_num":      day,
        "loc":          loc,
    }
    _TODAY_CACHE[mem_key] = result
    if not is_today:
        _save_today_to_disk(cache_key, cache_loc, result)
    return result

def compute_today_last7(end_date=None, loc=None):
    """Category/percentile for each of the 7 days ending at end_date (inclusive),
    ascending by date. Reuses compute_today_status per day, so each day rides the
    same in-memory/disk cache rather than introducing a second cache layer.
    """
    if end_date is None:
        end_date = _today_local().date()
    days = []
    for offset in range(6, -1, -1):
        d = end_date - datetime.timedelta(days=offset)
        r = compute_today_status(d, loc)
        if not r.get("available"):
            continue
        days.append({
            "date":         r["date"],
            "day_label":    r["day_label"],
            "today_temp":   r["today_temp"],
            "percentile":   r["percentile"],
            "category_key": r["category_key"],
            "color":        r["color"],
        })
    return {"available": bool(days), "days": days}

_WARMING_STRIPES_CACHE = {}

def compute_warming_stripes(loc=None):
    """One annual mean temperature per year (national average across stations,
    or a single station's own mean when loc is given), for the classic
    'warming stripes' visualisation. Years with no data are dropped.
    """
    cache_key = loc or "national"
    if cache_key in _WARMING_STRIPES_CACHE:
        return _WARMING_STRIPES_CACHE[cache_key]

    loc_data = data[data["location"] == loc] if loc else data
    annual = (
        loc_data.groupby("year")["temperature_mean_corr"]
        .mean()
        .dropna()
    )
    annual = annual[annual.index >= CONFIG["trend_start_year"]]
    annual = annual[annual.index < _today_local().year]  # drop current, incomplete year

    years = [{"year": int(y), "value": round(float(v), 2)} for y, v in annual.items()]
    result = {
        "available": bool(years),
        "years":     years,
        "loc":       loc,
    }
    _WARMING_STRIPES_CACHE[cache_key] = result
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
    if path in ("/", "/dashboard") or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-store"
    elif path.endswith((".js", ".css", ".json")):
        response.headers["Cache-Control"] = "no-cache"
    return response

def _feature_enabled(key: str) -> bool:
    """Return True when the named feature is enabled in CONFIG."""
    return bool(CONFIG["features"].get(key, False))

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/dashboard")
def dashboard():
    return send_from_directory("static", "dashboard.html")

@app.route("/fires")
def fires_page():
    if not _feature_enabled("fires_map"):
        return "", 404
    return send_from_directory("static", "fires.html")

@app.route("/geo/<path:filename>")
def serve_geo(filename):
    return send_from_directory(os.path.join("static", "geo"), filename)

@app.route("/api/meta")
def api_meta():
    return jsonify({
        # ── existing keys (unchanged) ──────────────────────────────────────────
        "locations":   LOCATIONS,
        "variables":   VARIABLES,
        "month_names": MONTH_NAMES,
        "palette":     PALETTE,
        "chat_enabled":          bool(DIRECT_LINE_SECRET),
        "chat_error_rate_limit":   CHAT_ERROR_RATE_LIMIT,
        "chat_error_generic":      CHAT_ERROR_GENERIC,
        "chat_error_global_limit": CHAT_ERROR_GLOBAL_LIMIT,
        # ── new keys from CONFIG ───────────────────────────────────────────────
        "country":          CONFIG["code"],
        "name":             CONFIG["name"],
        "timezone":         CONFIG["timezone"],
        "default_location": CONFIG["default_location"],
        "default_language": CONFIG["default_language"],
        "languages":        CONFIG["languages"],
        "map":              CONFIG["map"],
        "branding":         CONFIG["branding"],
        "features":         CONFIG["features"],
        "fires":            _fires_meta(),
    })

def _fires_meta():
    """Resolved fire-map layer config for the frontend, honouring feature flags.
    Only exposes a layer's URL/name when its feature is enabled — a disabled
    feature yields no config, so the frontend can't build a layer for it."""
    if not _fires_any_enabled():
        return None
    fc = FIRES_CFG
    meta = {
        "bbox":    fc.get("bbox"),
        "sensors": [s["key"] for s in fc.get("firms_sources", [])]
                   + (["SENTINEL3"] if fc.get("sentinel3") else []),
        "sensor_start": _fires_sensor_start_dates(),
    }
    # WMS overlays are served through our own tile proxy (/api/fires/tiles/<key>),
    # which caches upstream Copernicus/JRC tiles — see api_fires_tile. The frontend
    # still passes the layer name as the WMS `layers=` param.
    if _feature_enabled("fires_danger"):
        meta["danger"] = {"wms": "/api/fires/tiles/danger",
                          "layer": fc.get("effis_danger_layer")}
    if _feature_enabled("fires_map") and fc.get("sentinel3"):
        # Sentinel-3 hotspots shown as a WMS overlay (reliable fallback to WFS points).
        meta["s3_wms"] = {"wms": "/api/fires/tiles/s3",
                          "layer": fc.get("effis_s3_layer")}
    if _feature_enabled("fires_satellite"):
        meta["satellite_tiles"] = fc.get("satellite_tiles")
    if _feature_enabled("fires_settlement"):
        meta["settlement"] = {"wms": "/api/fires/tiles/settlement",
                              "builtup_layer": fc.get("ghsl_builtup_layer")}
    if _feature_enabled("fires_burnt_area"):
        meta["burnt_area"] = {"wms": "/api/fires/tiles/burnt_area",
                              "layer": fc.get("effis_ba_layer")}
    if _feature_enabled("fires_protected_areas"):
        meta["protected_areas"] = {"wms": "/api/fires/tiles/protected_areas",
                                   "layer": fc.get("effis_pa_layer")}
    return meta

@app.route("/api/regression")
def api_regression():
    if not (_feature_enabled("regression_chart") or _feature_enabled("hero_cards")):
        return "", 204
    locs   = request.args.getlist("loc") or [CONFIG["default_location"]]
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
    if not _feature_enabled("trend_calendar"): return "", 204
    loc    = request.args.get("loc",    CONFIG["default_location"])
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
    if not _feature_enabled("station_map"): return "", 204
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
    if not _feature_enabled("today_section"): return "", 204
    date_str = request.args.get("date")
    loc = request.args.get("loc") or None
    if loc and loc not in LOCATIONS:
        return jsonify({"available": False}), 400
    target = None
    if date_str:
        try:
            target = pd.Timestamp(date_str).date()
        except Exception:
            return jsonify({"available": False}), 400
    return jsonify(compute_today_status(target, loc))


@app.route("/api/today_status/last7")
def api_today_status_last7():
    if not _feature_enabled("today_section"): return "", 204
    date_str = request.args.get("date")
    loc = request.args.get("loc") or None
    if loc and loc not in LOCATIONS:
        return jsonify({"available": False}), 400
    end_date = None
    if date_str:
        try:
            end_date = pd.Timestamp(date_str).date()
        except Exception:
            return jsonify({"available": False}), 400
    return jsonify(compute_today_last7(end_date, loc))


@app.route("/api/warming_stripes")
def api_warming_stripes():
    if not _feature_enabled("warming_stripes"): return "", 204
    loc = request.args.get("loc") or None
    if loc and loc not in LOCATIONS:
        return jsonify({"available": False}), 400
    return jsonify(compute_warming_stripes(loc))


@app.route("/api/today_status/refresh")
def api_today_status_refresh():
    """
    Force a fresh Open-Meteo pull for today's live forecast. Clears today's
    in-memory cache (national + any previously-cached location) and the shared
    raw 20-station fetch, then refetches once (national). Every location's
    next request is served from that same refetched data with no further
    network call — only national needs an explicit refetch here. Intended for
    cron — the live forecast for "today" only improves through the day
    (morning estimate vs. the near-final afternoon read once the day's max has
    likely occurred), so a single per-process-lifetime cache otherwise goes
    stale until midnight.
    Access: GET /api/today_status/refresh?key=<TODAY_REFRESH_KEY>
    """
    if not _feature_enabled("today_section"): return "", 204
    key = request.args.get("key", "")
    if not _TODAY_REFRESH_KEY or key != _TODAY_REFRESH_KEY:
        return Response("Forbidden", status=403)

    cache_key = _today_local().date().isoformat()
    previous = _TODAY_CACHE.get(f"{cache_key}|national", {}).get("today_temp")

    _TODAY_RAW_CACHE.pop(cache_key, None)
    for mem_key in [k for k in list(_TODAY_CACHE) if k.startswith(f"{cache_key}|")]:
        _TODAY_CACHE.pop(mem_key, None)

    result = compute_today_status()  # national — refetches + reseeds the shared raw cache
    return jsonify({
        "refreshed":       result.get("available", False),
        "computed_at":     result.get("computed_at"),
        "computed_at_tz":  result.get("computed_at_tz"),
        "previous_national_today_temp": previous,
        "new_national_today_temp":      result.get("today_temp"),
    })


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
    BASELINE_START, BASELINE_END = CONFIG["baseline"]["start"], CONFIG["baseline"]["end"]
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
    if not _feature_enabled("season_heat_heatmap"): return "", 204
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
    BASELINE_START, BASELINE_END = CONFIG["baseline"]["start"], CONFIG["baseline"]["end"]
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
    if not _feature_enabled("spei_heatmap"): return "", 204
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
    BASELINE_START, BASELINE_END = CONFIG["baseline"]["start"], CONFIG["baseline"]["end"]
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
    if not _feature_enabled("drought_trend_chart"): return "", 204
    return jsonify(compute_spei_station_seasonal())


# ── Tropical days / nights (generalised from the original "hot nights" chart) ──

_THRESHOLD_DAYS_CACHE = {}

def _streak_filter(qualifies, min_streak):
    """Zero out True runs in a boolean array shorter than min_streak."""
    out = qualifies.copy()
    n = len(out)
    i = 0
    while i < n:
        if out[i]:
            j = i
            while j < n and out[j]:
                j += 1
            if j - i < min_streak:
                out[i:j] = False
            i = j
        else:
            i += 1
    return out

def compute_threshold_days(var, threshold, min_streak):
    """
    Per-station annual count of days where `var` (tmax/tmin, elevation-corrected)
    exceeds `threshold`. When min_streak > 1, only days that are part of a run of
    >= min_streak consecutive qualifying days are counted (min_streak=1 counts
    every qualifying day, same as a plain threshold count).
    Trend: Negative Binomial GLM (count data — Theil-Sen/Mann-Kendall guardrail
    method applies to continuous annual aggregates, not per-year event counts).
    """
    cache_key = (var, threshold, min_streak)
    if cache_key in _THRESHOLD_DAYS_CACHE:
        return _THRESHOLD_DAYS_CACHE[cache_key]

    col = "temperature_min_corr" if var == "tmin" else "temperature_max_corr"
    result = {}
    for loc in LOCATIONS:
        loc_df = data[data["location"] == loc].sort_values("date").copy()
        qualifies = (loc_df[col] > threshold).to_numpy()
        if min_streak > 1:
            qualifies = _streak_filter(qualifies, min_streak)
        loc_df = loc_df.assign(_qualifies=qualifies)

        annual = (
            loc_df[loc_df["_qualifies"]]
            .groupby("year")
            .size()
            .reindex(range(int(loc_df["year"].min()), int(loc_df["year"].max()) + 1), fill_value=0)
        )
        years  = annual.index.tolist()
        counts = [int(v) for v in annual.values]
        # Exclude the current incomplete year from the NB fit (bars still show it)
        current_year = _CSV_MAX_DATE.year
        fit_mask   = [i for i, y in enumerate(years) if y != current_year]
        fit_years  = [years[i]  for i in fit_mask]
        fit_counts = [counts[i] for i in fit_mask]
        trend         = {}
        nonzero_count = sum(1 for c in fit_counts if c > 0)
        if len(fit_years) >= 10 and nonzero_count >= 10:
            try:
                years_arr = np.array(fit_years, dtype=float)
                year_mean = float(years_arr[0])
                X_c       = sm.add_constant(years_arr - year_mean)
                fitted    = sm.NegativeBinomial(fit_counts, X_c).fit(disp=False, maxiter=200)
                x_dense   = np.linspace(years[0], years[-1], len(years))
                X_dense   = sm.add_constant(x_dense - year_mean)
                pred      = fitted.get_prediction(X_dense)
                pred_df   = pred.summary_frame(alpha=0.05)
                mid_year       = float(np.median(fit_years))
                mid_mu         = float(np.exp(fitted.params[0] + fitted.params[1] * (mid_year - year_mean)))
                days_per_decade = round(mid_mu * (float(np.exp(fitted.params[1] * 10)) - 1), 1)
                alpha_val       = round(float(fitted.params[-1]), 3)
                mu_dense = pred_df["predicted"].values
                se_pred  = np.sqrt(mu_dense + mu_dense**2 * alpha_val)
                pi_low   = np.maximum(0.0, mu_dense - 1.96 * se_pred)
                pi_high  = mu_dense + 1.96 * se_pred
                trend     = {
                    "model_used":      "nb",
                    "rate_per_year":   round(max(-50.0, min(50.0, float(np.exp(fitted.params[1]) - 1) * 100)), 2),
                    "days_per_decade": days_per_decade,
                    "p_value":         round(max(0.0001, min(0.9999, float(fitted.pvalues[1]))), 3),
                    "alpha":           alpha_val,
                    "aic":             round(float(fitted.aic), 1),
                    "fit_year_max":    int(fit_years[-1]),
                    "x_line":          [round(float(x), 2) for x in x_dense],
                    "y_line":          pred_df["predicted"].round(2).tolist(),
                    "ci_low":          pred_df["ci_lower"].round(2).tolist(),
                    "ci_high":         pred_df["ci_upper"].round(2).tolist(),
                    "pi_low":          pi_low.round(2).tolist(),
                    "pi_high":         pi_high.round(2).tolist(),
                }
                yl = trend["y_line"]; cl = trend["ci_low"]; ch = trend["ci_high"]
                if not (
                    all(np.isfinite(v) and v >= 0 for v in yl) and
                    all(np.isfinite(v)             for v in cl) and
                    all(np.isfinite(v)             for v in ch) and
                    all(cl[i] <= ch[i] for i in range(len(cl)))
                ):
                    trend = {}
            except Exception as e:
                import sys
                print(f"[threshold_days] fit failed for {loc} ({var},{threshold},{min_streak}): {e}", file=sys.stderr)
        result[loc] = {"years": years, "counts": counts, "trend": trend, "nonzero_count": nonzero_count}

    out = {
        "stations":   result,
        "era5_last":  str(_CSV_MAX_DATE),
        "threshold":  threshold,
        "min_streak": min_streak,
        "variable":   var,
    }
    _THRESHOLD_DAYS_CACHE[cache_key] = out
    return out


@app.route("/api/tropical_days")
def api_tropical_days():
    if not _feature_enabled("tropical_days_chart"): return "", 204
    threshold  = max(15.0, min(45.0, request.args.get("threshold", 30.0, type=float)))
    min_streak = max(1, min(60, request.args.get("streak", 1, type=int)))
    return jsonify(compute_threshold_days("tmax", threshold, min_streak))


@app.route("/api/tropical_nights")
def api_tropical_nights():
    if not _feature_enabled("tropical_nights_chart"): return "", 204
    threshold  = max(5.0, min(35.0, request.args.get("threshold", 20.0, type=float)))
    min_streak = max(1, min(60, request.args.get("streak", 1, type=int)))
    return jsonify(compute_threshold_days("tmin", threshold, min_streak))


@app.route("/api/climate_news")
@limiter.limit("30 per minute")
def api_climate_news():
    if not _feature_enabled("climate_news"): return "", 204
    # Bluesky posts are excluded here — they're already shown in the page's
    # dedicated Bluesky widget, so including them in the news list would be
    # redundant. The RSS feed below still includes them.
    resp = jsonify(compute_climate_news(include_bluesky=False))
    # Short cache lifetime so browsers/CDN don't hold onto a stale response —
    # the underlying archive only changes every 6h (RSS/Bing) or 1x/day
    # (SerpApi) anyway, so this is purely about avoiding a hard-refresh need.
    resp.headers["Cache-Control"] = "public, max-age=900"
    return resp


@app.route("/rss/climate_news.xml")
@limiter.limit("30 per minute")
def rss_climate_news():
    if not _feature_enabled("climate_news"): return "", 204
    xml = build_rss_xml(compute_climate_news())
    return Response(xml, mimetype="application/rss+xml")


@app.route("/api/annual_trend")
def api_annual_trend():
    if not _feature_enabled("today_section"): return "", 204
    date_str = request.args.get("date")
    loc = request.args.get("loc") or None
    if loc and loc not in LOCATIONS:
        return jsonify({"available": False}), 400
    target = None
    if date_str:
        try:
            target = pd.Timestamp(date_str).date()
        except Exception:
            pass
    return jsonify(compute_annual_trend(target, loc))


@app.route("/api/token")
@limiter.limit(TOKEN_LIMIT_MINUTE)
@limiter.limit(TOKEN_LIMIT_HOUR)
def get_token():
    if not _feature_enabled("chatbot"): return "", 204
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
    if not _feature_enabled("chatbot"): return "", 204
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
    if not _feature_enabled("analytics_export"): return "", 204
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
    if not _feature_enabled("analytics_export"): return "", 204
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
    # Pre-warm gap dates (CSV max + 1 day → yesterday) so the archive API is
    # called once per date on startup rather than on first user navigation click.
    # Cache hits from disk are instant so restarts don't re-fetch already-cached dates.
    _gap = _CSV_MAX_DATE + datetime.timedelta(days=1)
    _today = _today_local().date()
    while _gap < _today:
        try: compute_today_status(_gap)
        except Exception: pass
        _gap += datetime.timedelta(days=1)
    # Calendar for all locations with default params (temperature_max, w=7, theilsen)
    for _loc in list(LOC_COORDS.keys()):
        try: compute_calendar(_loc, "temperature_max", "temperature_max", 7, "theilsen")
        except Exception: pass

threading.Thread(target=_prewarm, daemon=True).start()


@app.route("/api/data/download")
def download_data():
    if not _feature_enabled("data_download"): return jsonify({"error": "disabled"}), 404
    zip_path = os.path.join(DATA_DIR, "all_stations.zip")
    if not os.path.exists(zip_path):
        return jsonify({"error": "No data archive available yet. Run mk_collect.py first."}), 404
    return send_file(
        zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{CONFIG['code']}_climate_data.zip",
        conditional=True,
    )


# ── Wildfire endpoints (/fires page) ───────────────────────────────────────────

_FIRES_CACHE_DIR = os.path.join(_CACHE_DIR, "fires")
# Cap points returned to the map so a multi-year range can't ship millions of rows.
_FIRES_POINTS_CAP = 20000

@app.route("/api/fires/points")
def api_fires_points():
    """Active-fire detections for a date range, optionally filtered by sensor.
    ?start=YYYY-MM-DD&end=YYYY-MM-DD&sensor=MODIS,VIIRS_SNPP (default all)."""
    if not _feature_enabled("fires_map"):
        return "", 204
    df = _load_fires()
    if df.empty:
        return jsonify({"points": [], "count": 0, "capped": False})

    end   = request.args.get("end")   or _today_local().date().isoformat()
    start = request.args.get("start") or (pd.to_datetime(end) - pd.Timedelta(days=7)).date().isoformat()
    sensors = [s for s in (request.args.get("sensor") or "").split(",") if s]

    try:
        s_ts, e_ts = pd.to_datetime(start), pd.to_datetime(end)
    except Exception:
        return jsonify({"points": [], "count": 0, "capped": False}), 400

    m = (df["acq_date"] >= s_ts) & (df["acq_date"] <= e_ts)
    if sensors:
        m &= df["sensor"].isin(sensors)
    sub = df[m]
    total = len(sub)
    capped = total > _FIRES_POINTS_CAP
    if capped:                       # keep the strongest fires when over the cap
        sub = sub.sort_values("frp", ascending=False).head(_FIRES_POINTS_CAP)

    points = [
        {"lat": round(float(r.latitude), 5), "lon": round(float(r.longitude), 5),
         "date": r.acq_date.date().isoformat(), "sensor": r.sensor,
         "frp": (None if pd.isna(r.frp) else float(r.frp)),
         "conf": (None if pd.isna(r.confidence) else str(r.confidence))}
        for r in sub.itertuples()
    ]
    return jsonify({"points": points, "count": total, "capped": capped,
                    "collected_at": _fires_collected_at(),
                    "latest_detection": _fires_latest_detection()})

def _fires_collected_at():
    """Timestamp (local ISO) of the last server-side fire collection, or None."""
    stamp = _fs_load(os.path.join(_FIRES_DIR, "_last_collected.json"))
    return stamp.get("collected_at") if isinstance(stamp, dict) else None

@app.route("/api/fires/yearly")
def api_fires_yearly():
    """Per-year fire-detection totals for the comparison chart.
    Prefers GFW's server-side aggregation when GFW_API_KEY + gfw_yearly_via_api
    are set; otherwise counts the local FIRMS CSVs. Cached daily.

    GFW's dataset (VIIRS-based) only goes back to 2012, but our local CSVs include
    MODIS back to 2000 — so any year before GFW's earliest is filled in from local
    counts (_extend_yearly_with_local) rather than silently dropped. The response
    marks which years came from the extension so the frontend can caption the
    sensor-boundary honestly (MODIS vs VIIRS aren't directly comparable counts)."""
    if not _feature_enabled("fires_year_chart"):
        return "", 204
    today   = _today_local().date().isoformat()
    use_gfw = bool(GFW_API_KEY and FIRES_CFG.get("gfw_yearly_via_api"))
    fs_path = os.path.join(_FIRES_CACHE_DIR, f"yearly_gfw_{today}.json")

    # 1) Today's GFW cache is warm → serve it (fast). Only trust it if it actually
    #    holds GFW data (guards against a stale local-in-gfw-file from older code).
    if use_gfw:
        cached = _fs_load(fs_path)
        if cached is not None and cached.get("source") == "gfw":
            return jsonify(_extend_yearly_with_local(cached))

    # 2) Otherwise respond immediately with local counts (never block the request
    #    on GFW's slow 15-call loop), and warm the GFW cache in the background so
    #    the next load gets the official totals.
    if use_gfw:
        _warm_gfw_yearly_async(fs_path, today)
        # If a previous day's GFW cache exists, prefer it over local (still fast).
        stale = _latest_gfw_yearly_cache()
        if stale is not None:
            return jsonify(_extend_yearly_with_local(stale))

    return jsonify(_fires_yearly_local())

def _extend_yearly_with_local(gfw_result):
    """Prepend any year before GFW's earliest year using local FIRMS counts (e.g.
    MODIS 2000-2011, before VIIRS/GFW coverage begins). Marks the filled years in
    `pre_gfw_years` so the frontend can caption the sensor-boundary clearly."""
    if not gfw_result.get("years"):
        return gfw_result
    local = _fires_yearly_local()
    gfw_start = min(gfw_result["years"])
    extra_years, extra_counts = [], []
    for y, c in zip(local["years"], local["counts"]):
        if y < gfw_start:
            extra_years.append(y)
            extra_counts.append(c)
    if not extra_years:
        return gfw_result
    return {
        "years": extra_years + gfw_result["years"],
        "counts": extra_counts + gfw_result["counts"],
        "source": "gfw",
        "pre_gfw_years": extra_years,
    }

def _latest_gfw_yearly_cache():
    """Most recent *valid* GFW yearly cache from any day (stale-but-instant
    fallback). Skips files that don't actually contain GFW data."""
    for f in sorted(glob.glob(os.path.join(_FIRES_CACHE_DIR, "yearly_gfw_*.json")),
                    reverse=True):
        d = _fs_load(f)
        if isinstance(d, dict) and d.get("source") == "gfw" and d.get("years"):
            return d
    return None

_gfw_warming = set()
def _warm_gfw_yearly_async(fs_path, today):
    """Build today's GFW yearly cache off the request thread (once at a time)."""
    if today in _gfw_warming:
        return
    _gfw_warming.add(today)
    def _work():
        try:
            result = _fires_yearly_gfw()
            if result:
                _fs_save(fs_path, result,
                         glob_pattern=os.path.join(_FIRES_CACHE_DIR, "yearly_gfw_*.json"),
                         anchor_date=today)
        finally:
            _gfw_warming.discard(today)
    threading.Thread(target=_work, daemon=True).start()

def _fires_yearly_local():
    df = _load_fires()
    if df.empty:
        return {"years": [], "counts": [], "source": "local"}
    g = df.groupby("year").size().sort_index()
    return {"years": [int(y) for y in g.index],
            "counts": [int(c) for c in g.values], "source": "local"}

def _fires_yearly_gfw():
    """Query GFW's fire-alerts dataset for ISO-filtered yearly counts.

    Notes from testing the live API: the dataset has no year column (year is
    derived from alert__date), the key is domain-restricted so an Origin header
    is required, and a full-history GROUP BY times out (504). Per-year queries
    return instantly, so we loop one date-bounded SUM per year. VIIRS alerts
    start in 2012. Any failure returns None → caller falls back to local counts."""
    ds     = FIRES_CFG.get("gfw_dataset", "nasa_viirs_fire_alerts")
    iso    = FIRES_CFG.get("iso3")
    origin = "https://" + (CONFIG.get("branding", {}).get("domain") or "climate.mk")
    url    = f"https://data-api.globalforestwatch.org/dataset/{ds}/latest/query/json"
    headers = {"x-api-key": GFW_API_KEY, "origin": origin}

    years, counts = [], []
    failures = 0
    for yr in range(2012, _today_local().year + 1):
        sql = ("SELECT SUM(alert__count) AS cnt FROM results "
               f"WHERE iso = '{iso}' AND alert__date >= '{yr}-01-01' "
               f"AND alert__date <= '{yr}-12-31'")
        try:
            r = http_requests.post(url, headers=headers, json={"sql": sql}, timeout=20)
            r.raise_for_status()
            rows = r.json().get("data", [])
        except Exception:
            failures += 1
            if failures > 3:      # GFW clearly unhealthy → fall back to local counts
                return None
            continue              # a single flaky year shouldn't sink the whole chart
        cnt = rows[0].get("cnt") if rows else None
        if cnt:
            years.append(yr)
            counts.append(int(cnt))
    if not years:
        return None
    return {"years": years, "counts": counts, "source": "gfw"}

@app.route("/api/fires/danger_meta")
def api_fires_danger_meta():
    """WMS config for the EFFIS fire-danger overlay (no key needed client-side)."""
    if not _feature_enabled("fires_danger"):
        return "", 204
    return jsonify({
        "wms":   FIRES_CFG.get("effis_wms"),
        "layer": FIRES_CFG.get("effis_danger_layer"),
        "bbox":  FIRES_CFG.get("bbox"),
    })

# ── WMS tile proxy + cache ─────────────────────────────────────────────────────
# The map overlays are third-party WMS layers (Copernicus EFFIS/GWIS, JRC GHSL).
# Instead of every visitor's browser hitting those public servers directly for
# every tile, we proxy through here: fetch once from upstream, cache the PNG bytes
# on disk, and serve cached bytes to everyone after. Cuts external load, hides
# outages/rate-limiting/geo-blocking, and speeds the page. The hourly fire cron
# warms the common (MK-bbox) tiles ahead of visitors (see /api/fires/tiles/warm).

_TILE_CACHE_DIR = os.path.join(_FIRES_CACHE_DIR, "tiles")

# One pooled session for tile fetches — otherwise every tile pays a fresh TLS
# handshake to Copernicus/JRC, which dominates the cost of a cache miss (and of
# the hourly warm, which fetches a few hundred tiles in a row).
_TILE_SESSION = http_requests.Session()
_TILE_SESSION.mount("https://", http_requests.adapters.HTTPAdapter(
    pool_connections=4, pool_maxsize=20))

# Concurrent upstream fetches during a warm. Kept well under the session's
# pool_maxsize so workers never contend for a connection.
_TILE_WARM_WORKERS = 6

# layer_key -> (config key for the WMS base url, config key for the layer name,
#               feature flag, cache time-to-live in seconds — None = long-lived)
_FIRES_TILE_LAYERS = {
    "danger":          ("effis_wms",    "effis_danger_layer", "fires_danger",          36 * 3600),
    "s3":              ("effis_wms",    "effis_s3_layer",     "fires_map",             15 * 60),
    "burnt_area":      ("effis_ba_wms", "effis_ba_layer",     "fires_burnt_area",      36 * 3600),
    "protected_areas": ("effis_pa_wms", "effis_pa_layer",     "fires_protected_areas", 60 * 86400),
    "settlement":      ("ghsl_wms",     "ghsl_builtup_layer", "fires_settlement",      60 * 86400),
}

# WMS params Leaflet sends that we forward upstream (everything geometry/format).
_TILE_FORWARD_PARAMS = ("bbox", "width", "height", "crs", "srs", "styles",
                        "format", "transparent", "version", "layers", "time")

# 1×1 transparent PNG, returned on upstream failure so the map shows a blank tile
# (not a broken-image icon) and we don't cache the failure.
_BLANK_TILE = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c6360000002000100" "05fe02fea7"
    "35814f0000000049454e44ae426082")

def _tile_upstream(layer_key):
    """Resolve (wms_url, expected_layer, ttl) for a layer_key, honouring flags.
    Returns None if unknown or its feature is disabled."""
    spec = _FIRES_TILE_LAYERS.get(layer_key)
    if not spec:
        return None
    url_key, layer_key_cfg, flag, ttl = spec
    if not _feature_enabled(flag):
        return None
    url = FIRES_CFG.get(url_key) or FIRES_CFG.get("effis_wms")
    layer = FIRES_CFG.get(layer_key_cfg)
    if not (url and layer):
        return None
    return url, layer, ttl

def _norm_tile_param(k, v):
    """Normalise a param value for cache keying. Leaflet sends bbox at full float
    precision (2269873.9919565944) while the warmer formats to 4 dp
    (2269873.9920) — the same tile, but a different key, so warmed tiles were
    never actually hit. Round both to 0.1 m, far below one pixel at any zoom."""
    if k != "bbox":
        return v
    try:
        # `+ 0.0` collapses -0.0 (tile edges on the equator / prime meridian) to 0.0.
        return ",".join(f"{round(float(x), 1) + 0.0:.1f}" for x in v.split(","))
    except ValueError:
        return v

def _tile_cache_path(layer_key, args):
    """Cache path for a tile request. Time-dimensioned layers bucket by their
    `time` value (so a new day/window is a fresh fetch); static layers use one
    bucket. The tile itself is keyed by a hash of the geometry params."""
    bucket = (args.get("time") or "_static").replace("/", "_").replace(":", "")
    key = "&".join(f"{k}={_norm_tile_param(k, args[k])}"
                   for k in sorted(args) if k in _TILE_FORWARD_PARAMS)
    h = hashlib.md5(key.encode()).hexdigest()
    return os.path.join(_TILE_CACHE_DIR, layer_key, bucket, f"{h}.png")

# How many time-buckets (≈ days/windows) to keep per layer; older ones are pruned
# so the tile cache can't grow without bound. Runs on the hourly warm.
_TILE_BUCKETS_KEEP = 3

def _prune_tile_buckets(layer_key, keep=_TILE_BUCKETS_KEEP):
    """Delete all but the `keep` most-recent time-bucket folders for a layer.
    Bucket names are date/range strings that sort chronologically, so a reverse
    sort keeps the newest. The static bucket has no dates to sort on, so it is
    swept by age instead (see _prune_static_bucket)."""
    layer_dir = os.path.join(_TILE_CACHE_DIR, layer_key)
    try:
        buckets = [d for d in os.listdir(layer_dir)
                   if os.path.isdir(os.path.join(layer_dir, d)) and d != "_static"]
    except Exception:
        return
    for stale in sorted(buckets, reverse=True)[keep:]:
        try:
            shutil.rmtree(os.path.join(layer_dir, stale))
        except Exception:
            pass
    _prune_static_bucket(layer_key, layer_dir)

def _prune_static_bucket(layer_key, layer_dir):
    """Drop expired tiles from a layer's `_static` folder. Past its TTL a tile is
    already unservable (_fs_load_bytes rejects it, _fetch_tile refetches), so it
    is dead weight — and without this sweep the folder only ever grows, one file
    per tile any visitor ever panned or zoomed to."""
    ttl = _FIRES_TILE_LAYERS[layer_key][3]
    if not ttl:
        return
    static_dir = os.path.join(layer_dir, "_static")
    cutoff = time.time() - ttl
    try:
        stale = [f for f in os.listdir(static_dir)
                 if os.path.getmtime(os.path.join(static_dir, f)) < cutoff]
    except Exception:
        return
    for f in stale:
        try:
            os.remove(os.path.join(static_dir, f))
        except Exception:
            pass

def _fetch_tile(layer_key, args, save=True, attempts=2):
    """Return (png_bytes, ok) for a tile — from cache if present/fresh, else
    fetched from upstream and cached. On upstream failure returns the blank tile
    with ok=False, so callers can tell a real tile from a hole in the map: a
    blank looks identical to "no fire danger here" but is really a missing tile,
    and must not be cached or counted as warmed."""
    up = _tile_upstream(layer_key)
    if not up:
        return None, False
    url, expected_layer, ttl = up
    # Only ever request the layer this key is bound to (no open-proxy / SSRF).
    params = {k: args[k] for k in _TILE_FORWARD_PARAMS if k in args}
    params["layers"] = expected_layer
    params.setdefault("service", "WMS")
    params.setdefault("request", "GetMap")

    path = _tile_cache_path(layer_key, args)
    cached = _fs_load_bytes(path, max_age_s=ttl)
    if cached is not None:
        return cached, True
    # EFFIS fails on scattered tiles under concurrent load; one retry turns most
    # of those into a rendered tile instead of a rectangular hole in the overlay.
    for attempt in range(attempts):
        try:
            r = _TILE_SESSION.get(url, params=params, timeout=12)
            r.raise_for_status()
            if r.content[:4] != b"\x89PNG":   # WMS error document, not an image
                continue
            if save:
                _fs_save_bytes(path, r.content)
            return r.content, True
        except Exception:
            pass
    return _BLANK_TILE, False

@app.route("/api/fires/tiles/<layer_key>")
def api_fires_tile(layer_key):
    """Proxy + cache a single WMS overlay tile. Leaflet appends the WMS query
    params; we forward them upstream, cache the PNG, and serve cached bytes."""
    if _tile_upstream(layer_key) is None:
        return "", 404
    png, ok = _fetch_tile(layer_key, request.args)
    if png is None:
        return "", 404
    resp = Response(png, mimetype="image/png")
    # A real tile is cacheable for a while; a blank is a failed fetch, and
    # caching it publicly for 15 min pins a hole in the overlay for everyone
    # behind Cloudflare long after the upstream recovers.
    resp.headers["Cache-Control"] = ("public, max-age=900" if ok
                                     else "no-store")
    return resp

def _bbox_tiles(bbox, zoom):
    """Yield (x, y) XYY tile indices covering a lon/lat bbox at a zoom level."""
    import math
    w, s, e, n = bbox
    def xy(lon, lat):
        lat = max(min(lat, 85.05), -85.05)
        nt = 2 ** zoom
        x = int((lon + 180.0) / 360.0 * nt)
        la = math.radians(lat)
        y = int((1.0 - math.asinh(math.tan(la)) / math.pi) / 2.0 * nt)
        return x, y
    x0, y1 = xy(w, s)     # south-west
    x1, y0 = xy(e, n)     # north-east
    for x in range(min(x0, x1), max(x0, x1) + 1):
        for y in range(min(y0, y1), max(y0, y1) + 1):
            yield x, y

def _tile_wms_params(bbox_3857, layer, time_val):
    """Build the WMS GetMap params for a single 256px tile (EPSG:3857)."""
    p = {"bbox": ",".join(f"{v:.4f}" for v in bbox_3857),
         "width": "256", "height": "256", "crs": "EPSG:3857",
         "styles": "", "format": "image/png", "transparent": "true",
         "version": "1.3.0", "layers": layer}
    if time_val:
        p["time"] = time_val
    return p

def _warm_tile_layer(layer_key, zooms=(7, 8, 9)):
    """Pre-fetch and cache the MK-bbox tiles for one layer at common zooms, so
    visitors are served from cache instead of hitting Copernicus live."""
    import math
    up = _tile_upstream(layer_key)
    if not up:
        return 0
    _url, layer, _ttl = up
    bbox = FIRES_CFG.get("bbox")
    if not bbox:
        return 0
    time_val = _tile_warm_time(layer_key)
    R = 6378137.0
    def merc(lon, lat):
        return (R * math.radians(lon),
                R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))
    jobs = []
    for z in zooms:
        nt = 2 ** z
        for x, y in _bbox_tiles(bbox, z):
            # tile lon/lat bounds → web-mercator bbox
            lon0 = x / nt * 360.0 - 180.0
            lon1 = (x + 1) / nt * 360.0 - 180.0
            lat0 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / nt))))
            lat1 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / nt))))
            xa, ya = merc(lon0, lat1)
            xb, yb = merc(lon1, lat0)
            jobs.append(_tile_wms_params((xa, ya, xb, yb), layer, time_val))

    # Each tile is ~4s of waiting on Copernicus, so a sequential warm of all five
    # layers ran ~10 minutes — long enough that the cache was still filling when
    # visitors arrived. The work is pure I/O wait, so a small pool collapses it;
    # kept modest to stay polite to a free public service (this runs hourly).
    with ThreadPoolExecutor(max_workers=_TILE_WARM_WORKERS) as pool:
        results = pool.map(lambda a: _fetch_tile(layer_key, a, save=True), jobs)
        warmed = sum(1 for _png, ok in results if ok)
    if warmed < len(jobs):
        print(f"[tile_warm] {layer_key}: {len(jobs) - warmed} of {len(jobs)} tiles "
              f"failed upstream — those render as holes in the overlay",
              file=sys.stderr)
    return warmed

def _tile_warm_time(layer_key):
    """The `time` value to warm for a time-dimensioned layer (matches what the
    frontend's default view requests). Static layers return None."""
    today = _today_local().date().isoformat()
    if layer_key == "danger":
        return today
    if layer_key == "burnt_area":
        return f"{today[:4]}-01-01/{today}"      # year-to-date (frontend default)
    if layer_key == "s3":
        end = _today_local().date()
        start = (end - pd.Timedelta(days=1)).isoformat()   # recent 48h window
        return f"{start}/{end.isoformat()}"
    return None

def warm_all_tiles():
    """Warm the cache for every enabled overlay layer, then prune old buckets so
    the cache stays bounded. Called by the hourly cron (via /api/fires/tiles/warm)
    and after an on-demand refresh. One layer failing must not sink the rest, so
    each is isolated — the warm is best-effort by nature (it only pre-fills a
    cache that _fetch_tile would otherwise fill on demand)."""
    total = {}
    for key in _FIRES_TILE_LAYERS:
        if not _tile_upstream(key):
            continue
        t0 = time.time()
        try:
            total[key] = _warm_tile_layer(key)
            _prune_tile_buckets(key)   # keep only the newest few day/window buckets
            print(f"[tile_warm] {key}: {total[key]} tiles in {time.time() - t0:.1f}s")
        except Exception:
            total[key] = None
            print(f"[tile_warm] {key} FAILED after {time.time() - t0:.1f}s",
                  file=sys.stderr)
            traceback.print_exc()
    return total

# Serialise warms so an overlapping cron tick / deploy can't launch a second pass
# over the same few hundred upstream tiles.
_TILE_WARM_LOCK = threading.Lock()

@app.route("/api/fires/tiles/warm", methods=["POST", "GET"])
@limiter.limit("6 per hour")
def api_fires_tiles_warm():
    """Kick off a warm of the overlay-tile cache (hourly cron + deploy target).
    Keyed like the other cron-only refresh endpoints so it can't be triggered by
    arbitrary visitors. Returns as soon as the warm starts: a cold rebuild is a
    few hundred live Copernicus/JRC fetches and can run for minutes, which is far
    too long to hold the caller's connection open (it broke the deploy's ssh)."""
    if not _fires_any_enabled():
        return "", 204
    key = request.args.get("key", "")
    if not _TODAY_REFRESH_KEY or key != _TODAY_REFRESH_KEY:
        return Response("Forbidden", status=403)
    if not _TILE_WARM_LOCK.acquire(blocking=False):
        return jsonify({"started": False, "busy": True}), 409

    def _work():
        # Nothing is waiting on this thread, so an unhandled error would vanish
        # silently and leave a half-filled cache with no trace. Log it.
        t0 = time.time()
        try:
            print("[tile_warm] starting")
            warmed = warm_all_tiles()
            print(f"[tile_warm] done in {time.time() - t0:.1f}s: {warmed}")
        except Exception:
            print(f"[tile_warm] ABORTED after {time.time() - t0:.1f}s",
                  file=sys.stderr)
            traceback.print_exc()
        finally:
            _TILE_WARM_LOCK.release()
    threading.Thread(target=_work, daemon=True).start()
    return jsonify({"started": True})

# Serialise on-demand refreshes so concurrent clicks don't launch parallel FIRMS pulls.
_FIRES_REFRESH_LOCK = threading.Lock()

@app.route("/api/fires/refresh", methods=["POST"])
@limiter.limit("4 per minute")
@limiter.limit("30 per hour")
def api_fires_refresh():
    """On-demand: pull today's fresh FIRMS/Sentinel-3 detections server-side, then
    invalidate the in-memory fire data so the next /api/fires/points reflects it.
    Rate-limited (it hits external APIs). The client waits for this to finish, then
    reloads its points. Returns how many new detections were added."""
    if not _feature_enabled("fires_map"):
        return "", 204
    global _FIRES_DF
    if not _FIRES_REFRESH_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "busy": True}), 409
    try:
        import fire_collect
        summary = fire_collect.refresh_today(today_only=True)  # fast: today's window only
        _FIRES_DF = None                      # force reload from the updated CSVs
    except Exception as e:
        return jsonify({"ok": False, "error": e.__class__.__name__}), 500
    finally:
        _FIRES_REFRESH_LOCK.release()
    # Re-warm the "today"-dependent overlay tiles in the background so the map's
    # danger/s3/burnt-area layers reflect the refresh without slowing the button.
    def _warm_today_layers():
        for k in ("danger", "s3", "burnt_area"):
            if _tile_upstream(k):
                _warm_tile_layer(k)
                _prune_tile_buckets(k)   # keep the cache bounded
    threading.Thread(target=_warm_today_layers, daemon=True).start()
    return jsonify(summary)


if __name__ == "__main__":
    _port = int(os.getenv("PORT", 5050))
    print(f"API running at http://127.0.0.1:{_port}")
    app.run(debug=False, host="0.0.0.0", port=_port, threaded=True)
