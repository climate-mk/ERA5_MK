"""
Interactive climate dashboard for North Macedonia ERA5 data.
Run:  source venv/bin/activate && python3 mk_dashboard.py
Then open:  http://127.0.0.1:8050
"""

import glob
import os
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from dash import Dash, dcc, html, Input, Output, State, callback, ctx, no_update
from scipy import stats
from scipy.stats import theilslopes, kendalltau
import pymannkendall as mk_test
import warnings
warnings.filterwarnings("ignore")

# ── Load all CSVs ─────────────────────────────────────────────────────────────

DATA_DIR = "./data"

dfs = []
for f in sorted(glob.glob(os.path.join(DATA_DIR, "*.csv"))):
    df = pd.read_csv(f, parse_dates=["date"])
    dfs.append(df)

data = pd.concat(dfs, ignore_index=True)
data["year"]  = data["date"].dt.year
data["month"] = data["date"].dt.month

LOCATIONS = sorted(data["location"].unique())
VARIABLES = {
    "temperature_max":        "Temperature Max (°C)",
    "temperature_min":        "Temperature Min (°C)",
    "temperature_mean":       "Temperature Mean (°C)",
    "precipitation_sum":      "Precipitation (mm)",
    "et0_evapotranspiration": "ET₀ Evapotranspiration (mm)",
}

LAPSE_RATE = 0.0065
for col in ["temperature_max", "temperature_min", "temperature_mean"]:
    data[col + "_corr"] = data[col] + data["elevation_diff_m"] * LAPSE_RATE

# ── Style ─────────────────────────────────────────────────────────────────────

PAGE_BG  = "#f4f5fb"
CARD_BG  = "#ffffff"
PLOT_BG  = "#fafbff"
TEXT     = "#1a1a2e"
ACCENT   = "#4c52c9"
SUBTEXT  = "#666677"
BORDER   = "#e0e2f0"

CARD = {"background": CARD_BG, "borderRadius": "10px", "padding": "20px",
        "marginBottom": "16px", "boxShadow": "0 1px 4px rgba(0,0,0,0.07)"}

MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun",
               "Jul","Aug","Sep","Oct","Nov","Dec"]

PALETTE = ["#4c52c9","#d94f4f","#2a9d5c","#e07b00",
           "#0099bb","#9b4dca","#c9880a","#3a7a3a"]

BTN_NORMAL = {"background": CARD_BG, "color": ACCENT,
              "border": f"1px solid {ACCENT}", "borderRadius": "6px",
              "width": "36px", "height": "36px", "fontSize": "14px",
              "cursor": "pointer", "flexShrink": "0", "transition": "all 0.2s"}
BTN_ACTIVE = {**BTN_NORMAL, "background": ACCENT, "color": "#ffffff"}

REGRESSION_METHODS = {
    "ols":      "OLS  (Ordinary Least Squares)",
    "theilsen": "Theil-Sen + Mann-Kendall TFPW  (robust · WMO standard)",
}

DESCRIPTIONS = {
    "ols":      ("Annual average of the selected variable over the ±N day window, "
                 "plotted as dots coloured by anomaly from the full-dataset mean.  "
                 "OLS straight line with 95 % confidence band."),
    "theilsen": ("Slope = median of all point-pair slopes — robust to extreme years.  "
                 "CI band from Theil-Sen slope uncertainty.  "
                 "Significance via Mann-Kendall with Yue-Wang TFPW: removes year-to-year "
                 "autocorrelation before testing, giving a correctly calibrated p-value.  "
                 "WMO standard for climate trend detection."),
}

# Variable-specific labelling & colours
VAR_STYLE = {
    "precipitation_sum": {
        "pos_label": "wetter ↑",    "neg_label": "drier ↓",
        "pos_dot":   "#1a5fc8",     "neg_dot":   "#a05c20",   # blue=wet, brown=dry
        "cal_pos":   (35, 100, 210),"cal_neg":   (180, 105, 25),
        "cal_legend":"blue = wetter · brown = drier",
        "dot_desc":  "Blue = above avg (wetter year) · Brown = below avg (drier year)",
        "chg_unit":  "mm",
    },
    "et0_evapotranspiration": {
        "pos_label": "higher ET₀ ↑","neg_label": "lower ET₀ ↓",
        "pos_dot":   "#e07b00",     "neg_dot":   "#2a9d5c",   # amber=high, green=low
        "cal_pos":   (210, 120, 0), "cal_neg":   (42, 157, 92),
        "cal_legend":"amber = higher ET₀ · green = lower ET₀",
        "dot_desc":  "Amber = above avg (higher ET₀) · Green = below avg (lower ET₀)",
        "chg_unit":  "mm",
    },
}
_TEMP_STYLE = {
    "pos_label": "warming ↑",   "neg_label": "cooling ↓",
    "pos_dot":   "#cc2222",     "neg_dot":   "#1a5fc8",
    "cal_pos":   (210, 55, 35), "cal_neg":   (35, 90, 210),
    "cal_legend":"red = warming · blue = cooling",
    "dot_desc":  "Red = above avg (warmer year) · Blue = below avg (cooler year)",
    "chg_unit":  "°C",
}

