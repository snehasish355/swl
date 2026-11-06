"""Wikipedia-derived India MW/FM/SW station catalogue.

This is a discovery/seed catalogue, not a verified on-air schedule. Every
record retains the article and (when available) the linked station article.
"""
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

import schedule_db

ROOT = Path(__file__).resolve().parent
DB_PATH = schedule_db.DB_PATH
FM_PAGE = "https://en.wikipedia.org/wiki/FM_broadcasting_in_India"
AIR_PAGE = "https://en.wikipedia.org/wiki/All_India_Radio"
BANGLADESH_PAGE = "https://en.wikipedia.org/wiki/Bangladesh_Betar"
REST_HTML = "https://en.wikipedia.org/api/rest_v1/page/html/"
API = "https://en.wikipedia.org/w/api.php"
UA = "SWL-Band-Finder/1.0 (personal local radio catalogue)"
INDIAN_CITY_STATES = {
    "kolkata": "West Bengal", "asansol": "West Bengal", "siliguri": "West Bengal",
    "santiniketan": "West Bengal", "bolpur": "West Bengal", "suri": "West Bengal",
    "murshidabad": "West Bengal", "chinsurah": "West Bengal", "kurseong": "West Bengal",
    "bhubaneswar": "Odisha", "cuttack": "Odisha", "balasore": "Odisha", "sambalpur": "Odisha",
    "patna": "Bihar", "bhagalpur": "Bihar", "darbhanga": "Bihar", "muzaffarpur": "Bihar",
    "ranchi": "Jharkhand", "jamshedpur": "Jharkhand", "dhanbad": "Jharkhand",
    "delhi": "Delhi", "lucknow": "Uttar Pradesh", "agra": "Uttar Pradesh", "varanasi": "Uttar Pradesh",
    "jaipur": "Rajasthan", "jodhpur": "Rajasthan", "udaipur": "Rajasthan", "ajmer": "Rajasthan",
    "ahmedabad": "Gujarat", "rajkot": "Gujarat", "surat": "Gujarat", "mumbai": "Maharashtra",
    "pune": "Maharashtra", "nagpur": "Maharashtra", "aurangabad": "Maharashtra",
    "bengaluru": "Karnataka", "bangalore": "Karnataka", "chennai": "Tamil Nadu",
    "coimbatore": "Tamil Nadu", "hyderabad": "Telangana", "guwahati": "Assam",
    "agartala": "Tripura", "shillong": "Meghalaya", "imphal": "Manipur", "aizawl": "Mizoram",
    "kohima": "Nagaland", "itanagar": "Arunachal Pradesh", "gangtok": "Sikkim",
    "jalandhar": "Punjab", "amritsar": "Punjab", "chandigarh": "Chandigarh",
    "srinagar": "Jammu and Kashmir", "jammu": "Jammu and Kashmir", "shimla": "Himachal Pradesh",
    "dehradun": "Uttarakhand", "bhopal": "Madhya Pradesh", "indore": "Madhya Pradesh",
    "jabalpur": "Madhya Pradesh", "raipur": "Chhattisgarh", "panaji": "Goa",
    "kochi": "Kerala", "thiruvananthapuram": "Kerala", "vijayawada": "Andhra Pradesh",
    "visakhapatnam": "Andhra Pradesh", "tirupati": "Andhra Pradesh",
}


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


