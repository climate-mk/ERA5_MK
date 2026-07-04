"""
Nightly refresh pipeline for the Podnebnik / ERA5-Slovenia stack.

Run manually or via cron (see cron/podnebnik_collect):
    COUNTRY=si python3 mk_refresh.py

Steps:
  1. mk_collect  — fetch latest ERA5-Land CSVs from Open-Meteo
  2. mk_precompute — rebuild per-station and aggregate stat CSVs
  3. rebuild_db — import CSVs into var/sqlite/era5-slovenia.db
  4. restart services (only when run as root/via cron)
"""

import glob, io, json, os, shutil, sqlite3, subprocess, sys
from pathlib import Path

COUNTRY   = os.environ.get("COUNTRY", "si")
BASE      = Path(__file__).parent
SQLITE_DIR = BASE / "var" / "sqlite"
DATA_DIR  = BASE / "data" / "era5-slovenia"
DB        = SQLITE_DIR / "era5-slovenia.db"

def run(cmd: list[str], **kw):
    print("  $", " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, **kw)
    if result.returncode != 0:
        print(f"  [WARN] exit {result.returncode}", file=sys.stderr)
    return result

def rebuild_db():
    """Import all era5-slovenia CSVs into SQLite without frictionless/invoke."""
    print("\n[3/3] Rebuilding SQLite database…")
    SQLITE_DIR.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()

    csvs = sorted(glob.glob(str(DATA_DIR / "data" / "*.csv")))
    if not csvs:
        print("  No CSVs found — skipping DB rebuild.", file=sys.stderr)
        return

    conn = sqlite3.connect(DB)
    for csv_path in csvs:
        table = Path(csv_path).stem          # e.g. "si_Ljubljana"
        print(f"  importing {Path(csv_path).name} → {table}")
        import csv as _csv
        with open(csv_path, newline="") as f:
            reader = _csv.DictReader(f)
            rows = list(reader)
        if not rows:
            continue
        cols = list(rows[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_defs = ", ".join(f'"{c}" TEXT' for c in cols)
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({col_defs})')
        conn.executemany(
            f'INSERT INTO "{table}" VALUES ({placeholders})',
            [[r[c] for c in cols] for r in rows],
        )
    conn.commit()
    conn.close()
    print(f"  wrote {DB} ({DB.stat().st_size // 1_000_000} MB)")

    # Regenerate datasette inspect file
    run(["venv/bin/datasette", "inspect", str(DB),
         "--inspect-file", str(SQLITE_DIR / "inspect-data.json")],
        cwd=BASE)


def warm_caches(api_port: int = 5053, sidecar_port: int = 5052) -> None:
    """Hit slow endpoints so they're in-memory before the first user arrives."""
    import time, urllib.request, urllib.error
    time.sleep(12)  # wait for services to fully bind after restart
    today = __import__("datetime").date.today().isoformat()

    endpoints = [
        (sidecar_port, f"/api/live/today_status?date={today}"),
        (sidecar_port, f"/api/live/today_status/last7?date={today}"),
        (api_port,     "/api/season_heatmap"),
        (api_port,     "/api/spei_heatmap"),
        (api_port,     "/api/spei_station_seasonal"),
        (api_port,     "/api/tropical_days"),
        (api_port,     "/api/tropical_nights"),
    ]
    for port, path in endpoints:
        url = f"http://127.0.0.1:{port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=90) as r:
                print(f"  warmed [{r.status}]: {path}")
        except urllib.error.HTTPError as e:
            if e.code == 204:
                print(f"  skipped (disabled): {path}")
            else:
                print(f"  warn [{e.code}]: {path}", file=sys.stderr)
        except Exception as e:
            print(f"  warn: {path} — {e}", file=sys.stderr)


def main():
    import datetime
    print(f"\n=== Podnebnik refresh — {datetime.datetime.now():%Y-%m-%d %H:%M} ===\n")

    print("[1/3] Collecting ERA5-Land data…")
    run(["venv/bin/python3", "mk_collect.py"], cwd=BASE,
        env={**os.environ, "COUNTRY": COUNTRY})

    print("\n[2/3] Pre-computing statistics…")
    run(["venv/bin/python3", "mk_precompute.py"], cwd=BASE,
        env={**os.environ, "COUNTRY": COUNTRY})

    rebuild_db()

    # Restart services if running as root (cron context)
    if os.geteuid() == 0:
        print("\nRestarting services…")
        run(["systemctl", "restart",
             "podnebnik_datasette", "podnebnik_api", "podnebnik_sidecar"])
        print("\nWarming caches…")
        warm_caches()

    print("\nDone.\n")


if __name__ == "__main__":
    main()
