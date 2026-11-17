"""
NYC subway next-train board for any station.

Pulls the MTA live GTFS-realtime feeds, extracts arrivals for the chosen
station's platforms, and serves them as JSON plus a full-screen HTML page.

Setup (once):  python build_stations.py      -> creates stations.json (MTA platforms)
               python build_wiki_index.py   -> creates station_index.json (Wikipedia names -> MTA ids)
Run:           python server.py
Open:          http://localhost:8000/?station=30th-avenue   (30 Av)
               http://localhost:8000/               (picker; remembers your choice)

Optional: set MTA_API_KEY in the environment if the MTA ever requires one.
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from google.transit import gtfs_realtime_pb2

BASE_FEED = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/"
# All subway realtime feeds. Together they cover every line.
FEEDS = ["nyct%2Fgtfs", "nyct%2Fgtfs-ace", "nyct%2Fgtfs-bdfm", "nyct%2Fgtfs-g",
         "nyct%2Fgtfs-jz", "nyct%2Fgtfs-nqrw", "nyct%2Fgtfs-l", "nyct%2Fgtfs-si"]
PER_PLATFORM = 5            # default number of trains per direction
MAX_PER_PLATFORM = 20       # upper limit the page slider can request
CACHE_SECONDS = 20
GRACE_SECONDS = 30          # keep a train for 30s after its scheduled arrival
PORT = int(os.environ.get("PORT", "8000"))
HERE = Path(__file__).resolve().parent

MTA_STATIONS = {}   # MTA complex id (e.g. "R04") -> platforms, from stations.json
INDEX = []          # Wikipedia station list, cross-referenced to MTA ids
INDEX_BY_ID = {}    # index id (e.g. "30-av") -> entry
MTA_TO_INDEX = {}   # MTA complex id -> index id, so old ?station=R04 links still work
_cache = {"at": 0.0, "feeds": None, "errors": []}
_lock = threading.Lock()


def load_stations():
    mta_path = HERE / "stations.json"
    index_path = HERE / "station_index.json"
    if not mta_path.exists() or not index_path.exists():
        raise SystemExit("Missing data files. Run: python build_stations.py, then python build_wiki_index.py")
    for s in json.loads(mta_path.read_text(encoding="utf-8")):
        MTA_STATIONS[s["id"]] = s
    INDEX.extend(json.loads(index_path.read_text(encoding="utf-8")))
    for entry in INDEX:
        INDEX_BY_ID[entry["id"]] = entry
        for mid in entry["mta_ids"]:
            MTA_TO_INDEX.setdefault(mid, entry["id"])


def resolve_station(station_key):
    """Accept an index id, or an MTA complex id for backward compatibility."""
    if station_key in INDEX_BY_ID:
        return INDEX_BY_ID[station_key]
    if station_key in MTA_TO_INDEX:
        return INDEX_BY_ID[MTA_TO_INDEX[station_key]]
    return None


def fetch_one(feed_name):
    headers = {"User-Agent": "subway-board/1.0"}
    key = os.environ.get("MTA_API_KEY")
    if key:
        headers["x-api-key"] = key
    req = urllib.request.Request(BASE_FEED + feed_name, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()
    msg = gtfs_realtime_pb2.FeedMessage()
    msg.ParseFromString(data)
    return msg


def get_feeds():
    """Return (list of FeedMessage, list of error strings), cached for CACHE_SECONDS."""
    with _lock:
        if _cache["feeds"] is None or time.time() - _cache["at"] > CACHE_SECONDS:
            feeds, errors = [], []
            with ThreadPoolExecutor(max_workers=len(FEEDS)) as pool:
                futures = {name: pool.submit(fetch_one, name) for name in FEEDS}
                for name, fut in futures.items():
                    try:
                        feeds.append(fut.result())
                    except Exception as exc:
                        errors.append(f"{urllib.parse.unquote(name)}: {exc}")
            if not feeds:
                raise RuntimeError("; ".join(errors) or "no feeds available")
            _cache.update(at=time.time(), feeds=feeds, errors=errors)
        return _cache["feeds"], _cache["errors"]


def build_arrivals(entry, count=PER_PLATFORM):
    complexes = [MTA_STATIONS[m] for m in entry["mta_ids"] if m in MTA_STATIONS]
    platform_ids = {}
    for c in complexes:
        for p in c["platforms"]:
            # Several MTA complexes can share one Wikipedia name; say which one each platform is in.
            label = p["label"] if len(complexes) == 1 else f"{c['name']} · {p['label']}"
            platform_ids[p["stop_id"]] = label
    feeds, errors = get_feeds()
    now = time.time()
    board = {sid: [] for sid in platform_ids}

    for feed in feeds:
        for entity in feed.entity:
            if not entity.HasField("trip_update"):
                continue
            tu = entity.trip_update
            route = tu.trip.route_id or "?"
            for stu in tu.stop_time_update:
                if stu.stop_id not in board:
                    continue
                ts = stu.arrival.time or stu.departure.time
                if not ts or ts < now - GRACE_SECONDS:
                    continue
                board[stu.stop_id].append({"route": route, "arrival": int(ts)})

    directions = []
    for sid, label in platform_ids.items():
        trains = sorted(board[sid], key=lambda t: t["arrival"])[:count]
        directions.append({"stop_id": sid, "label": label, "trains": trains})
    # Manhattan-bound platforms first; the rest keep their original order.
    directions.sort(key=lambda d: 0 if "manhattan" in d["label"].lower() else 1)

    return {
        "station": {"id": entry["id"], "name": entry["name"], "live": bool(complexes)},
        "generated_at": int(now),
        "directions": directions,
        "feed_errors": errors,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/api/stations":
            body = json.dumps([
                {"id": e["id"], "name": e["name"], "borough": e["borough"],
                 "lines": e["lines"], "live": bool(e["mta_ids"])}
                for e in INDEX
            ]).encode()
            return self._send(200, "application/json", body)

        if parsed.path == "/api/arrivals":
            station_key = (query.get("station") or [""])[0]
            entry = resolve_station(station_key)
            if entry is None:
                body = json.dumps({"error": f"unknown station '{station_key}'"}).encode()
                return self._send(404, "application/json", body)
            try:
                count = int((query.get("count") or [PER_PLATFORM])[0])
            except ValueError:
                count = PER_PLATFORM
            count = max(1, min(MAX_PER_PLATFORM, count))
            try:
                body = json.dumps(build_arrivals(entry, count)).encode()
                return self._send(200, "application/json", body)
            except Exception as exc:
                body = json.dumps({"error": str(exc), "generated_at": int(time.time())}).encode()
                return self._send(503, "application/json", body)

        if parsed.path in ("/", "/index.html"):
            return self._send(200, "text/html; charset=utf-8", (HERE / "index.html").read_bytes())

        self._send(404, "text/plain", b"not found")

    def _send(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    load_stations()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Subway board running at http://localhost:{PORT}  ({len(INDEX)} stations, Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
