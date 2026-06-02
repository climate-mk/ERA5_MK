"""
config.py — single CONFIG object for the ERA5 Climate Dashboard.

Usage:
    from config import CONFIG

    CONFIG['name']        # "North Macedonia"
    CONFIG['stations']    # list of dicts with name/lat/lon/elevation_m
    CONFIG['features']    # dict of feature-flag booleans
    CONFIG.feature('today_section')  # True/False with safe False default

The country is selected by the COUNTRY environment variable (default "mk").
The YAML file lives at countries/<COUNTRY>.yaml.

Step 1 of refactor: this module is NEW and read-only.
mk_collect.py and mk_api.py are NOT yet modified — they still run unchanged.
"""

import os
import sys

try:
    import yaml
except ImportError:
    sys.exit(
        "pyyaml is required. Install it with:  pip install pyyaml\n"
        "Or:  pip install -r requirements.txt"
    )

# ── Required top-level keys ────────────────────────────────────────────────────

_REQUIRED_KEYS = {
    "code", "name", "timezone", "stations", "default_location",
    "languages", "default_language", "data_start_date", "baseline",
    "trend_start_year", "projection_end_year", "map", "branding", "features",
}

# Canonical feature keys with safe defaults (False = disabled when not listed).
# This is the authoritative registry — every feature the app knows about lives here.
_FEATURE_DEFAULTS = {
    "regression_chart":      False,
    "trend_calendar":        False,
    "station_map":           False,
    "hero_cards":            False,
    "today_section":         False,
    "season_heat_heatmap":   False,
    "spei_heatmap":          False,
    "drought_trend_chart":   False,
    "chatbot":               False,
    "welcome_modal":         False,
    "next_episodes_teaser":  False,
    "mk2036_section":        False,
    "analytics_export":      False,
}


# ── Loader ────────────────────────────────────────────────────────────────────

def _load(country_code: str) -> dict:
    yaml_path = os.path.join(
        os.path.dirname(__file__), "countries", f"{country_code}.yaml"
    )
    if not os.path.exists(yaml_path):
        sys.exit(
            f"Country config not found: {yaml_path}\n"
            f"Create countries/{country_code}.yaml to add this country."
        )

    with open(yaml_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        sys.exit(f"Invalid YAML (expected a mapping): {yaml_path}")

    # Validate required keys
    missing = _REQUIRED_KEYS - raw.keys()
    if missing:
        sys.exit(
            f"countries/{country_code}.yaml is missing required keys: "
            + ", ".join(sorted(missing))
        )

    # Validate stations list
    stations = raw.get("stations", [])
    if not stations or not isinstance(stations, list):
        sys.exit(f"countries/{country_code}.yaml: 'stations' must be a non-empty list")
    for i, s in enumerate(stations):
        for field in ("name", "lat", "lon", "elevation_m"):
            if field not in s:
                sys.exit(
                    f"countries/{country_code}.yaml: station[{i}] missing '{field}'"
                )

    # Validate baseline
    bl = raw.get("baseline", {})
    if "start" not in bl or "end" not in bl:
        sys.exit(
            f"countries/{country_code}.yaml: 'baseline' must have 'start' and 'end'"
        )

    # Resolve feature flags: start from all-False defaults, overlay YAML values
    yaml_features = raw.get("features", {})
    if not isinstance(yaml_features, dict):
        sys.exit(
            f"countries/{country_code}.yaml: 'features' must be a mapping"
        )
    resolved_features = {**_FEATURE_DEFAULTS, **yaml_features}
    # Warn about unrecognised feature keys (not fatal — forward-compatible)
    unknown = set(yaml_features.keys()) - set(_FEATURE_DEFAULTS.keys())
    if unknown:
        print(
            f"[config] Warning: unrecognised feature key(s) in {country_code}.yaml: "
            + ", ".join(sorted(unknown)),
            file=sys.stderr,
        )
    raw["features"] = resolved_features

    return raw


class _Config(dict):
    """dict subclass with a convenience .feature() accessor."""

    def feature(self, key: str) -> bool:
        """Return True if the named feature is enabled, False otherwise."""
        return bool(self.get("features", {}).get(key, False))


# ── Module-level singleton ─────────────────────────────────────────────────────

_COUNTRY = os.getenv("COUNTRY", "mk").strip().lower()
CONFIG: _Config = _Config(_load(_COUNTRY))
