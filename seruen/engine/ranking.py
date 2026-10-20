"""Transparent scoring. Every part is returned so the "how I chose" panel can show it.

score = 0.35·relevance + 0.25·closeness + 0.15·hours confidence + 0.15·weather fit + 0.10·popularity
then a diversity pass so three results are not three coffee shops.
Weights are a starting point tuned on the test scenarios, not learned.
"""
from __future__ import annotations

import math

from seruen.common.models import CATEGORIES, POI, Constraints, Context
from .intents import WANT_MAP

W = {"relevance": 0.35, "closeness": 0.25, "hours": 0.15, "weather": 0.15, "popularity": 0.10}
KIDS_EXCLUDE = {"bar", "nightclub"}
NIGHT_ONLY = {"nightclub"}


def matched_wants(poi: POI, c: Constraints) -> list[str]:
    out = []
    for w in c.wants:
        cats, tags = WANT_MAP.get(w, (set(), set()))
        if poi.category in cats or (tags & set(poi.walk_tags)):
            out.append(w)
    if c.kids and ({"kids", "family"} & set(poi.walk_tags) or poi.category in WANT_MAP["kids"][0]):
        out.append("kids")
    return out


def hard_filter(poi: POI, c: Constraints, ctx: Context) -> str | None:
    """Return a reason string when the POI must be excluded."""
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
    indoor = CATEGORIES.get(poi.category, {}).get("indoor", True)
    if ctx.mode == "indoor" and not indoor:
        return "outdoor_in_bad_weather"
    if poi.category in {"playground"} and ctx.is_dark:
        return "dark_playground"
    if c.kids and ctx.is_dark and not indoor and poi.category in {"park", "embankment", "viewpoint"}:
        return "dark_outdoor_with_kids"
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
    return round(s, 3)


def hours_conf(poi: POI, ctx: Context) -> float:
    if not poi.opening_hours:
        indoor = CATEGORIES.get(poi.category, {}).get("indoor", True)
        if not indoor:
            return 0.5 if ctx.is_dark else 0.8
        return 0.15 if (ctx.is_dark or ctx.is_holiday) else 0.3
    return {"high": 1.0, "medium": 0.8, "low": 0.45, "unknown": 0.2, "open_area": 0.8}.get(poi.opening_hours.level, 0.3)


def relevance(poi: POI, c: Constraints, semantic: float, lexical: float, matched: list[str]) -> float:
    tag = min(1.0, len(matched) / max(1, min(2, len(c.wants) or 1))) if (c.wants or c.kids) else 0.0
    sem = max(0.0, min(1.0, (semantic - 0.15) / 0.45))      # cosine of short texts lives in ~0.15..0.6
    lex = max(0.0, min(1.0, lexical))
    if c.wants or c.kids:
        return round(0.55 * tag + 0.30 * sem + 0.15 * lex, 3)
    return round(0.75 * sem + 0.25 * lex, 3)


def score(poi: POI, c: Constraints, ctx: Context, walk_min: float, semantic: float, lexical: float) -> tuple[float, dict, list[str]]:
    m = matched_wants(poi, c)
    parts = {
        "relevance": relevance(poi, c, semantic, lexical, m),
        "closeness": round(math.exp(-walk_min / 15.0), 3),
        "hours": hours_conf(poi, ctx),
        "weather": weather_fit(poi, ctx),
        "popularity": popularity(poi),
    }
    s = sum(W[k] * v for k, v in parts.items())
    if poi.status in ("possibly_closed", "unconfirmed"):
        s *= 0.6
    return round(s, 4), parts, m


def diversify(ranked: list[tuple[str, float, str]], k: int = 8) -> list[str]:
    """ranked: (id, score, category) sorted desc. Penalise repeated categories (×0.75 per repeat)."""
    chosen, seen = [], {}
    pool = list(ranked)
    while pool and len(chosen) < k:
        best_i, best_v = 0, -1.0
        for i, (pid, s, cat) in enumerate(pool):
            v = s * (0.75 ** seen.get(cat, 0))
            if v > best_v:
                best_i, best_v = i, v
        pid, s, cat = pool.pop(best_i)
        chosen.append(pid)
        seen[cat] = seen.get(cat, 0) + 1
    return chosen