def var_style(var):
    return VAR_STYLE.get(var, _TEMP_STYLE)

# ── Helpers ───────────────────────────────────────────────────────────────────

def hex_to_rgba(hex_color, alpha):
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def resolve_col(var, corr):
    if corr == "corr" and var in ["temperature_max", "temperature_min", "temperature_mean"]:
        return var + "_corr"
    return var


def window_filter(loc_data, month, day, half_window):
    """
    Return rows within ±half_window calendar days of (month, day) in any year,
    with correct year-end wraparound (e.g. a ±7 d window around Jan 3 also
    captures Dec 27–31 of the previous year).

    Adds '_window_year': the year whose target date each row belongs to.
      • Dec 30, 1949  →  window_year 1950  (centre = Jan 3, 1950)
      • Jan  3, 1951  →  window_year 1950  (centre = Dec 30, 1950)
    """
    try:
        target_doy = pd.Timestamp(2001, month, day).dayofyear
    except ValueError:
        target_doy = pd.Timestamp(2001, month, 28).dayofyear

    row_doy  = loc_data["date"].dt.dayofyear.to_numpy()
    raw_diff = (row_doy - target_doy).astype(int)   # –364 … +364

    # Shortest circular distance on 365-day year → –182 … +182
    circ_diff = ((raw_diff + 182) % 365) - 182
    in_window = np.abs(circ_diff) <= half_window

    out          = loc_data[in_window].copy()
    raw_diff_out = raw_diff[in_window]

    # Year assignment:
    #   raw_diff > 182  → row is in late Dec, centre is early Jan NEXT year
    #                      → window_year = row_year + 1
    #   raw_diff < -182 → row is in early Jan, centre is late Dec PREV year
    #                      → window_year = row_year − 1
    year_adj = np.where(raw_diff_out >  182,  1,
               np.where(raw_diff_out < -182, -1, 0))
    out["_window_year"] = out["year"].to_numpy() + year_adj
    return out


def window_series(loc_data, month, day, half_window, col):
    """Annual aggregates for dot display — grouped by window year."""
    sub    = window_filter(loc_data, month, day, half_window)
    agg_fn = "sum" if col in ["precipitation_sum", "et0_evapotranspiration"] else "mean"
    return sub.groupby("_window_year")[col].agg(agg_fn).dropna()


def window_raw(loc_data, month, day, half_window, col):
    """
    All raw daily values with decimal-year x — used for regression fitting.
    x uses the actual calendar date for correct temporal positioning;
    year-boundary rows (e.g. Dec 30) appear near the correct end/start of year.
    """
    sub = window_filter(loc_data, month, day, half_window)
    sub = sub.dropna(subset=[col])
    # Decimal year from actual date so temporal position is always correct.
    # Dec 30, 1949 → x ≈ 1949.994  (not 1950.994)
    sub["x"] = sub["year"] + (sub["date"].dt.dayofyear - 1) / 365.0
    return sub["x"].to_numpy(dtype=float), sub[col].to_numpy(dtype=float)


def ols_ci_band(x_arr, y_pred, residuals, confidence=0.95):
    n       = len(x_arr)
    se_res  = np.sqrt(np.sum(residuals ** 2) / (n - 2))
    ss_x    = np.sum((x_arr - x_arr.mean()) ** 2)
    t_crit  = stats.t.ppf((1 + confidence) / 2, df=n - 2)
    se_line = se_res * np.sqrt(1 / n + (x_arr - x_arr.mean()) ** 2 / ss_x)
    return y_pred + t_crit * se_line, y_pred - t_crit * se_line


def doy_to_md(doy):
    ref = pd.Timestamp("2001-01-01") + pd.Timedelta(days=int(doy) - 1)
    return ref.month, ref.day


def _fit_label(var, si):
    """Human-readable description of what data was used for fitting."""
    ar1_str = f"  ·  AR(1)={si['ar1']:.2f}" if "ar1" in si else ""
    if var in ["precipitation_sum", "et0_evapotranspiration"]:
        # Sum variables: one annual sum per year — can't use individual daily values
        return (f"Fitted on {si.get('n_years', '?')} annual window sums  "
                f"(1 per year — sum variables){ar1_str}")
    else:
        return (f"Fitted on {si.get('n_days', '?')} daily values  "
                f"({si.get('n_years', 1)} years){ar1_str}")


