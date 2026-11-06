#!/usr/bin/env python3
"""Local schedule adapter for the SWL Band Finder.

It fetches public schedule pages from Short-Wave.Info, extracts their result
table, and serves JSON to the local HTML UI. The listener is bound to loopback
only; query parameters are allow-listed and cannot select arbitrary URLs.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import schedule_db
import india_radio

HOST = "127.0.0.1"
PORT = 9080
ROOT = Path(__file__).resolve().parent
BASE = "https://www.short-wave.info/index.php"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
QUERY_KEYS = {"band", "freq", "station", "language", "txsite"}

WEATHER_URL = "https://api.openweathermap.org/data/4.0/onecall/current"
WEATHER_CACHE_SECONDS = 900
weather_cache: dict[str, tuple[float, dict]] = {}
weather_lock = threading.Lock()


def load_local_env() -> None:
    """Load simple KEY=VALUE settings without a third-party dotenv dependency."""
    path = ROOT / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip().strip("\"'")
        if name and name not in os.environ:
            os.environ[name] = value


def fetch_weather(lat: float, lon: float) -> dict:
    cache_key = f"{lat:.2f},{lon:.2f}"
    now = time.time()
    with weather_lock:
        cached = weather_cache.get(cache_key)
        if cached and now - cached[0] < WEATHER_CACHE_SECONDS:
            return cached[1]
    api_key = os.environ.get("OPENWEATHER_API_KEY", "").strip()
    if not api_key or api_key.lower() in {"your_api_key_here", "add_your_key_here", "paste_your_api_key_here"}:
        raise RuntimeError("Add your OpenWeather API key to .env as OPENWEATHER_API_KEY=your_key")
    query = urllib.parse.urlencode({"lat": lat, "lon": lon, "units": "metric", "appid": api_key})
    request = urllib.request.Request(WEATHER_URL + "?" + query, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError("OpenWeather rejected the key or One Call API 4.0 subscription. Check your account and key in .env") from None
        raise RuntimeError(f"OpenWeather returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise RuntimeError("Could not retrieve current weather from OpenWeather") from None
    records = payload.get("data") or []
    current = records[0] if isinstance(records, list) and records else records if isinstance(records, dict) else {}
    conditions = current.get("weather") or []
    condition = conditions[0] if conditions else {}
    condition_id = condition.get("id")
    result = {
        "lat": round(lat, 2), "lon": round(lon, 2),
        "observed_at": current.get("dt"),
        "temperature_c": current.get("temp"),
        "feels_like_c": current.get("feels_like"),
        "humidity": current.get("humidity"),
        "pressure_hpa": current.get("pressure"),
        "wind_mps": current.get("wind_speed"),
        "wind_deg": current.get("wind_deg"),
        "visibility_m": current.get("visibility"),
        "clouds_pct": current.get("clouds"),
        "condition": condition.get("main", "Unknown"),
        "icon": condition.get("icon", ""),
        "description": condition.get("description", "Current conditions"),
        "condition_id": condition_id,
        "storm_noise_risk": isinstance(condition_id, int) and 200 <= condition_id < 300,
    }
    with weather_lock:
        weather_cache[cache_key] = (now, result)
    return result


load_local_env()


def clean(value: str) -> str:
    return " ".join(value.replace("\u2009", " ").replace("\xa0", " ").split())


class ResultsParser(HTMLParser):
    """Extract the known `table#output` fields without retaining remote markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_output = False
        self.row: dict | None = None
        self.cell_index: int | None = None
        self.cell_text: list[str] = []
        self.cells: list[str] = []
        self.links: list[dict[str, str]] = []
        self.link: dict[str, str] | None = None
        self.records: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "table" and a.get("id") == "output":
            self.in_output = True
            return
        if not self.in_output:
            return
        if tag == "tr" and (a.get("id") or "").startswith("row") and a.get("id") != "rowZ":
            self.row = {
                "on_air": "onair" in (a.get("class") or "").split(),
                "lat": None,
                "lon": None,
            }
            match = re.search(r"cross\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", a.get("onmouseover") or "")
            if match:
                self.row["lat"], self.row["lon"] = map(float, match.groups())
            self.cells = []
            self.links = []
        elif tag == "td" and self.row is not None:
            self.cell_index = len(self.cells)
            self.cell_text = []
        elif tag == "a" and self.cell_index is not None:
            self.link = {"href": a.get("href") or "", "text": ""}
        elif tag == "div" and self.cell_index == 8 and self.row is not None:
            classes = (a.get("class") or "").split()
            self.row["country_code"] = next((c for c in classes if c != "flag"), a.get("title", ""))

    def handle_data(self, data: str) -> None:
        if self.cell_index is not None:
            self.cell_text.append(data)
        if self.link is not None:
            self.link["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if not self.in_output:
            return
        if tag == "a" and self.link is not None:
            self.link["text"] = clean(self.link["text"])
            self.links.append(self.link)
            self.link = None
        elif tag == "td" and self.cell_index is not None:
            self.cells.append(clean("".join(self.cell_text)))
            self.cell_index = None
        elif tag == "tr" and self.row is not None:
            self._finish_row()
            self.row = None
        elif tag == "table":
            self.in_output = False

    def _finish_row(self) -> None:
        if len(self.cells) < 9:
            return
        cells = self.cells
        freq_match = re.search(r"\d+(?:\.\d+)?", cells[0])
        power_match = re.search(r"\d+(?:\.\d+)?", cells[6])
        freq_link = next((x["href"] for x in self.links if "freq=" in x["href"]), "")
        station_link = next((x["href"] for x in self.links if "station=" in x["href"]), "")
        language_links = [x["href"] for x in self.links if "language=" in x["href"]]
        site_link = next((x["href"] for x in self.links if "txsite=" in x["href"]), "")
        site_anchor = next((x["text"] for x in self.links if "txsite=" in x["href"]), "")
        languages = [x["text"] for x in self.links if "language=" in x["href"]]
        record = {
            **self.row,
            "frequency_khz": float(freq_match.group()) if freq_match else None,
            "station": cells[1],
            "start_utc": cells[2],
            "end_utc": cells[3],
            "days": cells[4],
            "language": ", ".join(languages) if languages else cells[5],
            "power_kw": float(power_match.group()) if power_match else None,
            "azimuth": cells[7] or "?",
            "site": clean((site_anchor or cells[8]).split("Latitude:", 1)[0]),
            "country_code": self.row.get("country_code", ""),
            "frequency_link": urllib.parse.urljoin(BASE, freq_link) if freq_link else "",
            "station_link": urllib.parse.urljoin(BASE, station_link) if station_link else "",
            "language_links": [urllib.parse.urljoin(BASE, x) for x in language_links],
            "site_link": urllib.parse.urljoin(BASE, site_link) if site_link else "",
        }
        if record["frequency_khz"] and record["station"]:
            self.records.append(record)


def fetch_shortwave_info(params: dict[str, str]) -> dict:
    query = {k: v for k, v in params.items() if k in QUERY_KEYS and v}
    if len(query) != 1:
        raise ValueError("Choose exactly one search: band, frequency, station, language, or transmitter site.")
    if "band" in query and not re.fullmatch(r"(?:120|90|75|60|49|41|31|25|22|19|16|15|13|11)m", query["band"]):
        raise ValueError("Unknown broadcast band.")
    if "freq" in query:
        try:
            freq = float(query["freq"])
        except ValueError as exc:
            raise ValueError("Frequency must be in kHz.") from exc
        if not 100 <= freq <= 30000:
            raise ValueError("Frequency must be between 100 and 30000 kHz.")
        query["freq"] = str(freq).rstrip("0").rstrip(".") if "." in str(freq) else str(int(freq))
        if params.get("porm") == "5":
            query["porm"] = "5"
    key = next(iter(query))
    query[key] = query[key][:120]
    url = BASE + "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        page = response.read(5_000_001)
    if len(page) > 5_000_000:
        raise RuntimeError("Schedule response was unexpectedly large.")
    parser = ResultsParser()
    try:
        html = page.decode("utf-8")
    except UnicodeDecodeError:
        html = page.decode("cp1252", "replace")
    parser.feed(html)
    for row in parser.records:
        row["source_url"] = url
    return {"source": url, "count": len(parser.records), "stations": parser.records}


class Handler(BaseHTTPRequestHandler):
    server_version = "SWLBandFinder/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print("[swl] " + fmt % args)

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            try:
                body = (ROOT / "index.html").read_bytes()
            except OSError:
                self.send_error(500, "index.html is missing")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/schedule":
            params = {k: values[0] for k, values in urllib.parse.parse_qs(parsed.query).items() if values}
            try:
                # Short-Wave.Info has no public full-database export. Refresh only
                # the requested search, with a persistent throttle, and keep its
                # rows beside the complete local feeds from Shortwave.am/.live.
                valid = schedule_db.query_schedule(params)
                key, value = next((k, v) for k, v in params.items() if k in schedule_db.QUERY_KEYS)
                query_key = f"{key}:{value.lower()}:{params.get('porm','')}"
                if schedule_db.should_refresh_info(query_key):
                    try:
                        fresh = fetch_shortwave_info(params)
                        schedule_db.save_shortwave_info(fresh["stations"], query_key)
                        schedule_db.finish_info_query(query_key)
                    except Exception as exc:
                        schedule_db.finish_info_query(query_key, str(exc))
                stations = schedule_db.query_schedule(params)
                state = schedule_db.status()
                source_counts = ", ".join(f"{name}: {state['sources'][name]['row_count']}" for name in ("shortwave.am", "shortwave.live", "short-wave.info") if state["sources"][name]["row_count"])
                source_query = {key: value}
                if key == "freq" and params.get("porm") == "5":
                    source_query["porm"] = "5"
                self.send_json(200, {"count": len(stations), "stations": stations,
                                     "source": "local SQLite schedule cache", "source_counts": source_counts,
                                     "source_url": BASE + "?" + urllib.parse.urlencode(source_query),
                                     "database_updated": max((x["updated_at"] for x in state["sources"].values() if x["updated_at"]), default=""),
                                     "refresh_running": refresh_running})
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception as exc:  # keep error responses useful to the UI
                self.send_json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/weather":
            params = urllib.parse.parse_qs(parsed.query)
            try:
                lat, lon = float(params.get("lat", [""])[0]), float(params.get("lon", [""])[0])
                if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                    raise ValueError("Latitude or longitude is outside its valid range.")
                self.send_json(200, fetch_weather(lat, lon))
            except ValueError as exc:
                self.send_json(400, {"error": str(exc) or "Enter valid receiver coordinates."})
            except RuntimeError as exc:
                self.send_json(503, {"error": str(exc)})
            return
        if parsed.path == "/api/status":
            state = schedule_db.status()
            state["refresh_running"] = refresh_running
            state["radio_catalogue"] = india_radio.status()
            self.send_json(200, state)
            return
        if parsed.path == "/api/config":
            try:
                self.send_json(200, json.loads((ROOT / "dx_modes.json").read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                self.send_json(500, {"error": "dx_modes.json is missing or invalid"})
            return
        if parsed.path == "/api/radio":
            params = urllib.parse.parse_qs(parsed.query)
            mode = (params.get("mode", [""])[0] or "FM").upper()
            query = params.get("q", [""])[0]
            try:
                mode_config = json.loads((ROOT / "dx_modes.json").read_text(encoding="utf-8"))
                if not mode_config.get("modes", {}).get(mode.lower(), {}).get("enabled", False):
                    self.send_json(403, {"error": f"{mode} mode is disabled in dx_modes.json"})
                    return
                rows = india_radio.search(mode, query)
                self.send_json(200, {"mode": mode, "count": len(rows), "stations": rows,
                                     "catalogue": india_radio.status(), "verified": False})
            except (OSError, json.JSONDecodeError) as exc:
                self.send_json(500, {"error": "Could not read mode settings."})
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/update":
            started = start_refresh()
            self.send_json(202, {"running": refresh_running, "started": started, "status": schedule_db.status()})
            return
        if parsed.path == "/api/radio/update":
            started = start_refresh()
            self.send_json(202, {"running": refresh_running, "started": started,
                                 "catalogue": india_radio.status()})
            return
        if parsed.path == "/api/shutdown":
            self.send_json(200, {"stopping": True})
            threading.Thread(target=self.server.shutdown, name="server-shutdown", daemon=True).start()
            return
        self.send_error(404)


refresh_lock = threading.Lock()
refresh_running = False


def refresh_worker() -> None:
    global refresh_running
    try:
        try:
            schedule_db.update_full_sources()
        except Exception as exc:
            schedule_db.record_failure("shortwave.am", str(exc))
        try:
            india_radio.refresh()
        except Exception as exc:
            schedule_db.record_failure("wikipedia.india.radio", str(exc))
    finally:
        with refresh_lock:
            refresh_running = False


def start_refresh() -> bool:
    global refresh_running
    with refresh_lock:
        if refresh_running:
            return False
        refresh_running = True
    threading.Thread(target=refresh_worker, name="schedule-refresh", daemon=True).start()
    return True


def main() -> None:
    schedule_db.init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"SWL Band Finder is running at http://{HOST}:{PORT}/")
    print("Keep this window open while using the app. Press Ctrl+C to stop.")
    if not schedule_db.full_sources_are_fresh() or india_radio.needs_refresh():
        start_refresh()
    threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}/")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping SWL Band Finder.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()


