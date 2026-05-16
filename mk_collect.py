"""
Fetch historical climate data from Open-Meteo for ~20 North Macedonia locations.
Saves one CSV per location, with lat/lon and date range in the filename.

Install dependencies:
    pip install openmeteo-requests requests-cache retry-requests pandas
"""

import openmeteo_requests
import requests_cache
import pandas as pd
from retry_requests import retry
import os
import time
import urllib3

# ── Configuration ────────────────────────────────────────────────────────────

START_DATE = "1950-01-01"
END_DATE   = "2026-04-01"
OUTPUT_DIR = "./data"

DAILY_VARIABLES = [
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
    "precipitation_sum",
    "et0_fao_evapotranspiration",
]

LOCATIONS = [
    {"name": "Skopje",        "lat": 41.9965, "lon": 21.4314, "elevation": 240},
    {"name": "Bitola",        "lat": 41.0314, "lon": 21.3347, "elevation": 589},
    {"name": "Ohrid",         "lat": 41.1231, "lon": 20.8016, "elevation": 695},
    {"name": "Tetovo",        "lat": 42.0092, "lon": 20.9714, "elevation": 468},
    {"name": "Kumanovo",      "lat": 42.1322, "lon": 21.7144, "elevation": 340},
    {"name": "Veles",         "lat": 41.7153, "lon": 21.7753, "elevation": 230},
    {"name": "Strumica",      "lat": 41.4378, "lon": 22.6431, "elevation": 230},
    {"name": "Gostivar",      "lat": 41.7956, "lon": 20.9089, "elevation": 510},
    {"name": "Stip",          "lat": 41.7457, "lon": 22.1961, "elevation": 310},
    {"name": "Kavadarci",     "lat": 41.4331, "lon": 22.0119, "elevation": 270},
    {"name": "Kochani",       "lat": 41.9167, "lon": 22.4167, "elevation": 350},
    {"name": "Kicevo",        "lat": 41.5131, "lon": 20.9589, "elevation": 630},
    {"name": "Gevgelija",     "lat": 41.1414, "lon": 22.5011, "elevation": 55},
    {"name": "Negotino",      "lat": 41.4831, "lon": 22.0894, "elevation": 222},
    {"name": "Debar",         "lat": 41.5239, "lon": 20.5239, "elevation": 670},
    {"name": "Radovis",       "lat": 41.6386, "lon": 22.4647, "elevation": 370},
    {"name": "Berovo",        "lat": 41.7047, "lon": 22.8556, "elevation": 827},
    {"name": "Lazaropole",    "lat": 41.5394, "lon": 20.6956, "elevation": 1330},
    {"name": "Demir_Kapija",  "lat": 41.4042, "lon": 22.2458, "elevation": 110},
    {"name": "Prilep",        "lat": 41.3453, "lon": 21.5550, "elevation": 640},
]

# ── Setup Open-Meteo client with cache + retry ────────────────────────────────

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

cache_session = requests_cache.CachedSession(".openmeteo_cache", expire_after=-1)
cache_session.verify = False  # disable SSL verification (corporate proxy workaround)
retry_session = retry(cache_session, retries=3, backoff_factor=2)
openmeteo = openmeteo_requests.Client(session=retry_session)

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Precompute strict date bounds as date objects
start_date = pd.to_datetime(START_DATE).date()
end_date   = pd.to_datetime(END_DATE).date()

# ── Helpers ──────────────────────────────────────────────────────────────────

def seconds_until_utc_midnight():
    """Seconds from now until next UTC midnight."""
    import datetime as dt
    now = dt.datetime.utcnow()
    midnight = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - now).total_seconds()) + 60  # +60s buffer


def wait_if_rate_limited():
    """Check API status and sleep until the appropriate limit resets."""
    probe = cache_session.get(
        "https://archive-api.open-meteo.com/v1/archive",
        params={"latitude": 41.9, "longitude": 21.4,
                "start_date": "2024-01-01", "end_date": "2024-01-02",
                "daily": "temperature_2m_max", "timezone": "UTC"},
    )
    if probe.from_cache:
        return  # cached response, no actual API call was made
    try:
        body = probe.json()
    except Exception:
        return
    reason = body.get("reason", "").lower() if isinstance(body, dict) else ""
    if "daily" in reason and "limit exceeded" in reason:
        secs = seconds_until_utc_midnight()
        hrs = secs / 3600
        print(f"  Daily API limit hit — waiting {hrs:.1f} hours until UTC midnight...", flush=True)
        time.sleep(secs)
    elif probe.status_code == 429 or ("hourly" in reason and "limit exceeded" in reason):
        print("  Hourly API limit hit — waiting 60 minutes...", flush=True)
        time.sleep(3600)


