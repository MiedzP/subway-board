"""
Builds stations.json (every NYC subway station and its platforms) from the
MTA static GTFS schedule. Run once, and again if the MTA adds stations.

    python build_stations.py
"""

import csv
import io
import json
import urllib.request
import zipfile
from pathlib import Path

GTFS_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip"
OUT = Path(__file__).resolve().parent / "stations.json"

# Optional friendly direction labels, keyed by "<station id>:<suffix>".
# Anything not listed gets "Northbound" / "Southbound" / the platform's own name.
LABEL_OVERRIDES = {
    "R04:N": "Toward Astoria / Ditmars Blvd",
    "R04:S": "Toward Manhattan",
}
SUFFIX_LABELS = {"N": "Northbound", "S": "Southbound", "E": "Eastbound", "W": "Westbound"}


def main():
    print("Downloading MTA static GTFS…")
    with urllib.request.urlopen(GTFS_URL, timeout=120) as resp:
        data = resp.read()
    z = zipfile.ZipFile(io.BytesIO(data))
    stops = list(csv.DictReader(io.TextIOWrapper(z.open("stops.txt"), encoding="utf-8")))

    parents = {r["stop_id"]: r for r in stops if r.get("location_type") == "1"}
    children = {}
    for r in stops:
        ps = r.get("parent_station")
        if ps in parents:
            children.setdefault(ps, []).append(r)

    stations = []
    for sid, p in sorted(parents.items(), key=lambda kv: kv[1]["stop_name"]):
        platforms = []
        for c in sorted(children.get(sid, []), key=lambda c: c["stop_id"]):
            suffix = c["stop_id"][len(sid):]  # e.g. "N", "S", or "" for a single-platform station
            key = f"{sid}:{suffix}"
            label = LABEL_OVERRIDES.get(key) or SUFFIX_LABELS.get(suffix, c["stop_name"])
            platforms.append({"stop_id": c["stop_id"], "label": label})
        stations.append({
            "id": sid,
            "name": p["stop_name"],
            "lat": float(p["stop_lat"]),
            "lon": float(p["stop_lon"]),
            "platforms": platforms,
        })

    OUT.write_text(json.dumps(stations, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(stations)} stations to {OUT}")


if __name__ == "__main__":
    main()
