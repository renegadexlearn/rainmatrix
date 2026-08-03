#!/usr/bin/env python3
"""
Mindoro Rain Matrix — Flask Web App (with SQLite cache)

- Loads places from places.json
- Visit:
    http://127.0.0.1:5000/                 -> shows today's matrix (Asia/Manila)
    http://127.0.0.1:5000/?date=2025-12-31 -> shows that day's matrix

Query params:
    date=YYYY-MM-DD
    tz=Asia/Manila
    country=PH
    model=ecmwf_ifs
    nocache=1   -> bypass SQLite HTML cache (useful while tweaking CSS/colors)

Day range policy:
    Allowed dates are only from "today" (in tz) up to 4 days in the future.
"""

from __future__ import annotations

import os
import sqlite3
import json
import html as html_lib
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from flask import Flask, request, Response

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

# ---------------- CONFIG ----------------

DEFAULT_TZ = "Asia/Manila"
DEFAULT_COUNTRY = "PH"
DEFAULT_MODEL = "ecmwf_ifs"
DEFAULT_PLACES_FILE = "places.json"

DAY_START_HOUR = 6
DAY_END_HOUR = 18

CLEAR_MAX = 25.0
PARTLY_MAX = 60.0

LIGHT_MAX = 2.5
MODERATE_MAX = 7.5

SCALE_MAX_MM = 7.0
SOLAR_SCALE_MAX_WM2 = 1000.0
SOLAR_TOTAL_CACHE_VERSION = "solar-total-v1"
BATTERY_NOMINAL_VOLTAGE = 51.2

SKYBLUE = "#87CEEB"
VIOLET = "#8A2BE2"

# Time column palette (from your image)
TC_1 = "#FFF2BD"
TC_2 = "#F4D797"
TC_3 = "#EBB58A"
TC_4 = "#DA7F7D"
TC_5 = "#B5728E"
TC_6 = "#776E99"

# Day changer range: today .. today+4
FUTURE_DAYS_ALLOWED = 4

WEATHER_ICONS_NO_RAIN = {
    "clear_day": "☀️",
    "clear_night": "🌙",
    "partly_day": "🌤️",
    "partly_night": "🌙☁️",
    "cloudy": "☁️",
}

# SQLite cache DB path (override via env var)
CACHE_DB_PATH = os.environ.get("RAIN_CACHE_DB", "rain_cache.sqlite3")

# Cache policy
CACHE_TTL_SECONDS = 60 * 60          # 1 hour
CACHE_RETENTION_DAYS = 2             # delete rows older than 2 days

# ---------------- DATA ----------------

@dataclass
class Place:
    label: str
    query: str
    lat: float
    lon: float
    admin: str = ""

# ---------------- HELPERS ----------------

def _to_float(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def is_day(dt: datetime) -> bool:
    return DAY_START_HOUR <= dt.hour < DAY_END_HOUR


def precip_display(p_mm: float) -> str:
    return "-" if _to_float(p_mm) <= 0 else f"{p_mm:.1f}"


def weather_icon(cloudcover_pct: float, precipitation_mm: float, dt: datetime) -> str:
    cc = _to_float(cloudcover_pct)
    p = _to_float(precipitation_mm)
    dayflag = is_day(dt)

    if p > 0:
        if p <= LIGHT_MAX:
            return "🌦️" if dayflag else "🌧️"
        elif p <= MODERATE_MAX:
            return "🌧️"
        return "⛈️"

    if cc <= CLEAR_MAX:
        return WEATHER_ICONS_NO_RAIN["clear_day"] if dayflag else WEATHER_ICONS_NO_RAIN["clear_night"]
    elif cc <= PARTLY_MAX:
        return WEATHER_ICONS_NO_RAIN["partly_day"] if dayflag else WEATHER_ICONS_NO_RAIN["partly_night"]
    return WEATHER_ICONS_NO_RAIN["cloudy"]


def _hex_to_rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02X}{g:02X}{b:02X}"


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _smoothstep(t: float) -> float:
    t = _clamp01(t)
    return t * t * (3.0 - 2.0 * t)


def _lerp_color(c0: str, c1: str, t: float) -> str:
    t = _smoothstep(t)
    r0, g0, b0 = _hex_to_rgb(c0)
    r1, g1, b1 = _hex_to_rgb(c1)
    return _rgb_to_hex(
        round(r0 + (r1 - r0) * t),
        round(g0 + (g1 - g0) * t),
        round(b0 + (b1 - b0) * t),
    )


def precip_bg_color(p_mm: float) -> str:
    p = max(0.0, _to_float(p_mm))
    if p >= SCALE_MAX_MM:
        return VIOLET

    t = p / SCALE_MAX_MM
    r0, g0, b0 = _hex_to_rgb(SKYBLUE)
    r1, g1, b1 = _hex_to_rgb(VIOLET)

    return _rgb_to_hex(
        round(r0 + (r1 - r0) * t),
        round(g0 + (g1 - g0) * t),
        round(b0 + (b1 - b0) * t),
    )


def solar_bg_color(watts_m2: float) -> str:
    w = max(0.0, _to_float(watts_m2))
    t = min(w / SOLAR_SCALE_MAX_WM2, 1.0)

    if t <= 0.5:
        return _lerp_color("#F1F5F9", "#FDE68A", t / 0.5)
    return _lerp_color("#FDE68A", "#F59E0B", (t - 0.5) / 0.5)


def solar_display(watts_m2: float) -> str:
    w = _to_float(watts_m2)
    return "-" if w <= 0 else f"{round(w):.0f}"


def solar_total_display(watts_m2: float) -> str:
    return f"{round(max(0.0, _to_float(watts_m2))):.0f}"


def time_bg_color(dt: datetime) -> str:
    """
    Smooth day/night gradient for Time column using your palette.
    """
    h = dt.hour + (dt.minute / 60.0)

    stops: List[Tuple[float, str]] = [
        (0.0,  TC_6),
        (4.5,  TC_5),
        (6.5,  TC_4),
        (8.5,  TC_3),
        (11.0, TC_2),
        (13.0, TC_1),
        (15.5, TC_2),
        (18.0, TC_3),
        (19.5, TC_4),
        (21.5, TC_5),
        (24.0, TC_6),
    ]

    for (h0, c0), (h1, c1) in zip(stops, stops[1:]):
        if h0 <= h <= h1:
            if h1 == h0:
                return c1
            t = (h - h0) / (h1 - h0)
            return _lerp_color(c0, c1, t)

    return TC_6


def hour_label(dt: datetime) -> str:
    return dt.strftime("%H:00")


def place_label_from_query(q: str) -> str:
    return q.split(",")[0].strip()


def read_places_file(path: str, list_id: Optional[str] = None) -> Tuple[List[Place], List[Dict[str, str]], str]:
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    default_list_id = data.get("default_list", "")
    lists_data = data.get("lists", [])

    if not lists_data:
        raise ValueError(f"No lists found in {path}.")

    if list_id is None:
        list_id = default_list_id

    selected_list = next((lst for lst in lists_data if lst["id"] == list_id), None)
    if selected_list is None:
        selected_list = lists_data[0]
        list_id = selected_list["id"]

    places: List[Place] = []
    for p in selected_list.get("places", []):
        places.append(
            Place(
                label=p["label"],
                query=p["label"],
                lat=float(p["lat"]),
                lon=float(p["lon"]),
            )
        )

    lists_info = [{"id": lst["id"], "name": lst["name"]} for lst in lists_data]

    return places, lists_info, list_id


def places_signature(path: str, list_id: str) -> str:
    st = os.stat(path)
    return f"{int(st.st_mtime)}:{st.st_size}:{list_id}"


def read_config_file(path: str) -> Dict[str, object]:
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if "lists" not in data:
        data["lists"] = []
    if "sites" not in data:
        data["sites"] = []

    return data


def read_sites_file(path: str) -> List[Dict[str, object]]:
    data = read_config_file(path)
    sites = data.get("sites", [])
    return sites if isinstance(sites, list) else []


def safe_now_date_in_tz(tz_name: str) -> date:
    if ZoneInfo is None:
        return datetime.now().date()

    try:
        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        return datetime.now().date()


def build_url(base_params: Dict[str, str], new_date: Optional[date]) -> str:
    params = dict(base_params)
    if new_date is None:
        params.pop("date", None)
    else:
        params["date"] = new_date.isoformat()
    return "/?" + urlencode(params)


def normalized_view(view: Optional[str]) -> str:
    return "solar" if view == "solar" else "rain"


def normalize_efficiency_factor(value: float) -> float:
    factor = _to_float(value, 0.80)
    if factor > 1.0:
        factor = factor / 100.0
    return max(0.0, min(factor, 1.0))


