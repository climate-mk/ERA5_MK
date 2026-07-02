"""
Pre-compute ERA5-Land statistics into CSVs for the Datasette / Podnebnik stack.

Run after mk_collect.py:
    COUNTRY=si python3 mk_precompute.py

Writes to data/era5-slovenia/data/:
  si_<Station>.csv          — raw daily ERA5 data (copy of data/si/*.csv)
  si_daily_window.csv       — ±7-day window percentile cutoffs + KDE per (station, month, day)
  si_annual_trend.csv       — national Theil-Sen annual trend per calendar (month, day)
"""

import glob, json, os, sys, warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import pymannkendall as mk_test
from scipy.stats import gaussian_kde, theilslopes

warnings.filterwarnings("ignore")

# ── Config ─────────────────────────────────────────────────────────────────────

import yaml
COUNTRY = os.environ.get("COUNTRY", "si")
with open(f"countries/{COUNTRY}.yaml") as f:
    CONFIG = yaml.safe_load(f)

DATA_DIR   = Path("data") / CONFIG["code"]
OUT_DIR    = Path("data") / "era5-slovenia" / "data"
LAPSE_RATE = 0.0065
TREND_START_YEAR = CONFIG.get("trend_start_year", 1950)
PROJ_END_YEAR    = CONFIG.get("projection_end_year", 2050)
MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun",
               "Jul","Aug","Sep","Oct","Nov","Dec"]

# ── Load all station CSVs ──────────────────────────────────────────────────────

def _load_csv(filepath: str) -> pd.DataFrame:
    df = pd.read_csv(filepath)
    df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d")
    return df

def load_all() -> pd.DataFrame:
    dfs = [_load_csv(f) for f in sorted(glob.glob(str(DATA_DIR / "*.csv")))]
    data = pd.concat(dfs, ignore_index=True)
    data = data[data["date"] <= pd.Timestamp.today()]
    data["year"]  = data["date"].dt.year
    data["month"] = data["date"].dt.month
    for c in ["temperature_max", "temperature_min", "temperature_mean"]:
        data[c + "_corr"] = data[c] + data["elevation_diff_m"] * LAPSE_RATE
    return data

# ── Window filter (mirrors mk_api.py) ─────────────────────────────────────────

def window_filter(loc_data: pd.DataFrame, month: int, day: int, half_window: int) -> pd.DataFrame:
    try:
        target_doy = pd.Timestamp(2001, month, day).dayofyear
    except ValueError:
        target_doy = pd.Timestamp(2001, month, 28).dayofyear
    row_doy   = loc_data["date"].dt.dayofyear.to_numpy()
    raw_diff  = (row_doy - target_doy).astype(int)
    circ_diff = ((raw_diff + 182) % 365) - 182
    in_win    = np.abs(circ_diff) <= half_window
    out       = loc_data[in_win].copy()
    rd_out    = raw_diff[in_win]
    year_adj  = np.where(rd_out >  182, 1, np.where(rd_out < -182, -1, 0))
    out["_window_year"] = out["year"].to_numpy() + year_adj
    return out

def window_series(loc_data: pd.DataFrame, month: int, day: int,
                  half_window: int, col: str) -> pd.Series:
    sub    = window_filter(loc_data, month, day, half_window)
    agg_fn = "sum" if col in ["precipitation_sum", "et0_evapotranspiration"] else "mean"
    return sub.groupby("_window_year")[col].agg(agg_fn).dropna()

# ── 1. Daily window stats ──────────────────────────────────────────────────────

def _compute_daily_window_row(station: str, loc_data: pd.DataFrame,
                               month: int, day: int) -> dict | None:
    """Percentile cutoffs + KDE for one (station, month, day) with ±7-day window."""
    window    = window_filter(loc_data, month, day, 7)
    daily_max = window.groupby("date")["temperature_max"].max().dropna()
    samples   = daily_max.to_numpy()
    if len(samples) < 50:
        return None

    cutoffs = {
        "p5":  round(float(np.percentile(samples,  5)), 2),
        "p10": round(float(np.percentile(samples, 10)), 2),
        "p20": round(float(np.percentile(samples, 20)), 2),
        "p50": round(float(np.percentile(samples, 50)), 2),
        "p80": round(float(np.percentile(samples, 80)), 2),
        "p95": round(float(np.percentile(samples, 95)), 2),
    }
    smin, smax = float(samples.min()), float(samples.max())
    pad    = max((smax - smin) * 0.05, 0.5)
    x_grid = np.linspace(smin - pad, smax + pad, 200)
    try:
        density = gaussian_kde(samples)(x_grid)
    except Exception:
        density = np.zeros_like(x_grid)
    distribution = [[round(float(x), 3), round(float(d), 6)]
                    for x, d in zip(x_grid, density)]

    return {
        "station":           station,
        "month":             month,
        "day":               day,
        "p5":                cutoffs["p5"],
        "p10":               cutoffs["p10"],
        "p20":               cutoffs["p20"],
        "p50":               cutoffs["p50"],
        "p80":               cutoffs["p80"],
        "p95":               cutoffs["p95"],
        "n_samples":         int(len(samples)),
        "year_min":          int(loc_data["year"].min()),
        "year_max":          int(loc_data["year"].max()),
        "distribution_json": json.dumps(distribution, separators=(",", ":")),
    }


