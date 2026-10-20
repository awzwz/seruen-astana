"""Transparent scoring. Every part is returned so the "how I chose" panel can show it.

score = 0.35·relevance + 0.25·closeness + 0.15·hours confidence + 0.15·weather fit + 0.10·popularity
(for tourists popularity weighs 0.20 and closeness 0.15 — they came for the city's sights),
then a diversity pass so three results are not three coffee shops.
Weights are a starting point tuned on the test scenarios, not learned.
"""
from __future__ import annotations

import math

from seruen.common.models import CATEGORIES, POI, Constraints, Context
from .intents import WANT_MAP

W = {"relevance": 0.35, "closeness": 0.25, "hours": 0.15, "weather": 0.15, "popularity": 0.10}
W_TOURIST = {"relevance": 0.35, "closeness": 0.15, "hours": 0.15, "weather": 0.15, "popularity": 0.20}
KIDS_EXCLUDE = {"bar", "nightclub"}
NIGHT_ONLY = {"nightclub"}
NIGHT_CATS = {"bar", "nightclub", "cinema", "concert", "theatre", "restaurant", "cafe", "fast_food", "mall", "attraction"}


def is_unnamed(poi: POI) -> bool:
    """OSM objects without a real name get the category label as name ("Детская площадка") — weak as advice."""
    n = (poi.name.ru or poi.name.kk or poi.name.en or "").strip().lower()
    labels = {v[k].lower() for v in CATEGORIES.values() for k in ("ru", "kk", "en")}
    generic = {"декоративный объект", "скульптура", "арт-объект", "арт объект", "памятник", "фонтан", "стела", "мурал",
               "детская площадка", "сквер", "парк", "кафе", "кофейня", "бар"}
    return not n or n in labels or n in generic


def want_strength(poi: POI, c: Constraints) -> dict[str, float]:
    """1.0 when the category itself matches the wish, 0.5 when only a tag does (a fast-food with Wi-Fi is not a coworking)."""
    out: dict[str, float] = {}
    tags = set(poi.walk_tags)
    for w in c.wants:
        cats, wt = WANT_MAP.get(w, (set(), set()))
        if poi.category in cats:
            out[w] = 1.0
        elif wt & tags:
            out[w] = 0.5
    if c.kids:
        if poi.category in WANT_MAP["kids"][0] or {"kids", "family"} & tags:
            out["kids"] = 1.0 if poi.category in {"playground", "attraction"} or "kids" in tags else 0.6
    return out


def matched_wants(poi: POI, c: Constraints) -> list[str]:
    return [w for w, s in want_strength(poi, c).items() if s >= 0.5]


def hard_filter(poi: POI, c: Constraints, ctx: Context, lexical: float = 0.0) -> str | None:
    """Return a reason string when the POI must be excluded."""
    named = lexical >= 0.6                     # the user named this exact place — never hide it silently
    indoor = CATEGORIES.get(poi.category, {}).get("indoor", True)
    if poi.status == "closed":
        return "closed"
    if c.kids and poi.category in KIDS_EXCLUDE:
        return "not_for_kids"
    if poi.category in NIGHT_ONLY and not c.night:
        return "night_only"
    if c.mobility == "wheelchair":
        w = poi.attributes.get("wheelchair")
        if w and w.value == "no":
            return "not_accessible"
    if named:
        return None
    if poi.category == "other":
        return "not_a_venue"
    if poi.category == "playground" and (not c.kids or ctx.is_dark):
        return "playground_without_kids"
    if is_unnamed(poi) and not (c.kids and poi.category in {"playground", "park"}):
        return "unnamed"
    if ctx.mode == "indoor" and not indoor:
        return "outdoor_in_bad_weather"
    if c.kids and ctx.is_dark and not indoor and poi.category in {"park", "embankment", "viewpoint"}:
        return "dark_outdoor_with_kids"
    if c.night and ctx.is_dark and not indoor and not ({"view", "landmark"} & set(c.wants)):
        return "dark_outdoor_at_night"
    return None