def daily_solar_production_kwh(system_kw: float, irradiation_wh_m2: float, efficiency_factor: float) -> float:
    return max(0.0, system_kw) * (max(0.0, irradiation_wh_m2) / 1000.0) * normalize_efficiency_factor(efficiency_factor)


def production_bar_color(kwh: float, max_kwh: float) -> str:
    if max_kwh <= 0:
        return "#D1D5DB"
    t = min(max(kwh, 0.0) / max_kwh, 1.0)
    if t < 0.35:
        return _lerp_color("#D1D5DB", "#FDE68A", t / 0.35)
    if t < 0.70:
        return _lerp_color("#FDE68A", "#FBBF24", (t - 0.35) / 0.35)
    return _lerp_color("#FBBF24", "#F97316", (t - 0.70) / 0.30)


def nice_solar_axis_max(max_kwh: float, step: int = 25) -> int:
    if max_kwh <= 0:
        return step
    return max(step, int(((max_kwh + step - 0.000001) // step) * step))

# ---------------- CLIENT ----------------

class OpenMeteoClient:
    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.session = requests.Session()

    def geocode(self, query: str, country_code: Optional[str]) -> Optional[Place]:
        name_only = query.split(",")[0].strip()

        params = {
            "name": name_only,
            "count": 10,
            "language": "en",
            "format": "json",
        }
        if country_code:
            params["country_code"] = country_code.upper()

        r = self.session.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params=params,
            timeout=self.timeout,
        )
        r.raise_for_status()
        results = r.json().get("results") or []
        if not results:
            return None

        best = max(results, key=lambda x: x.get("population") or 0)

        return Place(
            label=place_label_from_query(query),
            query=query,
            lat=best["latitude"],
            lon=best["longitude"],
        )

    def hourly_forecast(self, lat: float, lon: float, tz: str, model: str):
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "precipitation,precipitation_probability,cloudcover,shortwave_radiation",
            "timezone": tz,
            "forecast_days": 7,
            "models": model,
        }
        r = self.session.get(
            "https://api.open-meteo.com/v1/dwd-icon",
            params=params,
            timeout=self.timeout,
        )
        r.raise_for_status()
        h = r.json()["hourly"]
        return {
            "time": [datetime.fromisoformat(t) for t in h["time"]],
            "precip": h["precipitation"],
            "pop": h.get("precipitation_probability") or [0] * len(h["time"]),
            "cloud": h["cloudcover"],
            "solar": h.get("shortwave_radiation") or [0] * len(h["time"]),
        }


# ---------------- CACHE (SQLite) ----------------

def cache_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CACHE_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def cache_init() -> None:
    with cache_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rain_cache (
              query_date  TEXT NOT NULL,
              target_date TEXT NOT NULL,
              tz          TEXT NOT NULL,
              country     TEXT NOT NULL,
              model       TEXT NOT NULL,
              places_sig  TEXT NOT NULL,
              html        TEXT NOT NULL,
              created_at  TEXT NOT NULL,
              PRIMARY KEY (query_date, target_date, tz, country, model, places_sig)
            )
            """
        )
    cache_prune()


def cache_prune() -> None:
    """Deletes cache rows older than CACHE_RETENTION_DAYS (based on created_at, UTC)."""
    with cache_connect() as conn:
        conn.execute(
            """
            DELETE FROM rain_cache
            WHERE created_at < datetime('now', ?)
            """,
            (f"-{CACHE_RETENTION_DAYS} days",),
        )


def cache_get(query_date: str, target_date: str, tz: str, country: str, model: str, places_sig: str) -> Optional[str]:
    with cache_connect() as conn:
        row = conn.execute(
            """
            SELECT html, created_at
            FROM rain_cache
            WHERE query_date=? AND target_date=? AND tz=? AND country=? AND model=? AND places_sig=?
            """,
            (query_date, target_date, tz, country, model, places_sig),
        ).fetchone()

        if not row:
            return None

        html, created_at = row

        # created_at stored as UTC "YYYY-MM-DD HH:MM:SS"
        try:
            created_dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
        except Exception:
            # If parsing fails, treat as expired to force refresh
            return None

        age = datetime.utcnow() - created_dt
        if age.total_seconds() > CACHE_TTL_SECONDS:
            return None

        return html


def cache_put(query_date: str, target_date: str, tz: str, country: str, model: str, places_sig: str, html: str) -> None:
    with cache_connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO rain_cache
              (query_date, target_date, tz, country, model, places_sig, html, created_at)
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                query_date,
                target_date,
                tz,
                country,
                model,
                places_sig,
                html,
                datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )


# ---------------- HTML RENDER ----------------

def render_html(
        target_date: date,
        min_date: date,
        max_date: date,
        tz: str,
        model: str,
        places: List[Place],
        lists_info: List[Dict[str, str]],
        active_list_id: str,
        time_index: List[datetime],
        cell_map: Dict[str, Dict[str, Tuple[str, float, int, float]]],  # icon, precip_mm, pop_pct, solar_wm2
        from_cache: bool,
        nocache: bool,
        base_params: Dict[str, str],
        view: str,
    ) -> str:
    """
    Updated pill/badge colors (POP):
      - < 30%     -> white
      - 30–50%    -> light green
      - 51–80%    -> yellow
      - > 80%     -> red
    """

    is_solar_view = view == "solar"
    title = "Mindoro Solar Forecast" if is_solar_view else "Mindoro Rain Forecast"
    table_id = "solarTable" if is_solar_view else "rainTable"
    download_prefix = "solar-matrix" if is_solar_view else "rain-matrix"

    prev_date = target_date - timedelta(days=1)
    next_date = target_date + timedelta(days=1)
    prev_ok = prev_date >= min_date
    next_ok = next_date <= max_date

    date_buttons = []
    d = min_date
    while d <= max_date:
        active = "active" if d == target_date else ""
        href = build_url(base_params, d)
        label = d.strftime("%a %b %d")
        date_buttons.append(f"<a class='dchip {active}' href='{href}'>{label}</a>")
        d += timedelta(days=1)

    list_buttons = []
    for l_info in lists_info:
        active = "active" if l_info["id"] == active_list_id else ""

        # Build URL for switching list but maintaining date
        l_params = dict(base_params)
        l_params["list"] = l_info["id"]
        # keep the same target_date
        l_href = build_url(l_params, target_date)

        list_buttons.append(f"<a class='dchip {active}' href='{l_href}'>{l_info['name']}</a>")

    view_buttons = []
    for view_id, label in [("rain", "Rain"), ("solar", "Solar")]:
        active = "active" if view_id == view else ""
        v_params = dict(base_params)
        if view_id == "solar":
            v_params["view"] = "solar"
        else:
            v_params.pop("view", None)
        v_href = build_url(v_params, target_date)
        view_buttons.append(f"<a class='dchip {active}' href='{v_href}'>{label}</a>")

    forecast_label = target_date.strftime("%d-%b-%y").upper()

    # ----- Legend samples (cell background gradient for precipitation) -----
    precip_samples = [
        (0.0, "0.0 mm - walang ulan"),
        (1.0, "1.0 mm - ambon lang"),
        (2.5, "2.5 mm - ulan na"),
        (5.0, "5.0 mm - malakas na ulan"),
        (SCALE_MAX_MM, f"{SCALE_MAX_MM:.1f} mm+ - buhos na ulan"),
    ]
    precip_legend_items = "".join(
        f"""
        <div class="legend-item">
          <span class="legend-rect" style="background:{precip_bg_color(mm)}"></span>
          <span class="legend-text">{label}</span>
        </div>
        """
        for mm, label in precip_samples
    )

    # Updated POP (pill/badge) colors + legend
    POP_WHITE = "#FFFFFF"  # < 30
    POP_GREEN = "#D1E7DD"  # 30–50 (light green)
    POP_YELLOW = "#FFF3CD" # 51–80
    POP_RED = "#F8D7DA"    # > 80

    pill_legend_items = f"""
      <div class="legend-item">
        <span class="legend-pill" style="background:{POP_WHITE}">
          <span class="legend-pill-dot"></span>
          POP &lt; 30%
        </span>malabong umulan
      </div>
      <div class="legend-item">
        <span class="legend-pill" style="background:{POP_GREEN}">
          <span class="legend-pill-dot"></span>
          POP 30–50%
        </span>baka umulan
      </div>
      <div class="legend-item">
        <span class="legend-pill" style="background:{POP_YELLOW}">
          <span class="legend-pill-dot"></span>
          POP 51–80%
        </span>maghanda sa posibleng ulan
      </div>
      <div class="legend-item">
        <span class="legend-pill" style="background:{POP_RED}">
          <span class="legend-pill-dot"></span>
          POP &gt; 80%
        </span>asahang uulan
      </div>
    """

    solar_samples = [
        (0.0, "0 W/m² - no usable sun"),
        (250.0, "250 W/m² - weak sun"),
        (500.0, "500 W/m² - moderate sun"),
        (750.0, "750 W/m² - strong sun"),
        (SOLAR_SCALE_MAX_WM2, f"{SOLAR_SCALE_MAX_WM2:.0f} W/m² - excellent sun"),
    ]
    solar_legend_items = "".join(
        f"""
        <div class="legend-item">
          <span class="legend-rect" style="background:{solar_bg_color(wm2)}"></span>
          <span class="legend-text">{label}</span>
        </div>
        """
        for wm2, label in solar_samples
    )

    if is_solar_view:
        legend_html = f"""
<div class="legend" aria-label="Legend">
  <h3>Legend</h3>
  <div class="legend-grid">
    <div class="legend-block">
      <div style="font-size:13px;font-weight:900;margin-bottom:8px;">Solar Irradiance</div>
      <div class="legend-items">
        {solar_legend_items}
      </div>
    </div>
  </div>
</div>
"""
    else:
        legend_html = f"""
<div class="legend" aria-label="Legend">
  <h3>Legend</h3>
  <div class="legend-grid">
    <div class="legend-block">
      <div style="font-size:13px;font-weight:900;margin-bottom:8px;">Chance of Rain</div>
      <div class="legend-items">
        {pill_legend_items}
      </div>
    </div>

    <div class="legend-block">
      <div style="font-size:13px;font-weight:900;margin-bottom:8px;">Rain Intensity</div>
      <div class="legend-items">
        {precip_legend_items}
      </div>
    </div>
  </div>
</div>
"""

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>{title}</title>

<style>
body {{ font-family: Segoe UI, Arial, sans-serif; margin:16px; }}

.navbar {{
  display:flex;
  gap:12px;
  align-items:center;
  flex-wrap:wrap;
  margin: 6px 0 14px;
}}

.btn {{
  display:inline-block;
  padding:7px 14px;
  border-radius:10px;
  background:#111;
  color:#fff;
  text-decoration:none;
  font-weight:700;
  font-size:13px;
}}

.dchips {{
  display:flex;
  gap:8px;
  flex-wrap:wrap;
}}

.dchip {{
  display:inline-block;
  padding:6px 10px;
  border-radius:999px;
  background:#fff;
  border:1px solid #ddd;
  text-decoration:none;
  color:#111;
  font-weight:700;
  font-size:13px;
}}

.dchip.active {{
  border-color:#111;
  box-shadow:0 1px 2px rgba(0,0,0,15);
}}

/* --- MOBILE-FRIENDLY TABLE: allow horizontal scroll instead of compressing --- */
.table-wrap {{
  overflow-x:auto;
  -webkit-overflow-scrolling:touch;
  border:1px solid #ddd;
  border-radius:12px;
  background:#fff;
}}

table {{
  width:max-content;
  min-width:100%;
  table-layout:fixed;
  border-collapse:separate;
  border-spacing:0;
  background:#ffffff;
}}

th, td {{ border:1px solid #ddd; padding:8px; text-align:center; }}

th {{
  background:#f5f5f5;
  position:sticky;
  top:0;
  z-index:3;
}}

th.timehead {{
  left:0;
  z-index:4;
  position:sticky;
}}

td.time {{
  width:90px;
  position:sticky;
  left:0;
  z-index:2;
  font-weight:700;
}}

.time-pill {{
  display:inline-block;
  padding:4px 10px;
  border-radius:999px;
  background:#fff;
  font-size:13px;
  font-weight:800;
  color:#111;
  box-shadow:0 1px 2px rgba(0,0,0,20);
}}

.pill {{
  display:inline-block;
  padding:4px 8px;
  background:#fff;
  border-radius:999px;
  box-shadow:0 1px 2px rgba(0,0,0,18);
  color:#111;
  line-height:1;
  white-space:nowrap;
}}

.pill .icon {{
  display:inline-block;
  vertical-align:middle;
  font-size:16px;
  margin-right:6px;
}}

.pill .val {{
  display:inline-block;
  vertical-align:middle;
  font-size:13px;
  font-weight:700;
  color:#111;
}}

.pill .pop {{
  display:inline-block;
  vertical-align:middle;
  font-size:11px;
  font-weight:800;
  margin-left:6px;
  padding:2px 6px;
  border-radius:999px;
  background:rgba(0,0,0,0.06);
  color:#111;
}}

.solar-val {{
  display:inline-block;
  min-width:56px;
  padding:4px 8px;
  border-radius:999px;
  background:rgba(255,255,255,0.86);
  box-shadow:0 1px 2px rgba(0,0,0,18);
  color:#111;
  font-size:13px;
  font-weight:900;
  line-height:1;
}}

.total-label {{
  background:#111827 !important;
  color:#fff;
}}

.total-pill {{
  display:inline-block;
  min-width:64px;
  padding:5px 10px;
  border-radius:999px;
  background:#111827;
  color:#fff;
  box-shadow:0 1px 2px rgba(0,0,0,22);
  font-size:13px;
  font-weight:900;
  line-height:1;
}}

/* Give each place column a minimum width so it stays readable */
th:not(.timehead), td:not(.time) {{
  min-width:92px;
}}

@media (max-width: 520px) {{
  body {{ margin:12px; }}
  th, td {{ padding:6px; }}
  th:not(.timehead), td:not(.time) {{ min-width:86px; }}
  .pill {{ padding:3px 7px; }}
  .pill .icon {{ font-size:14px; margin-right:5px; }}
  .pill .val {{ font-size:12px; }}
  .time-pill {{ font-size:12px; padding:3px 9px; }}
  .pill .pop {{ font-size:10px; padding:2px 5px; }}
  .solar-val {{ font-size:12px; min-width:50px; padding:3px 7px; }}
  .total-pill {{ font-size:12px; min-width:56px; padding:4px 8px; }}
}}

/* ---------- LEGEND ---------- */
.legend {{
  margin-top:14px;
  padding:12px;
  border:1px solid #ddd;
  border-radius:12px;
  background:#fff;
}}

.legend h3 {{
  margin:0 0 8px;
  font-size:14px;
}}

.legend-grid {{
  display:flex;
  flex-wrap:wrap;
  gap:18px;
}}

.legend-block {{
  min-width:220px;
}}

.legend-items {{
  display:flex;
  flex-direction:column;
  gap:8px;
}}

.legend-item {{
  display:flex;
  align-items:center;
  gap:10px;
}}

.legend-rect {{
  width:34px;
  height:18px;
  border-radius:4px;
  border:1px solid rgba(0,0,0,0.18);
  box-shadow:0 1px 1px rgba(0,0,0,0.08);
}}

.legend-text {{
  font-size:13px;
  font-weight:700;
  color:#111;
}}

.legend-pill {{
  display:inline-flex;
  align-items:center;
  gap:8px;
  padding:5px 10px;
  border-radius:999px;
  border:1px solid rgba(0,0,0,0.10);
  box-shadow:0 1px 2px rgba(0,0,0,0.12);
  font-size:13px;
  font-weight:800;
  color:#111;
}}

.legend-pill-dot {{
  width:10px;
  height:10px;
  border-radius:999px;
  background:rgba(0,0,0,0.18);
}}
</style>
</head>

<body>

<h2 style="margin:0 0 6px;">{title}</h2>

<div class="navbar">
  <div class="dchips">
    {''.join(list_buttons)}
  </div>
</div>

<div class="navbar">
  <div class="dchips">
    {''.join(date_buttons)}
  </div>

  <div class="dchips">
    {''.join(view_buttons)}
  </div>

  <a href="/solar-site" class="btn">Site Production</a>
  <a href="#" class="btn" id="downloadBtn">Download JPG</a>
</div>

<div class="table-wrap">
  <table id="{table_id}">
    <tr>
      <th class="timehead">{forecast_label}</th>
      {''.join(f"<th>{p.label}</th>" for p in places)}
    </tr>
"""

    for t in time_index:
        hk = t.strftime("%I:00 %p")
        tbg = time_bg_color(t)

        html += (
            f"<tr>"
            f"<td class='time' style='background:{tbg}'>"
            f"<span class='time-pill'>{hk}</span>"
            f"</td>"
        )

        for p in places:
            icon, precip, pop, solar = cell_map.get(p.label, {}).get(
                t.strftime("%H:00"), ("—", 0.0, 0, 0.0)
            )

            if is_solar_view:
                html += (
                    f"<td style='background:{solar_bg_color(solar)}'>"
                    f"<span class='solar-val'>{solar_display(solar)}</span>"
                    f"</td>"
                )
            else:
                bg = precip_bg_color(precip)
                val = precip_display(precip)

                # Updated badge/pill color based on precipitation probability
                pop_i = int(pop or 0)
                if pop_i > 80:
                    pill_bg = POP_RED       # > 80
                elif pop_i > 50:
                    pill_bg = POP_YELLOW    # 51–80
                elif pop_i >= 30:
                    pill_bg = POP_GREEN     # 30–50
                else:
                    pill_bg = POP_WHITE     # < 30

                html += (
                    f"<td style='background:{bg}'>"
                    f"<span class='pill' style='background:{pill_bg}'>"
                    f"<span class='icon'>{icon}</span>"
                    f"<span class='val'>{val}</span>"
                    f"</span>"
                    f"</td>"
                )

        html += "</tr>"

    if is_solar_view:
        html += (
            "<tr>"
            "<td class='time total-label'>"
            "<span class='time-pill'>TOTAL</span>"
            "</td>"
        )

        for p in places:
            total_solar = sum(
                cell_map.get(p.label, {}).get(t.strftime("%H:00"), ("—", 0.0, 0, 0.0))[3]
                for t in time_index
            )
            html += (
                "<td class='total-cell'>"
                f"<span class='total-pill'>{solar_total_display(total_solar)}</span>"
                "</td>"
            )

        html += "</tr>"

    html += f"""
  </table>
</div>

<!-- LEGEND (after table) -->
{legend_html}

<script src="https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js"></script>
<script>
document.getElementById("downloadBtn").addEventListener("click", function (e) {{
  e.preventDefault();

  const table = document.getElementById("{table_id}");

  html2canvas(table, {{
    backgroundColor: "#ffffff",
    scale: 2,
    useCORS: true
  }}).then(canvas => {{
    const link = document.createElement("a");
    const d = new Date().toISOString().slice(0,10);

    link.download = `{download_prefix}-${{d}}.jpg`;
    link.href = canvas.toDataURL("image/jpeg", 0.95);
    link.click();
  }});
}});
</script>

</body>
</html>
"""
    return html


def render_solar_site_html(
        site_name: str,
        site_id: str,
        sites: List[Dict[str, object]],
        model: str,
        daily_rows: List[Dict[str, object]],
        battery_days: List[Dict[str, object]],
        battery_capacity_ah: float,
        max_charging_ampere: float,
        initial_soc_percent: float,
        consumption_kwh_per_hour: float,
        charge_first: bool,
    ) -> str:
    safe_name = html_lib.escape(site_name)
    max_kwh = max((_to_float(row["production_kwh"]) for row in daily_rows), default=0.0)
    axis_step = 25
    axis_max = nice_solar_axis_max(max_kwh, axis_step)
    axis_ticks = list(range(axis_max, -1, -axis_step))

    site_options = ""
    for site in sites:
        option_id = str(site.get("id", "")).strip()
        option_name = str(site.get("name", option_id)).strip() or option_id
        selected = " selected" if option_id == site_id else ""
        site_options += (
            f'<option value="{html_lib.escape(option_id)}"{selected}>'
            f'{html_lib.escape(option_name)}</option>'
        )

    if not site_options:
        site_options = '<option value="">No saved sites</option>'

    y_axis_html = '<div class="y-unit">kWh</div>'
    grid_lines_html = ""
    for tick in axis_ticks:
        tick_pct = (tick / axis_max) * 100.0 if axis_max else 0.0
        tick_top = (1.0 - (tick_pct / 100.0)) * 220.0
        grid_top = 26.0 + tick_top
        y_axis_html += f'<span class="y-tick" style="top:{tick_top:.2f}px;">{tick:,}</span>'
        grid_lines_html += f'<span class="grid-line" style="top:{grid_top:.2f}px;"></span>'

    bars_html = ""
    for row in daily_rows:
        day = row["date"]
        if isinstance(day, date):
            date_label = day.strftime("%b%d")
            weekday_label = day.strftime("%a")
        else:
            date_label = html_lib.escape(str(day))
            weekday_label = ""

        production_kwh = _to_float(row["production_kwh"])
        bar_height = (production_kwh / axis_max) * 100.0 if axis_max else 0.0
        bg = production_bar_color(production_kwh, max_kwh)
        value_label = f"{int(round(production_kwh)):,}"
        bars_html += f"""
        <div class="bar-slot">
          <div class="bar-value">{value_label}</div>
          <div class="bar-track" aria-label="{date_label} estimated production {value_label} kWh">
            <div class="bar-fill" style="height:{bar_height:.2f}%;background:{bg};"></div>
          </div>
          <div class="x-label">
            <span>{date_label}</span>
            <strong>{weekday_label}</strong>
          </div>
        </div>
"""

    site_name_json = json.dumps(site_name)
    battery_forecast_json = json.dumps(
        {
            "capacityAh": max(0.0, battery_capacity_ah),
            "maxChargingAmpere": max(0.0, max_charging_ampere),
            "nominalVoltage": BATTERY_NOMINAL_VOLTAGE,
            "initialSocPercent": min(max(initial_soc_percent, 0.0), 100.0),
            "consumptionKwhPerHour": max(0.0, consumption_kwh_per_hour),
            "chargeFirst": bool(charge_first),
            "days": battery_days,
        }
    )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Solar Production Forecast - {safe_name}</title>
<style>
body {{
  font-family: Segoe UI, Arial, sans-serif;
  margin:0;
  background:#F8FAFC;
  color:#111827;
}}

.page {{
  max-width:980px;
  margin:0 auto;
  padding:18px;
}}

.topbar {{
  display:flex;
  justify-content:space-between;
  align-items:center;
  gap:12px;
  flex-wrap:wrap;
  margin-bottom:14px;
}}

h1 {{
  font-size:24px;
  margin:0;
  letter-spacing:0;
}}

.navlink {{
  display:inline-block;
  padding:7px 12px;
  border:1px solid #CBD5E1;
  border-radius:8px;
  color:#111827;
  background:#fff;
  text-decoration:none;
  font-size:13px;
  font-weight:800;
}}

.top-actions {{
  display:flex;
  gap:8px;
  flex-wrap:wrap;
}}

.site-panel {{
  background:#fff;
  border:1px solid #CBD5E1;
  border-radius:8px;
  padding:14px;
  margin-bottom:14px;
}}

.site-select-row {{
  display:flex;
  gap:10px;
  align-items:end;
  flex-wrap:wrap;
}}

.site-select-row .select-wrap {{
  flex:1 1 260px;
}}

label {{
  display:block;
  font-size:12px;
  font-weight:800;
  color:#334155;
  margin-bottom:4px;
}}

select {{
  width:100%;
  box-sizing:border-box;
  padding:8px 9px;
  border:1px solid #CBD5E1;
  border-radius:6px;
  font-size:14px;
  background:#fff;
}}

button {{
  width:100%;
  padding:9px 12px;
  border:0;
  border-radius:6px;
  background:#111827;
  color:#fff;
  font-size:14px;
  font-weight:900;
  cursor:pointer;
}}

.forecast-export {{
  background:#fff;
  border:1px solid #CBD5E1;
  border-radius:8px;
  padding:14px;
}}

.export-heading {{
  display:flex;
  justify-content:space-between;
  gap:12px;
  align-items:flex-end;
  flex-wrap:wrap;
  margin-bottom:12px;
}}

.export-kicker {{
  font-size:12px;
  font-weight:900;
  color:#475569;
  text-transform:uppercase;
  letter-spacing:0;
}}

.export-title {{
  font-size:24px;
  font-weight:900;
  margin-top:2px;
}}

.chart-card {{
  display:grid;
  grid-template-columns:58px minmax(0, 1fr);
  gap:12px;
  min-height:330px;
}}

.y-axis {{
  position:relative;
  height:220px;
  margin-top:26px;
  margin-bottom:44px;
  color:#475569;
  font-size:12px;
  font-weight:900;
}}

.y-unit {{
  position:absolute;
  right:0;
  bottom:calc(100% + 10px);
  color:#334155;
  font-size:12px;
  font-weight:900;
}}

.y-tick {{
  position:absolute;
  right:0;
  transform:translateY(-50%);
  line-height:1;
  white-space:nowrap;
}}

.chart-body {{
  position:relative;
  display:grid;
  grid-template-columns:repeat(7, minmax(54px, 1fr));
  gap:12px;
  min-width:620px;
}}

.grid-line {{
  position:absolute;
  left:0;
  right:0;
  height:0;
  border-top:1px dotted #CBD5E1;
  pointer-events:none;
}}

.bar-slot {{
  position:relative;
  z-index:1;
  display:grid;
  grid-template-rows:20px minmax(220px, 1fr) 38px;
  gap:6px;
  min-width:0;
}}

.bar-value {{
  text-align:center;
  font-size:12px;
  font-weight:900;
  color:#111827;
}}

.bar-track {{
  display:flex;
  align-items:flex-end;
  height:100%;
  min-height:220px;
  border-bottom:1px solid #94A3B8;
}}

.bar-fill {{
  width:100%;
  min-height:2px;
  border-radius:6px 6px 0 0;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.45);
}}

