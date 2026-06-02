# CLAUDE.md

Guidance for Claude Code working in this repository.

This file has two parts:
1. **Project orientation** — what the app is and how it fits together (durable context).
2. **The mission** — refactor the app so a *new country* can be added with minimum
   customization, ideally by editing one config file and running data collection,
   with **no edits to Python/JS source**.

---

## 1. Project orientation

**ERA5_MK** is an interactive web dashboard that explores long-term climate trends
(1950–present) for ~20 locations in North Macedonia, built on ERA5-Land reanalysis
data fetched from the free Open-Meteo archive API. Live at `climate.mk`.

### Stack
- **Backend:** Python · Flask · pandas · numpy · scipy · pymannkendall
- **Frontend:** vanilla JS + Highcharts (single-page app)
- **Data:** Open-Meteo ERA5-Land archive → one CSV per location in `./data/`
- **Hosting:** Hetzner + nginx + systemd, behind Cloudflare; deployed via GitHub Actions

### Data & compute flow
```
mk_collect.py  →  data/<Location>.csv   (daily: tmax, tmin, tmean, precip, ET0; 1950→today)
                        │
mk_api.py loads all CSVs into one pandas DataFrame at startup
                        │
        ┌───────────────┼────────────────────────────────────────┐
   regression       calendar / trends / map      today-status / season heatmaps / SPEI
   (Theil-Sen +     (per-day-of-year slopes)      (national daily-max vs historical
    MK TFPW / OLS)                                  distribution; 1950–1980 baseline)
                        │
        Flask JSON endpoints (/api/*)  →  static/app.js  →  Highcharts
```

### Repo map
| Path | Purpose |
|------|---------|
| `mk_collect.py` | Fetches/updates ERA5-Land CSVs from Open-Meteo (differential or `--force-refresh`). |
| `mk_api.py` | Flask API — loads data, all statistics, all `/api/*` routes. ~1600 lines. |
| `mk_dashboard.py` | (read before touching) dashboard/analytics helper. |
| `chat_config.py` | Config for the "Ognen" AI chat assistant (Azure Direct Line). |
| `static/index.html` | SPA shell, titles, meta, branding. |
| `static/app.js` | All chart logic, API calls, map config, default selections, i18n loading. |
| `static/style.css` | Theming via CSS custom properties. |
| `static/locales/*.json` | UI translations: `en`, `mk`, `sq`. |
| `data/` | Per-location CSVs (gitignored on server). |
| `cache/` | Auto-generated JSON caches (gitignored). |
| `cron/mk_collect` | cron.d entry for nightly collection. |
| `deploy.sh`, `.github/workflows/deploy.yml` | CI/CD: rsync, write `.env`, restart service, warm caches. |
| `.env.example` | Secrets template (`DIRECT_LINE_SECRET`, `ANALYTICS_EXPORT_KEY`, …). |