def sig_stars(p):
    return ("***" if p < 0.001 else "**" if p < 0.01
            else "*" if p < 0.05 else "ns")


def sig_label(p):
    return {"***": "p < 0.001  ★★★", "**": "p < 0.01  ★★",
            "*": "p < 0.05  ★", "ns": "not significant"}[sig_stars(p)]


def chart_layout(ylabel="", title=""):
    return dict(
        paper_bgcolor=CARD_BG,
        plot_bgcolor=PLOT_BG,
        font_color=TEXT,
        font=dict(family="Inter, sans-serif"),
        margin={"r": 16, "t": 44, "l": 58, "b": 44},
        yaxis_title=ylabel,
        title=dict(text=title, font=dict(color=TEXT, size=13), x=0.01) if title else {},
        legend={"bgcolor": "rgba(0,0,0,0)", "font": {"color": TEXT}},
        xaxis={"gridcolor": BORDER, "linecolor": BORDER, "zerolinecolor": BORDER},
        yaxis={"gridcolor": BORDER, "linecolor": BORDER, "zerolinecolor": BORDER},
    )


# ── Regression engines ────────────────────────────────────────────────────────

def fit_ols(x_raw, y_raw, x_ann, y_ann, x_line, color, color_faint, loc, unit):
    """
    Slope fitted on all raw daily values (maximum data for slope precision).
    R² and p-value computed on annual means — one independent obs per year,
    avoids pseudo-replication from correlated days within the same window.
    """
    # Slope from raw daily values
    slope, intercept, _, _, _ = stats.linregress(x_raw, y_raw)
    y_line = slope * x_line + intercept

    # R² and p on annual means (statistically valid effective sample size)
    _, _, r_val, p_val, _ = stats.linregress(x_ann, y_ann)

    # CI band width from raw residuals (captures real scatter)
    residuals = y_raw - (slope * x_raw + intercept)
    n_raw  = len(x_raw)
    se_res = np.sqrt(np.sum(residuals**2) / (n_raw - 2))
    ss_x   = np.sum((x_raw - x_raw.mean())**2)
    t_crit = stats.t.ppf(0.975, df=len(x_ann) - 2)
    se_ln  = se_res * np.sqrt(1/n_raw + (x_line - x_raw.mean())**2 / ss_x)
    upper, lower = y_line + t_crit * se_ln, y_line - t_crit * se_ln

    trend10 = slope * 10
    lbl = (f"{loc}  OLS {trend10:+.3f} {unit}/decade  "
           f"p={p_val:.3f} {sig_stars(p_val)}  R²={r_val**2:.3f}  "
           f"(n={len(x_raw)} days / {len(x_ann)} yrs)")
    traces = [
        go.Scatter(x=np.concatenate([x_line, x_line[::-1]]),
                   y=np.concatenate([upper, lower[::-1]]),
                   fill="toself", fillcolor=color_faint,
                   line=dict(width=0), hoverinfo="skip",
                   showlegend=False, legendgroup=loc),
        go.Scatter(x=x_line, y=y_line, mode="lines", name=lbl,
                   line=dict(color=color, width=2.5, dash="solid"),
                   hoverinfo="skip", legendgroup=loc, showlegend=True),
    ]
    return traces, dict(method="OLS", slope=slope, r2=r_val**2,
                        p_val=p_val, trend10=trend10,
                        n_days=len(x_raw), n_years=len(x_ann))


