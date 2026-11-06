"""Local SQLite schedule cache and polite public-source refreshers."""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
import sqlite3
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "schedule.sqlite3"
UA = "SWL-Band-Finder/1.0 (personal local schedule cache)"
SHORTWAVE_AM_API = "https://shortwave.am/api/results"
SHORTWAVE_LIVE = "https://shortwave.live/freq"
INFO_HOST = "https://www.short-wave.info/index.php"
FULL_FEEDS = ("shortwave.am", "shortwave.live")
QUERY_KEYS = {"band", "freq", "station", "language", "txsite"}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_stamp() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db() -> None:
    with connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS schedules (
                source TEXT NOT NULL,
                source_key TEXT NOT NULL,
                frequency_khz REAL NOT NULL,
                station TEXT NOT NULL,
                site TEXT DEFAULT '',
                language TEXT DEFAULT '',
                target TEXT DEFAULT '',
                start_utc TEXT DEFAULT '',
                end_utc TEXT DEFAULT '',
                days TEXT DEFAULT '',
                power_kw REAL,
                azimuth TEXT DEFAULT '',
                lat REAL,
                lon REAL,
                country_code TEXT DEFAULT '',
                on_air INTEGER DEFAULT 0,
                source_url TEXT DEFAULT '',
                raw_json TEXT DEFAULT '{}',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source, source_key)
            );
            CREATE INDEX IF NOT EXISTS idx_schedules_frequency ON schedules(frequency_khz);
            CREATE INDEX IF NOT EXISTS idx_schedules_station ON schedules(station COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_schedules_source ON schedules(source);
            CREATE TABLE IF NOT EXISTS source_state (
                source TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                updated_at TEXT DEFAULT '',
                row_count INTEGER DEFAULT 0,
                message TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS info_query_cache (
                query_key TEXT PRIMARY KEY,
                attempted_at TEXT NOT NULL,
                succeeded_at TEXT DEFAULT '',
                message TEXT DEFAULT ''
            );
        """)


def number(value):
    try:
        if value in (None, "", "ND", "?"):
            return None
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def coordinate(value):
    """Decode decimal, DD[NSEW]MM, or DDMMSS[NSEW] coordinates."""
    if value is None:
        return None
    text = str(value).strip().upper()
    match = re.fullmatch(r"(\d{1,3})([NSEW])(\d{1,2})", text)
    if match:
        deg, direction, minutes = match.groups()
        value = int(deg) + int(minutes) / 60
        return -value if direction in "SW" else value
    match = re.fullmatch(r"(\d{2,3})(\d{2})(\d{2})([NSEW])", text)
    if match:
        deg, minutes, seconds, direction = match.groups()
        value = int(deg) + int(minutes) / 60 + int(seconds) / 3600
        return -value if direction in "SW" else value
    try:
        return float(text)
    except ValueError:
        return None


def hhmm(value):
    text = re.sub(r"\D", "", str(value or ""))
    if len(text) == 3:
        text = "0" + text
    if len(text) == 4:
        return text[:2] + ":" + text[2:]
    return str(value or "")[:5]


def schedule_on_air(start: str, end: str, days: str = "") -> bool:
    """Estimate whether the published time/day window includes the current UTC minute."""
    now = utc_now()
    try:
        a = int(re.sub(r"\D", "", start)[-4:])
        b = int(re.sub(r"\D", "", end)[-4:])
        minute = now.hour * 100 + now.minute
        active = (a <= minute < b) if a <= b else (minute >= a or minute < b)
        if not active:
            return False
        d = (days or "").lower()
        if not d or any(x in d for x in ("everyday", "daily", "all days")):
            return True
        day_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                   "friday": 4, "saturday": 5, "sunday": 6}
        selected = {idx for name, idx in day_map.items() if name[:3] in d or name in d}
        if not selected:
            return True
        day = now.weekday()
        if a > b and minute < b:
            day = (day - 1) % 7
        return day in selected
    except (TypeError, ValueError):
        return False


def normalize_record(row: dict, source: str, key: str | None = None) -> dict | None:
    freq = number(row.get("frequency_khz", row.get("frequency")))
    station = str(row.get("station", row.get("name", "")) or "").strip()
    if not freq or not station:
        return None
    start = hhmm(row.get("start_utc", row.get("timestart", row.get("start", ""))))
    end = hhmm(row.get("end_utc", row.get("timeend", row.get("end", ""))))
    days = str(row.get("days", "") or "").strip()
    az = row.get("azimuth", row.get("azi", ""))
    aznum = number(az)
    if aznum is not None and 0 <= aznum < 360:
        az = f"{aznum:g}"
    elif str(az).strip().lower() in ("nd", "non-directional", "nondirectional") or aznum == 999:
        az = "ND"
    else:
        az = str(az or "?").strip()
    normalized = {
        "source": source,
        "source_key": str(key if key is not None else row.get("id") or hashlib.sha1(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()),
        "frequency_khz": freq,
        "station": station,
        "site": str(row.get("site", row.get("transmitter", "")) or "").strip(),
        "language": str(row.get("language", "") or "").strip(),
        "target": str(row.get("target", "") or "").strip(),
        "start_utc": start,
        "end_utc": end,
        "days": days,
        "power_kw": number(row.get("power_kw", row.get("power"))),
        "azimuth": az,
        "lat": coordinate(row.get("lat", row.get("latitude"))),
        "lon": coordinate(row.get("lon", row.get("longitude"))),
        "country_code": str(row.get("country_code", "") or "").strip().upper(),
        "on_air": bool(row.get("on_air", row.get("onnow", schedule_on_air(start, end, days)))),
        "source_url": str(row.get("source_url", "") or ""),
        "raw_json": json.dumps(row, ensure_ascii=False, separators=(",", ":")),
        "updated_at": utc_stamp(),
    }
    return normalized


def save_feed(source: str, records: list[dict], replace: bool) -> int:
    normalized = [x for x in (normalize_record(r, source) for r in records) if x]
    if not normalized:
        raise RuntimeError(f"{source} returned no usable schedule records; keeping the previous database rows.")
    fields = list(normalized[0])
    marks = ",".join("?" for _ in fields)
    with connect() as db:
        if replace:
            db.execute("DELETE FROM schedules WHERE source=?", (source,))
        db.executemany(
            f"INSERT OR REPLACE INTO schedules ({','.join(fields)}) VALUES ({marks})",
            [[record[field] for field in fields] for record in normalized],
        )
        db.execute("""INSERT INTO source_state(source,status,updated_at,row_count,message)
                      VALUES(?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET
                      status=excluded.status,updated_at=excluded.updated_at,
                      row_count=excluded.row_count,message=excluded.message""",
                   (source, "ok", utc_stamp(), len(normalized), ""))
    return len(normalized)


def record_failure(source: str, error: str) -> None:
    with connect() as db:
        prior = db.execute("SELECT row_count,updated_at FROM source_state WHERE source=?", (source,)).fetchone()
        db.execute("""INSERT INTO source_state(source,status,updated_at,row_count,message)
                      VALUES(?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET
                      status=excluded.status,message=excluded.message""",
                   (source, "error", prior["updated_at"] if prior else "", prior["row_count"] if prior else 0, str(error)[:500]))


def request(url: str, data: bytes | None = None, content_type: str | None = None, timeout: int = 45) -> bytes:
    headers = {"User-Agent": UA, "Accept": "application/json,text/html,application/xhtml+xml,*/*"}
    if content_type:
        headers["Content-Type"] = content_type
    if "shortwave.live" in url:
        headers["Referer"] = "https://shortwave.live/"
    if "shortwave.am" in url:
        headers["Referer"] = "https://shortwave.am/"
        headers["X-Requested-With"] = "XMLHttpRequest"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = response.read(20_000_001)
    if len(payload) > 20_000_000:
        raise RuntimeError("Schedule source response exceeded the 20 MB safety limit.")
    return payload


def fetch_shortwave_am() -> list[dict]:
    now = utc_now()
    form = urllib.parse.urlencode({
        "station": "ANY", "language": "ANY", "freq": "",
        "hour": f"{now.hour:02d}", "minute": f"{now.minute:02d}",
    }).encode()
    raw = request(SHORTWAVE_AM_API, form, "application/x-www-form-urlencoded; charset=UTF-8")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("Shortwave.am returned an unexpected JSON format.")
    out = []
    for row in data:
        if not isinstance(row, dict):
            continue
        row["source_url"] = "https://shortwave.am/"
        out.append(row)
    return out


class LiveScheduleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self.row = None
        self.cells: list[str] = []
        self.cell: list[str] | None = None
        self.country_code = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr" and attrs.get("start_time") is not None:
            self.row = {"id": attrs.get("id", ""), "start_utc": attrs.get("start_time", ""),
                        "end_utc": attrs.get("end_time", ""), "days": attrs.get("days", "")}
            self.cells = []
        elif tag == "td" and self.row is not None:
            self.cell = []
        elif tag == "a" and self.cell is not None:
            href = attrs.get("href", "")
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
            if "country" in query:
                self.country_code = query["country"][0]

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.cells.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if len(self.cells) >= 8:
                self.row.update({
                    "frequency": self.cells[0], "name": self.cells[1],
                    "transmitter": self.cells[2], "country_code": self.country_code,
                    "language": self.cells[3], "target": self.cells[4],
                    "start": self.cells[5], "end": self.cells[6], "days": self.cells[7],
                    "source_url": "https://shortwave.live/",
                })
                self.rows.append(self.row)
            self.row = None
            self.cell = None
            self.country_code = ""


def fetch_shortwave_live() -> list[dict]:
    now = utc_now()
    query = urllib.parse.urlencode({
        "station": "", "language": "", "target": "", "band": "2300-29999",
        "hour": f"{now.hour:02d}", "minute": f"{now.minute:02d}",
    })
    raw = request(SHORTWAVE_LIVE + "?" + query, timeout=90)
    parser = LiveScheduleParser()
    parser.feed(raw.decode("utf-8", "replace"))
    if not parser.rows:
        raise RuntimeError("Shortwave.live returned no parseable schedule rows.")
    return parser.rows


def update_full_sources() -> dict:
    """Refresh complete schedule dumps; one failed source never erases its old cache."""
    results = {}
    for name, fetcher in (("shortwave.am", fetch_shortwave_am), ("shortwave.live", fetch_shortwave_live)):
        try:
            rows = fetcher()
            count = save_feed(name, rows, replace=True)
            results[name] = {"status": "ok", "rows": count}
        except Exception as exc:
            record_failure(name, str(exc))
            results[name] = {"status": "error", "error": str(exc)}
    return results


def save_shortwave_info(rows: list[dict], query_key: str) -> int:
    def stable_key(row: dict) -> str:
        identity = "|".join(str(row.get(k, "")) for k in ("frequency_khz", "station", "start_utc", "end_utc", "language", "site"))
        return hashlib.sha1(identity.lower().encode("utf-8")).hexdigest()
    normalized = [x for x in (normalize_record(r, "short-wave.info", stable_key(r)) for r in rows) if x]
    if not normalized:
        raise RuntimeError("Short-Wave.Info returned no parseable rows for this search.")
    fields = list(normalized[0])
    marks = ",".join("?" for _ in fields)
    with connect() as db:
        db.executemany(f"INSERT OR REPLACE INTO schedules ({','.join(fields)}) VALUES ({marks})",
                       [[x[f] for f in fields] for x in normalized])
        count = db.execute("SELECT COUNT(*) FROM schedules WHERE source='short-wave.info'").fetchone()[0]
        db.execute("""INSERT INTO source_state(source,status,updated_at,row_count,message)
                      VALUES('short-wave.info','ok',?,?,?) ON CONFLICT(source) DO UPDATE SET
                      status='ok',updated_at=excluded.updated_at,row_count=excluded.row_count,message=''""",
                   (utc_stamp(), count, f"On-demand cache; last search: {query_key}"))
    return len(normalized)


def should_refresh_info(query_key: str, min_gap_minutes: int = 10, query_ttl_hours: int = 6) -> bool:
    now = utc_now()
    with connect() as db:
        row = db.execute("SELECT attempted_at,succeeded_at,message FROM info_query_cache WHERE query_key=?", (query_key,)).fetchone()
        if row and row["succeeded_at"]:
            try:
                if now - dt.datetime.fromisoformat(row["succeeded_at"]) < dt.timedelta(hours=query_ttl_hours):
                    return False
            except ValueError:
                pass
        state = db.execute("SELECT updated_at,status FROM source_state WHERE source='short-wave.info'").fetchone()
        if state and state["status"] == "error":
            try:
                if now - dt.datetime.fromisoformat(state["updated_at"]) < dt.timedelta(minutes=30):
                    return False
            except ValueError:
                pass
        latest = db.execute("SELECT attempted_at,message FROM info_query_cache ORDER BY attempted_at DESC LIMIT 1").fetchone()
        if latest:
            try:
                cooldown = 30 if latest["message"] else min_gap_minutes
                if now - dt.datetime.fromisoformat(latest["attempted_at"]) < dt.timedelta(minutes=cooldown):
                    return False
            except ValueError:
                pass
        db.execute("""INSERT INTO info_query_cache(query_key,attempted_at,succeeded_at,message)
                      VALUES(?,?, '', '') ON CONFLICT(query_key) DO UPDATE SET attempted_at=excluded.attempted_at,message=''""",
                   (query_key, utc_stamp()))
    return True


def finish_info_query(query_key: str, error: str = "") -> None:
    with connect() as db:
        if error:
            db.execute("UPDATE info_query_cache SET message=? WHERE query_key=?", (str(error)[:500], query_key))
            prior = db.execute("SELECT row_count,updated_at FROM source_state WHERE source='short-wave.info'").fetchone()
            db.execute("""INSERT INTO source_state(source,status,updated_at,row_count,message)
                          VALUES('short-wave.info','error',?,?,?) ON CONFLICT(source) DO UPDATE SET
                          status='error',message=excluded.message""",
                       (prior["updated_at"] if prior else "", prior["row_count"] if prior else 0, str(error)[:500]))
        else:
            db.execute("UPDATE info_query_cache SET succeeded_at=?,message='' WHERE query_key=?", (utc_stamp(), query_key))


def query_schedule(params: dict[str, str]) -> list[dict]:
    """Read matching rows only from the local database."""
    query = {k: v.strip() for k, v in params.items() if k in {"band", "freq", "station", "language", "txsite"} and v.strip()}
    if len(query) != 1:
        raise ValueError("Choose exactly one search: band, frequency, station, language, or transmitter site.")
    clauses, values = [], []
    if "band" in query:
        bounds = {"120m":(2300,2500),"90m":(3200,3400),"75m":(3900,4050),"60m":(4700,5100),"49m":(5800,6300),"41m":(7200,7600),"31m":(9300,9995),"25m":(11500,12200),"22m":(13500,13900),"19m":(15000,15900),"16m":(17400,17995),"15m":(18800,19100),"13m":(21450,21995),"11m":(25500,26200)}
        if query["band"] not in bounds:
            raise ValueError("Unknown broadcast band.")
        lo, hi = bounds[query["band"]]
        clauses.append("frequency_khz BETWEEN ? AND ?"); values.extend([lo, hi])
    elif "freq" in query:
        freq = number(query["freq"])
        if freq is None or not 100 <= freq <= 30000:
            raise ValueError("Frequency must be between 100 and 30000 kHz.")
        tol = 5 if params.get("porm") == "5" else 0.05
        clauses.append("frequency_khz BETWEEN ? AND ?"); values.extend([freq-tol, freq+tol])
    elif "station" in query:
        clauses.append("station LIKE ? COLLATE NOCASE"); values.append("%"+query["station"][:120]+"%")
    elif "language" in query:
        clauses.append("language LIKE ? COLLATE NOCASE"); values.append("%"+query["language"][:120]+"%")
    elif "txsite" in query:
        clauses.append("(site LIKE ? COLLATE NOCASE OR country_code LIKE ? COLLATE NOCASE)")
        values.extend(["%"+query["txsite"][:120]+"%", "%"+query["txsite"][:20]+"%"])
    with connect() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM schedules WHERE " + " AND ".join(clauses) + " ORDER BY on_air DESC, frequency_khz, station COLLATE NOCASE", values)]
    for row in rows:
        if row.get("days"):
            row["on_air"] = schedule_on_air(row["start_utc"], row["end_utc"], row["days"])
    return combine_records(rows)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _norm_language(value: str) -> str:
    text = _norm(value)
    aliases = {
        "standardarabic": "arabic", "modernstandardarabic": "arabic",
        "iranianpersian": "persian", "farsi": "persian",
    }
    return aliases.get(text, text)


def _sites_match(a: dict, b: dict) -> bool:
    if all(a.get(k) is not None and b.get(k) is not None for k in ("lat", "lon")):
        from math import asin, cos, radians, sin, sqrt
        lat1, lon1, lat2, lon2 = map(radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
        h = sin((lat2-lat1)/2)**2 + cos(lat1)*cos(lat2)*sin((lon2-lon1)/2)**2
        return 6371 * 2 * asin(sqrt(min(1, h))) <= 100
    sa, sb = _norm(a.get("site", "")), _norm(b.get("site", ""))
    if sa in {"unknown", "na", "notlisted"}: sa = ""
    if sb in {"unknown", "na", "notlisted"}: sb = ""
    if not sa or not sb:
        return True
    from difflib import SequenceMatcher
    return sa == sb or SequenceMatcher(None, sa, sb).ratio() >= 0.78


def combine_records(rows: list[dict]) -> list[dict]:
    """Merge matching cross-site schedule rows without conflating transmitter sites."""
    priority = {"short-wave.info": 0, "shortwave.am": 1, "shortwave.live": 2}
    labels = {"short-wave.info": "Short-Wave.Info", "shortwave.am": "Shortwave.am", "shortwave.live": "Shortwave.live"}
    rows.sort(key=lambda row: priority.get(row["source"], 9))
    groups: list[dict] = []
    index: dict[tuple, list[dict]] = {}
    for row in rows:
        core = (round(float(row["frequency_khz"]), 1), _norm(row["station"]), row["start_utc"], row["end_utc"], _norm_language(row["language"]))
        candidates = index.setdefault(core, [])
        target = _norm(row.get("target", ""))
        match = None
        for group in candidates:
            other_target = _norm(group.get("target", ""))
            if target and other_target and target != other_target:
                continue
            if _sites_match(group, row):
                match = group
                break
        if match is None:
            match = dict(row)
            match["sources"] = [row["source"]]
            match["source_names"] = [labels.get(row["source"], row["source"])]
            candidates.append(match)
            groups.append(match)
            continue
        match["sources"].append(row["source"])
        match["source_names"].append(labels.get(row["source"], row["source"]))
        for key in ("site", "language", "target", "days", "power_kw", "azimuth", "lat", "lon", "country_code", "source_url"):
            if match.get(key) in (None, "", "?") and row.get(key) not in (None, "", "?"):
                match[key] = row[key]
        if row["source"] == "shortwave.live":
            for key in ("site", "language", "target", "days", "country_code"):
                if row.get(key) not in (None, "", "?"):
                    match[key] = row[key]
        match["on_air"] = bool(match.get("on_air") or row.get("on_air"))
    for group in groups:
        group["on_air"] = bool(group.get("on_air"))
        group["source"] = " + ".join(dict.fromkeys(group.pop("source_names")))
    return groups


def status() -> dict:
    init_db()
    with connect() as db:
        states = {row["source"]: dict(row) for row in db.execute("SELECT * FROM source_state")}
        total = db.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]
        feeds = {row["source"]: row["n"] for row in db.execute("SELECT source,COUNT(*) AS n FROM schedules GROUP BY source")}
    for name in (*FULL_FEEDS, "short-wave.info"):
        states.setdefault(name, {"source": name, "status": "waiting", "updated_at": "", "row_count": feeds.get(name, 0), "message": ""})
        states[name]["row_count"] = feeds.get(name, 0)
    return {"total": total, "sources": states}


def full_sources_are_fresh(hours: int = 12) -> bool:
    st = status()["sources"]
    for source in FULL_FEEDS:
        entry = st[source]
        if entry["status"] != "ok" or not entry["updated_at"]:
            return False
        try:
            if utc_now() - dt.datetime.fromisoformat(entry["updated_at"]) >= dt.timedelta(hours=hours):
                return False
        except ValueError:
            return False
    return True