.x-label {{
  text-align:center;
  font-size:12px;
  font-weight:900;
  color:#111827;
  line-height:1.05;
}}

.x-label strong {{
  display:block;
  margin-top:4px;
  font-size:11px;
  color:#64748B;
}}

.chart-scroll {{
  overflow-x:auto;
  -webkit-overflow-scrolling:touch;
}}

.chart-legend {{
  display:flex;
  justify-content:flex-end;
  gap:10px;
  flex-wrap:wrap;
  margin-top:12px;
  color:#475569;
  font-size:12px;
  font-weight:800;
}}

.legend-item {{
  display:inline-flex;
  align-items:center;
  gap:5px;
}}

.legend-swatch {{
  width:12px;
  height:12px;
  border-radius:3px;
}}

.battery-card {{
  margin-top:14px;
}}

.battery-controls {{
  display:grid;
  grid-template-columns:1.2fr 1fr 1fr;
  gap:10px;
  align-items:end;
  margin-bottom:12px;
}}

.battery-control input {{
  width:100%;
  box-sizing:border-box;
  padding:8px 9px;
  border:1px solid #CBD5E1;
  border-radius:6px;
  font-size:14px;
  background:#fff;
}}

.battery-check {{
  display:flex;
  align-items:center;
  gap:8px;
  min-height:37px;
  font-size:13px;
  font-weight:900;
  color:#334155;
}}