class ArticleParser(HTMLParser):
    """Collect city list items and regional frequency table cells."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heading_tag = ""
        self.heading_text = []
        self.section = ""
        self.subsection = ""
        self.li_depth = 0
        self.li_text: list[str] = []
        self.li_links: list[tuple[str, str]] = []
        self.link_depth = 0
        self.link_text: list[str] = []
        self.link_href = ""
        self.in_tr = False
        self.in_cell = False
        self.cell_text: list[str] = []
        self.cells: list[str] = []
        self.rows: list[dict] = []
        self.items: list[dict] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("h2", "h3", "h4", "h5", "h6"):
            self.heading_tag, self.heading_text = tag, []
        elif tag == "li":
            if self.li_depth == 0:
                self.li_text, self.li_links = [], []
            self.li_depth += 1
        elif tag == "a" and self.li_depth:
            self.link_depth += 1
            self.link_text, self.link_href = [], attrs.get("href", "")
        elif tag == "tr":
            self.in_tr, self.cells = True, []
        elif tag in ("th", "td") and self.in_tr:
            self.in_cell, self.cell_text = True, []

    def handle_endtag(self, tag):
        if tag == self.heading_tag and self.heading_tag:
            heading = _clean("".join(self.heading_text))
            if self.heading_tag == "h2":
                self.section = heading
                self.subsection = ""
            else:
                self.subsection = heading
            self.heading_tag = ""
        elif tag == "a" and self.link_depth:
            label = _clean("".join(self.link_text))
            if label and self.link_href:
                self.li_links.append((label, self.link_href))
            self.link_depth -= 1
        elif tag == "li" and self.li_depth:
            self.li_depth -= 1
            if self.li_depth == 0:
                text = _clean("".join(self.li_text))
                if text:
                    self.items.append({"section": self.section, "heading": self.subsection,
                                       "text": text, "links": self.li_links[:]})
        elif tag in ("th", "td") and self.in_cell:
            self.cells.append(_clean("".join(self.cell_text)))
            self.in_cell = False
        elif tag == "tr" and self.in_tr:
            if self.cells:
                self.rows.append({"section": self.section, "heading": self.subsection,
                                  "cells": self.cells[:]})
            self.in_tr = False

    def handle_data(self, data):
        if self.heading_tag:
            self.heading_text.append(data)
        if self.li_depth:
            self.li_text.append(data)
            if self.link_depth:
                self.link_text.append(data)
        if self.in_cell:
            self.cell_text.append(data)


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html, application/json"})
    with urllib.request.urlopen(request, timeout=25) as response:
        data = response.read(16_000_001)
    if len(data) > 16_000_000:
        raise RuntimeError("Wikipedia response exceeded 16 MB")
    return data


def _article(title: str) -> tuple[ArticleParser, str]:
    url = REST_HTML + urllib.parse.quote(title.replace(" ", "_"), safe="_")
    parser = ArticleParser()
    parser.feed(_get(url).decode("utf-8", "replace"))
    return parser, "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"), safe="_()")


FREQ_RE = re.compile(r"(?i)(?:\bFM\s*)?(\d{2,5}(?:\.\d+)?)\s*(kHz|MHz)\b")


def _station_link(links: list[tuple[str, str]], station: str) -> str:
    candidates = []
    for label, href in links:
        if href.startswith("/wiki/") and not href.startswith("/wiki/File:"):
            if label.casefold() not in {"radio", "fm", "mw", "edit", "listen", "yes"}:
                candidates.append("https://en.wikipedia.org" + href)
    if candidates:
        return candidates[0]
    slug = urllib.parse.quote(station.strip().replace(" ", "_"), safe="_")
    return "https://en.wikipedia.org/wiki/" + slug


def _record(mode: str, station: str, city: str, state: str, freq_khz: float,
            station_url: str, page_url: str, details: str = "") -> dict:
    identity = "|".join((mode, station.casefold(), city.casefold(), f"{freq_khz:.3f}"))
    key = hashlib.sha1(identity.encode()).hexdigest()
    return {"mode": mode, "record_key": key, "station": station, "city": city,
            "state": state, "frequency_khz": round(freq_khz, 3), "station_url": station_url,
            "page_url": page_url, "details": details, "data_quality": "Wikipedia seed · verify on air"}


def _city_state(heading: str) -> tuple[str, str]:
    heading = _clean(re.sub(r"\[.*?\]", "", heading))
    if "," in heading:
        city, state = (part.strip() for part in heading.split(",", 1))
        return city, state
    return heading, "India"


def _normalize_air_city(place: str) -> tuple[str, str, str]:
    """Return (station/site label, listening city, state) for AIR table cells."""
    label = _clean(place.split("(", 1)[0])
    # Several AIR table entries name the service/transmitter first and put its
    # city in parentheses, e.g. Gitanjali (Kolkata A), Sanchayita (Kolkata B).
    for match in re.finditer(r"\(([^()]*)\)", place):
        nested_city = re.match(r"\s*([A-Za-z .'-]+?)(?:\s+[A-D])?\s*$", match.group(1))
        if nested_city and nested_city.group(1).casefold() in {
            "kolkata", "mumbai", "delhi", "chennai", "bengaluru", "hyderabad",
            "ahmedabad", "lucknow", "jaipur", "patna", "ranchi", "bhopal",
            "guwahati", "chandigarh", "srinagar", "jalandhar", "nagpur", "pune",
        }:
            city, state = _city_state(nested_city.group(1))
            return label, city, state
    city, state = _city_state(label)
    city = re.sub(r"\s+[A-D]$", "", city).strip()
    return label, city, state


def parse_fm(parser: ArticleParser, page_url: str) -> list[dict]:
    records = []
    for item in parser.items:
        if "radio stations by location" not in item["section"].casefold():
            continue
        city, state = _city_state(item["heading"])
        if state == "India":
            state = INDIAN_CITY_STATES.get(city.casefold(), state)
        if city.casefold() == "national capital region":
            state = "Delhi"
        if not city or city.casefold() in {"radio stations by location", "history", "content"}:
            continue
        # Each city list entry states its FM frequency directly; no cross-city
        # assignment is made from national multi-frequency summaries.
        matches = list(FREQ_RE.finditer(item["text"]))
        if not matches:
            continue
        station_text = item["text"].split("(", 1)[0].strip(" ·–—:-")
        station_text = re.sub(r"^Radio\s+", "", station_text, flags=re.I).strip()
        if not station_text:
            station_text = item["links"][-1][0] if item["links"] else "Unnamed FM station"
        station_url = _station_link(item["links"], station_text)
        for match in matches:
            freq_khz = float(match.group(1)) * 1000
            records.append(_record("FM", station_text, city, state, freq_khz,
                                   station_url, page_url, item["text"]))
    return records


def _frequency_tokens(text: str) -> list[tuple[float, str, str]]:
    matches = list(FREQ_RE.finditer(text))
    out = [(float(m.group(1)) * (1000 if m.group(2).lower() == "mhz" else 1),
            m.group(2).lower(), m.group(0)) for m in matches]
    # Most regional MW table cells contain only a bare kHz value.
    if not out:
        bare = _clean(text).replace(",", " ")
        if re.fullmatch(r"\d{3,4}(?:\s+\d{3,4})*", bare):
            out = [(float(x), "khz", x) for x in bare.split()]
    return out


def parse_air(parser: ArticleParser, page_url: str) -> list[dict]:
    records = []
    for row in parser.rows:
        if "regional service" not in (row["section"] + " " + row["heading"]).casefold():
            continue
        cells = [c for c in row["cells"] if c]
        # Regional-service tables repeat City / Frequency column pairs.
        for idx in range(0, len(cells) - 1, 2):
            place, freq_cell = cells[idx], cells[idx + 1]
            if place.casefold() in {"city", "frequency"}:
                continue
            station_label, city, state = _normalize_air_city(place)
            if state == "India":
                state = INDIAN_CITY_STATES.get(city.casefold(), state)
            if not city or len(city) > 70:
                continue
            for freq_khz, unit, raw in _frequency_tokens(freq_cell):
                if unit == "mhz" or "fm" in raw.lower():
                    mode = "FM"
                elif 2200 <= freq_khz <= 30000:
                    mode = "SW"
                elif 150 <= freq_khz <= 1800:
                    mode = "MW"
                else:
                    continue
                records.append(_record(mode, f"Akashvani {station_label}", city, state,
                                       freq_khz, page_url,
                                       page_url, f"{row['heading']} · {freq_cell}"))
    return records


def parse_bangladesh_betar(parser: ArticleParser, page_url: str) -> list[dict]:
    records = []
    current_mode = ""
    for row in parser.rows:
        if row["section"].casefold() != "stations":
            continue
        cells = row["cells"]
        if len(cells) >= 2 and "frequency" in cells[1].casefold():
            current_mode = "FM" if "mhz" in cells[1].casefold() else "MW" if "khz" in cells[1].casefold() else ""
            continue
        if not current_mode or len(cells) < 2:
            continue
        center, frequency_text = _clean(cells[0]), _clean(cells[1])
        if not center or not frequency_text:
            continue
        nums = re.findall(r"\d{2,4}(?:\.\d+)?", frequency_text)
        for number_text in nums:
            value = float(number_text)
            freq_khz = value * 1000 if current_mode == "FM" else value
            if current_mode == "FM" and not 65000 <= freq_khz <= 120000:
                continue
            if current_mode == "MW" and not 150 <= freq_khz <= 1800:
                continue
            if "," in center:
                station_label, city = (part.strip() for part in center.split(",", 1))
            else:
                station_label = center
                city = next((name for name in ("Dhaka", "Chittagong", "Rajshahi", "Khulna", "Rangpur", "Sylhet", "Barishal", "Thakurgaon", "Rangamati", "Cox's Bazar", "Bandarban", "Cumilla", "Gopalganj") if center.casefold().startswith(name.casefold())), "Dhaka")
            details = " · ".join(x for x in ((f"{cells[3]} kW" if len(cells) > 3 and cells[3] else ""),
                                              (cells[4] if len(cells) > 4 else "")) if x)
            station = f"Bangladesh Betar · {station_label} · {city}"
            records.append(_record(current_mode, station, city, "Bangladesh", freq_khz,
                                   page_url, page_url, details))
    return records


def _city_coordinates(cities: list[str]) -> dict[str, tuple[float, float]]:
    if not cities:
        return {}
    out = {}
    for offset in range(0, len(cities), 50):
        batch = cities[offset:offset + 50]
        params = urllib.parse.urlencode({"action": "query", "prop": "coordinates", "titles": "|".join(batch),
                                        "format": "json", "colimit": "50", "coprop": "primary"})
        try:
            payload = json.loads(_get(API + "?" + params).decode("utf-8"))
            aliases = {}
            query = payload.get("query", {})
            for item in query.get("normalized", []) + query.get("redirects", []):
                aliases[item.get("from", "").casefold()] = item.get("to", "").casefold()
            batch_coords = {}
            for page in query.get("pages", {}).values():
                coords = page.get("coordinates") or []
                if coords:
                    batch_coords[page.get("title", "").casefold()] = (coords[0]["lat"], coords[0]["lon"])
            out.update(batch_coords)
            for city in batch:
                resolved = aliases.get(city.casefold(), city.casefold())
                if resolved in batch_coords:
                    out[city.casefold()] = batch_coords[resolved]
        except Exception:
            continue
    return out


def refresh() -> dict:
    fm_parser, fm_url = _article("FM broadcasting in India")
    air_parser, air_url = _article("All India Radio")
    bd_parser, bd_url = _article("Bangladesh Betar")
    fm_rows, air_rows, bd_rows = parse_fm(fm_parser, fm_url), parse_air(air_parser, air_url), parse_bangladesh_betar(bd_parser, bd_url)
    records = fm_rows + air_rows + bd_rows
    # Keep the old cache intact if upstream structure changes or parsing fails.
    if len(records) < 10:
        raise RuntimeError(f"Wikipedia pages yielded only {len(records)} usable radio records; keeping prior catalogue")
    cities = sorted({r["city"] for r in records})
    coords = _city_coordinates(cities)
    for record in records:
        found = coords.get(record["city"].casefold())
        if found:
            record["lat"], record["lon"] = found
        else:
            record["lat"], record["lon"] = None, None
    now = _stamp()
    with schedule_db.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS india_radio (
            mode TEXT NOT NULL, record_key TEXT NOT NULL, station TEXT NOT NULL,
            city TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'India',
            frequency_khz REAL NOT NULL, station_url TEXT NOT NULL DEFAULT '',
            page_url TEXT NOT NULL, details TEXT NOT NULL DEFAULT '',
            data_quality TEXT NOT NULL, lat REAL, lon REAL, updated_at TEXT NOT NULL,
            PRIMARY KEY(mode, record_key))""")
        db.execute("DELETE FROM india_radio")
        db.executemany("""INSERT INTO india_radio
            (mode,record_key,station,city,state,frequency_khz,station_url,page_url,details,data_quality,lat,lon,updated_at)
            VALUES(:mode,:record_key,:station,:city,:state,:frequency_khz,:station_url,:page_url,:details,:data_quality,:lat,:lon,:updated_at)""",
            [dict(record, updated_at=now) for record in records])
        for source_name, count, url in (("wikipedia.fm.india", len(fm_rows), fm_url),
                                        ("wikipedia.air.india", len(air_rows), air_url),
                                        ("wikipedia.bangladesh.betar", len(bd_rows), bd_url)):
            db.execute("""INSERT INTO source_state(source,status,updated_at,row_count,message) VALUES(?,?,?,?,?)
                ON CONFLICT(source) DO UPDATE SET status='ok',updated_at=excluded.updated_at,
                row_count=excluded.row_count,message=excluded.message""",
                (source_name, "ok", now, count, url))
    return {"count": len(records), "updated_at": now,
            "sources": {"fm_india": len(fm_rows), "air_india": len(air_rows), "bangladesh_betar": len(bd_rows)}}


