"""Quality: curated anchors, per-field confidence, hours / attribute levels and status.

confidence(field) = base(source) × freshness(age) × agreement (×1.2 when ≥ 2 sources agree), capped at 1.0
  base: osm 0.8, wikidata 0.85, visitastana 0.9, wikivoyage 0.8, overture = its own confidence, llm 0.3
  freshness: ≤ 1 y → 1.0, 1–3 y → 0.8, > 3 y → 0.5
hours level: high (visitastana / official site / OSM check_date ≤ 30 days), medium (≤ 12 months), low (older or no
  date), unknown (none), open_area (parks, squares, monuments, viewpoints, embankments, playgrounds without hours).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from rapidfuzz import fuzz

from seruen.common.geo import haversine_m
from seruen.common.models import CATEGORIES, POI, Attr, Confidence, Description, Names, Popularity, SourceRef

from . import anchors as anchors_mod
from .dedup import _merge, name_keys
from .normalize import dedupe_alt, finish_tags
from .util import norm_name, now_iso

log = logging.getLogger("seruen.ingest.quality")

BASE = {"osm": 0.8, "wikidata": 0.85, "visitastana": 0.9, "wikivoyage": 0.8, "llm": 0.3, "seruen-curated": 0.9,
        "template": 0.5}
UNDATED_FRESH = {"wikidata": 1.0, "overture": 1.0, "visitastana": 1.0, "seruen-curated": 1.0}
OPEN_AREA = {"park", "embankment", "monument", "viewpoint", "playground"}
BUSINESS = {"cafe", "coffee", "restaurant", "fast_food", "bar", "nightclub", "coworking"}
CLOSED_BEFORE = "2020-01-01"     # OSM-only business not edited since 2019 → possibly_closed


def _d(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def age_days(as_of: Optional[str], now: datetime) -> Optional[int]:
    d = _d(as_of)
    return None if d is None else (now.date() - d).days


def freshness(as_of: Optional[str], now: datetime, src: Optional[str] = None) -> float:
    a = age_days(as_of, now)
    if a is None:
        return UNDATED_FRESH.get(src or "", 0.8)
    return 1.0 if a <= 365 else 0.8 if a <= 3 * 365 else 0.5


def base_conf(src: str, poi: POI) -> float:
    s = src.split(":")[0]
    if s == "overture":
        return float(poi.popularity.overture_confidence or 0.6)
    return BASE.get(s, 0.6)


def hours_level(poi: POI, now: datetime) -> str:
    h = poi.opening_hours
    if h is None:
        if poi.category in OPEN_AREA or poi.attributes.get("outside_only"):
            return "open_area"
        return "unknown"
    if h.src in ("visitastana", "official", "partner"):
        return "high"
    chk = poi.attributes.get("hours_check_date")
    ca = age_days(chk.value if chk else None, now)
    if ca is not None and ca <= 30:
        return "high"
    a = age_days(h.as_of, now)
    if a is None:
        return "low"
    return "medium" if a <= 365 else "low"


def attr_level(a: Attr, now: datetime) -> str:
    if a.value is None:
        return "unknown"
    src = a.src or ""
    if ":description" in src or src.startswith("category"):
        return "low"
    if src.startswith(("visitastana", "official", "partner")):
        return "high"
    ag = age_days(a.as_of, now)
    if ag is None:
        return "low"
    return "high" if ag <= 30 else "medium" if ag <= 365 else "low"


# ======================================================================= anchors
def _anchor_poi(a: dict, fetched: str) -> POI:
    ref = a["ref"]
    cat = a["category"]
    return POI(poi_id=f"anchor:{a['id']}", name=Names(**a["names"]), lat=ref["lat"], lon=ref["lon"],
               geo_src=f"seruen-curated ({ref.get('src', 'ref')})", category=cat,
               visit_min=CATEGORIES.get(cat, CATEGORIES["other"])["visit_min"],
               popularity=Popularity(sources_confirming=1),
               external_ids={k: v for k, v in (("wikidata", a.get("qid")), ("osm", a.get("osm"))) if v},
               sources=[SourceRef(name="seruen-curated", id=a["id"], license="CC0 (Seruen curated)",
                                  fetched_at=fetched)])


def apply_anchors(pois: list[POI], anchors: Optional[list[dict]] = None) -> list[POI]:
    """Find each anchor among the POIs (QID → OSM id → name near ref), force its category / tags / description."""
    anchors = anchors if anchors is not None else anchors_mod.load()
    fetched = now_iso()
    pois = list(pois)
    used: set[str] = set()
    found = created = missing = 0
    for a in anchors:
        idx = [i for i, p in enumerate(pois) if a.get("qid") and p.external_ids.get("wikidata") == a["qid"]]
        if not idx and a.get("osm"):
            idx = [i for i, p in enumerate(pois) if p.external_ids.get("osm") == a["osm"]]
        if not idx:
            akeys = {norm_name(n) for n in anchors_mod.all_names(a)} - {""}
            ref = a.get("ref")
            scored = []
            for i, p in enumerate(pois):
                if p.poi_id in used:
                    continue
                if ref and haversine_m(ref["lat"], ref["lon"], p.lat, p.lon) > 600:
                    continue
                pk = name_keys(p)
                s = max((fuzz.ratio(x, y) for x in akeys for y in pk), default=0)
                if s >= 92:
                    scored.append((s, p.popularity.sources_confirming, i))
            if scored and (ref or len(scored) == 1 or max(scored)[0] == 100):
                scored.sort(reverse=True)
                top = scored[0][0]
                idx = [i for s, _, i in scored if s >= top - 2][:3] if ref else [scored[0][2]]
        idx = [i for i in idx if pois[i].poi_id not in used]
        if idx:
            group = [pois[i] for i in idx]
            target = _merge(group) if len(group) > 1 else group[0]
            for i in sorted(idx, reverse=True):
                pois.pop(i)
            found += 1
        elif a.get("ref"):
            target = _anchor_poi(a, fetched)
            created += 1
        else:
            log.warning("anchor %s not found in any source and has no reference point — skipped", a["id"])
            missing += 1
            continue
        # apply the curated layer
        cat = a["category"]
        n = target.name.model_copy()
        old_names = [x for x in (n.ru, n.kk, n.en) if x]
        for lang, v in a["names"].items():          # curated names win; source names move to alt
            if v:
                setattr(n, lang, v)
        alts = [*old_names, *n.alt, *a.get("alt", [])]
        n.alt = dedupe_alt(alts, n, cap=14)
        tags = set(target.walk_tags) | set(a.get("walk_tags", []))
        tags.discard("indoor")
        tags.discard("outdoor")
        indoor = CATEGORIES.get(cat, CATEGORIES["other"])["indoor"]
        if "warm_stop" in a.get("walk_tags", []) or cat == "other":
            indoor = True
        if a.get("outside_only"):
            indoor = False
        tags.add("indoor" if indoor else "outdoor")
        if a.get("outside_only"):
            tags.discard("warm_stop")
        attrs = dict(target.attributes)
        if a.get("status_note"):
            attrs["status_note"] = Attr(value=a["status_note"], src="seruen-curated", as_of=fetched[:10])
        if a.get("visit_note"):
            attrs["visit_note"] = Attr(value=a["visit_note"], src="seruen-curated", as_of=fetched[:10])
        hours = target.opening_hours
        if a.get("outside_only"):
            attrs["outside_only"] = Attr(value=True, src="seruen-curated", as_of=fetched[:10])
            if hours is not None:
                attrs["hours_text"] = Attr(value=hours.raw, src=hours.src, as_of=hours.as_of)
            hours = None
        desc = target.description
        if a.get("description") and (desc is None or desc.lang != "ru"):
            desc = Description(text=a["description"], lang="ru", src="seruen-curated", license="CC0 (Seruen curated)",
                               ai_generated=False)
        pop = target.popularity.model_copy()
        pop.curated_in = sorted(set(pop.curated_in) | {"seruen-anchors"})
        ext = dict(target.external_ids)
        if a.get("qid"):
            ext.setdefault("wikidata", a["qid"])
        ext["anchor"] = a["id"]
        srcs = list(target.sources)
        if not any(s.name == "seruen-curated" for s in srcs):
            srcs.append(SourceRef(name="seruen-curated", id=a["id"], license="CC0 (Seruen curated)",
                                  fetched_at=fetched))
        target = target.model_copy(update=dict(
            name=n, category=cat, walk_tags=finish_tags(tags, attrs), attributes=attrs, opening_hours=hours,
            description=desc, popularity=pop, external_ids=ext, sources=srcs,
            status=a.get("status", target.status),
            visit_min=CATEGORIES.get(cat, CATEGORIES["other"])["visit_min"]))
        used.add(target.poi_id)
        pois.append(target)
    # "other" objects were only kept as anchor candidates (stations, office buildings, name twins of anchors)
    before = len(pois)
    pois = [p for p in pois if p.category != "other" or p.external_ids.get("anchor")]
    apply_anchors.last_stats = {"found": found, "created_from_ref": created, "missing": missing,  # type: ignore[attr-defined]
                                "dropped_non_anchor_other": before - len(pois)}
    log.info("anchors: %s", apply_anchors.last_stats)
    return pois


# ======================================================================= scoring
def score(pois: list[POI], now: Optional[datetime] = None, with_anchors: bool = True) -> list[POI]:
    """Confidence / levels / status for every POI. Applies the curated anchors first unless already applied."""
    now = now or datetime.now(timezone.utc)
    if with_anchors and not any(p.external_ids.get("anchor") for p in pois):
        pois = apply_anchors(pois)
    out = []
    for p in pois:
        multi = p.popularity.sources_confirming >= 2
        # exists: best source × its freshness, ×1.2 when confirmed by another source
        ex = 0.0
        for s in p.sources:
            ex = max(ex, base_conf(s.name, p) * freshness(s.last_edit, now, s.name))
        ex = min(1.0, ex * (1.2 if multi else 1.0))
        # location: source of the coordinates × freshness, agreement when ≥ 2 located sources are within 75 m
        geo_src = p.geo_src.split(" ")[0]
        prim = next((s for s in p.sources if s.name == geo_src), p.sources[0] if p.sources else None)
        loc = base_conf(geo_src, p) * freshness(prim.last_edit if prim else None, now, geo_src)
        spread = p.attributes.get("geo_spread_m")
        if spread is not None and spread.value is not None and spread.value <= 75:
            loc *= 1.2
        loc = min(1.0, loc)
        # hours
        hc = 0.0
        if p.opening_hours:
            h = p.opening_hours
            hc = base_conf(h.src, p) * freshness(h.as_of, now, h.src)
            agree = p.attributes.get("hours_agree")
            if agree is not None and agree.value and len(agree.value) >= 2:
                hc *= 1.2
            hc = min(1.0, hc)
            p.opening_hours.level = hours_level(p, now)  # type: ignore[union-attr]
        for k, a in p.attributes.items():
            if k in ("wheelchair", "wifi", "power_sockets", "outdoor_seating", "fee", "cuisine"):
                a.level = attr_level(a, now)  # type: ignore[assignment]
        status = p.status
        if status == "active" and p.category in BUSINESS and not multi and p.sources and p.sources[0].name == "osm":
            le = p.sources[0].last_edit
            if le and le < CLOSED_BEFORE:
                status = "possibly_closed"
        out.append(p.model_copy(update=dict(
            confidence=Confidence(exists=round(ex, 3), location=round(loc, 3), hours=round(hc, 3)),
            status=status)))
    return out


def hours_level_counts(pois: list[POI], now: Optional[datetime] = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    c: dict[str, int] = {}
    for p in pois:
        lv = p.opening_hours.level if p.opening_hours else hours_level(p, now)
        c[lv] = c.get(lv, 0) + 1
    return c
