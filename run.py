#!/usr/bin/env python3
"""
Mindoro Rain Matrix — Flask Web App (with SQLite cache)

- Loads places from places.txt
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
DEFAULT_PLACES_FILE = "places.txt"

DAY_START_HOUR = 6
DAY_END_HOUR = 18

CLEAR_MAX = 25.0
PARTLY_MAX = 60.0

LIGHT_MAX = 2.5
MODERATE_MAX = 7.5

SCALE_MAX_MM = 7.0

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


def read_places_file(path: str) -> List[Place]:
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    places: List[Place] = []

    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue

            parts = [p.strip() for p in s.split(",")]
            if len(parts) != 3:
                raise ValueError(
                    f"Invalid format in {path} line {lineno}. "
                    f"Expected: Label, lat, lon"
                )

            label, lat, lon = parts

            places.append(
                Place(
                    label=label,
                    query=label,
                    lat=float(lat),
                    lon=float(lon),
                )
            )

    return places



def places_signature(path: str) -> str:
    st = os.stat(path)
    return f"{int(st.st_mtime)}:{st.st_size}"


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
            "hourly": "precipitation,precipitation_probability,cloudcover",
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
        time_index: List[datetime],
        cell_map: Dict[str, Dict[str, Tuple[str, float, int]]],  # icon, precip_mm, pop_pct
        from_cache: bool,
        nocache: bool,
        base_params: Dict[str, str],
    ) -> str:
    """
    Updated pill/badge colors (POP):
      - < 30%     -> white
      - 30–50%    -> light green
      - 51–80%    -> yellow
      - > 80%     -> red
    """

    title = "Mindoro Rain Forecast"

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
    {''.join(date_buttons)}
  </div>

  <a href="#" class="btn" id="downloadBtn">Download JPG</a>
</div>

<div class="table-wrap">
  <table id="rainTable">
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
            icon, precip, pop = cell_map.get(p.label, {}).get(
                t.strftime("%H:00"), ("—", 0.0, 0)
            )

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

    html += f"""
  </table>
</div>

<!-- LEGEND (after table) -->
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

<script src="https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js"></script>
<script>
document.getElementById("downloadBtn").addEventListener("click", function (e) {{
  e.preventDefault();

  const table = document.getElementById("rainTable");

  html2canvas(table, {{
    backgroundColor: "#ffffff",
    scale: 2,
    useCORS: true
  }}).then(canvas => {{
    const link = document.createElement("a");
    const d = new Date().toISOString().slice(0,10);

    link.download = `rain-matrix-${{d}}.jpg`;
    link.href = canvas.toDataURL("image/jpeg", 0.95);
    link.click();
  }});
}});
</script>

</body>
</html>
"""
    return html



# ---------------- FLASK APP ----------------

app = Flask(__name__)
cache_init()

@app.get("/")
def index():
    # Query params with sane defaults
    tz = request.args.get("tz", DEFAULT_TZ)
    country = request.args.get("country", DEFAULT_COUNTRY)  # kept for URL/cache compatibility
    model = request.args.get("model", DEFAULT_MODEL)

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
        places = read_places_file(DEFAULT_PLACES_FILE)  # returns List[Place]
        p_sig = places_signature(DEFAULT_PLACES_FILE)
    except FileNotFoundError:
        return Response(
            f"Missing places file: {DEFAULT_PLACES_FILE}\n"
            "Create it with lines like:\n"
            "AIVR, 13.174, 121.278\n",
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
    }
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

    # cell_map[place_label][hour_key] = (icon, precip_mm, pop_pct)
    cell_map: Dict[str, Dict[str, Tuple[str, float, int]]] = {}
    time_index: List[datetime] = []
    seen_hours = set()

    for p in places:
        hourly = client.hourly_forecast(p.lat, p.lon, tz, model)
        cells: Dict[str, Tuple[str, float, int]] = {}

        for t, pr, pop, cc in zip(hourly["time"], hourly["precip"], hourly["pop"], hourly["cloud"]):
            if t.date() != target_date:
                continue

            hk = t.strftime("%H:00")
            if hk not in seen_hours:
                seen_hours.add(hk)
                time_index.append(t)

            cells[hk] = (weather_icon(cc, pr, t), pr, int(pop or 0))

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
        time_index=time_index,
        cell_map=cell_map,
        from_cache=False,
        nocache=nocache,
        base_params=base_params,
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


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
