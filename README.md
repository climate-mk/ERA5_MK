# ERA5_MK — North Macedonia Climate Explorer

Interactive web dashboard for exploring long-term climate trends across 20 locations in North Macedonia, powered by ERA5-Land reanalysis data (1950–present).

**Live:** [climate.mk](https://climate.mk)

---

## What it does

### Core charts
- **Regression chart** — daily temperature, precipitation and evapotranspiration trends per station with Theil-Sen + Mann-Kendall or OLS; scatter points, CI band, ±N-day window filter
- **Year-round trend calendar** — one bar per day of year, coloured by trend direction and significance; one panel per selected location
- **Station map** — all 20 stations coloured by trend slope; click or tap to select
- **Hero cards** — trend slope, p-value and significance summary for each selected location

### Today section
- **"Is it hot in Macedonia today?"** — compares the current national daily maximum temperature against the ERA5-Land historical distribution for the same calendar day (±window), with KDE curve, percentile rank and a plain-language verdict

### Seasonal heatmaps (new)
- **Seasonal heat ranking** — one coloured cell per (year, season) from 1950 to present; percentile rank of each season's mean national daily-maximum temperature against the **1950–1980 baseline**; colour: blue = cold, orange/red = hot/extreme; animate, filter, stats, tooltip
- **Seasonal drought index (SPEI)** — same grid layout but showing the **SPEI** (Standardized Precipitation-Evapotranspiration Index) per season; dry = orange, wet = blue; 1950–1980 baseline

### Drought trend chart (work in progress)
- **Per-station SPEI trend** — Highcharts scatter + Theil-Sen trend line per station and period; two time scales:
  - **SPEI-3** (seasonal, ~90 days): Annual, Winter, Spring, Summer, Autumn
  - **SPEI-30** (monthly, calendar month): Jan through Dec
- Stats box: slope per decade, Mann-Kendall trend direction, significance
- Extrapolation: estimates the year the trend line crosses the extreme drought (SPEI −1.5) or extremely wet (SPEI +1.5) threshold, guarded against zero-slope division

### Other features
- Multi-language UI: English, Macedonian (МК), Albanian (SQ) via JSON locale files
- Mobile-responsive with hamburger drawer, vertical season labels on heatmaps
- Chat with **Ognen** — AI climate assistant (Azure Bot Framework / Direct Line)
- Welcome modal, "In the next episodes…" teaser section
- Dark/light variable theming via CSS custom properties
- **Climate news** (`climate-news.html`, MK only) — recent climate-related headlines aggregated from Macedonian news outlet RSS feeds, plus the site's X (Twitter) timeline

---

## Stack

| Layer | Technology |
|-------|-----------|
| Data source | [Open-Meteo](https://open-meteo.com/) ERA5-Land archive API |
| Backend | Python · Flask · pandas · scipy · pymannkendall |
| Frontend | Vanilla JS · [Highcharts](https://www.highcharts.com/) |
| Hosting | Hetzner CX23 · nginx · systemd |
| CDN / HTTPS | Cloudflare |

---

## Project structure

```
ERA5_MK/
├── mk_collect.py          # Data collection — fetches ERA5-Land CSVs from Open-Meteo
├── mk_api.py              # Flask API — all statistics and route handlers
├── climate_news.py        # Standalone climate-news aggregation (MK outlet RSS → cache)
├── requirements.txt       # Python dependencies
├── cron/
│   ├── mk_collect         # cron.d file — runs mk_collect.py nightly
│   └── climate_news       # cron.d file — refreshes climate-news cache every 6h
├── static/
│   ├── index.html         # Single-page app shell
│   ├── app.js             # All chart logic, API calls, UI interactions
│   ├── style.css          # Light-theme responsive CSS
│   ├── user-manual.html   # Standalone user manual page
│   ├── climate-news.html  # Standalone climate-news page (MK only)
│   └── locales/           # JSON translation files (en, mk, sq)
├── data/                  # ERA5-Land CSVs, one per station (gitignored on server)
└── cache/                 # Auto-generated JSON cache files (gitignored)
```

---

## API endpoints

| Endpoint | Parameters | Returns |
|----------|-----------|---------|
| `GET /api/meta` | — | Location list, variable labels, colour palette |
| `GET /api/regression` | `loc`, `var`, `doy`, `window`, `corr`, `method` | Scatter points, trend line, CI band, stats |
| `GET /api/calendar` | `loc`, `var`, `window`, `corr`, `method` | 365-day trend array for the calendar chart |
| `GET /api/trends` | `var`, `doy`, `window`, `method` | Trend slope per station for the map |
| `GET /api/annual_trend` | — | Annual mean temperature trend (national) |
| `GET /api/today_status` | — | Today's temperature vs historical distribution (KDE, percentile, category) |
| `GET /api/season_heatmap` | — | Seasonal temperature percentiles vs 1950–1980 baseline (all years) |
| `GET /api/spei_heatmap` | — | Seasonal SPEI vs 1950–1980 baseline (all years) |
| `GET /api/spei_station_seasonal` | — | Per-station SPEI-3 (seasonal) + SPEI-30 (monthly) series with Theil-Sen trend |
| `GET /api/token` | — | Short-lived Direct Line token for the chatbot (rate-limited) |
| `GET /api/data/download` | — | Zip archive of all station CSVs (tmax, tmin, tmean, precip, ET₀; 1950–present) |
| `GET /api/climate_news` | — | Recent Macedonian climate-news headlines, aggregated from MK outlet RSS feeds and filtered by keyword (archive refreshed by cron every 6h via `climate_news.py`) |

---

## Caching

Computed results are cached at two levels:

1. **In-memory** (`_TODAY_CACHE` dict) — survives for the lifetime of the process; clears on restart
2. **Disk** (`cache/` directory, JSON files) — survives restarts; filename contains the `era5_last` date so the cache auto-invalidates when new ERA5 data arrives

| Cache file | Typical size | Cold compute time |
|---|---|---|
| `today_YYYY-MM-DD.json` | 4 KB | ~1s |
| `season_heatmap_YYYY-MM-DD.json` | 45 KB | ~0.2s |
| `spei_heatmap_YYYY-MM-DD.json` | 45 KB | ~0.2s |
| `spei_station_seasonal_v2_YYYY-MM-DD.json` | 357 KB | ~6 min on server |

The GitHub Actions deploy workflow automatically warms all slow caches after each deployment so users never hit the cold path.

---

## SPEI methodology

**SPEI** (Standardized Precipitation-Evapotranspiration Index) was introduced by Vicente-Serrano, Beguería & López-Moreno (2010, *Journal of Climate*, doi:[10.1175/2009JCLI2909.1](https://doi.org/10.1175/2009JCLI2909.1)).

Implementation here:
1. Daily water balance **D = P − ET₀** (national mean precipitation minus mean reference evapotranspiration across all 20 stations, or per-station for the trend chart)
2. Seasonal / monthly sum of D (mm)
3. **3-parameter log-logistic distribution** fitted to the **1950–1980 baseline** values per season/month using `scipy.stats.fisk` with a shift parameter so all values are positive
4. CDF transformed to standard normal via `scipy.stats.norm.ppf` → SPEI score, clipped to ±3

Thresholds follow WMO convention:

| SPEI | Category |
|------|----------|
| < −1.5 | Extreme drought |
| −1.5 to −1.0 | Dry |
| −1.0 to +1.0 | Normal |
| +1.0 to +1.5 | Wet |
| > +1.5 | Extremely wet |

Using a fixed 1950–1980 baseline (rather than the full record) means colours reflect change relative to the pre-warming reference period, making the drying trend visually explicit.

---

## Data

ERA5-Land reanalysis data is fetched per location via the Open-Meteo archive API and stored as one CSV per location in `./data/`. Variables: `temperature_max`, `temperature_min`, `temperature_mean`, `precipitation_sum`, `et0_evapotranspiration`.

### Updating data

The collection script supports **differential updates**:

```bash
source venv/bin/activate
python3 mk_collect.py
```

This detects the latest date in each CSV and fetches only new data. A cron job on the server runs this nightly.

**Force a complete re-fetch:**
```bash
python3 mk_collect.py --force-refresh
```

---

## Local setup

```bash
git clone git@github.com:kesma01/ERA5_MK.git
cd ERA5_MK

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python3 mk_collect.py        # fetch/update data
python3 mk_api.py            # start server → http://127.0.0.1:5050
```

To test on a phone on the same Wi-Fi:
```bash
ipconfig getifaddr en0       # find your local IP
# then open http://<local-ip>:5050 on the device
```

To enable the AI chatbot, create a `.env` file:
```
DIRECT_LINE_SECRET=your_secret_here
```

---

## Statistical methods

**Theil-Sen + TFPW Mann-Kendall** (default):
- Theil-Sen slope is robust to outliers
- Yue-Wang TFPW corrects for AR(1) autocorrelation, giving properly calibrated p-values
- Computed on annual means to avoid pseudo-replication from daily values

**OLS** is provided for comparison.

---

## Deployment

CI/CD via GitHub Actions (`.github/workflows/deploy.yml`): on push to `main`, rsync files to the Hetzner server, write `.env`, restart systemd service, run health check, warm slow caches.

The app runs on Hetzner CX23 behind nginx, served through Cloudflare with Full (strict) SSL:

```
Browser → Cloudflare (HTTPS) → nginx (HTTPS, Origin Cert) → Flask (HTTP, localhost:5050)
```

See the workflow file for the full deploy sequence.

---

## Data source credit

Climate data: [Open-Meteo ERA5-Land](https://open-meteo.com/) — free, open reanalysis data from ECMWF.

SPEI index: Vicente-Serrano, S.M., Beguería, S., López-Moreno, J.I. (2010). *A Multiscalar Drought Index Sensitive to Global Warming: The Standardized Precipitation Evapotranspiration Index.* Journal of Climate, 23(7), 1696–1718.
