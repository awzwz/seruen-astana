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

API = "https://catalog.api.2gis.com/3.0/items"
FIELDS = "items.point,items.schedule,items.rubrics,items.reviews,items.attribute_groups,items.address,items.full_address_name"
LICENSE = "2GIS API — live, not stored"

WANT_Q = {
    "coffee": ["кофейня"], "food": ["кафе"], "museum": ["музей"], "culture": ["музей", "театр"], "park": ["парк"],
    "view": ["смотровая площадка", "достопримечательность"], "night": ["бар", "кинотеатр"],
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


def _budget_ok() -> tuple[bool, int]:
    limit = int(os.environ.get("DGIS_DAILY_LIMIT", "600"))
    used = int(kv(1).get(f"dgis:{date.today().isoformat()}") or 0)
    return used < limit, limit - used


@endpoint("places-live", "/v1/nearby")
async def nearby(p: dict) -> dict:
    key = os.environ.get("DGIS_API_KEY")
    if not key:
        return {"pois": [], "stats": {"enabled": False, "reason": "no_key"}}
    ok, left = _budget_ok()
    if not ok:
        return {"pois": [], "stats": {"enabled": True, "reason": "daily_budget_exhausted"}}
    qs = queries_for(p.get("wants", []), bool(p.get("kids")), bool(p.get("night")), p.get("mode", "walk"))[: max(1, min(3, left))]
    lat, lon = p["lat"], p["lon"]
    radius = int(min(3000, max(300, p.get("radius_m", 1000))))
    t = time.perf_counter()

    async def one(c: httpx.AsyncClient, q: str):
        r = await c.get(API, params={"q": q, "point": f"{lon:.6f},{lat:.6f}", "radius": radius, "type": "branch,attraction",
                                     "fields": FIELDS, "page_size": 10, "locale": "ru_KZ", "key": key})
        kv(1).incrby(f"dgis:{date.today().isoformat()}", 1, ex=2 * 86400)
        d = r.json()
        if d.get("meta", {}).get("code") != 200:
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
