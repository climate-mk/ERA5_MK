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
# or, in powershell: .\venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Fetch data (requires internet, takes ~5 min)
python3 mk_collect.py

# Run the API server
python3 mk_api.py
# Open http://127.0.0.1:5050
```

### Testing the full web app locally

With the server running (`python3 mk_api.py`), open **http://127.0.0.1:5050** in your browser. You should see the full dashboard with the regression chart loading for Skopje on Apr 15.

Quick checklist:
- Regression chart loads with scatter points and a trend line
- Stats card appears below the chart with slope, τ² and p-value
- Switching variable (e.g. Precipitation) updates the chart and changes colours
- Moving the DOY slider updates the chart after a short debounce
- ▶ Play animates through the year automatically
- Clicking **Year-round calendar** computes and draws 365 bars (~10 s first run, instant after)
- Selecting a second location adds a second series to the regression chart

To also test on a phone or tablet on the same Wi-Fi, find your local IP:

```bash
ipconfig getifaddr en0   # Mac
```

Then open `http://<your-local-ip>:5050` on the second device. The `host="0.0.0.0"` setting in `mk_api.py` already allows this.

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