### Dev commands
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 mk_collect.py            # fetch/update data (differential)
python3 mk_collect.py --force-refresh   # full re-fetch
python3 mk_api.py                # serve → http://127.0.0.1:5050
```

### Guardrails — do NOT change these (they are country-agnostic)
The statistical machinery is correct and intentional. Localization must not alter it:
- **Theil-Sen slope + Yue-Wang TFPW Mann-Kendall** (AR(1)-corrected p-values), fit on
  **annual aggregates** (not raw daily values — see the comment in `compute_regression`).
- **SPEI** via 3-parameter log-logistic (`scipy.stats.fisk`) → normal CDF.
- **Lapse-rate elevation correction** (`LAPSE_RATE = 0.0065`).
- KDE / percentile logic in `today_status`; the two-level (memory + filesystem) cache design.

Treat these as fixed. The refactor is about **configuration, feature visibility, and strings**,
not algorithms.

---

## 2. The mission: make it multi-country with minimum customization

**Acceptance criterion.** Adding a new country should be:
```bash
# 1. create one config file describing the country (no code)
cp countries/mk.yaml countries/rs.yaml && edit countries/rs.yaml
# 2. collect its data
COUNTRY=rs python3 collect.py
# 3. run it
COUNTRY=rs python3 api.py
```
…with **zero edits to `.py` / `.js` source** required to stand up `rs`, `gr`, `it`, etc.

### 2.0 Source requirements (from the maintainer)
Verbatim goals this refactor must satisfy:
> - make the app much easier to be usable for different countries
> - make all possible hardcoded text live in configs
> - make each card or part of the app individually choosable in config to be shown or not
> - recheck FAQs / how-tos / help so their text is shown or hidden based on the
>   functionalities, cards, and graphs that are actually enabled — e.g. if a graph is
>   not chosen to be shown, do not mention it in the help pages
> - make the API, graphs, and data (including collection) generalizable for every country

Where each is handled below: hardcoded text → config (§2.2, §2.3, §2.6); show/hide each card
→ feature toggles (§2.4 + the `features:` block in §2.2); help/FAQ consistency with enabled
features (§2.5); generalized API / graphs / data / collection (§2.1, §2.3, §2.4).

### 2.1 Design principle — one source of truth
Today, every country-specific value is hardcoded and **station coordinates are duplicated**
(`LOCATIONS` in `mk_collect.py` *and* `LOC_COORDS` in `mk_api.py`). Unify everything into a
single per-country profile selected by a `COUNTRY` environment variable (default `mk`).
Everything country-specific — stations, strings, map, branding, **and which features are
enabled** — lives in this one profile.

Recommended layout:
```
countries/
  mk.yaml        # North Macedonia (migrate current hardcoded values here)
  _schema.md     # documents every field
config.py        # loads countries/<COUNTRY>.yaml, validates, exposes one CONFIG object
data/<COUNTRY>/  # per-country CSVs (isolated)
cache/<COUNTRY>/ # per-country caches
static/locales/<lang>.json   # keep JSON locale system; just make the language SET configurable
```
YAML is recommended (human-editable by non-developers; add `pyyaml` to `requirements.txt`).
A zero-dependency alternative is a Python dict per country in `countries/<cc>.py` — pick one
and apply it consistently. **Both `collect.py` and `api.py` must import station data from this
single source** so the duplication bug disappears.

### 2.2 Proposed config schema (`countries/mk.yaml`)
```yaml
code: mk
name: North Macedonia            # used in titles / fallback strings
timezone: Europe/Skopje          # IANA tz — drives "today" date logic
default_location: Skopje
languages: [en, mk, sq]          # which locale JSONs to load
default_language: mk

# climatology / methodology windows (sane shared defaults; override per country only if needed)
data_start_date: "1950-01-01"
baseline:        { start: 1950, end: 1980 }   # SPEI + season-heatmap reference period
trend_start_year: 1950
projection_end_year: 2050

# map (Highcharts) — center the station map on the country
map: { center_lat: 41.6, center_lon: 21.7, zoom: 7 }
# OR boundary_geojson: static/geo/mk.geojson

branding:
  site_title: "North Macedonia Climate Explorer"
  domain: "climate.mk"
  chatbot_name: "Ognen"          # persona for the Direct Line assistant

# show/hide every card, section, and graph (see §2.4). Everything currently shipped = true.
features:
  regression_chart: true
  trend_calendar: true
  station_map: true
  hero_cards: true
  today_section: true
  season_heat_heatmap: true
  spei_heatmap: true
  drought_trend_chart: false     # WIP — off by default
  chatbot: true
  welcome_modal: true
  next_episodes_teaser: false

stations:        # SINGLE source of truth — collect.py and api.py both read this
  - { name: Skopje,  lat: 41.9965, lon: 21.4314, elevation: 240 }
  - { name: Bitola,  lat: 41.0314, lon: 21.3347, elevation: 589 }
  # … all 20 …