.battery-check input {{
  width:16px;
  height:16px;
}}

.battery-chart-wrap {{
  overflow-x:auto;
  -webkit-overflow-scrolling:touch;
}}

.battery-chart {{
  display:block;
  width:100%;
  min-width:620px;
  height:280px;
}}

.battery-summary {{
  display:flex;
  gap:10px;
  justify-content:space-between;
  flex-wrap:wrap;
  margin-top:10px;
  color:#475569;
  font-size:13px;
  font-weight:800;
}}

.battery-summary strong {{
  color:#111827;
  font-size:16px;
}}

.battery-chart circle {{
  cursor:pointer;
}}

@media (max-width: 760px) {{
  .page {{ padding:12px; }}
  .chart-card {{ grid-template-columns:52px minmax(0, 1fr); }}
  .chart-body {{ min-width:560px; gap:9px; }}
  .battery-controls {{ grid-template-columns:1fr; }}
}}

@media (max-width: 460px) {{
  h1 {{ font-size:21px; }}
}}
</style>
</head>
<body>
<main class="page">
  <div class="topbar">
    <h1>Solar Production Forecast</h1>
    <div class="top-actions">
      <a class="navlink" href="/?view=solar">Solar Matrix</a>
      <a class="navlink" href="#" id="siteDownloadBtn">Download JPG</a>
    </div>
  </div>

  <section class="site-panel">
    <form method="get" action="/solar-site" class="site-select-row">
      <div class="select-wrap">
        <label for="site">Saved Site</label>
        <select id="site" name="site" onchange="this.form.submit()">
          {site_options}
        </select>
      </div>
      <input type="hidden" name="model" value="{html_lib.escape(model)}" />
      <div>
        <button type="submit">Show Site</button>
      </div>
    </form>
  </section>

  <section class="forecast-export" id="solarSiteCapture">
    <div class="export-heading">
      <div>
        <div class="export-kicker">Solar Production Forecast</div>
        <div class="export-title">{safe_name}</div>
      </div>
    </div>

    <div class="chart-scroll" role="img" aria-label="Estimated solar production by date">
      <div class="chart-card">
        <div class="y-axis" aria-hidden="true">
          {y_axis_html}
        </div>
        <div>
          <div class="chart-body">
            {grid_lines_html}
{bars_html}
          </div>
        </div>
      </div>
    </div>

    <div class="chart-legend" aria-label="Production color scale">
      <span class="legend-item"><span class="legend-swatch" style="background:#D1D5DB"></span>Low</span>
      <span class="legend-item"><span class="legend-swatch" style="background:#FDE68A"></span>Moderate</span>
      <span class="legend-item"><span class="legend-swatch" style="background:#FBBF24"></span>Strong</span>
      <span class="legend-item"><span class="legend-swatch" style="background:#F97316"></span>Peak</span>
    </div>
  </section>

  <section class="forecast-export battery-card" id="batteryForecastCard">
    <div class="export-heading">
      <div>
        <div class="export-kicker">Battery Charging Forecast</div>
        <div class="export-title">{safe_name}</div>
      </div>
    </div>

    <div class="battery-controls">
      <div class="battery-control">
        <label for="batteryDate">Forecast Date</label>
        <select id="batteryDate"></select>
      </div>
      <div class="battery-control">
        <label for="initialSoc">Initial SOC %</label>
        <input id="initialSoc" type="number" min="0" max="100" step="1" value="{min(max(initial_soc_percent, 0.0), 100.0):.0f}" />
      </div>
      <div class="battery-control">
        <label for="consumptionKwh">Approx. kWh Consumption / Hour</label>
        <input id="consumptionKwh" type="number" min="0" step="0.1" value="{max(0.0, consumption_kwh_per_hour):.1f}" />
      </div>
      <label class="battery-check" for="chargeFirst">
        <input id="chargeFirst" type="checkbox"{' checked' if charge_first else ''} />
        Charge first
      </label>
    </div>

    <div class="battery-chart-wrap">
      <svg class="battery-chart" id="batterySocChart" viewBox="0 0 720 280" role="img" aria-label="Forecasted battery state of charge from 6am to 6pm"></svg>
    </div>
    <div class="battery-summary">
      <span>Forecasted SOC: <strong id="batteryEndSoc">0%</strong></span>
      <span>Capacity: <strong>{max(0.0, battery_capacity_ah):,.0f} Ah</strong></span>
      <span>Max charge: <strong>{max(0.0, max_charging_ampere):,.0f} A</strong></span>
    </div>
  </section>
