"""
Builds station_index.json: the station names from Wikipedia's
"List of New York City Subway stations", each cross-referenced to the MTA
station IDs in stations.json (run build_stations.py first).

    python build_wiki_index.py

Matching is by normalized name (Avenue -> Av, "138th Street" -> "138 St", etc.).
Names with no MTA match are kept in the index with an empty mta_ids list, and
are listed at the end of the run so you can review them.
"""

import difflib
import html as htmllib
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MTA_PATH = HERE / "stations.json"
OUT = HERE / "station_index.json"
WIKI_PAGE = "List_of_New_York_City_Subway_stations"

ABBR = {
    "avenue": "av", "ave": "av", "street": "st", "boulevard": "blvd", "road": "rd",
    "place": "pl", "square": "sq", "drive": "dr", "parkway": "pkwy", "saint": "st",
    "center": "ctr", "centre": "ctr", "heights": "hts", "junction": "jct",
    "terminal": "term", "north": "n", "south": "s", "east": "e", "west": "w",
}
ORDINALS = {
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5", "sixth": "6",
    "seventh": "7", "eighth": "8", "ninth": "9", "tenth": "10", "eleventh": "11", "twelfth": "12",
}


def fetch_wiki_html():
    url = ("https://en.wikipedia.org/w/api.php?action=parse&format=json&formatversion=2"
           "&prop=text&page=" + urllib.parse.quote(WIKI_PAGE))
    req = urllib.request.Request(url, headers={"User-Agent": "subway-board/1.0 (local project)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)["parse"]["text"]


def cell_text(raw):
    return htmllib.unescape(re.sub(r"<[^>]+>", "", raw)).replace("\xa0", " ").strip()


def parse_wiki_stations(page_html):
    """Return {name: {"borough": str, "lines": set}} from the main sortable station table."""
    table = next(t for t in re.findall(r"<table[^>]*>.*?</table>", page_html, re.S)
                 if "sortable" in t[:300] and "Station" in t[:3000])
    rows = re.findall(r"<tr.*?</tr>", table, re.S)
    headers = [cell_text(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", rows[0], re.S)]
    idx = {h: i for i, h in enumerate(headers)}
    stations = {}
    for row in rows[1:]:
        cells = re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", row, re.S)
        if len(cells) < len(headers):
            continue  # rowspan continuation rows: their station is already captured
        name = cell_text(cells[idx["Station"]])
        if not name:
            continue
        entry = stations.setdefault(name, {"borough": "", "lines": set()})
        if "Borough" in idx and not entry["borough"]:
            entry["borough"] = cell_text(cells[idx["Borough"]])
        if "Line" in idx:
            line = cell_text(cells[idx["Line"]])
            if line:
                entry["lines"].add(line)
    return stations


def normalize(name):
    s = name.lower().replace("–", " ").replace("—", " ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(\d+)(st|nd|rd|th)\b", r"\1", s)     # 138th -> 138
    words = [ORDINALS.get(w, ABBR.get(w, w)) for w in s.split()]
    return " ".join(w for w in words if w not in ("the", "of", "at"))


def main():
    print("Fetching Wikipedia station list…")
    wiki = parse_wiki_stations(fetch_wiki_html())
    mta = json.loads(MTA_PATH.read_text(encoding="utf-8"))

    by_key = {}
    for s in mta:
        by_key.setdefault(normalize(s["name"]), []).append(s["id"])
    mta_keys = list(by_key)

    index, unmatched = [], []
    for name, info in sorted(wiki.items()):
        key = normalize(name)
        ids, quality = by_key.get(key), "exact"
        if not ids:
            close = difflib.get_close_matches(key, mta_keys, n=1, cutoff=0.9)
            ids = by_key[close[0]] if close else None
        if not ids and re.search(r"[/–]", name):
            # "A/B" or "A–B": fall back to the first part alone (approximate)
            first = normalize(re.split(r"[/–]", name)[0])
            ids, quality = by_key.get(first), "approx"
        if not ids:
            ids, quality = [], "none"
            unmatched.append(name)
        index.append({
            "id": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
            "name": name,
            "borough": info["borough"],
            "lines": sorted(info["lines"]),
            "mta_ids": sorted(set(ids)),
            "match": quality,
        })

    OUT.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    approx = sum(1 for x in index if x["match"] == "approx")
    matched = len(index) - len(unmatched)
    print(f"Wrote {len(index)} stations to {OUT}  ({matched} matched to MTA IDs, {approx} approximate)")
    if unmatched:
        print("No MTA match for:")
        for n in unmatched:
            print("  -", n)


if __name__ == "__main__":
    main()