def compute_daily_window(data: pd.DataFrame) -> pd.DataFrame:
    rows = []
    stations = sorted(data["location"].unique())
    total = len(stations) * 365
    done  = 0
    for station in stations:
        loc_data = data[data["location"] == station]
        for month in range(1, 13):
            for day in range(1, 32):
                try:
                    pd.Timestamp(2001, month, day)
                except ValueError:
                    continue
                row = _compute_daily_window_row(station, loc_data, month, day)
                if row:
                    rows.append(row)
                done += 1
                if done % 100 == 0:
                    pct = done / total * 100
                    print(f"  daily_window {done}/{total} ({pct:.0f}%)", end="\r", flush=True)
    print()
    return pd.DataFrame(rows)

# ── 2. National annual trend ───────────────────────────────────────────────────

def _compute_annual_trend_row(data: pd.DataFrame, month: int, day: int) -> dict | None:
    """National Theil-Sen trend for one (month, day).  Mirrors compute_annual_trend."""
    window     = window_filter(data, month, day, 30)
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
    annual = annual_raw[annual_raw.index >= TREND_START_YEAR]
    if len(annual) < 10:
        return None

    x_arr = annual.index.to_numpy(float)
    y_arr = annual.values
    first_yr, last_yr = int(x_arr.min()), int(x_arr.max())

    res    = theilslopes(y_arr, x_arr, 0.95)
    slope  = res.slope
    x_med  = float(np.median(x_arr))
    y_med  = float(np.median(y_arr))
    ic     = y_med - slope          * x_med
    ic_hi  = y_med - res.high_slope * x_med
    ic_lo  = y_med - res.low_slope  * x_med
    mk_r   = mk_test.yue_wang_modification_test(y_arr)

    x_hist = np.linspace(x_arr.min(), x_arr.max(), 300)
    y_hist = slope          * x_hist + ic
    u_hist = res.high_slope * x_hist + ic_hi
    l_hist = res.low_slope  * x_hist + ic_lo

    x_fc = np.linspace(last_yr, PROJ_END_YEAR, 200)
    y_fc = slope          * x_fc + ic
    u_fc = res.high_slope * x_fc + ic_hi
    l_fc = res.low_slope  * x_fc + ic_lo

    scatter = [{"x": int(yr), "y": round(float(v), 2)} for yr, v in zip(x_arr, y_arr)]
    dlabel  = f"{MONTH_NAMES[month - 1]} {day}"

    return {
        "month":        month,
        "day":          day,
        "day_label":    dlabel,
        "year_min":     first_yr,
        "year_max":     last_yr,
        "trend10":      round(float(slope * 10), 3),
        "p_val":        round(float(mk_r.p), 5),
        "tau":          round(float(mk_r.Tau), 3),
        "n_years":      int(len(x_arr)),
        "scatter_json": json.dumps(scatter, separators=(",", ":")),
        "hist_x_json":      json.dumps([round(v, 2) for v in x_hist.tolist()], separators=(",", ":")),
        "hist_y_json":      json.dumps([round(v, 3) for v in y_hist.tolist()], separators=(",", ":")),
        "hist_upper_json":  json.dumps([round(v, 3) for v in u_hist.tolist()], separators=(",", ":")),
        "hist_lower_json":  json.dumps([round(v, 3) for v in l_hist.tolist()], separators=(",", ":")),
        "proj_x_json":      json.dumps([round(v, 2) for v in x_fc.tolist()],  separators=(",", ":")),
        "proj_y_json":      json.dumps([round(v, 3) for v in y_fc.tolist()],  separators=(",", ":")),
        "proj_upper_json":  json.dumps([round(v, 3) for v in u_fc.tolist()],  separators=(",", ":")),
        "proj_lower_json":  json.dumps([round(v, 3) for v in l_fc.tolist()],  separators=(",", ":")),
    }


def compute_annual_trends(data: pd.DataFrame) -> pd.DataFrame:
    rows = []
    total = 365
    done  = 0
    for month in range(1, 13):
        for day in range(1, 32):
            try:
                pd.Timestamp(2001, month, day)
            except ValueError:
                continue
            row = _compute_annual_trend_row(data, month, day)
            if row:
                rows.append(row)
            done += 1
            if done % 10 == 0:
                print(f"  annual_trend {done}/{total} ({done/total*100:.0f}%)", end="\r", flush=True)
    print()
    return pd.DataFrame(rows)

# ── 3. Copy raw station CSVs ───────────────────────────────────────────────────

def copy_station_csvs() -> list[str]:
    copied = []
    for src in sorted(glob.glob(str(DATA_DIR / "*.csv"))):
        name = Path(src).stem        # e.g. "Ljubljana"
        dst  = OUT_DIR / f"si_{name}.csv"
        df   = pd.read_csv(src)
        df.to_csv(dst, index=False)
        copied.append(f"si_{name}")
        print(f"  copied {name} → {dst.name}")
    return copied

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading ERA5 station data…")
    data = load_all()
    print(f"  {len(data):,} rows, {data['location'].nunique()} stations, "
          f"{data['year'].min()}–{data['year'].max()}")

    print("\n[1/3] Copying raw station CSVs…")
    copy_station_csvs()

    print("\n[2/3] Computing daily window stats (±7 day, per station × day)…")
    dw = compute_daily_window(data)
    out_dw = OUT_DIR / "si_daily_window.csv"
    dw.to_csv(out_dw, index=False)
    print(f"  wrote {len(dw):,} rows → {out_dw}")

    print("\n[3/3] Computing national annual trend (per calendar day)…")
    at = compute_annual_trends(data)
    out_at = OUT_DIR / "si_annual_trend.csv"
    at.to_csv(out_at, index=False)
    print(f"  wrote {len(at):,} rows → {out_at}")

    print("\nDone. Run: uv run invoke create-databases")

if __name__ == "__main__":
    main()