</main>
<script src="https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js"></script>
<script>
document.getElementById("siteDownloadBtn").addEventListener("click", function (e) {{
  e.preventDefault();

  const target = document.getElementById("solarSiteCapture");
  const siteName = {site_name_json};

  html2canvas(target, {{
    backgroundColor: "#ffffff",
    scale: 2,
    useCORS: true,
    width: target.scrollWidth,
    windowWidth: target.scrollWidth
  }}).then(canvas => {{
    const link = document.createElement("a");
    const d = new Date().toISOString().slice(0,10);
    const safeName = siteName.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "") || "solar-site";

    link.download = `${{safeName}}-solar-forecast-${{d}}.jpg`;
    link.href = canvas.toDataURL("image/jpeg", 0.95);
    link.click();
  }});
}});

const batteryForecast = {battery_forecast_json};
const batteryDate = document.getElementById("batteryDate");
const initialSoc = document.getElementById("initialSoc");
const consumptionKwh = document.getElementById("consumptionKwh");
const chargeFirst = document.getElementById("chargeFirst");
const batteryChart = document.getElementById("batterySocChart");
const batteryEndSoc = document.getElementById("batteryEndSoc");

function clampNumber(value, min, max) {{
  const n = Number(value);
  if (!Number.isFinite(n)) return min;
  return Math.min(Math.max(n, min), max);
}}

function svgEl(name, attrs = {{}}, text = "") {{
  const el = document.createElementNS("http://www.w3.org/2000/svg", name);
  Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
  if (text) el.textContent = text;
  return el;
}}

function initBatteryDates() {{
  batteryDate.innerHTML = "";
  (batteryForecast.days || []).forEach((day, index) => {{
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = day.label;
    batteryDate.appendChild(option);
  }});
}}

