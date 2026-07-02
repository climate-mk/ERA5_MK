"""
Minimal Flask sidecar for live Open-Meteo endpoints.
Reads pre-computed cutoffs from var/sqlite/era5-slovenia.db.

Run:
    COUNTRY=si python3 mk_sidecar.py
Serves on http://127.0.0.1:5052
"""

import datetime, json, os, sqlite3, warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import requests as http_requests
import yaml
from flask import Flask, jsonify, request
from flask_cors import CORS

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
                # National aggregate: average the cutoffs across all stations
                row = conn.execute(
                    """SELECT
                         AVG(p5) AS p5, AVG(p10) AS p10, AVG(p20) AS p20,
                         AVG(p50) AS p50, AVG(p80) AS p80, AVG(p95) AS p95,
                         SUM(n_samples) AS n_samples,
                         MIN(year_min) AS year_min, MAX(year_max) AS year_max
                       FROM si_daily_window WHERE month=? AND day=?""",
                    (month, day),
                ).fetchone()
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


if __name__ == "__main__":
    print(f"Sidecar listening on http://127.0.0.1:{PORT}")
    app.run(host="127.0.0.1", port=PORT, debug=False)
