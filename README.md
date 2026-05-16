# ERA5_MK — North Macedonia Climate Explorer

Interactive web dashboard for exploring long-term climate trends across 20 locations in North Macedonia, powered by ERA5-Land reanalysis data (1950–2024).

**Live:** [climate.kesma.wtf](https://climate.kesma.wtf)

---

## What it does

- Visualises daily temperature, precipitation and evapotranspiration trends for 20 MK locations
- Day-of-year slider with ±N day window filter and year-end wraparound
- Two regression methods: **OLS** and **Theil-Sen + Mann-Kendall TFPW** (corrects for AR(1) autocorrelation)
- Year-round trend calendar — one bar per day of year, coloured by trend direction and significance
- Multi-location overlay with colour-coded scatter + confidence band
- Lapse-rate elevation correction for temperature comparisons
- Fully responsive — works on desktop and mobile

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
├── mk_dashboard.py        # Original Dash/Plotly dashboard (local use)
├── requirements.txt       # Python dependencies
├── static/
│   ├── index.html         # Single-page app shell
│   ├── app.js             # Highcharts charts, API calls, play animation
│   └── style.css          # Light-theme responsive CSS
└── data/                  # ERA5-Land CSVs (not committed — see Data section)
```

---

## API endpoints

| Endpoint | Parameters | Returns |
|----------|-----------|---------|
| `GET /api/meta` | — | Location list, variable labels, colour palette |
| `GET /api/regression` | `loc`, `var`, `doy`, `window`, `corr`, `method` | Scatter points, trend line, CI band, stats |
| `GET /api/calendar` | `loc`, `var`, `window`, `corr`, `method` | 365-day trend array for the calendar chart |

---

## Data

ERA5-Land reanalysis data is fetched per location via the Open-Meteo archive API and stored as one CSV per location in `./data/`. The data directory is excluded from version control due to file size (~50 MB total).

To re-fetch the data, run:

```bash
source venv/bin/activate
python3 mk_collect.py
```

Variables collected: `temperature_2m_max`, `temperature_2m_min`, `temperature_2m_mean`, `precipitation_sum`, `et0_fao_evapotranspiration`.

---

## Local setup

```bash
# Clone
git clone git@github.com:kesma01/ERA5_MK.git
cd ERA5_MK

# Create virtualenv and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Fetch data (requires internet, takes ~5 min)
python3 mk_collect.py

# Run the API server
python3 mk_api.py
# Open http://127.0.0.1:5050
```

### Verifying the setup

Once the server is running, confirm all three API endpoints respond correctly:

```bash
# Should return list of locations, variables and colour palette
curl http://127.0.0.1:5050/api/meta

# Should return scatter points, trend line and stats for Skopje on Apr 15
curl "http://127.0.0.1:5050/api/regression?loc=Skopje&var=temperature_mean&doy=105&window=7&method=theilsen"

# Should return 365-day trend calendar for Skopje (takes ~10 s first run, cached after)
curl "http://127.0.0.1:5050/api/calendar?loc=Skopje&var=temperature_mean&window=7&method=theilsen"
```

All three should return JSON without errors. Then open **http://127.0.0.1:5050** in your browser — the chart should load within a few seconds and show a warming trend for the selected location and day.

To test on another device on the same network, edit the last line of `mk_api.py` and add `host="0.0.0.0"`:

```python
app.run(debug=False, host="0.0.0.0", port=5050, threaded=True)
```

Then open `http://<your-local-ip>:5050` on the second device.

---

## Statistical methods

**Theil-Sen + TFPW Mann-Kendall** is the default and recommended method:
- Theil-Sen slope is robust to outliers
- Yue-Wang TFPW (Trend-Free Pre-Whitening) corrects for AR(1) autocorrelation (~0.20 in annual temperature series), giving properly calibrated p-values
- Slope and R²/τ² are computed on annual means to avoid pseudo-replication from daily values

**OLS** is provided for comparison.

---

## Deployment

The app runs on a Hetzner CX23 VPS behind nginx, served via Cloudflare with Full (strict) SSL.

```
Browser → Cloudflare (HTTPS) → nginx (HTTPS, Origin Cert) → Flask (HTTP, localhost:5050)
```

### Server requirements

- Ubuntu 24.04
- Python 3.12 + venv
- nginx
- A domain managed by Cloudflare (for free HTTPS via Origin Certificate)

### First-time deploy

1. Provision a VPS (tested on Hetzner CX23) with Ubuntu 24.04 and SSH access.
2. Add your SSH public key to the server during provisioning.
3. Upload the project files and data:

```bash
SERVER="root@<your-server-ip>"
APP_DIR="/opt/mk_climate"

ssh $SERVER "mkdir -p $APP_DIR/data $APP_DIR/static"
rsync -az mk_api.py requirements.txt $SERVER:$APP_DIR/
rsync -az data/   $SERVER:$APP_DIR/data/
rsync -az static/ $SERVER:$APP_DIR/static/
```

4. On the server, set up Python, nginx and the systemd service:

```bash
apt-get install -y python3 python3-venv nginx
cd /opt/mk_climate
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

5. Create `/etc/systemd/system/mk_climate.service`:

```ini
[Unit]
Description=MK Climate Explorer
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/mk_climate
ExecStart=/opt/mk_climate/venv/bin/python3 mk_api.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
chown -R www-data:www-data /opt/mk_climate
systemctl enable --now mk_climate
```

6. Create `/etc/nginx/sites-available/mk_climate` (replace `climate.example.com` with your domain):

```nginx
server {
    listen 80;
    server_name climate.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name climate.example.com;

    ssl_certificate     /etc/nginx/ssl/origin.crt;   # Cloudflare Origin Certificate
    ssl_certificate_key /etc/nginx/ssl/origin.key;

    location / {
        proxy_pass         http://127.0.0.1:5050/;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
    }
}
```

```bash
ln -s /etc/nginx/sites-available/mk_climate /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

7. In **Cloudflare → DNS**, add an `A` record pointing your subdomain to the server IP (Proxied).
8. In **Cloudflare → SSL/TLS → Origin Server**, create an Origin Certificate, save it to `/etc/nginx/ssl/origin.crt` and `/etc/nginx/ssl/origin.key` on the server.
9. Set **Cloudflare SSL/TLS mode** to **Full (strict)**.

### Re-deploying after code changes

Upload changed files and restart the service:

```bash
rsync -az mk_api.py static/ $SERVER:$APP_DIR/
ssh $SERVER "systemctl restart mk_climate"
```

### Verifying the deployment

```bash
# Check the service is running
ssh $SERVER "systemctl status mk_climate --no-pager"

# Confirm nginx proxies correctly
ssh $SERVER "curl -s http://localhost/ | head -3"
```

---

## Data source credit

Climate data: [Open-Meteo ERA5-Land](https://open-meteo.com/) — free, open reanalysis data from ECMWF.
