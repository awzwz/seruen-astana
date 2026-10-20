"""OpenStreetMap collector: Overpass (several mirrors) → fallback Geofabrik PBF filtered locally with pyosmium.

Raw record payload = one Overpass-style element: {type, id, tags, lat, lon, timestamp, version}.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import httpx

from ..util import (ASTANA_AREA, ASTANA_BBOX, USER_AGENT, cache_path, in_bbox, now_iso,
                    raw_record)
from .. import util
from ..anchors import is_anchor_name

log = logging.getLogger("seruen.ingest.osm")

LICENSE = "ODbL 1.0, © OpenStreetMap contributors"
MIRRORS = [
    ("https://overpass-api.de/api/interpreter", 60),
    ("https://overpass.kumi.systems/api/interpreter", 40),
    ("https://overpass.private.coffee/api/interpreter", 30),
]
PBF_URL = "https://download.geofabrik.de/asia/kazakhstan-latest.osm.pbf"

AMENITY = ("cafe|restaurant|bar|pub|biergarten|fast_food|food_court|ice_cream|theatre|cinema|nightclub|arts_centre|"
           "library|community_centre|planetarium|place_of_worship|marketplace|fountain|coworking_space|events_venue|"
           "music_venue|concert_hall|exhibition_centre|internet_cafe")
TOURISM = "museum|gallery|attraction|viewpoint|artwork|zoo|aquarium|theme_park"
LEISURE = ("park|playground|garden|sports_centre|stadium|water_park|amusement_arcade|ice_rink|nature_reserve|"
           "escape_game|bowling_alley|trampoline_park|miniature_golf|sports_hall|adult_gaming_centre")
WALK_NAME_RE = r"жағалау|набережн|бульвар|[Ee]mbankment|[Bb]oulevard|алаңы|площадь"

QUERY = f"""[out:json][timeout:180];
area(id:{ASTANA_AREA})->.a;
(
  nwr["amenity"~"^({AMENITY})$"](area.a);
  nwr["tourism"~"^({TOURISM})$"](area.a);
  nwr["leisure"~"^({LEISURE})$"](area.a);
  nwr["shop"~"^(mall|department_store)$"](area.a);
  nwr["historic"](area.a);
  nwr["memorial"](area.a);
  nwr["office"="coworking"](area.a);
  nwr["place"="square"](area.a);
  nwr["highway"="pedestrian"]["name"](area.a);
  way["highway"="footway"]["name"~"{WALK_NAME_RE}"](area.a);
  nwr["man_made"~"^(embankment|tower)$"]["name"](area.a);
  nwr["wikidata"]["name"]["building"](area.a);
  nwr["railway"="station"]["name"](area.a);
  nwr["aeroway"="aerodrome"](area.a);
  nwr["museum"](area.a);
  nwr["name"~"Astana IT|AITU",i](area.a);
);
out meta center;"""

_AM = set(AMENITY.split("|"))
_TO = set(TOURISM.split("|"))
_LE = set(LEISURE.split("|"))
_WALK_RX = re.compile(WALK_NAME_RE)


def wanted(tags: dict) -> bool:
    """Same selection as QUERY, used for the PBF fallback."""
    g = tags.get
    if g("amenity") in _AM or g("tourism") in _TO or g("leisure") in _LE:
        return True
    if g("shop") in ("mall", "department_store") or g("historic") or g("memorial") or g("office") == "coworking":
        return True
    if g("museum"):
        return True
    if g("public_transport") in ("stop_position", "platform", "stop_area") or g("railway") in ("stop", "platform"):
        return False
    if any(is_anchor_name(g(k)) for k in ("name", "name:ru", "name:en")):
        return True
    if g("place") == "square" or g("aeroway") == "aerodrome":
        return True
    name = g("name")
    if not name:
        return False
    if g("highway") == "pedestrian" or (g("highway") == "footway" and _WALK_RX.search(name)):
        return True
    if g("man_made") in ("embankment", "tower") or g("railway") == "station":
        return True
    if g("wikidata") and g("building"):
        return True
    return False


# ------------------------------------------------------------------ Overpass
def _overpass() -> list[dict] | None:
    cp = cache_path("osm", "overpass_main.json")
    if cp.exists() and (util.OFFLINE or time.time() - cp.stat().st_mtime < 20 * 3600):
        return json.loads(cp.read_text())["elements"]
    if util.OFFLINE:
        return None
    for url, to in MIRRORS:
        t = time.time()
        try:
            body = bytearray()
            with httpx.stream("POST", url, data={"data": QUERY}, headers={"User-Agent": USER_AGENT},
                              timeout=httpx.Timeout(to, connect=10)) as r:
                if r.status_code != 200:
                    log.warning("overpass %s → HTTP %s", url, r.status_code)
                    continue
                for chunk in r.iter_bytes(1 << 16):     # per-read timeouts alone let a trickling reply hang
                    body.extend(chunk)
                    if time.time() - t > to * 2:
                        raise httpx.ReadTimeout("overall deadline exceeded")
            data = json.loads(body)
            if not data.get("elements"):
                log.warning("overpass %s → empty (%s)", url, data.get("remark", "")[:120])
                continue
            cp.write_bytes(bytes(body))
            log.info("overpass %s ok: %d elements in %.1fs", url, len(data["elements"]), time.time() - t)
            return data["elements"]
        except (httpx.HTTPError, ValueError) as e:
            log.warning("overpass %s failed after %.0fs: %s", url, time.time() - t, type(e).__name__)
    return None


# ------------------------------------------------------------------ PBF fallback
def _pbf_path() -> Path:
    p = cache_path("osm", "kazakhstan-latest.osm.pbf")
    if p.exists() and p.stat().st_size > 50_000_000:
        return p
    if util.OFFLINE:
        raise FileNotFoundError(p)
    log.info("downloading %s (≈214 MB) …", PBF_URL)
    tmp = p.with_suffix(".part")
    with httpx.stream("GET", PBF_URL, headers={"User-Agent": USER_AGENT}, timeout=600, follow_redirects=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    tmp.rename(p)
    return p


def _from_pbf() -> list[dict]:
    cp = cache_path("osm", "pbf_astana.json")
    pbf = _pbf_path()
    if cp.exists() and cp.stat().st_mtime > pbf.stat().st_mtime:
        return json.loads(cp.read_text())
    import osmium  # optional dependency, only for the fallback

    la0, lo0, la1, lo1 = ASTANA_BBOX
    out: list[dict] = []
    way_center: dict[int, tuple[float, float]] = {}
    rel_members: dict[int, list[int]] = {}

    def meta(o) -> dict:
        return {"timestamp": o.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") if o.timestamp else None,
                "version": o.version}

    class H(osmium.SimpleHandler):
        def node(self, n):
            if not n.tags or not n.location.valid():
                return
            lat, lon = n.location.lat, n.location.lon
            if not (la0 <= lat <= la1 and lo0 <= lon <= lo1):
                return
            tags = dict(n.tags)
            if wanted(tags):
                out.append({"type": "node", "id": n.id, "lat": lat, "lon": lon, "tags": tags, **meta(n)})

        def way(self, w):
            pts = []
            for nd in w.nodes:
                if nd.location.valid():
                    pts.append((nd.location.lat, nd.location.lon))
            if not pts:
                return
            lat = sum(p[0] for p in pts) / len(pts)
            lon = sum(p[1] for p in pts) / len(pts)
            if not (la0 <= lat <= la1 and lo0 <= lon <= lo1):
                return
            way_center[w.id] = (lat, lon)
            if not w.tags:
                return
            tags = dict(w.tags)
            if wanted(tags):
                out.append({"type": "way", "id": w.id, "center": {"lat": lat, "lon": lon}, "tags": tags, **meta(w)})

        def relation(self, r):
            tags = dict(r.tags)
            if tags and wanted(tags):
                rel_members[r.id] = [m.ref for m in r.members if m.type == "w"]
                out.append({"type": "relation", "id": r.id, "center": None, "tags": tags, **meta(r)})

    t = time.time()
    H().apply_file(str(pbf), locations=True, idx="flex_mem")
    res = []
    for e in out:
        if e["type"] == "relation":
            pts = [way_center[m] for m in rel_members.get(e["id"], []) if m in way_center]
            if not pts:
                continue
            e["center"] = {"lat": sum(p[0] for p in pts) / len(pts), "lon": sum(p[1] for p in pts) / len(pts)}
        res.append(e)
    log.info("pbf fallback: %d elements in %.1fs", len(res), time.time() - t)
    cp.write_text(json.dumps(res, ensure_ascii=False))
    return res


def collect() -> list[dict]:
    fetched = now_iso()
    via_pref = os.environ.get("SERUEN_OSM_VIA", "auto")      # auto | overpass | pbf
    elements = None if via_pref == "pbf" else _overpass()
    via = "overpass"
    if elements is None:
        elements = _from_pbf()
        via = "geofabrik-pbf"
    recs = []
    for e in elements:
        tags = e.get("tags") or {}
        if not tags:
            continue
        if e["type"] == "node":
            lat, lon = e.get("lat"), e.get("lon")
        else:
            c = e.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if not in_bbox(lat, lon):
            continue
        sid = f"{e['type']}/{e['id']}"
        payload = {"type": e["type"], "id": e["id"], "lat": lat, "lon": lon, "tags": tags,
                   "timestamp": e.get("timestamp"), "version": e.get("version"), "via": via}
        recs.append(raw_record("osm", sid, payload, LICENSE, f"https://www.openstreetmap.org/{sid}", fetched))
    log.info("osm: %d raw records via %s", len(recs), via)
    return recs