def fit_theilsen(x_raw, y_raw, x_ann, y_ann, x_line, color, color_faint, loc, unit):
    """
    Slope: Theil-Sen estimator on all raw daily values.
    Significance: Yue-Wang TFPW Mann-Kendall on annual means.

    Standard Mann-Kendall is too conservative when annual means have positive
    lag-1 autocorrelation (warm years tend to follow warm years, AR1 ≈ 0.2).
    The Yue-Wang Trend-Free Pre-Whitening (TFPW) method removes that
    autocorrelation before computing the MK statistic, giving a correctly
    calibrated p-value — the WMO recommended approach for climate series.
    """
    res     = theilslopes(y_raw, x_raw, 0.95)
    slope   = res.slope
    trend10 = slope * 10

    # Yue-Wang TFPW Mann-Kendall on annual means
    # (one independent obs per year; TFPW corrects for AR(1) in the series)
    mk_result = mk_test.yue_wang_modification_test(y_ann)
    tau   = mk_result.Tau
    p_val = mk_result.p

    # AR(1) for display (lag-1 autocorrelation of annual means)
    if len(y_ann) > 2:
        ar1 = float(np.corrcoef(y_ann[:-1], y_ann[1:])[0, 1])
    else:
        ar1 = 0.0

    # CI lines pivot through raw-data median point
    x_med = np.median(x_raw)
    y_med = np.median(y_raw)
    intercept      = y_med - slope          * x_med
    intercept_high = y_med - res.high_slope * x_med
    intercept_low  = y_med - res.low_slope  * x_med

    y_line  = slope          * x_line + intercept
    y_upper = res.high_slope * x_line + intercept_high
    y_lower = res.low_slope  * x_line + intercept_low

    lbl = (f"{loc}  Theil-Sen {trend10:+.3f} {unit}/decade  "
           f"MK(TFPW) p={p_val:.3f} {sig_stars(p_val)}  τ={tau:.3f}  "
           f"(n={len(x_raw)} days / {len(x_ann)} yrs  AR1={ar1:.2f})")
    traces = [
        go.Scatter(x=np.concatenate([x_line, x_line[::-1]]),
                   y=np.concatenate([y_upper, y_lower[::-1]]),
                   fill="toself", fillcolor=color_faint,
                   line=dict(width=0), hoverinfo="skip",
                   showlegend=False, legendgroup=loc),
        go.Scatter(x=x_line, y=y_line, mode="lines", name=lbl,
                   line=dict(color=color, width=2.5, dash="dash"),
                   hoverinfo="skip", legendgroup=loc, showlegend=True),
    ]
    return traces, dict(method="Theil-Sen+MK(TFPW)", slope=slope, r2=tau**2,
                        p_val=p_val, trend10=trend10, ar1=ar1,
                        n_days=len(x_raw), n_years=len(x_ann))


FIT_FN = {"ols": fit_ols, "theilsen": fit_theilsen}


# ── Year-round trend calendar (precomputed on-demand) ─────────────────────────

def compute_trend_calendar(loc, col, half_window, method="theilsen"):
    """
    Compute slope and significance for every DOY using the selected method.
    Returns a JSON-serialisable dict keyed by DOY string.
    ~1 s for one location.
    """
    ld     = data[data["location"] == loc]
    agg_fn = "sum" if col in ["precipitation_sum", "et0_evapotranspiration"] else "mean"
    out    = {}
    for doy in range(1, 366):
        ref = pd.Timestamp("2001-01-01") + pd.Timedelta(days=doy - 1)
        sub = window_filter(ld, ref.month, ref.day, half_window)
        series = sub.groupby("_window_year")[col].agg(agg_fn).dropna()
        if len(series) < 10:
            continue
        x = series.index.to_numpy(float)
        y = series.values
        try:
            if method == "ols":
                slope, _, r_val, p_val, _ = stats.linregress(x, y)
                metric = r_val ** 2      # R²
            else:                        # theilsen (default)
                ts_r   = theilslopes(y, x, 0.95)
                slope  = ts_r.slope
                mk_r   = mk_test.yue_wang_modification_test(y)
                p_val  = mk_r.p
                metric = mk_r.Tau        # τ
            out[str(doy)] = {
                "metric":  float(metric),
                "p":       float(p_val),
                "slope10": float(slope * 10),
            }
        except Exception:
            pass
    return out


# ── App layout ────────────────────────────────────────────────────────────────

app = Dash(__name__)
app.title = "MK Climate Explorer"

