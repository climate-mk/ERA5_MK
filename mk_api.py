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
from scipy.stats import theilslopes
import pymannkendall as mk_test
import warnings
warnings.filterwarnings("ignore")
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

# ── Direct Line config ─────────────────────────────────────────────────────────

DIRECT_LINE_SECRET   = os.getenv("DIRECT_LINE_SECRET", "")
_DL_GENERATE_URL     = "https://europe.directline.botframework.com/v3/directline/tokens/generate"
_DL_REFRESH_URL      = "https://europe.directline.botframework.com/v3/directline/tokens/refresh"
_TOKEN_CACHE_BUFFER  = 300   # treat token as expired if < 5 min remaining

# Rate limits — change these two strings to tune the /api/token endpoints
TOKEN_LIMIT_MINUTE = "10 per minute"
TOKEN_LIMIT_HOUR   = "200 per hour"

# ── Load data ──────────────────────────────────────────────────────────────────

DATA_DIR = "./data"
dfs = [pd.read_csv(f, parse_dates=["date"])
       for f in sorted(glob.glob(os.path.join(DATA_DIR, "*.csv")))]
data = pd.concat(dfs, ignore_index=True)
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

    # Raw data for fitting
    is_sum = col in ["precipitation_sum","et0_evapotranspiration"]
    x_raw, y_raw = (x_arr, y_arr) if is_sum else window_raw(ld, month, day, half_window, col)

    x_line = np.linspace(x_arr.min(), x_arr.max(), 300)

    if method == "ols":
        slope, intercept, _, _, _ = stats.linregress(x_raw, y_raw)
        _, _, r_ann, p_val, _     = stats.linregress(x_arr, y_arr)
        y_line    = slope * x_line + intercept
        residuals = y_raw - (slope * x_raw + intercept)
        se_res    = np.sqrt(np.sum(residuals**2) / max(len(x_raw) - 2, 1))
        ss_x      = np.sum((x_raw - x_raw.mean())**2)
        t_crit    = stats.t.ppf(0.975, df=max(len(x_arr) - 2, 1))
        se_ln     = se_res * np.sqrt(1/len(x_raw) + (x_line - x_raw.mean())**2 / max(ss_x, 1e-12))
        upper, lower = y_line + t_crit * se_ln, y_line - t_crit * se_ln
        metric, metric_lbl, ar1 = r_ann**2, "R²", None
    else:
        res    = theilslopes(y_raw, x_raw, 0.95)
        slope  = res.slope
        mk_r   = mk_test.yue_wang_modification_test(y_arr)
        p_val, tau = mk_r.p, mk_r.Tau
        x_med, y_med = float(np.median(x_raw)), float(np.median(y_raw))
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
    fit_desc  = (f"Fitted on {len(x_arr)} annual sums (1/year)" if is_sum
                 else f"Fitted on {len(x_raw)} daily values ({len(x_arr)} years)")
    if ar1 is not None:
        fit_desc += f"  ·  AR(1)={ar1:.2f}"

    return {
        "loc": loc,
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
            r, g, b = vs["cal_pos"] if metric >= 0 else vs["cal_neg"]
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
