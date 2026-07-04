"""
Fetch historical + near-real-time wildfire detections for the /fires page.

Sources (all free):
  • NASA FIRMS country API — MODIS + VIIRS (SNPP / NOAA-20 / NOAA-21) active-fire
    detections. Needs FIRMS_MAP_KEY in .env. Capped at 10 days per request, so
    the backfill runs a chunked loop over each sensor's own date range.
  • Copernicus EFFIS WFS — Sentinel-3 SLSTR hotspots (blended into EFFIS's
    active-fire hotspot layer), pulled as GeoJSON. No key. Rolling window only
    (no deep archive), so Sentinel-3 history grows forward from first collection.

One CSV per sensor under data/<COUNTRY>/fires/ (MODIS.csv, VIIRS_SNPP.csv,
VIIRS_NOAA20.csv, VIIRS_NOAA21.csv, SENTINEL3.csv), all with the same normalised
schema:  sensor, acq_date, acq_time, latitude, longitude, confidence, frp, daynight

Differential by default (fetch from the day after each file's last date);
--force-refresh rebuilds every file from the source's start date.

Install dependencies:
    pip install requests pandas python-dotenv
"""

import os
import sys
import time
import json
import argparse
import io
from datetime import datetime, timedelta, date, timezone

import requests
import pandas as pd
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

load_dotenv()

# Force UTF-8 stdout so log output (arrows, °, etc.) survives Windows cp1252 pipes.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from config import CONFIG

# ── CLI ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(
    description=f"Fetch wildfire detections (FIRMS + Sentinel-3) for {CONFIG['name']}."
)
parser.add_argument(
    "--force-refresh", action="store_true",
    help="Rebuild every sensor file from its source start date (ignores existing files).",
)
parser.add_argument(
    "--verbose", action="store_true", help="Verbose output for debugging.",
)
# parse_known_args so importing this module (e.g. from mk_api.py to run an
# on-demand refresh) doesn't choke on the host process's own argv.
args, _ = parser.parse_known_args()

# ── Config / constants ────────────────────────────────────────────────────────

FIRES_CFG   = CONFIG.get("fires")
if not FIRES_CFG:
    sys.exit("No 'fires:' block in the country config — nothing to collect.")

FIRMS_KEY   = os.getenv("FIRMS_MAP_KEY", "").strip()
ISO3        = FIRES_CFG["iso3"]
BBOX        = FIRES_CFG["bbox"]                 # [W, S, E, N]
BBOX_STR    = ",".join(str(x) for x in BBOX)    # FIRMS area endpoint: W,S,E,N
OUTPUT_DIR  = os.path.join("data", CONFIG["code"], "fires")
GAPS_PATH   = os.path.join(OUTPUT_DIR, "_gaps.json")
STAMP_PATH  = os.path.join(OUTPUT_DIR, "_last_collected.json")
TIMEZONE    = CONFIG.get("timezone", "UTC")

# FIRMS serves near-real-time for ~the last 2 months; older dates need the
# standard-processing (_SP) source. Use SP up to this many days back, NRT after.
NRT_CUTOFF_DAYS = 60
# The FIRMS /api/area CSV endpoint caps a single request at 5 days.
CHUNK_DAYS      = 5
# FIRMS is generous vs Open-Meteo; a short pause is enough to be polite.
REQUEST_PAUSE   = 1.0
# On differential runs, always re-fetch the last N days (incl. today) so the map
# stays current as today's near-real-time detections keep arriving.
REFETCH_DAYS    = 3
# Include today: FIRMS near-real-time data accrues through the current day as
# satellites pass, and the hourly cron re-fetches today so the map always shows
# the latest detections. (Today's row count grows during the day — that's fine.)
END_DATE        = datetime.now(timezone.utc).date()

# The /api/country endpoint currently returns "Invalid API call"; the /api/area
# endpoint (bbox-filtered) works and is more precise for a small country anyway.
FIRMS_AREA_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

# Normalised output schema (order matters for the CSV).
COLUMNS = ["sensor", "acq_date", "acq_time", "latitude", "longitude",
           "confidence", "frp", "daynight"]

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Gap log (self-healing re-attempt of failed/empty windows) ─────────────────