```
The set of Open-Meteo variables to collect is **derived from `features`**, not hardcoded
(see §2.4, layer 3).

### 2.3 Country-specific surface to migrate (inventory)
This is what I found hardcoded. Treat it as a **starting list, not exhaustive** — run the
discovery greps in §2.7 to catch anything missed (especially in `app.js`, `index.html`,
locale JSONs, `chat_config.py`, and the deploy files, which should also be audited).

**`mk_collect.py`**
- `LOCATIONS` (20 station dicts) → `CONFIG.stations`.
- Docstring + argparse text say "North Macedonia" → use `CONFIG.name`; add `--country` / read `COUNTRY`.
- `START_DATE = "1950-01-01"` → `CONFIG.data_start_date`.
- `wait_if_rate_limited()` probe uses hardcoded `latitude=41.9, longitude=21.4` (Skopje) →
  use the first station / country centroid from config.
- Output should go to `data/<COUNTRY>/`.

**`mk_api.py`**
- `LOC_COORDS` dict → **delete**; derive from `CONFIG.stations` (kills the duplication).
- `DATA_DIR = "./data"` → `data/<COUNTRY>/`.
- `_today_mk()` → `tz="Europe/Skopje"` → `CONFIG.timezone`. Rename to `_today_local()`.
- `_fetch_om()` → `"timezone": "Europe/Skopje"` → `CONFIG.timezone`.
- `_TODAY_CATEGORIES` description strings: **"…feels like in Macedonia."**, **"76-year record"**,
  **"top 5% … since 1950"** → these bake in country name *and* record length. Move the text to
  the locale files and **compute the record length from the data** (`year_max - year_min + 1`),
  don't hardcode "76-year" / "1950".
- `compute_annual_trend`: `TREND_START_YEAR = 1950`, projection to `2050`, `WINDOW_HALF = 30`
  → `CONFIG.trend_start_year`, `CONFIG.projection_end_year` (window 30 can stay shared).
- Season heatmap + SPEI: `BASELINE_START, BASELINE_END = 1950, 1980` → `CONFIG.baseline`.
- Default `loc` (`"Skopje"`) in `/api/regression`, `/api/calendar`, etc. → `CONFIG.default_location`.
- Flask docstring "MK Climate Explorer" and any user-visible API labels → config / locale.
- `app.run(... port=5050)` → keep 5050 default but make it overridable (`PORT` env).
- `/api/meta` should additionally return `country`, `default_location`, `default_language`,
  `languages`, `map`, `branding`, **and the resolved `features` object** so the frontend
  configures itself from one call.

**Frontend (`static/index.html`, `static/app.js`, `static/style.css`)** — audit and parametrize:
- `<title>`, meta description/OG tags, header text, footer, any "Macedonia"/`climate.mk` strings.
- Highcharts **map center/zoom** (and/or the country GeoJSON boundary) → from `/api/meta`.
- Default selected station(s) and default language → from `/api/meta`.
- Locale loader: load only `CONFIG.languages`; default to `CONFIG.default_language`.
- Move any remaining hardcoded English UI text into the locale JSONs.

**`chat_config.py`** — chatbot persona "Ognen", any localized canned/error messages, Direct
Line wiring → drive name/persona from `CONFIG.branding` and the `chatbot` feature flag; keep
the secret in `.env`.

**Deploy (`deploy.sh`, `.github/workflows/deploy.yml`, `cron/mk_collect`)** — server host,
`climate.mk` domain, systemd service name, cache-warming endpoints. Parametrize by country
(e.g. `COUNTRY`, `DOMAIN`, `SERVICE_NAME` as workflow inputs / secrets) so each country is a
separate deploy target without editing scripts. Cache-warming must skip disabled features (§2.4).

### 2.4 Feature toggles — show/hide every card, section, and graph
Each discrete UI unit (section, card, graph) and the chatbot must be switchable from the country
config. Define a **canonical registry of feature keys** in `config.py` (with safe defaults) that
each `countries/<cc>.yaml` overrides via its `features:` block. Map keys 1:1 to the app's parts
and their endpoints:

| Feature key | UI part | Endpoint(s) | Open-Meteo vars needed |
|---|---|---|---|
| `regression_chart` | Regression chart | `/api/regression` | temperature (+ precip/ET0 if selectable) |
| `trend_calendar` | Year-round trend calendar | `/api/calendar` | selected variable |
| `station_map` | Station trend map | `/api/trends` | temperature |
| `hero_cards` | Per-location summary cards | `/api/regression` | temperature |
| `today_section` | "Is it hot today" | `/api/today_status`, `/api/annual_trend` | temperature |
| `season_heat_heatmap` | Seasonal heat ranking | `/api/season_heatmap` | temperature |
| `spei_heatmap` | Seasonal drought (SPEI) | `/api/spei_heatmap` | precipitation + ET0 |
| `drought_trend_chart` | Per-station SPEI trend (WIP) | `/api/spei_station_seasonal` | precipitation + ET0 |
| `chatbot` | "Ognen" assistant | `/api/token` | — |
| `welcome_modal`, `next_episodes_teaser` | static UI | — | — |

Enforce each toggle at **three layers** so a disabled feature is truly absent, not just hidden:
1. **Frontend** (`app.js`): read `features` from `/api/meta`; only build/mount enabled cards and
   charts. Don't render hidden DOM, and don't call disabled endpoints.
2. **Backend** (`api.py`): each gated route returns `404`/`204` when its feature is off, and the
   deploy cache-warming step skips disabled features. (Prevents shipping compute/data for hidden parts.)
3. **Collection** (`collect.py`): derive the required Open-Meteo variables from the enabled
   features and fetch only those — e.g. skip `precipitation_sum` + `et0_fao_evapotranspiration`
   when `spei_heatmap`, `drought_trend_chart`, and any precip/ET0 chart are all disabled. Always
   keep variables that any enabled feature needs.

`/api/meta` returns the resolved `features` object as the single consumer-facing source.
Disabling a parent section should disable its child graphs — resolve that hierarchy once in
`config.py` so every layer sees the same resolved truth.

### 2.5 Help / FAQ / how-to consistency
Help, FAQ, and how-to content must never describe a feature that is turned off. Drive it from the
**same** `features` flags — do not maintain a second list:
- Store each help / FAQ / how-to entry as structured data (in the locale JSON or a dedicated
  `help` structure) with a `feature` field naming the key(s) it documents. Entries with no key
  (general intro, data-source credit, methodology) are always shown.
- The frontend renders an entry only if **all** its required feature keys are enabled — reuse the
  exact gating helper from §2.4.
- Result: toggling a feature off removes its card, its endpoint, *and* its documentation in one
  place, with no second source of truth to keep in sync.
- Verify by disabling a feature and confirming its help / FAQ / how-to entry disappears with it.

### 2.6 Locale (i18n) handling
The JSON locale system already exists — **extend it, don't replace it**:
- Make the **language set** configurable (`CONFIG.languages`), not the hardcoded `en/mk/sq`.
- Every newly-discovered hardcoded user-facing string (incl. the `today_status` category
  descriptions and all help/FAQ text from §2.5) gets a key in `static/locales/en.json` first,
  then translations.
- A new country reuses `en.json` and adds its own language file(s) as needed. English must be a
  complete fallback so a country can launch with `[en]` only.

### 2.7 Discovery commands — find every hardcoded value
Run these and fold anything new into the config / locale / feature migration before declaring done:
```bash
# country / brand / persona / domain references
grep -rniE "macedonia|skopje|europe/skopje|climate\.mk|ognen" \
  --include=*.py --include=*.js --include=*.html --include=*.json \
  --include=*.css --include=*.sh --include=*.yml .