app.layout = html.Div(
    style={"background": PAGE_BG, "minHeight": "100vh",
           "padding": "24px 32px", "fontFamily": "Inter, sans-serif", "color": TEXT},
    children=[

    html.H1("🌤 North Macedonia Climate Explorer  (ERA5-Land 1950–2026)",
            style={"textAlign": "center", "color": ACCENT,
                   "marginBottom": "4px", "fontSize": "22px", "fontWeight": "700"}),
    html.P("Daily ERA5-Land data for 20 locations · Lapse-rate corrected temperatures available",
           style={"textAlign": "center", "color": SUBTEXT,
                  "marginBottom": "24px", "fontSize": "13px"}),

    html.Div(style=CARD, children=[

        html.H3("📅 Date-Window Trend & Regression",
                style={"color": TEXT, "fontSize": "15px",
                       "fontWeight": "600", "margin": "0 0 4px 0"}),
        html.P(id="reg-description",
               style={"color": SUBTEXT, "fontSize": "12px", "marginBottom": "14px"}),

        # ── Top controls ────────────────────────────────────────────────────
        html.Div(style={"display": "flex", "gap": "16px", "flexWrap": "wrap",
                        "alignItems": "flex-end", "marginBottom": "14px"}, children=[
            html.Div([
                html.Label("Stations", style={"color": SUBTEXT, "fontSize": "12px",
                                              "display": "block", "marginBottom": "4px"}),
                dcc.Dropdown(
                    id="reg-loc",
                    options=[{"label": l, "value": l} for l in LOCATIONS],
                    value=["Skopje"], multi=True,
                    style={"minWidth": "280px"},
                ),
            ]),
            html.Div([
                html.Label("Variable", style={"color": SUBTEXT, "fontSize": "12px",
                                              "display": "block", "marginBottom": "4px"}),
                dcc.Dropdown(
                    id="reg-var",
                    options=[{"label": v, "value": k} for k, v in VARIABLES.items()],
                    value="temperature_mean", clearable=False,
                    style={"minWidth": "210px"},
                ),
            ]),
            html.Div([
                html.Label("Lapse-rate correction", style={"color": SUBTEXT, "fontSize": "12px",
                                                            "display": "block", "marginBottom": "4px"}),
                dcc.RadioItems(
                    id="reg-corr",
                    options=[{"label": "  Off", "value": "raw"},
                             {"label": "  On",  "value": "corr"}],
                    value="raw", inline=True,
                    style={"color": TEXT, "fontSize": "13px"},
                ),
            ]),
        ]),

        # ── Regression method ────────────────────────────────────────────
        html.Div(style={"marginBottom": "14px"}, children=[
            html.Label("Regression method",
                       style={"color": SUBTEXT, "fontSize": "12px",
                              "display": "block", "marginBottom": "6px"}),
            dcc.RadioItems(
                id="reg-method",
                options=[{"label": f"  {v}", "value": k}
                         for k, v in REGRESSION_METHODS.items()],
                value="ols", inline=True,
                labelStyle={"marginRight": "28px"},
                style={"color": TEXT, "fontSize": "13px"},
            ),
        ]),

        # ── Chart ────────────────────────────────────────────────────────
        dcc.Graph(id="reg-chart",
                  config={"displayModeBar": True, "scrollZoom": True},
                  style={"height": "520px"}),

        # ── Stats cards ──────────────────────────────────────────────────
        html.Div(id="reg-stats",
                 style={"display": "flex", "gap": "12px", "flexWrap": "wrap",
                        "margin": "12px 0 16px 0"}),

        # ── Year-round trend calendar ─────────────────────────────────────
        html.Div(style={"marginBottom": "16px"}, children=[
            html.Div(style={"display": "flex", "justifyContent": "space-between",
                            "alignItems": "center", "marginBottom": "4px"}, children=[
                html.Span("Year-round trend overview",
                          style={"color": TEXT, "fontSize": "13px", "fontWeight": "600"}),
                html.Span("Theil-Sen slope · opacity = significance (TFPW MK) · "
                          "recalculates on location / variable / window change · "
                          "click bar to navigate",
                          style={"color": SUBTEXT, "fontSize": "11px"}),
            ]),
            dcc.Loading(
                children=dcc.Graph(id="trend-calendar",
                                   config={"displayModeBar": False},
                                   style={"height": "170px"}),
                type="default",
                overlay_style={"visibility": "visible",
                               "filter": "blur(1px)"},
                custom_spinner=html.Div("⏳  Calculating year-round trends…",
                                        style={"color": ACCENT, "fontSize": "13px",
                                               "fontWeight": "600", "padding": "60px 0",
                                               "textAlign": "center"}),
            ),
        ]),

        # Animation state + ticker
        dcc.Store(id="play-state",     data={"direction": 0}),
        dcc.Interval(id="play-interval", interval=600, disabled=True),

        # ── Bottom controls bar ──────────────────────────────────────────
        html.Div(style={"background": PAGE_BG, "borderRadius": "8px",
                        "border": f"1px solid {BORDER}", "padding": "14px 20px"}, children=[

            # Date row
            html.Div(style={"marginBottom": "20px"}, children=[
                html.Div(style={"display": "flex", "justifyContent": "space-between",
                                "alignItems": "center", "marginBottom": "6px"}, children=[
                    html.Label("Date", style={"color": SUBTEXT, "fontSize": "12px"}),
                    html.Span(id="reg-date-label",
                              style={"color": ACCENT, "fontSize": "13px",
                                     "fontWeight": "700"}),
                ]),
                html.Div(style={"display": "flex", "alignItems": "center", "gap": "10px"}, children=[
                    html.Button("◀", id="play-bwd-btn", n_clicks=0, style=BTN_NORMAL),
                    html.Div(style={"flex": "1"}, children=[
                        dcc.Slider(
                            id="reg-doy", min=1, max=365, step=1, value=105,
                            marks={1:"Jan", 32:"Feb", 60:"Mar", 91:"Apr", 121:"May",
                                   152:"Jun", 182:"Jul", 213:"Aug", 244:"Sep",
                                   274:"Oct", 305:"Nov", 335:"Dec"},
                            tooltip={"always_visible": False}, included=False,
                        ),
                    ]),
                    html.Button("▶", id="play-fwd-btn", n_clicks=0, style=BTN_NORMAL),
                    html.Div(style={"display": "flex", "flexDirection": "column",
                                    "alignItems": "center", "gap": "2px",
                                    "minWidth": "110px"}, children=[
                        html.Label("Speed", style={"color": SUBTEXT, "fontSize": "11px"}),
                        dcc.Slider(
                            id="play-speed",
                            min=1, max=5, step=1, value=3,
                            marks={1:"slow", 3:"mid", 5:"fast"},
                            tooltip={"always_visible": False},
                            included=False,
                        ),
                    ]),
                ]),
            ]),

            # Window slider
            html.Div([
                html.Label("Window  ±days  (total = 2N+1 days)",
                           style={"color": SUBTEXT, "fontSize": "12px",
                                  "display": "block", "marginBottom": "6px"}),
                dcc.Slider(
                    id="reg-window", min=1, max=45, step=1, value=7,
                    marks={1:"±1", 7:"±7", 15:"±15", 30:"±30", 45:"±45"},
                    tooltip={"placement": "top", "always_visible": True},
                    included=False,
                ),
            ]),
        ]),
    ]),
])