function estimateSoc(day) {{
  const capacity = Math.max(Number(batteryForecast.capacityAh) || 0, 0)
    * Math.max(Number(batteryForecast.nominalVoltage) || 0, 0) / 1000;
  const initialPct = clampNumber(initialSoc.value, 0, 100);
  const consumptionPerHour = Math.max(Number(consumptionKwh.value) || 0, 0);
  const shouldChargeFirst = chargeFirst.checked;
  const points = day?.points || [];
  const maxChargeKwh = Math.max(Number(batteryForecast.maxChargingAmpere) || 0, 0)
    * Math.max(Number(batteryForecast.nominalVoltage) || 0, 0) / 1000;
  let socKwh = capacity * (initialPct / 100);

  return points.map((point, index) => {{
    if (index === 0) {{
      const socPct = capacity > 0 ? (socKwh / capacity) * 100 : 0;
      return {{
        ...point,
        socPct,
        chargeKwh: 0,
        unmetLoadKwh: 0,
        loadKwh: 0,
        netSolarKwh: 0,
        isInitial: true
      }};
    }}

    const interval = points[index - 1];
    const production = Math.max(Number(interval.productionKwh) || 0, 0);
    const chargeableSolarKwh = shouldChargeFirst ? production : Math.max(production - consumptionPerHour, 0);
    const netSolarKwh = production - consumptionPerHour;
    const chargeKwh = chargeableSolarKwh > 0 && maxChargeKwh > 0 ? Math.min(chargeableSolarKwh, maxChargeKwh) : 0;
    const unmetLoadKwh = netSolarKwh < 0 ? Math.abs(netSolarKwh) : 0;
    socKwh = Math.min(Math.max(socKwh + chargeKwh, 0), capacity || 0);
    const socPct = capacity > 0 ? (socKwh / capacity) * 100 : 0;
    return {{ ...point, socPct, chargeKwh, unmetLoadKwh, loadKwh: consumptionPerHour, netSolarKwh }};
  }});
}}

function renderBatteryChart() {{
  const day = batteryForecast.days?.[Number(batteryDate.value) || 0];
  const points = estimateSoc(day);
  batteryChart.innerHTML = "";

  const width = 720;
  const height = 280;
  const left = 48;
  const right = 16;
  const top = 18;
  const bottom = 42;
  const plotW = width - left - right;
  const plotH = height - top - bottom;

  [0, 25, 50, 75, 100].forEach(tick => {{
    const y = top + plotH - (tick / 100) * plotH;
    batteryChart.appendChild(svgEl("line", {{
      x1: left, y1: y, x2: width - right, y2: y,
      stroke: "#CBD5E1", "stroke-width": "1", "stroke-dasharray": "3 5"
    }}));
    batteryChart.appendChild(svgEl("text", {{
      x: left - 8, y: y + 4, "text-anchor": "end",
      "font-size": "12", "font-weight": "800", fill: "#475569"
    }}, `${{tick}}%`));
  }});

  batteryChart.appendChild(svgEl("text", {{
    x: left - 8, y: top - 4, "text-anchor": "end",
    "font-size": "12", "font-weight": "900", fill: "#334155"
  }}, "SOC"));

  if (!points.length) {{
    batteryChart.appendChild(svgEl("text", {{
      x: width / 2, y: height / 2, "text-anchor": "middle",
      "font-size": "14", "font-weight": "900", fill: "#64748B"
    }}, "No battery forecast data"));
    batteryEndSoc.textContent = "0%";
    return;
  }}

  const coords = points.map((point, index) => {{
    const x = left + (points.length === 1 ? 0 : (index / (points.length - 1)) * plotW);
    const y = top + plotH - (clampNumber(point.socPct, 0, 100) / 100) * plotH;
    return {{ x, y, point, index }};
  }});

  batteryChart.appendChild(svgEl("polyline", {{
    points: coords.map(p => `${{p.x.toFixed(2)}},${{p.y.toFixed(2)}}`).join(" "),
    fill: "none", stroke: "#F97316", "stroke-width": "4",
    "stroke-linecap": "round", "stroke-linejoin": "round"
  }}));

  coords.forEach(({{ x, y, point, index }}) => {{
    const marker = svgEl("circle", {{
      cx: x, cy: y, r: "4", fill: "#FBBF24", stroke: "#92400E", "stroke-width": "1"
    }});
    marker.appendChild(svgEl(
      "title",
      {{}},
      point.isInitial
        ? `${{point.time}}: initial ${{Math.round(point.socPct)}}% SOC`
        : `${{point.time}}: ${{Math.round(point.socPct)}}% SOC | solar ${{point.productionKwh.toFixed(2)}} kWh | load ${{point.loadKwh.toFixed(2)}} kWh | charge ${{point.chargeKwh.toFixed(2)}} kWh | unmet load ${{point.unmetLoadKwh.toFixed(2)}} kWh`
    ));
    batteryChart.appendChild(marker);
    batteryChart.appendChild(svgEl("text", {{
      x, y: Math.max(top + 10, y - 10), "text-anchor": "middle",
      "font-size": "12", "font-weight": "900", fill: "#111827"
    }}, `${{Math.round(point.socPct)}}%`));
    if (index % 2 === 0 || index === coords.length - 1) {{
      batteryChart.appendChild(svgEl("text", {{
        x, y: height - 14, "text-anchor": "middle",
        "font-size": "12", "font-weight": "800", fill: "#475569"
      }}, point.time));
    }}
  }});

  const last = coords[coords.length - 1].point.socPct;
  batteryEndSoc.textContent = `${{Math.round(last)}}%`;
}}