# hardcoded years / record length / projection
grep -rniE "\b(1950|1980|2050)\b|76-?year" --include=*.py --include=*.js .

# all station names (confirm none remain in source after migration)
grep -rniE "bitola|ohrid|tetovo|kumanovo|prilep|strumica|gostivar" .

# hardcoded port / data dir
grep -rnE "5050|\./data" --include=*.py .

# help / FAQ / how-to blocks (should be data-tagged + feature-gated, not hardcoded inline)
grep -rniE "faq|how.?to|help|tutorial|guide" --include=*.js --include=*.html --include=*.json static/
```

### 2.8 Per-country items a human must decide (cannot be auto-derived)
Surface these to the maintainer when porting; don't guess:
- **Station list** — which towns/grid points represent the country (name, lat, lon, elevation).
  ERA5-Land is gridded, so "stations" are just representative coordinates.
- **Which features/cards to enable** — not every country wants every graph (e.g. SPEI needs
  reliable precip/ET0; the drought trend chart is WIP). Choose the `features:` subset.
- **Timezone** (single IANA zone; for large/multi-tz countries pick the dominant one and note it).
- **Languages** to ship and which is default.
- **Baseline suitability** — 1950–1980 is a sensible pre-warming reference and should usually
  stay, but confirm ERA5-Land coverage starts early enough for the chosen country.
- **Branding** — site title, domain, and the chatbot persona name (chatbot on/off is a feature flag).
- **Map framing** — center/zoom or a boundary GeoJSON.

### 2.9 Suggested order of work (incremental, verify each step)
1. Add `config.py` + `countries/mk.yaml` mirroring today's exact MK values, **with a `features:`
   block where everything currently shown is `true`**. Don't change behavior yet.
2. Repoint `collect.py` and `api.py` to read stations/dates/timezone/baseline from `CONFIG`;
   delete `LOC_COORDS`. Verify the app renders identically for `mk`.
3. Namespace `data/<COUNTRY>/` and `cache/<COUNTRY>/`; add `--country`/`COUNTRY` to collection.
4. Externalize the `today_status` strings + record length into locales; extend `/api/meta` to
   return `country`, defaults, `languages`, `map`, `branding`, **and `features`**.
5. Parametrize the frontend (title/branding/map/defaults/languages) from `/api/meta`.
6. Implement **feature toggles** across all three layers (§2.4) plus the cache-warming skip.
7. Make help / FAQ / how-to content **data-tagged and gated** by the same flags (§2.5).
8. Parametrize deploy + cron by country.
9. **Prove it:** add a second country with a *different feature subset* (e.g. drought charts off,
   `[en]` only) and run the 3-command flow. Confirm: hidden cards, their endpoints, *and* their
   help entries are all gone; collection skipped the unused variables; every remaining chart, the
   map, the "today" verdict, and language switching work — with **no source edits**.

### 2.10 Definition of done
- A new country needs only: a `countries/<cc>.yaml` (incl. its `features:` choices), optional
  locale JSON(s), optional GeoJSON, then `COUNTRY=<cc> python3 collect.py && COUNTRY=<cc> python3 api.py`.
- No string `"Macedonia"`, `"Skopje"`, `"Europe/Skopje"`, `"climate.mk"`, `"Ognen"`, `1950`,
  `1980`, `2050`, or station name remains in `.py`/`.js` source (greps in §2.7 come back clean,
  except inside `countries/mk.yaml` and locale files).
- **Every card / section / graph is individually toggleable from config**, and a toggle removes the
  UI, its endpoint exposure, *and* its help / FAQ / how-to entries together.
- **No help / FAQ / how-to text references a disabled feature.**
- **Collection fetches only the Open-Meteo variables required by the enabled features.**
- `mk` behaves exactly as before (no statistical or visual regressions).
- English is a complete fallback locale.

### Optional follow-ups (flag as decisions, don't do silently)
- Rename `mk_*.py` → `collect.py` / `api.py` / `dashboard.py` for a country-neutral codebase
  (higher churn; do only if the maintainer agrees).
- Support multiple countries in a single deployment (path/subdomain routing) vs. one deploy per
  country. The config-per-country design above supports either; default to one-deploy-per-country
  unless asked.