# ── Main callback ─────────────────────────────────────────────────────────────

@callback(
    Output("reg-chart",       "figure"),
    Output("reg-stats",       "children"),
    Output("reg-date-label",  "children"),
    Output("reg-description", "children"),
    Input("reg-loc",    "value"),
    Input("reg-var",    "value"),
    Input("reg-doy",    "value"),
    Input("reg-window", "value"),
    Input("reg-corr",   "value"),
    Input("reg-method", "value"),
)
def update_regression(locs, var, doy, half_window, corr, method):
    month, day = doy_to_md(doy or 105)
    date_str   = f"{day} {MONTH_NAMES[month - 1]}"
    vs         = var_style(var)
    desc       = DESCRIPTIONS.get(method, "") + f"  {vs['dot_desc']}."
    empty      = go.Figure()
    empty.update_layout(**chart_layout())
    if not locs:
        return empty, [], date_str, desc

    col    = resolve_col(var, corr)
    ylabel = VARIABLES[var]
    unit   = ylabel.split("(")[-1].rstrip(")") if "(" in ylabel else ""

    baselines = {}
    for loc in locs:
        s = window_series(data[data["location"] == loc], month, day, half_window, col)
        baselines[loc] = float(s.mean()) if len(s) else np.nan

    date_label = f"{day} {MONTH_NAMES[month - 1]}  ±{half_window} d"
    fig        = go.Figure()
    stat_cards = []
    fit_fn     = FIT_FN[method]

    for i, loc in enumerate(locs):
        color       = PALETTE[i % len(PALETTE)]
        color_faint = hex_to_rgba(color, 0.10)

        series = window_series(data[data["location"] == loc], month, day, half_window, col)
        if len(series) < 5:
            continue

        x_arr   = series.index.to_numpy(dtype=float)
        y_arr   = series.values
        base    = baselines[loc]
        anomaly = y_arr - base

        max_abs    = max(np.abs(anomaly).max(), 1e-6)
        dot_colors = [
            hex_to_rgba(vs["pos_dot"], 0.45 + 0.50 * abs(a) / max_abs) if a >= 0
            else hex_to_rgba(vs["neg_dot"], 0.45 + 0.50 * abs(a) / max_abs)
            for a in anomaly
        ]
        if var == "precipitation_sum":
            ano_pos, ano_neg = "wetter", "drier"
        elif var == "et0_evapotranspiration":
            ano_pos, ano_neg = "higher ET₀", "lower ET₀"
        else:
            ano_pos, ano_neg = "warmer", "cooler"
        hover = [
            f"<b>{loc}</b><br>Year: {int(yr)}<br>{ylabel}: {v:.2f}<br>"
            f"{'Above' if a >= 0 else 'Below'} avg "
            f"({ano_pos if a >= 0 else ano_neg}): {a:+.2f}"
            for yr, v, a in zip(x_arr, y_arr, anomaly)
        ]

        fig.add_trace(go.Scatter(
            x=x_arr, y=y_arr, mode="markers", name=loc,
            marker=dict(color=dot_colors, size=7,
                        line=dict(width=0.5, color="rgba(0,0,0,0.15)")),
            hovertext=hover, hoverinfo="text",
            legendgroup=loc, showlegend=True,
        ))

        # For sum variables (precipitation, ET0) the daily raw values have a
        # completely different scale from the annual window sums plotted as dots
        # (e.g. daily precip 0–5 mm vs annual window total 30 mm).
        # Using raw daily values for regression draws the line near y=0.
        # → use annual sums directly for regression on sum variables.
        if col in ["precipitation_sum", "et0_evapotranspiration"]:
            x_raw, y_raw = x_arr, y_arr   # annual sums — correct scale
        else:
            x_raw, y_raw = window_raw(data[data["location"] == loc],
                                      month, day, half_window, col)
        reg_traces, si = fit_fn(x_raw, y_raw, x_arr, y_arr, x_arr, color, color_faint, loc, unit)
        for t in reg_traces:
            fig.add_trace(t)

        if len(locs) == 1 and not np.isnan(base):
            fig.add_hline(
                y=base,
                line=dict(color="rgba(0,0,0,0.18)", width=1, dash="dot"),
                annotation_text="full-dataset mean",
                annotation_font=dict(color=SUBTEXT, size=11),
            )

        # Stats card
        trend10   = si["trend10"]
        p_val     = si["p_val"]
        r2        = si["r2"]
        direction = vs["pos_label"] if trend10 > 0 else vs["neg_label"]
        slope_abs = abs(si["slope"])
        chg_unit  = vs["chg_unit"]
        yrs_per   = 1.0 / slope_abs if slope_abs > 1e-9 else None
        chg_str   = f"1 {chg_unit} change every  {yrs_per:.1f} yrs" if yrs_per else "No trend"

        stat_cards.append(html.Div(style={
            "background": PAGE_BG,
            "borderRadius": "8px",
            "padding": "10px 16px",
            "borderLeft": f"4px solid {color}",
            "minWidth": "230px",
            "boxShadow": "0 1px 3px rgba(0,0,0,0.06)",
        }, children=[
            html.Div(f"{loc}  [{si['method']}]",
                     style={"color": color, "fontWeight": "700",
                            "fontSize": "13px", "marginBottom": "4px"}),
            html.Div(f"{trend10:+.3f} {unit}/decade  ({direction})",
                     style={"color": TEXT, "fontSize": "14px", "fontWeight": "600"}),
            html.Div(chg_str,
                     style={"color": TEXT, "fontSize": "12px", "margin": "3px 0"}),
            html.Div(f"R²/τ² = {r2:.3f}  ·  {sig_label(p_val)}",
                     style={"color": SUBTEXT, "fontSize": "11px"}),
            html.Div(
                (_fit_label(var, si)),
                style={"color": SUBTEXT, "fontSize": "11px"}),
        ]))

    fig.update_layout(**chart_layout(ylabel, f"Trend around  <b>{date_label}</b>"))
    fig.update_layout(
        xaxis_title="Year",
        legend=dict(
            bgcolor="rgba(255,255,255,0.88)",
            bordercolor=BORDER, borderwidth=1,
            font=dict(color=TEXT, size=11),
            orientation="h",
            yanchor="bottom", y=0.01,
            xanchor="left",   x=0.01,
        ),
        hovermode="closest",
    )
    return fig, stat_cards, date_str, desc