def weather_fit(poi: POI, ctx: Context) -> float:
    indoor = CATEGORIES.get(poi.category, {}).get("indoor", True)
    if ctx.mode == "walk":
        return 1.0 if not indoor else 0.75
    if ctx.mode == "short":
        return 1.0 if indoor else 0.35
    return 1.0 if indoor else 0.0


def popularity(poi: POI) -> float:
    p = poi.popularity
    s = min(1.0, math.log1p(p.wikidata_sitelinks) / math.log(40))
    s = max(s, min(1.0, (p.sources_confirming - 1) / 3))
    if p.curated_in:
        s = max(s, 0.8)
    if p.overture_confidence:
        s = max(s, 0.3 * p.overture_confidence)
    if p.reviews:            # 2GIS: 4.8★ from 3 000 reviews ≈ 0.95; 4.0★ from 20 reviews ≈ 0.3
        s = max(s, min(1.0, math.log1p(p.reviews) / math.log(2000)) * ((p.rating or 4.0) / 5.0))
    return round(s, 3)


def hours_conf(poi: POI, ctx: Context) -> float:
    if not poi.opening_hours:
        indoor = CATEGORIES.get(poi.category, {}).get("indoor", True)
        if not indoor:
            return 0.5 if ctx.is_dark else 0.8
        return 0.15 if (ctx.is_dark or ctx.is_holiday) else 0.3
    return {"high": 1.0, "medium": 0.8, "low": 0.45, "unknown": 0.2, "open_area": 0.8}.get(poi.opening_hours.level, 0.3)


def relevance(poi: POI, c: Constraints, semantic: float, lexical: float, strength: dict[str, float]) -> float:
    n = len(c.wants) + (1 if c.kids else 0)
    tag = min(1.0, sum(strength.values()) / max(1, min(2, n))) if n else 0.0
    sem = max(0.0, min(1.0, (semantic - 0.15) / 0.45))      # cosine of short texts lives in ~0.15..0.6
    lex = max(0.0, min(1.0, lexical))
    r = (0.55 * tag + 0.30 * sem + 0.15 * lex) if n else (0.75 * sem + 0.25 * lex)
    if lex >= 0.6:                                         # an explicitly named place is what the user asked for
        r = max(r, 0.95)
    return round(r, 3)


def score(poi: POI, c: Constraints, ctx: Context, walk_min: float, semantic: float, lexical: float) -> tuple[float, dict, list[str]]:
    st = want_strength(poi, c)
    w = dict(W_TOURIST if c.tourist else W)
    if ctx.is_dark:            # after sunset knowing that a place is open matters more than a few metres
        w["hours"], w["closeness"] = w["hours"] + 0.10, w["closeness"] - 0.10
    parts = {
        "relevance": relevance(poi, c, semantic, lexical, st),
        "closeness": round(math.exp(-walk_min / 15.0), 3),
        "hours": hours_conf(poi, ctx),
        "weather": weather_fit(poi, ctx),
        "popularity": popularity(poi),
    }
    s = sum(w[k] * v for k, v in parts.items())
    if poi.status in ("possibly_closed", "unconfirmed"):
        s *= 0.6
    if is_unnamed(poi):
        s *= 0.7
    return round(s, 4), parts, [k for k, v in st.items() if v >= 0.5]


def diversify(ranked: list[tuple[str, float, str]], k: int = 8, names: dict[str, str] | None = None) -> list[str]:
    """ranked: (id, score, category) sorted desc. Penalise repeated categories (×0.75 per repeat), drop repeated names."""
    chosen, seen, seen_names = [], {}, set()
    pool = list(ranked)
    while pool and len(chosen) < k:
        best_i, best_v = 0, -1.0
        for i, (pid, s, cat) in enumerate(pool):
            v = s * (0.75 ** seen.get(cat, 0))
            if v > best_v:
                best_i, best_v = i, v
        pid, s, cat = pool.pop(best_i)
        nm = (names or {}).get(pid)
        if nm and nm in seen_names:
            continue
        if nm:
            seen_names.add(nm)
        chosen.append(pid)
        seen[cat] = seen.get(cat, 0) + 1
    return chosen