initBatteryDates();
[batteryDate, initialSoc, consumptionKwh, chargeFirst].forEach(el => el.addEventListener("input", renderBatteryChart));
[batteryDate, initialSoc, consumptionKwh, chargeFirst].forEach(el => el.addEventListener("change", renderBatteryChart));
renderBatteryChart();
</script>
</body>
</html>
"""



# ---------------- FLASK APP ----------------

app = Flask(__name__)
cache_init()

@app.get("/")
def index():
    # Query params with sane defaults
    tz = request.args.get("tz", DEFAULT_TZ)
    country = request.args.get("country", DEFAULT_COUNTRY)  # kept for URL/cache compatibility
    model = request.args.get("model", DEFAULT_MODEL)
    list_id = request.args.get("list")
    view = normalized_view(request.args.get("view"))

    # Cache bypass
    nocache = request.args.get("nocache") == "1"

    # Keep cache tidy
    cache_prune()

    # "Today" is based on requested timezone
    qdate = safe_now_date_in_tz(tz)
    min_date = qdate
    max_date = qdate + timedelta(days=FUTURE_DAYS_ALLOWED)

    # Target date (date being queried)
    date_str = request.args.get("date")
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return Response("Invalid date format. Use YYYY-MM-DD.", status=400, mimetype="text/plain")

        if not (min_date <= target_date <= max_date):
            return Response(
                f"Date out of allowed range. Use {min_date.isoformat()} to {max_date.isoformat()} (tz={tz}).",
                status=400,
                mimetype="text/plain",
            )
    else:
        target_date = qdate

    # Places file (NEW: coordinates-based)
    try:
        places, lists_info, active_list_id = read_places_file(DEFAULT_PLACES_FILE, list_id)
        p_sig = f"{places_signature(DEFAULT_PLACES_FILE, active_list_id)}:{view}:{SOLAR_TOTAL_CACHE_VERSION}"
    except FileNotFoundError:
        return Response(
            f"Missing places file: {DEFAULT_PLACES_FILE}\n"
            "Create it as a valid JSON.\n",
            status=500,
            mimetype="text/plain",
        )
    except Exception as e:
        return Response(f"Error reading places file: {e}", status=500, mimetype="text/plain")

    if not places:
        return Response(
            f"No places found in {DEFAULT_PLACES_FILE}. Add lines like:\nAIVR, 13.174, 121.278\n",
            status=500,
            mimetype="text/plain",
        )

    # Base params for nav buttons
    base_params = {
        "tz": tz,
        "country": country,
        "model": model,
        "list": active_list_id
    }
    if view == "solar":
        base_params["view"] = "solar"
    if nocache:
        base_params["nocache"] = "1"

    # Try cache first (unless nocache)
    if not nocache:
        cached_html = cache_get(
            query_date=qdate.isoformat(),
            target_date=target_date.isoformat(),
            tz=tz,
            country=country,
            model=model,
            places_sig=p_sig,
        )
        if cached_html is not None:
            return Response(cached_html, mimetype="text/html")

    # ---------------- LIVE BUILD ----------------

    client = OpenMeteoClient()

    # cell_map[place_label][hour_key] = (icon, precip_mm, pop_pct, solar_wm2)
    cell_map: Dict[str, Dict[str, Tuple[str, float, int, float]]] = {}
    time_index: List[datetime] = []
    seen_hours = set()

    for p in places:
        hourly = client.hourly_forecast(p.lat, p.lon, tz, model)
        cells: Dict[str, Tuple[str, float, int, float]] = {}

        for t, pr, pop, cc, solar in zip(
            hourly["time"],
            hourly["precip"],
            hourly["pop"],
            hourly["cloud"],
            hourly["solar"],
        ):
            if t.date() != target_date:
                continue

            hk = t.strftime("%H:00")
            if hk not in seen_hours:
                seen_hours.add(hk)
                time_index.append(t)

            cells[hk] = (weather_icon(cc, pr, t), pr, int(pop or 0), _to_float(solar))

        cell_map[p.label] = cells

    # Sort the hours left-to-right
    time_index.sort(key=lambda x: x)

    html = render_html(
        target_date=target_date,
        min_date=min_date,
        max_date=max_date,
        tz=tz,
        model=model,
        places=places,
        lists_info=lists_info,
        active_list_id=active_list_id,
        time_index=time_index,
        cell_map=cell_map,
        from_cache=False,
        nocache=nocache,
        base_params=base_params,
        view=view,
    )

    # Store in cache (only if not nocache)
    if not nocache:
        cache_put(
            query_date=qdate.isoformat(),
            target_date=target_date.isoformat(),
            tz=tz,
            country=country,
            model=model,
            places_sig=p_sig,
            html=html,
        )

    return Response(html, mimetype="text/html")


@app.get("/solar-site")
def solar_site_page():
    tz = request.args.get("tz", DEFAULT_TZ)
    model = request.args.get("model", DEFAULT_MODEL)
    selected_site_id = (request.args.get("site") or "").strip()

    try:
        sites = read_sites_file(DEFAULT_PLACES_FILE)
    except Exception:
        sites = []

    selected_site = next(
        (site for site in sites if str(site.get("id", "")).strip() == selected_site_id),
        None,
    )
    if not selected_site_id and sites:
        selected_site = sites[0]
        selected_site_id = str(selected_site.get("id", "")).strip()

    default_name = str(selected_site.get("name", "Solar Site")) if selected_site else "Solar Site"
    default_lat = _to_float(selected_site.get("lat") if selected_site else None, 13.174000)
    default_lon = _to_float(selected_site.get("lon") if selected_site else None, 121.278000)
    default_kw = _to_float(
        selected_site.get("system_kw", selected_site.get("system_size_kw", selected_site.get("kw"))) if selected_site else None,
        95.84,
    )
    default_eff = _to_float(
        selected_site.get("efficiency_factor", selected_site.get("efficiency", selected_site.get("eff"))) if selected_site else None,
        0.80,
    )
    default_battery_capacity_ah = _to_float(
        selected_site.get(
            "total_battery_capacity_ah",
            selected_site.get("battery_capacity_ah", selected_site.get("total_battery_capacity_kwh", selected_site.get("battery_capacity_kwh"))),
        ) if selected_site else None,
        100.0,
    )
    default_max_charging_ampere = _to_float(
        selected_site.get("max_charging_ampere", selected_site.get("max_charge_ampere")) if selected_site else None,
        100.0,
    )
    default_initial_soc_percent = _to_float(
        selected_site.get("initial_soc_percent", selected_site.get("initial_soc")) if selected_site else None,
        20.0,
    )
    default_consumption_kwh_per_hour = _to_float(
        selected_site.get("consumption_kwh_per_hour", selected_site.get("hourly_consumption_kwh")) if selected_site else None,
        0.0,
    )
    default_charge_first = bool(selected_site.get("charge_first", False)) if selected_site else False

    site_name = default_name.strip() or "Solar Site"
    lat = default_lat
    lon = default_lon
    system_kw = max(0.0, default_kw)
    efficiency_factor = normalize_efficiency_factor(default_eff)
    battery_capacity_ah = max(0.0, default_battery_capacity_ah)
    max_charging_ampere = max(0.0, default_max_charging_ampere)
    initial_soc_percent = min(max(default_initial_soc_percent, 0.0), 100.0)
    consumption_kwh_per_hour = max(0.0, default_consumption_kwh_per_hour)
    charge_first = default_charge_first

    if selected_site is None and selected_site_id:
        selected_site_id = ""

    if not (-90.0 <= lat <= 90.0):
        return Response("Invalid latitude. Use a value from -90 to 90.", status=400, mimetype="text/plain")
    if not (-180.0 <= lon <= 180.0):
        return Response("Invalid longitude. Use a value from -180 to 180.", status=400, mimetype="text/plain")

    try:
        hourly = OpenMeteoClient().hourly_forecast(lat, lon, tz, model)
    except Exception as e:
        return Response(f"Error fetching forecast: {e}", status=502, mimetype="text/plain")

    by_day: Dict[date, float] = {}
    for t, solar in zip(hourly["time"], hourly["solar"]):
        by_day[t.date()] = by_day.get(t.date(), 0.0) + max(0.0, _to_float(solar))

    daily_rows: List[Dict[str, object]] = []
    for day in sorted(by_day.keys())[:7]:
        irradiation_wh_m2 = by_day[day]
        production_kwh = daily_solar_production_kwh(system_kw, irradiation_wh_m2, efficiency_factor)
        daily_rows.append(
            {
                "date": day,
                "irradiation_wh_m2": irradiation_wh_m2,
                "production_kwh": production_kwh,
            }
        )

    battery_days: List[Dict[str, object]] = []
    for day in sorted(by_day.keys())[:7]:
        points: List[Dict[str, object]] = []
        for t, solar in zip(hourly["time"], hourly["solar"]):
            if t.date() != day or not (6 <= t.hour <= 18):
                continue
            production_kwh = daily_solar_production_kwh(system_kw, _to_float(solar), efficiency_factor)
            points.append(
                {
                    "time": t.strftime("%I%p").lstrip("0").lower(),
                    "hour": t.hour,
                    "productionKwh": round(production_kwh, 3),
                }
            )
        battery_days.append(
            {
                "date": day.isoformat(),
                "label": f"{day.strftime('%b%d')} {day.strftime('%a')}",
                "points": points,
            }
        )

    html = render_solar_site_html(
        site_name=site_name,
        site_id=selected_site_id,
        sites=sites,
        model=model,
        daily_rows=daily_rows,
        battery_days=battery_days,
        battery_capacity_ah=battery_capacity_ah,
        max_charging_ampere=max_charging_ampere,
        initial_soc_percent=initial_soc_percent,
        consumption_kwh_per_hour=consumption_kwh_per_hour,
        charge_first=charge_first,
    )
    return Response(html, mimetype="text/html")


@app.get("/admin")
def admin_page():
    # Simple HTML page with Vue.js to manage places
    html = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Mindoro Rain Forecast - Admin</title>
<script src="https://cdn.jsdelivr.net/npm/vue@3/dist/vue.global.js"></script>
<style>
body { font-family: Segoe UI, Arial, sans-serif; margin:16px; background:#f5f5f5;}
.container { max-width: 960px; margin: auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
h1, h2, h3 { margin-top: 0; }
.form-group { margin-bottom: 10px; }
input { padding: 8px; border: 1px solid #ccc; border-radius: 4px; }
button { padding: 8px 12px; border: none; background: #111; color: white; border-radius: 4px; cursor: pointer; }
button:hover { background: #333; }
button.danger { background: #dc3545; }
button.danger:hover { background: #c82333; }
.list-item, .place-item, .site-item { border: 1px solid #ddd; padding: 10px; margin-bottom: 10px; border-radius: 4px; background: #fafafa; display: flex; align-items: center; gap: 10px;}
.place-item input { flex: 1; }
.site-item { flex-wrap: wrap; }
.site-field { flex: 1 1 120px; display: flex; flex-direction: column; gap: 4px; }
.site-field label { font-size: 11px; font-weight: bold; color: #555; }
.site-field input { width: 100%; box-sizing: border-box; }
.site-field.name-field { flex-basis: 180px; }
.site-field.id-field { flex-basis: 140px; }
.site-check { flex: 0 0 110px; display: flex; align-items: center; gap: 7px; font-size: 12px; font-weight: bold; color: #555; }
.site-check input { width: 16px; height: 16px; }
.actions { margin-left: auto; display: flex; gap: 5px; }
.dragger { cursor: grab; padding: 0 10px; color: #888; }
.section { margin-bottom: 30px; border-bottom: 1px solid #eee; padding-bottom: 20px;}
.section:last-child { border-bottom: none; }
</style>
</head>
<body>
<div id="app" class="container">
  <h1>Admin Page</h1>
  <div v-if="loading">Loading...</div>
  <div v-else>
    <div class="section">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 15px;">
        <h2 style="margin:0;">Lists</h2>
        <button @click="addList">Add List</button>
      </div>
      <div class="form-group">
        <label>Default List ID:</label>
        <select v-model="config.default_list" style="padding: 8px;">
            <option v-for="l in config.lists" :value="l.id">{{ l.name }} ({{ l.id }})</option>
        </select>
      </div>
      <div style="display:flex; gap: 10px; flex-wrap:wrap;">
         <div v-for="(l, index) in config.lists" :key="l.id" style="border: 1px solid #ccc; padding: 10px; border-radius: 5px; cursor:pointer;" :style="{background: activeListId === l.id ? '#e0f7fa' : '#fff'}" @click="activeListId = l.id">
            <div style="font-weight:bold;">{{ l.name }}</div>
            <div style="font-size: 12px; color: #666;">ID: {{ l.id }}</div>
            <button class="danger" style="padding: 4px 8px; font-size: 12px; margin-top:5px;" @click.stop="removeList(index)">Delete</button>
         </div>
      </div>
    </div>

    <div class="section" v-if="activeList">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 15px;">
          <h2 style="margin:0;">Edit List: <input v-model="activeList.name" placeholder="List Name" /></h2>
      </div>
      <div class="form-group">
        <label style="font-size: 12px; color: #666;">List ID (must be unique):</label>
        <input v-model="activeList.id" placeholder="List ID" style="display:block; margin-top:4px;" />
      </div>

      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 10px;">
          <h3 style="margin:0;">Places</h3>
          <button @click="addPlace">Add Place</button>
      </div>

      <div v-for="(place, index) in activeList.places" :key="index" class="place-item">
         <span class="dragger" @click="movePlaceUp(index)" title="Move Up">▲</span>
         <span class="dragger" @click="movePlaceDown(index)" title="Move Down">▼</span>
         <input v-model="place.label" placeholder="Label" style="max-width: 100px;" />
         <input v-model="place.lat" type="number" step="0.000001" placeholder="Latitude" />
         <input v-model="place.lon" type="number" step="0.000001" placeholder="Longitude" />
         <button class="danger" @click="removePlace(index)">X</button>
      </div>
    </div>

    <div class="section">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 10px;">
          <h2 style="margin:0;">Solar Sites</h2>
          <button @click="addSite">Add Site</button>
      </div>

      <div v-if="!config.sites || config.sites.length === 0" style="color:#666; font-size: 13px; margin-bottom: 10px;">
        No solar sites yet.
      </div>

      <div v-for="(site, index) in config.sites" :key="site.id || index" class="site-item">
         <div class="site-field name-field">
           <label>Site Name</label>
           <input v-model="site.name" />
         </div>
         <div class="site-field id-field">
           <label>Site ID</label>
           <input v-model="site.id" />
         </div>
         <div class="site-field">
           <label>System kW</label>
           <input v-model.number="site.system_kw" type="number" step="0.01" min="0" />
         </div>
         <div class="site-field">
           <label>Efficiency</label>
           <input v-model.number="site.efficiency_factor" type="number" step="0.01" min="0" max="1" />
         </div>
         <div class="site-field">
           <label>Battery Capacity Ah</label>
           <input v-model.number="site.total_battery_capacity_ah" type="number" step="0.1" min="0" />
         </div>
         <div class="site-field">
           <label>Max Charging A</label>
           <input v-model.number="site.max_charging_ampere" type="number" step="1" min="0" />
         </div>
         <div class="site-field">
           <label>Initial SOC %</label>
           <input v-model.number="site.initial_soc_percent" type="number" step="1" min="0" max="100" />
         </div>
         <div class="site-field">
           <label>Consumption kWh / Hour</label>
           <input v-model.number="site.consumption_kwh_per_hour" type="number" step="0.1" min="0" />
         </div>
         <label class="site-check">
           <input v-model="site.charge_first" type="checkbox" />
           Charge first
         </label>
         <div class="site-field">
           <label>Latitude</label>
           <input v-model.number="site.lat" type="number" step="0.000001" />
         </div>
         <div class="site-field">
           <label>Longitude</label>
           <input v-model.number="site.lon" type="number" step="0.000001" />
         </div>
         <a :href="'/solar-site?site=' + encodeURIComponent(site.id || '')" target="_blank" style="font-size: 13px; font-weight: bold; color:#111;">Forecast</a>
         <button class="danger" @click="removeSite(index)">X</button>
      </div>
    </div>

    <div style="text-align: right; margin-top: 20px;">
       <button @click="saveConfig" style="font-size: 16px; padding: 10px 20px;">Save Configuration</button>
    </div>
  </div>
</div>

<script>
const { createApp } = Vue;

createApp({
  data() {
    return {
      loading: true,
      config: { default_list: "", lists: [], sites: [] },
      activeListId: null
    };
  },
  computed: {
    activeList() {
      if (!this.config.lists) return null;
      return this.config.lists.find(l => l.id === this.activeListId);
    }
  },
  mounted() {
    this.fetchConfig();
  },
  methods: {
    fetchConfig() {
      fetch('/api/places')
        .then(res => res.json())
        .then(data => {
          this.config = data;
          if (!this.config.lists) this.config.lists = [];
          if (!this.config.sites) this.config.sites = [];
          this.config.sites.forEach(site => {
            if (site.total_battery_capacity_ah === undefined) {
              site.total_battery_capacity_ah = site.total_battery_capacity_kwh === undefined ? 100 : site.total_battery_capacity_kwh;
            }
            if (site.max_charging_ampere === undefined) site.max_charging_ampere = 100;
            if (site.initial_soc_percent === undefined) site.initial_soc_percent = 20;
            if (site.consumption_kwh_per_hour === undefined) site.consumption_kwh_per_hour = 0;
            if (site.charge_first === undefined) site.charge_first = false;
          });
          if (this.config.lists && this.config.lists.length > 0) {
              if(!this.activeListId || !this.config.lists.find(l=>l.id === this.activeListId)){
                  this.activeListId = this.config.lists[0].id;
              }
          }
          this.loading = false;
        });
    },
    saveConfig() {
      fetch('/api/places', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(this.config)
      })
      .then(res => {
         if (res.ok) {
             alert('Saved successfully!');
             this.fetchConfig();
         } else {
             alert('Error saving configuration.');
         }
      });
    },
    addList() {
      const newId = 'list_' + Date.now();
      this.config.lists.push({
        id: newId,
        name: 'New List',
        places: []
      });
      this.activeListId = newId;
    },
    removeList(index) {
      if (confirm('Are you sure you want to delete this list?')) {
        this.config.lists.splice(index, 1);
        if (this.config.lists.length > 0) {
            this.activeListId = this.config.lists[0].id;
        } else {
            this.activeListId = null;
        }
      }
    },
    addPlace() {
      if (this.activeList) {
        this.activeList.places.push({ label: '', lat: 0, lon: 0 });
      }
    },
    addSite() {
      if (!this.config.sites) this.config.sites = [];
      const newId = 'site_' + Date.now();
      this.config.sites.push({
        id: newId,
        name: 'New Solar Site',
        system_kw: 95.84,
        efficiency_factor: 0.80,
        total_battery_capacity_ah: 100,
        max_charging_ampere: 100,
        initial_soc_percent: 20,
        consumption_kwh_per_hour: 0,
        charge_first: false,
        lat: 13.174,
        lon: 121.278
      });
    },
    removeSite(index) {
      if (confirm('Are you sure you want to delete this site?')) {
        this.config.sites.splice(index, 1);
      }
    },
    removePlace(index) {
      if (this.activeList) {
        this.activeList.places.splice(index, 1);
      }
    },
    movePlaceUp(index) {
        if (index > 0 && this.activeList) {
            const temp = this.activeList.places[index - 1];
            this.activeList.places[index - 1] = this.activeList.places[index];
            this.activeList.places[index] = temp;
        }
    },
    movePlaceDown(index) {
        if (this.activeList && index < this.activeList.places.length - 1) {
            const temp = this.activeList.places[index + 1];
            this.activeList.places[index + 1] = this.activeList.places[index];
            this.activeList.places[index] = temp;
        }
    }
  }
}).mount('#app');
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


@app.get("/api/places")
def api_get_places():
    if not os.path.exists(DEFAULT_PLACES_FILE):
        return {"default_list": "", "lists": [], "sites": []}
    return read_config_file(DEFAULT_PLACES_FILE)


@app.post("/api/places")
def api_post_places():
    data = request.json
    if not data:
        return Response("Invalid JSON", status=400)

    if "lists" not in data:
        data["lists"] = []
    if "sites" not in data:
        data["sites"] = []

    # Save to file
    with open(DEFAULT_PLACES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return {"status": "ok"}


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