# ── Play controls ─────────────────────────────────────────────────────────────

SPEED_MS = {1: 1200, 2: 800, 3: 600, 4: 350, 5: 150}


@callback(
    Output("play-state",    "data"),
    Output("play-interval", "disabled"),
    Output("play-interval", "interval"),
    Output("play-bwd-btn",  "style"),
    Output("play-fwd-btn",  "style"),
    Input("play-bwd-btn",   "n_clicks"),
    Input("play-fwd-btn",   "n_clicks"),
    Input("play-speed",     "value"),
    State("play-state",     "data"),
    prevent_initial_call=True,
)
def toggle_play(bwd_clicks, fwd_clicks, speed, state):
    direction = state["direction"]
    interval  = SPEED_MS.get(speed or 3, 600)
    if ctx.triggered_id == "play-speed":
        # Speed changed while playing — just update interval, keep direction
        return state, direction == 0, interval, \
               BTN_ACTIVE if direction == -1 else BTN_NORMAL, \
               BTN_ACTIVE if direction ==  1 else BTN_NORMAL
    new_dir = (0 if direction ==  1 else  1) if ctx.triggered_id == "play-fwd-btn" \
         else (0 if direction == -1 else -1)
    playing  = new_dir != 0
    return ({"direction": new_dir}, not playing, interval,
            BTN_ACTIVE if new_dir == -1 else BTN_NORMAL,
            BTN_ACTIVE if new_dir ==  1 else BTN_NORMAL)


@callback(
    Output("reg-doy",      "value"),
    Input("play-interval", "n_intervals"),
    State("reg-doy",       "value"),
    State("play-state",    "data"),
    prevent_initial_call=True,
)
def advance_doy(_, doy, state):
    d = state["direction"]
    return doy if d == 0 else (doy - 1 + d) % 365 + 1


