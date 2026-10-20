"""places-live: live layer over the 2GIS Places API (catalog.api.2gis.com) at request time.

2GIS terms (law.2gis.ru/api-rules, п. 4.2) forbid storing their data, so nothing is persisted or cached here:
results go straight into this one answer, labelled "2GIS", with the required attribution in the UI.
Only the centre of the privacy cell (~150 m) and the kind of places are sent to 2GIS — never the exact point.
A daily request budget protects the key quota; when it is exhausted the system silently uses only its own catalog.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from datetime import date, datetime, timezone

import httpx

from seruen.common.clients import endpoint
from seruen.common.models import CATEGORIES, POI, Attr, Description, Hours, Names, Popularity, SourceRef
from seruen.common.stores import kv
from seruen.services import dgis_keys as K

API = "https://catalog.api.2gis.com/3.0/items"
FIELDS = "items.point,items.schedule,items.rubrics,items.reviews,items.attribute_groups,items.address,items.full_address_name"
LICENSE = "2GIS API — live, not stored"

WANT_Q = {
    "coffee": ["кофейня"], "food": ["кафе"], "museum": ["музей"], "culture": ["музей", "театр"], "park": ["парк"],
    "view": ["смотровая площадка", "достопримечательность"], "night": ["бар", "караоке", "ночной клуб"],
    "unusual": ["достопримечательность", "развлечения"], "kids": ["детский развлекательный центр", "парк"],
    "quiet": ["коворкинг", "кофейня"], "power": ["коворкинг"], "shopping": ["торговый центр"],
    "landmark": ["достопримечательность"], "attraction": ["развлечения"], "sport": ["спорт"], "religious": ["мечеть"],
}

# rubric keyword → category; checked for every rubric, the most specific category wins (PRECEDENCE)
RUBRIC_MAP = [
    ("кинотеатр", "cinema"), ("театр", "theatre"), ("филармон", "concert"), ("концерт", "concert"),
    ("ночные клубы", "nightclub"), ("кофейн", "coffee"), ("ресторан", "restaurant"), ("быстрое питание", "fast_food"),
    ("фастфуд", "fast_food"), ("пиццер", "fast_food"), ("бургер", "fast_food"), ("шаурм", "fast_food"),
    ("суши-бар", "restaurant"), ("бары", "bar"), ("паб", "bar"), ("лаундж", "bar"), ("кальян", "bar"),
    ("кафе", "cafe"), ("чайхан", "cafe"), ("кондитерск", "cafe"), ("пекарн", "cafe"), ("столов", "cafe"),
    ("музе", "museum"), ("художественные выставки", "gallery"), ("галере", "gallery"), ("выставочн", "gallery"),
    ("смотровые площадки", "viewpoint"), ("памятник", "monument"), ("скульптур", "monument"),
    ("торгово-развлекательные", "mall"), ("торговые центры", "mall"), ("рынк", "market"), ("библиотек", "library"),
    ("коворкинг", "coworking"), ("мечет", "religious"), ("церк", "religious"), ("храм", "religious"),
    ("собор", "religious"), ("детские игровые", "attraction"), ("аттракцион", "attraction"), ("аквапарк", "attraction"),
    ("океанариум", "attraction"), ("зоопарк", "attraction"), ("квест", "attraction"), ("боулинг", "attraction"),
    ("картинг", "attraction"), ("батутн", "attraction"), ("развлекательн", "attraction"), ("планетари", "attraction"),
    ("караоке", "bar"), ("бильярд", "attraction"), ("парки", "park"), ("сквер", "park"), ("сады", "park"),
    ("фитнес", "sport"), ("бассейн", "sport"), ("каток", "sport"), ("спортивн", "sport"),
    ("интересные здания", "landmark"), ("достопримечательн", "landmark"),
]
PRECEDENCE = ["mall", "attraction", "museum", "gallery", "theatre", "cinema", "concert", "coworking", "library",
              "coffee", "restaurant", "cafe", "fast_food", "bar", "nightclub", "viewpoint", "religious", "market",
              "sport", "park", "monument", "landmark"]
DAYS = [("Mon", "Mo"), ("Tue", "Tu"), ("Wed", "We"), ("Thu", "Th"), ("Fri", "Fr"), ("Sat", "Sa"), ("Sun", "Su")]
SKIP_ATTR = ("оплата", "расчёт", "перевод", "qr", "премия", "кабинки", "доставка")


def _category(rubrics: list[str]) -> str | None:
    found = set()
    for r in rubrics:
        low = r.lower()
        for kw, cat in RUBRIC_MAP:
            if kw in low:
                found.add(cat)
                break
    for cat in PRECEDENCE:
        if cat in found:
            return cat
    return None


def _hours(schedule: dict | None) -> str | None:
    if not schedule:
        return None
    if schedule.get("is_24x7"):
        return "24/7"
    parts = []
    for k, osm in DAYS:
        wh = (schedule.get(k) or {}).get("working_hours") or []
        spans = [f"{w['from']}-{w['to']}" for w in wh if w.get("from") and w.get("to")]
        if spans:
            parts.append(f"{osm} {','.join(spans)}")
    return "; ".join(parts) or None


def to_poi(it: dict, query: str) -> POI | None:
    pt = it.get("point") or {}
    rubrics = [r.get("name", "") for r in it.get("rubrics") or []]
    cat = _category(rubrics)
    if not cat or "lat" not in pt:
        return None
    attrs = [a.get("name", "") for g in it.get("attribute_groups") or [] for a in g.get("attributes") or []]
    low = " | ".join(attrs).lower() + " | " + " | ".join(rubrics).lower()
    meta = CATEGORIES[cat]
    tags = {"indoor" if meta["indoor"] else "outdoor"}
    if meta["indoor"]:
        tags.add("warm_stop")
    if cat in ("museum", "gallery", "theatre", "concert"):
        tags.add("culture")
    if cat in ("restaurant", "cafe", "fast_food", "coffee"):
        tags.add("food")
    if cat == "coffee" or "кофе" in low:
        tags.add("coffee")
    if cat in ("coworking", "library"):
        tags |= {"quiet", "power", "wifi"}
    if "wi-fi" in low:
        tags.add("wifi")
    if "розетк" in low:
        tags.add("power")
    if "детск" in low or "для детей" in low or cat == "attraction":
        tags |= {"kids", "family"}
    if "панорамн" in low or cat == "viewpoint":
        tags.add("view")
    if "интерактив" in low or cat == "attraction":
        tags.add("unusual")
    if "танцпол" in low or "живая музыка" in low or cat in ("bar", "nightclub"):
        tags.add("night")
    access = "доступный вход" in low or "пандус" in low or "широкий лифт" in low
    if access:
        tags.add("accessible")
    hours = _hours(it.get("schedule"))
    today = date.today().isoformat()
    rv = it.get("reviews") or {}
    rating = rv.get("general_rating")
    nrev = int(rv.get("general_review_count") or 0)
    facts = [a for a in attrs if not any(s in a.lower() for s in SKIP_ATTR)][:4]
    name = (it.get("name") or "").split(",")[0].strip()
    return POI(
        poi_id=f"2gis:{it['id']}", name=Names(ru=name, alt=[it.get("name", "")] if it.get("name") != name else []),
        lat=pt["lat"], lon=pt["lon"], geo_src="2gis", category=cat, walk_tags=sorted(tags),
        description=Description(text=" · ".join(facts), lang="ru", src="2gis", license=LICENSE) if facts else None,
        opening_hours=Hours(raw=hours, src="2gis", as_of=today, level="high") if hours else None,
        attributes={**({"wheelchair": Attr(value="yes", src="2gis", as_of=today, level="high")} if access else {}),
                    **({"wifi": Attr(value=True, src="2gis", as_of=today, level="high")} if "wi-fi" in low else {}),
                    "rubrics": Attr(value=rubrics[:3], src="2gis")},
        visit_min=meta["visit_min"],
        popularity=Popularity(sources_confirming=1, rating=rating, reviews=nrev),
        address=it.get("address_name") or it.get("full_address_name"),
        sources=[SourceRef(name="2gis", id=str(it["id"]), license=LICENSE,
                           fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))],
        confidence={"exists": 0.95, "location": 0.95, "hours": 0.95 if hours else 0.0},
        status="active",
    )


def queries_for(wants: list[str], kids: bool, night: bool, mode: str) -> list[str]:
    q: list[str] = []
    for w in (["kids"] if kids else []) + (["night"] if night else []) + list(wants):
        for x in WANT_Q.get(w, []):
            if x not in q:
                q.append(x)
    if not q:
        q = ["торговый центр", "кафе", "музей"] if mode != "walk" else ["достопримечательность", "парк", "кафе"]
    return q[:3]


@endpoint("places-live", "/v1/nearby")
async def nearby(p: dict) -> dict:
    if not K.keys():
        return {"pois": [], "stats": {"enabled": False, "reason": "no_key"}}
    if not K.order("places"):
        return {"pois": [], "stats": {"enabled": True, "reason": "all_keys_exhausted"}}
    qs = queries_for(p.get("wants", []), bool(p.get("kids")), bool(p.get("night")), p.get("mode", "walk"))[:3]
    lat, lon = p["lat"], p["lon"]
    radius = int(min(3000, max(300, p.get("radius_m", 1000))))
    t = time.perf_counter()

    async def one(c: httpx.AsyncClient, q: str):
        st, d = await K.request("places", "GET", API, client=c, params={
            "q": q, "point": f"{lon:.6f},{lat:.6f}", "radius": radius, "type": "branch,attraction",
            "fields": FIELDS, "page_size": 10, "locale": "ru_KZ"})
        if not d or d.get("meta", {}).get("code") != 200:
            return q, []
        return q, (d.get("result") or {}).get("items") or []

    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            res = await asyncio.gather(*[one(c, q) for q in qs], return_exceptions=True)
    except Exception as e:
        return {"pois": [], "stats": {"enabled": True, "reason": type(e).__name__}}
    seen, pois = set(), []
    for r in res:
        if isinstance(r, Exception):
            continue
        q, items = r
        for it in items:
            if it.get("id") in seen:
                continue
            seen.add(it.get("id"))
            poi = to_poi(it, q)
            if poi:
                pois.append(poi.model_dump(mode="json"))
    return {"pois": pois, "stats": {"enabled": True, "queries": qs, "requests": len(qs), "items": len(pois),
                                    "ms": round((time.perf_counter() - t) * 1000)}}


# ============================================================ 2GIS gateway: where / suggest / walk / transit / taxi
ROUTING = "https://routing.api.2gis.com/routing/7.0.0/global"
TRANSIT = "https://routing.api.2gis.com/public_transport/2.0"
SUGGEST = "https://catalog.api.2gis.com/3.0/suggests"
GEOCODE = "https://catalog.api.2gis.com/3.0/items/geocode"
SUBTYPE_RU = {"bus": "автобус", "trolleybus": "троллейбус", "tram": "трамвай", "shuttle_bus": "маршрутка",
              "light_metro": "LRT", "metro": "метро", "light_rail": "LRT", "suburban_train": "электричка"}


def _wkt(sel: str) -> list[list[float]]:
    """'LINESTRING(71.4 51.1, 71.5 51.2)' → [[71.4, 51.1], [71.5, 51.2]] (extra z values ignored)."""
    try:
        inner = sel[sel.index("(") + 1: sel.rindex(")")]
        out = []
        for pair in inner.split(","):
            xs = pair.strip().split()
            out.append([float(xs[0]), float(xs[1])])
        return out
    except Exception:
        return []


def directions_link(a: tuple[float, float], b: tuple[float, float]) -> str:
    """Deep link that opens the route in 2GIS (there the user can also call a taxi)."""
    return f"https://2gis.kz/astana/directions/points/{a[1]:.6f}%2C{a[0]:.6f}%7C{b[1]:.6f}%2C{b[0]:.6f}"


@endpoint("places-live", "/v1/where")
async def where(p: dict) -> dict:
    """Reverse geocoding of the privacy-cell centre. Caching geocoding results is explicitly allowed by 2GIS (п. 4.2)."""
    cell = p.get("cell") or f"{p['lat']:.4f},{p['lon']:.4f}"
    ck = f"dgis:where:{cell}"
    hit = kv(1).get(ck)
    if hit:
        import orjson

        return {**orjson.loads(hit), "cached": True}
    st, d = await K.request("geocoder", "GET", GEOCODE, timeout=3.0, params={
        "lat": p["lat"], "lon": p["lon"], "fields": "items.point,items.adm_div,items.full_name", "locale": "ru_KZ"})
    if not d or (d.get("meta") or {}).get("code") != 200:
        return {"address": None}
    it = ((d.get("result") or {}).get("items") or [{}])[0]
    district = next((a.get("name") for a in it.get("adm_div") or [] if a.get("type") == "district"), None)
    building = (it.get("building_name") or "").split(",")[0] or None
    out = {"address": it.get("address_name") or it.get("name"), "building": building, "district": district, "src": "2GIS"}
    import orjson

    kv(1).set(ck, orjson.dumps(out), ex=7 * 86400)
    return out


@endpoint("places-live", "/v1/suggest")
async def suggest(p: dict) -> dict:
    q = (p.get("q") or "").strip()[:80]
    if len(q) < 2:
        return {"items": []}
    params = {"q": q, "fields": "items.point,items.full_name", "locale": "ru_KZ", "page_size": 7,
              "location": f"{p.get('lon', 71.43):.4f},{p.get('lat', 51.13):.4f}"}
    st, d = await K.request("suggest", "GET", SUGGEST, params=params, timeout=3.0)
    items = ((d or {}).get("result") or {}).get("items") or []
    out = []
    for it in items:
        pt = it.get("point")
        if not pt:
            continue
        out.append({"name": (it.get("name") or "").split(",")[0], "subtitle": it.get("address_name") or it.get("full_name") or "",
                    "lat": pt["lat"], "lon": pt["lon"]})
    return {"items": out[:6], "src": "2GIS"}


@endpoint("places-live", "/v1/walk")
async def walk(p: dict) -> dict:
    """Walking legs between consecutive points [[lat, lon], ...] — one 2GIS request per leg, in parallel."""
    pts = p.get("points") or []
    if len(pts) < 2:
        return {"legs": None}

    async def leg(c, a, b):
        st, body = await K.request("routing", "POST", ROUTING, client=c, json={
            "points": [{"lat": a[0], "lon": a[1], "type": "walking", "start": True}, {"lat": b[0], "lon": b[1], "type": "walking"}],
            "transport": "walking", "output": "detailed", "locale": "ru"})
        res = ((body or {}).get("result") or [None])[0]
        if not res:
            return None
        geom: list[list[float]] = []
        for part in (res.get("begin_pedestrian_path"),):
            if part:
                geom += _wkt((part.get("geometry") or {}).get("selection", ""))
        for m in res.get("maneuvers") or []:
            for g in (m.get("outcoming_path") or {}).get("geometry") or []:
                geom += _wkt(g.get("selection", ""))
        end = res.get("end_pedestrian_path")
        if end:
            geom += _wkt((end.get("geometry") or {}).get("selection", ""))
        return {"minutes": max(1, round(res.get("total_duration", 0) / 60)), "meters": int(res.get("total_distance", 0)),
                "geometry": geom}

    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            legs = await asyncio.gather(*[leg(c, a, b) for a, b in zip(pts[:-1], pts[1:])], return_exceptions=True)
    except Exception:
        return {"legs": None}
    if any(isinstance(x, Exception) or x is None for x in legs):
        return {"legs": None}
    return {"legs": legs, "src": "2GIS"}


@endpoint("places-live", "/v1/transit")
async def transit(p: dict) -> dict:
    """Best public-transport option (bus, LRT …) between two points, with lines, transfers and geometry."""
    a, b = p["from"], p["to"]
    st, body = await K.request("transit", "POST", TRANSIT, timeout=5.0, json={
        "source": {"point": {"lat": a[0], "lon": a[1]}}, "target": {"point": {"lat": b[0], "lon": b[1]}},
        "transport": ["bus", "trolleybus", "tram", "shuttle_bus", "light_metro", "metro", "light_rail"], "locale": "ru"})
    variants = body if isinstance(body, list) else []
    variants = [v for v in variants if not v.get("pedestrian")]
    if not variants:
        return {"option": None}
    import re

    def walk_min(x):
        m = re.search(r"(\d+)", x.get("total_walkway_distance") or "")
        return int(m.group(1)) if m else 0

    short = [x for x in variants if walk_min(x) <= 20] or variants
    v = min(short, key=lambda x: x.get("total_duration") or 1e9)
    lines, geom, wait = [], [], 0
    for m in v.get("movements") or []:
        wait += int(m.get("waiting_duration") or 0)
        for rt in m.get("routes") or []:
            st = rt.get("subtype", "")
            lines.append({"kind": SUBTYPE_RU.get(st, rt.get("subtype_name") or st), "subtype": st,
                          "names": rt.get("names", [])[:3], "color": rt.get("color")})
        for alt in (m.get("alternatives") or [])[:1]:
            seg = []
            for g in alt.get("geometry") or []:
                seg += _wkt(g.get("selection", ""))
            if seg:
                geom.append({"type": m.get("type"), "coords": seg})
    times = [s.get("precise_time") for s in v.get("schedules") or [] if s.get("precise_time")][:3]
    return {"option": {"minutes": round((v.get("total_duration") or 0) / 60), "transfers": v.get("transfer_count", 0),
                       "walk": v.get("total_walkway_distance"), "lines": lines, "departures": times,
                       "wait_min": round(wait / 60), "geometry": geom, "link": directions_link(tuple(a), tuple(b))},
            "src": "2GIS"}


@endpoint("places-live", "/v1/taxi")
async def taxi(p: dict) -> dict:
    """Car time with current traffic (2GIS 'taxi' routing) + a link that opens the route in 2GIS."""
    a, b = p["from"], p["to"]
    st, body = await K.request("routing", "POST", ROUTING, timeout=4.0, json={
        "points": [{"lat": a[0], "lon": a[1], "type": "stop", "start": True}, {"lat": b[0], "lon": b[1], "type": "stop"}],
        "transport": "taxi", "route_mode": "fastest", "traffic_mode": "jam", "output": "summary", "locale": "ru"})
    res = ((body or {}).get("result") or [None])[0] if isinstance(body, dict) else None
    if not res or not res.get("duration"):
        return {"option": None}
    return {"option": {"minutes": max(1, round(res["duration"] / 60)) + 5, "drive_min": max(1, round(res["duration"] / 60)),
                       "km": round(res.get("length", 0) / 1000, 1), "traffic": True, "link": directions_link(tuple(a), tuple(b))},
            "src": "2GIS"}


@endpoint("places-live", "/v1/keys")
async def keys_status(p: dict) -> dict:
    return K.status()