def search(mode: str, query: str = "") -> list[dict]:
    mode = mode.upper()
    if mode not in {"FM", "MW", "SW"}:
        raise ValueError("Mode must be FM, MW, or SW")
    like = f"%{query.strip()}%"
    with schedule_db.connect() as db:
        try:
            rows = db.execute("""SELECT * FROM india_radio WHERE mode=? AND
                (?='' OR station LIKE ? COLLATE NOCASE OR city LIKE ? COLLATE NOCASE OR
                 state LIKE ? COLLATE NOCASE OR CAST(frequency_khz AS TEXT) LIKE ? OR
                 CAST(frequency_khz/1000.0 AS TEXT) LIKE ?)
                ORDER BY city COLLATE NOCASE, frequency_khz, station COLLATE NOCASE LIMIT 500""",
                (mode, query.strip(), like, like, like, like, like)).fetchall()
        except sqlite3.OperationalError:
            return []
    results = []
    for row in rows:
        result = dict(row)
        result["frequency_display"] = (f"{row['frequency_khz']/1000:g} MHz" if mode == "FM"
                                       else f"{row['frequency_khz']:g} kHz")
        results.append(result)
    return results


def status() -> dict:
    with schedule_db.connect() as db:
        try:
            count = db.execute("SELECT COUNT(*) FROM india_radio").fetchone()[0]
            updated = db.execute("SELECT MAX(updated_at) FROM india_radio").fetchone()[0]
        except sqlite3.OperationalError:
            count, updated = 0, ""
    return {"count": count, "updated_at": updated or "", "sources": [FM_PAGE, AIR_PAGE, BANGLADESH_PAGE]}


def needs_refresh(days: int = 7) -> bool:
    current = status()
    if not current["count"] or not current["updated_at"]:
        return True
    try:
        updated = dt.datetime.fromisoformat(current["updated_at"])
        return dt.datetime.now(dt.timezone.utc) - updated > dt.timedelta(days=days)
    except ValueError:
        return True