# ── Year-round calendar callbacks ────────────────────────────────────────────

@callback(
    Output("trend-calendar", "figure"),
    Input("reg-loc",    "value"),
    Input("reg-var",    "value"),
    Input("reg-window", "value"),
    Input("reg-corr",   "value"),
    Input("reg-method", "value"),
)
def update_calendar(locs, var, half_window, corr, method):
    """Compute and render the year-round calendar using the selected regression method.
    Only fires when location/variable/window/correction/method changes — never on DOY.
    dcc.Loading shows the custom spinner while this runs (~1 s).
    """
    empty = go.Figure()
    empty.update_layout(**chart_layout())

    loc = (locs[0] if isinstance(locs, list) else locs) if locs else None
    if not loc:
        return empty

    col      = resolve_col(var or "temperature_mean", corr or "raw")
    cal_data = compute_trend_calendar(loc, col, half_window or 7, method or "theilsen")

    multi  = f"  (first of {len(locs)} selected)" if isinstance(locs, list) and len(locs) > 1 else ""
    ylabel = VARIABLES.get(var or "temperature_mean", "")
    unit   = ylabel.split("(")[-1].rstrip(")") if "(" in ylabel else ""

    doys    = sorted(int(k) for k in cal_data)
    slopes  = [cal_data[str(d)]["slope10"] for d in doys]
    pvals   = [cal_data[str(d)]["p"]       for d in doys]
    metrics = [cal_data[str(d)]["metric"]  for d in doys]

    is_ols     = (method or "theilsen") == "ols"
    metric_lbl = "R²" if is_ols else "τ"

    vs = var_style(var)
    bar_colors = []
    for m, p in zip(metrics, pvals):
        alpha = 0.95 if p < 0.001 else 0.70 if p < 0.01 else 0.40 if p < 0.05 else 0.12
        r, g, b = vs["cal_pos"] if m >= 0 else vs["cal_neg"]
        bar_colors.append(f"rgba({r},{g},{b},{alpha})")

    pos_lbl = vs["pos_label"].replace(" ↑", "")
    neg_lbl = vs["neg_label"].replace(" ↓", "")
    hover = []
    for d, s, m, p in zip(doys, slopes, metrics, pvals):
        ref = pd.Timestamp("2001-01-01") + pd.Timedelta(days=d - 1)
        sig = "★★★" if p < 0.001 else "★★" if p < 0.01 else "★" if p < 0.05 else "ns"
        direction_lbl = pos_lbl if s >= 0 else neg_lbl
        hover.append(
            f"<b>{ref.strftime('%b %d')}</b><br>"
            f"Slope: {s:+.3f} {unit}/decade  ({direction_lbl})<br>"
            f"{metric_lbl} = {m:.3f}  ·  p = {p:.3f}  {sig}"
        )

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=doys, y=slopes,
        marker_color=bar_colors,
        marker_line_width=0,
        hovertext=hover, hoverinfo="text",
        width=1,
        showlegend=False,
    ))
    fig.add_hline(y=0, line=dict(color="rgba(0,0,0,0.18)", width=1))

    # Base layout first (contains xaxis/yaxis keys from chart_layout)
    fig.update_layout(**chart_layout(
        f"{unit}/decade",
        f"Year-round trend · {loc}{multi}  "
        f"<span style='font-weight:400;font-size:11px;color:{SUBTEXT}'>"
        f"{'OLS · R²' if is_ols else 'Theil-Sen · TFPW MK · τ'}  "
        f"· {vs['cal_legend']} · opacity = significance</span>",
    ))
    # Override xaxis/yaxis separately to avoid duplicate-kwarg error
    fig.update_layout(
        xaxis=dict(
            tickvals=[1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335],
            ticktext=["Jan","Feb","Mar","Apr","May","Jun",
                      "Jul","Aug","Sep","Oct","Nov","Dec"],
            range=[0, 366],
            gridcolor=BORDER, linecolor=BORDER,
        ),
        yaxis=dict(gridcolor=BORDER, linecolor=BORDER, zerolinecolor=BORDER),
        bargap=0, bargroupgap=0,
        margin={"r": 16, "t": 44, "l": 58, "b": 36},
    )
    return fig


@callback(
    Output("reg-doy", "value", allow_duplicate=True),
    Input("trend-calendar", "clickData"),
    prevent_initial_call=True,
)
def calendar_click(click_data):
    """Click a bar to jump to that day."""
    if click_data and click_data.get("points"):
        return int(click_data["points"][0]["x"])
    return no_update


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Dashboard running at http://127.0.0.1:8050")
    app.run(debug=False)