def fetch_location(loc):
    name      = loc["name"]
    lat       = loc["lat"]
    lon       = loc["lon"]
    elevation = loc["elevation"]

    start_str = START_DATE.replace("-", "")
    end_str   = END_DATE.replace("-", "")
    filename  = f"{name}_{lat}_{lon}_{start_str}_{end_str}.csv"
    filepath  = os.path.join(OUTPUT_DIR, filename)

    if os.path.exists(filepath):
        print(f"Skipping {name} — file already exists: {filepath}", flush=True)
        return

    print(f"Fetching {name} ({lat}, {lon})...", flush=True)

    wait_if_rate_limited()

    params = {
        "latitude":   lat,
        "longitude":  lon,
        "start_date": START_DATE,
        "end_date":   END_DATE,
        "daily":      DAILY_VARIABLES,
        "timezone":   "UTC",
    }

    try:
        responses = openmeteo.weather_api(
            "https://archive-api.open-meteo.com/v1/archive", params=params
        )
        response = responses[0]

        era5_elevation = response.Elevation()
        elev_diff = era5_elevation - elevation
        print(f"  ERA5-Land model elevation : {era5_elevation:.1f} m", flush=True)
        print(f"  Station elevation         : {elevation} m", flush=True)
        print(f"  Difference (ERA5-station) : {elev_diff:.1f} m", flush=True)

        daily = response.Daily()

        df = pd.DataFrame({
            "date": pd.date_range(
                start=pd.to_datetime(daily.Time(),    unit="s", utc=True),
                end=pd.to_datetime(daily.TimeEnd(),   unit="s", utc=True),
                freq=pd.Timedelta(seconds=daily.Interval()),
                inclusive="left",
            ).date,
            "temperature_max":        daily.Variables(0).ValuesAsNumpy(),
            "temperature_min":        daily.Variables(1).ValuesAsNumpy(),
            "temperature_mean":       daily.Variables(2).ValuesAsNumpy(),
            "precipitation_sum":      daily.Variables(3).ValuesAsNumpy(),
            "et0_evapotranspiration": daily.Variables(4).ValuesAsNumpy(),
        })

        before = len(df)
        df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
        dropped = before - len(df)
        if dropped:
            print(f"  Dropped {dropped} out-of-range row(s) (timezone artifact)", flush=True)

        print(f"  Date range in CSV: {df['date'].iloc[0]} -> {df['date'].iloc[-1]}", flush=True)

        df.insert(0, "location",            name)
        df.insert(1, "latitude",            lat)
        df.insert(2, "longitude",           lon)
        df.insert(3, "elevation_station_m", elevation)
        df.insert(4, "elevation_era5_m",    round(era5_elevation, 1))
        df.insert(5, "elevation_diff_m",    round(elev_diff, 1))

        df.to_csv(filepath, index=False)
        print(f"  Saved {len(df)} rows -> {filepath}", flush=True)

    except Exception as e:
        err = str(e).lower()
        if "daily" in err and "limit exceeded" in err:
            secs = seconds_until_utc_midnight()
            hrs = secs / 3600
            print(f"  Daily API limit hit — waiting {hrs:.1f} hours until UTC midnight, then will retry {name}...", flush=True)
            time.sleep(secs)
            # Re-queue this location by recursing once
            fetch_location(loc)
            return
        elif "hourly" in err and "limit exceeded" in err:
            print(f"  Hourly API limit hit — waiting 60 minutes, then will retry {name}...", flush=True)
            time.sleep(3600)
            fetch_location(loc)
            return
        else:
            print(f"  Failed for {name}: {e}", flush=True)

    print(f"  Waiting 65 seconds before next request...", flush=True)
    time.sleep(65)


# ── Fetch and save ────────────────────────────────────────────────────────────

for loc in LOCATIONS:
    fetch_location(loc)

print("\nDone! All files saved to:", os.path.abspath(OUTPUT_DIR))