def _load_gaps():
    try:
        with open(GAPS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_collection_stamp():
    """Record when fire data was last collected (local timezone ISO string).
    Read by the API so the /fires page can show true data freshness."""
    try:
        now = pd.Timestamp.now(tz=TIMEZONE).isoformat()
        with open(STAMP_PATH, "w", encoding="utf-8") as f:
            json.dump({"collected_at": now, "tz": TIMEZONE}, f)
    except Exception:
        pass


def read_collection_stamp():
    """Return the last-collection stamp dict, or None."""
    try:
        with open(STAMP_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_gaps(gaps):
    try:
        with open(GAPS_PATH, "w", encoding="utf-8") as f:
            json.dump(gaps, f, indent=2)
    except Exception as e:
        if args.verbose:
            print(f"    [DEBUG] could not write gap log: {e}")


# ── Helpers ────────────────────────────────────────────────────────────────────


def file_for(sensor_key):
    return os.path.join(OUTPUT_DIR, f"{sensor_key}.csv")


def reset_sensor_file(filepath):
    """Delete a sensor's CSV so a --force-refresh run rebuilds it from scratch
    while merge_and_save still accumulates every chunk within the run."""
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
    except Exception:
        pass


def last_date_in(filepath):
    """Latest acq_date in an existing sensor CSV, or None."""
    try:
        df = pd.read_csv(filepath, usecols=["acq_date"])
        if df.empty:
            return None
        return pd.to_datetime(df["acq_date"]).max().date()
    except Exception:
        return None


def date_chunks(start, end, size=CHUNK_DAYS):
    """Yield (chunk_start, chunk_len_days) covering start..end inclusive."""
    cur = start
    while cur <= end:
        span = min(size, (end - cur).days + 1)
        yield cur, span
        cur = cur + timedelta(days=span)


def merge_and_save(filepath, df_new):
    """Append df_new to the existing file, dedup, sort, write.

    Always accumulates: each collection chunk appends to what's already on disk.
    (A --force-refresh run truncates the file once at the start of the sensor via
    reset_sensor_file(), so it rebuilds cleanly while still accumulating chunks.)
    Guards against a transient empty refetch erasing populated history: an empty
    df_new never removes existing rows.
    """
    if df_new is None or df_new.empty:
        return 0
    df_new = df_new[COLUMNS]
    if os.path.exists(filepath):
        try:
            df_old = pd.read_csv(filepath)
            df_all = pd.concat([df_old, df_new], ignore_index=True)
        except Exception:
            df_all = df_new
    else:
        df_all = df_new
    before = len(df_all)
    df_all = df_all.drop_duplicates(
        subset=["sensor", "acq_date", "acq_time", "latitude", "longitude"],
        keep="last",
    )
    df_all = df_all.sort_values(["acq_date", "acq_time"]).reset_index(drop=True)
    df_all.to_csv(filepath, index=False)
    return len(df_all) - (before - len(df_new))  # net new rows added


# ── FIRMS fetch ────────────────────────────────────────────────────────────────


def firms_source_for(sp, nrt, chunk_start):
    """Pick SP (archive) vs NRT (recent) source for a chunk start date.

    NOAA-20/21 have sp=None → always NRT (no deep archive).
    """
    days_back = (END_DATE - chunk_start).days
    if days_back > NRT_CUTOFF_DAYS and sp:
        return sp
    return nrt


def normalise_firms(df, sensor_key):
    """Map a FIRMS CSV (MODIS or VIIRS schema) onto the common COLUMNS."""
    out = pd.DataFrame()
    out["sensor"]    = [sensor_key] * len(df)
    out["acq_date"]  = df.get("acq_date")
    out["acq_time"]  = df.get("acq_time")
    out["latitude"]  = df.get("latitude")
    out["longitude"] = df.get("longitude")
    out["confidence"] = df.get("confidence")
    out["frp"]       = df.get("frp")
    out["daynight"]  = df.get("daynight")
    return out


def fetch_firms_chunk(source, chunk_start, span):
    """One FIRMS area CSV request (bbox-filtered). Returns a DataFrame or None."""
    url = f"{FIRMS_AREA_URL}/{FIRMS_KEY}/{source}/{BBOX_STR}/{span}/{chunk_start.isoformat()}"
    try:
        resp = requests.get(url, timeout=60)
    except Exception as e:
        if args.verbose:
            print(f"    [DEBUG] request error {source} {chunk_start}: {e}")
        return None
    if resp.status_code != 200:
        if args.verbose:
            print(f"    [DEBUG] HTTP {resp.status_code} {source} {chunk_start}")
        return None
    text = resp.text.strip()
    # FIRMS returns a plain-text error string (not CSV) on invalid key/params.
    if not text or text.lower().startswith(("invalid", "error")) or "," not in text.split("\n", 1)[0]:
        if args.verbose:
            print(f"    [DEBUG] non-CSV response {source} {chunk_start}: {text[:80]}")
        return None
    try:
        return pd.read_csv(io.StringIO(text))
    except Exception:
        return None


def collect_firms_sensor(src):
    """Backfill/update one FIRMS sensor file across chunked windows.

    FIRE_HISTORY_START env var (YYYY-MM-DD) can raise the earliest backfill date
    for a quick partial run without editing the config; the sensor's own start is
    still respected when it is later than the override."""
    key   = src["key"]
    sp    = src.get("sp")
    nrt   = src["nrt"]
    start = pd.to_datetime(src["start"]).date()
    _floor = os.getenv("FIRE_HISTORY_START")
    if _floor:
        start = max(start, pd.to_datetime(_floor).date())
    fp    = file_for(key)

    if args.force_refresh:
        reset_sensor_file(fp)          # rebuild from scratch; chunks then accumulate
    else:
        last = last_date_in(fp)
        if last is not None:
            # Re-fetch the last few days (incl. today) rather than resuming strictly
            # after the last stored date: today's NRT detections keep arriving during
            # the day, so a rolling re-fetch keeps "today" current. Dedup on save
            # makes the overlap harmless.
            resume = last + timedelta(days=1)
            rolling = END_DATE - timedelta(days=REFETCH_DAYS)
            start = min(resume, rolling)

    if start > END_DATE:
        print(f"  {key}: up-to-date (through {END_DATE}).", flush=True)
        return 0

    print(f"  {key}: fetching {start} -> {END_DATE} "
          f"({(END_DATE - start).days + 1} days, {span_count(start)} chunks)...", flush=True)

    gaps = _load_gaps()
    sensor_gaps = set(gaps.get(key, []))
    total_new = 0

    for chunk_start, span in date_chunks(start, END_DATE):
        source = firms_source_for(sp, nrt, chunk_start)
        df = fetch_firms_chunk(source, chunk_start, span)
        if df is None:
            sensor_gaps.add(chunk_start.isoformat())   # remember to retry later
            time.sleep(REQUEST_PAUSE)
            continue
        sensor_gaps.discard(chunk_start.isoformat())   # succeeded → clear if listed
        if not df.empty:
            total_new += merge_and_save(fp, normalise_firms(df, key))
        time.sleep(REQUEST_PAUSE)

    # Re-attempt previously failed windows that are now (hopefully) available.
    for iso in sorted(sensor_gaps.copy()):
        cs = pd.to_datetime(iso).date()
        if cs > END_DATE:
            continue
        source = firms_source_for(sp, nrt, cs)
        df = fetch_firms_chunk(source, cs, min(CHUNK_DAYS, (END_DATE - cs).days + 1))
        if df is not None:
            sensor_gaps.discard(iso)
            if not df.empty:
                total_new += merge_and_save(fp, normalise_firms(df, key))
        time.sleep(REQUEST_PAUSE)

    gaps[key] = sorted(sensor_gaps)
    _save_gaps(gaps)
    print(f"  {key}: +{total_new} new detections "
          f"({len(sensor_gaps)} window(s) still pending retry).", flush=True)
    return total_new


def span_count(start):
    return (END_DATE - start).days // CHUNK_DAYS + 1


# ── Sentinel-3 via EFFIS WFS ───────────────────────────────────────────────────


def _ln(tag):
    """Local name of an XML tag (strip the {namespace})."""
    return tag.rsplit("}", 1)[-1]


def collect_sentinel3():
    """Pull Sentinel-3 (SLSTR) hotspots from the EFFIS WFS.

    This MapServer 502s on WFS 2.0/1.1 JSON output, but WFS 1.0.0 + GML works, so
    we request version 1.0.0 and parse the GML featureMembers. EFFIS keeps a
    rolling window (not a deep archive), so this fetches the currently-available
    window and merges it forward. On failure we log a gap for retry; the map still
    shows Sentinel-3 via the WMS overlay (s3.hs) regardless.
    """
    wfs   = FIRES_CFG.get("effis_wfs")
    layer = FIRES_CFG.get("effis_s3_wfs_layer") or FIRES_CFG.get("effis_s3_layer")
    ver   = FIRES_CFG.get("effis_wfs_version", "1.0.0")
    if not (wfs and layer):
        print("  SENTINEL3: no effis_wfs/effis_s3 layer configured — skipping.", flush=True)
        return 0
    w, s, e, n = BBOX
    params = {
        "service": "WFS", "version": ver, "request": "GetFeature",
        "typeName": layer,
        "bbox": f"{w},{s},{e},{n}",   # WFS 1.0.0 EPSG:4326 bbox is lon,lat (W,S,E,N)
    }
    try:
        resp = requests.get(wfs, params=params, timeout=90,
                            headers={"User-Agent": "climate.mk/1.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        gaps = _load_gaps()
        gaps["SENTINEL3"] = ["retry"]   # marker: WFS window still owed
        _save_gaps(gaps)
        print(f"  SENTINEL3: EFFIS WFS unavailable ({e.__class__.__name__}) — "
              f"logged for retry; map still shows S-3 via WMS overlay.", flush=True)
        return 0
    # Success → clear any pending retry marker.
    _gaps = _load_gaps()
    if _gaps.pop("SENTINEL3", None) is not None:
        _save_gaps(_gaps)

    rows = []
    for member in (el for el in root.iter() if _ln(el.tag) == "featureMember"):
        feat = list(member)
        if not feat:
            continue
        fields = {}
        for child in feat[0]:
            fields[_ln(child.tag)] = (child.text or "").strip()
        # confidence field is textual (Low/Nominal/High) on this layer
        rows.append({
            "sensor":     "SENTINEL3",
            "acq_date":   fields.get("acq_date"),
            "acq_time":   fields.get("acq_time", ""),
            "latitude":   fields.get("lat"),
            "longitude":  fields.get("lon"),
            "confidence": fields.get("confidence", ""),
            "frp":        fields.get("frp", ""),
            "daynight":   fields.get("night", ""),
        })
    rows = [r for r in rows if r["acq_date"] and r["latitude"] and r["longitude"]]
    if not rows:
        print("  SENTINEL3: EFFIS WFS returned no features for the bbox.", flush=True)
        return 0
    added = merge_and_save(file_for("SENTINEL3"), pd.DataFrame(rows))
    print(f"  SENTINEL3: +{added} new detections (EFFIS WFS {ver}, rolling window).", flush=True)
    return added


# ── Run ────────────────────────────────────────────────────────────────────────

def fetch_today_only(src):
    """Fetch just today's window for one FIRMS sensor (fast path for the on-demand
    'Refresh now' button). One request per sensor, merged with dedup."""
    key   = src["key"]
    sp    = src.get("sp")
    nrt   = src["nrt"]
    span  = 1
    source = firms_source_for(sp, nrt, END_DATE)
    df = fetch_firms_chunk(source, END_DATE, span)
    if df is None or df.empty:
        return 0
    return merge_and_save(file_for(key), normalise_firms(df, key))


def refresh_today(today_only=False):
    """Pull fresh detections and return a small summary dict. Importable by the API.

    today_only=True  → one request per sensor for just today (fast; the 'Refresh
                       now' button uses this).
    today_only=False → full differential incl. the rolling re-fetch window (the
                       hourly cron path).
    Never raises — collection errors are swallowed so the caller can still respond."""
    args.force_refresh = False
    total = 0
    try:
        if FIRMS_KEY:
            for src in FIRES_CFG["firms_sources"]:
                total += (fetch_today_only(src) if today_only
                          else collect_firms_sensor(src)) or 0
        # Sentinel-3 (EFFIS WFS) returns the whole rolling window each call, so it's
        # slow — skip it on the fast on-demand path (its WMS overlay stays live).
        if FIRES_CFG.get("sentinel3") and not today_only:
            total += collect_sentinel3() or 0
    except Exception as e:
        return {"ok": False, "error": e.__class__.__name__, "new": total}
    write_collection_stamp()
    stamp = read_collection_stamp()
    return {"ok": True, "new": total, "through": END_DATE.isoformat(),
            "collected_at": stamp.get("collected_at") if stamp else None}


def main():
    mode = "[FORCE REFRESH]" if args.force_refresh else "[DIFFERENTIAL]"
    print(f"\nWildfire collection {mode} — {CONFIG['name']} ({ISO3}), through {END_DATE}\n")

    if FIRMS_KEY:
        for src in FIRES_CFG["firms_sources"]:
            collect_firms_sensor(src)
    else:
        print("  FIRMS_MAP_KEY not set — skipping FIRMS sensors "
              "(get a free key at firms.modaps.eosdis.nasa.gov/api/map_key/).", flush=True)

    if FIRES_CFG.get("sentinel3"):
        collect_sentinel3()

    write_collection_stamp()
    print(f"\nDone. Files in {os.path.abspath(OUTPUT_DIR)}")


if __name__ == "__main__":
    main()
