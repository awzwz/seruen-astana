"""Entity resolution across (and within) sources.

Candidates: pairs within ~250 m (H3 res 8 cell + its ring). Score = 0.6·name similarity (rapidfuzz on normalised +
transliterated names, generic words removed) + 0.25·proximity + 0.15·category compatibility; match at ≥ 0.8.
Hard keys: same Wikidata QID / same OSM id → match; same website or phone → match when ≤ 150 m and names are
not unrelated. Within one source the bar is higher (≥ 0.9 name, ≤ 100 m) so neighbouring branches stay apart.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from itertools import combinations
from typing import Optional

import h3
from rapidfuzz import fuzz

from seruen.common.geo import haversine_m
from seruen.common.models import CATEGORIES, POI, Attr, Names, Popularity

from .normalize import VenueIndex, dedupe_alt, finish_tags
from .util import norm_name

log = logging.getLogger("seruen.ingest.dedup")

RADIUS_M = 250.0
THRESHOLD = 0.8
SOURCE_RANK = {"osm": 0, "overture": 1, "wikidata": 2, "visitastana": 3, "wikivoyage": 4, "seruen-curated": 5}
DESC_RANK = {"seruen-curated": -1, "wikivoyage": 0, "visitastana": 1, "wikipedia": 2, "template": 8, "llm": 9}
ADDR_RANK = {"osm": 0, "visitastana": 1, "wikivoyage": 2, "overture": 3, "wikidata": 4}

GROUPS = [
    {"cafe", "coffee", "restaurant", "fast_food", "bar"},
    {"bar", "nightclub", "concert"},
    {"museum", "gallery", "landmark", "monument", "theatre", "concert", "religious"},
    {"park", "embankment", "landmark", "monument", "viewpoint", "playground"},
    {"attraction", "mall", "cinema", "sport", "landmark"},
    {"market", "mall"},
    {"library", "coworking", "gallery"},
]


def cat_compat(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if "other" in (a, b):
        return 0.5
    return 0.7 if any(a in g and b in g for g in GROUPS) else 0.0


def name_keys(p: POI) -> list[str]:
    if p.attributes.get("unnamed"):
        return []
    keys = []
    for x in (p.name.ru, p.name.kk, p.name.en, *p.name.alt):
        k = norm_name(x)
        if k and k not in keys:
            keys.append(k)
    return keys[:6]


def name_sim(ka: list[str], kb: list[str]) -> float:
    best = 0.0
    for a in ka:
        for b in kb:
            s = fuzz.ratio(a, b)
            na, nb = a.count(" ") + 1, b.count(" ") + 1
            # token-set only when it cannot reduce to "one shared word" (e.g. "Думан" ⊂ "бильярдная в отеле Думан")
            if min(len(a), len(b)) >= 4 and (min(na, nb) >= 2 or max(na, nb) <= 2):
                ts = fuzz.token_set_ratio(a, b)
                short = min(len(a), len(b)) < 6
                s = max(s, ts - (10 if short else 3))
            if s > best:
                best = s
                if best >= 100:
                    return 1.0
    return best / 100.0


def _ext(p: POI, k: str) -> Optional[str]:
    v = p.external_ids.get(k)
    return v.strip() if isinstance(v, str) and v.strip() else None


def pair_score(a: POI, b: POI, ka: list[str], kb: list[str], d: float) -> tuple[float, str]:
    """(score, reason). score ≥ THRESHOLD means same place."""
    qa, qb = _ext(a, "wikidata"), _ext(b, "wikidata")
    if qa and qb:
        return (1.0, "wikidata") if qa == qb else (0.0, "different_qid")
    oa, ob = _ext(a, "osm"), _ext(b, "osm")
    if oa and ob and oa == ob:
        return 1.0, "osm_id"
    if not ka or not kb:
        return 0.0, "unnamed"
    sim = name_sim(ka, kb)
    same_src = a.sources[0].name == b.sources[0].name
    if same_src:
        ok = sim >= 0.9 and d <= 100 and cat_compat(a.category, b.category) > 0
        return (1.0 if ok else 0.0), "within_source"
    for key in ("website", "phone"):
        va, vb = _ext(a, key), _ext(b, key)
        if va and vb and va == vb and d <= 150 and sim >= 0.5:
            return 1.0, key
    short = min(len(x) for x in ka + kb) < 6
    if sim < (0.85 if short else 0.75):
        return 0.0, "name"
    s = 0.6 * sim + 0.25 * max(0.0, 1 - d / RADIUS_M) + 0.15 * cat_compat(a.category, b.category)
    return s, "score"


class _DSU:
    def __init__(self, n: int):
        self.p = list(range(n))
        self.members = {i: [i] for i in range(n)}

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if len(self.members[ra]) < len(self.members[rb]):
            ra, rb = rb, ra
        self.p[rb] = ra
        self.members[ra].extend(self.members.pop(rb))


def _src(p: POI) -> str:
    return p.sources[0].name if p.sources else p.geo_src


def _richness(p: POI) -> tuple:
    return (bool(_ext(p, "wikidata")), bool(p.opening_hours), bool(p.website), len(p.attributes),
            (p.external_ids.get("osm") or "").startswith(("way", "relation")))


def _oh_norm(s: str) -> str:
    return s.replace(" ", "").replace("Mo-Su", "").lower()


def _merge(group: list[POI]) -> POI:
    group = sorted(group, key=lambda p: (SOURCE_RANK.get(_src(p), 9), tuple(-int(x) for x in _richness(p))))
    prim = group[0]
    if len(group) == 1:
        prim.popularity.sources_confirming = 1
        return prim
    cat = prim.category
    if cat == "other":
        cat = next((p.category for p in group if p.category != "other"), "other")
    # names
    n = Names(ru=prim.name.ru, kk=prim.name.kk, en=prim.name.en)
    alts: list[str] = list(prim.name.alt)
    for p in group[1:]:
        for lang in ("ru", "kk", "en"):
            v = getattr(p.name, lang)
            if v and not getattr(n, lang) and not prim.attributes.get("unnamed"):
                setattr(n, lang, v)
            elif v:
                alts.append(v)
        alts.extend(p.name.alt)
    if prim.attributes.get("unnamed"):
        named = next((p for p in group if not p.attributes.get("unnamed")), None)
        if named:
            n = Names(ru=named.name.ru, kk=named.name.kk, en=named.name.en)
    n.alt = dedupe_alt(alts, n, cap=12)
    # description: ru first, then source priority (wikivoyage > visitastana > wikipedia)
    descs = [p.description for p in group if p.description]
    desc = min(descs, key=lambda d: (d.lang != "ru", DESC_RANK.get(d.src, 5))) if descs else None
    # hours: freshest among trustworthy sources
    hrs = [p.opening_hours for p in group if p.opening_hours]
    hours = max(hrs, key=lambda h: (h.as_of or "0000", h.src == "visitastana")) if hrs else None
    # attributes: freshest per key
    attrs: dict = {}
    for p in group:
        for k, a in p.attributes.items():
            if k == "unnamed" and not prim.attributes.get("unnamed"):
                continue
            cur = attrs.get(k)
            if cur is None or (a.as_of or "") > (cur.as_of or ""):
                attrs[k] = a
    if any(not p.attributes.get("unnamed") for p in group):
        attrs.pop("unnamed", None)
    # agreement signals used by quality.score
    same = sorted({h.src for h in hrs if hours and _oh_norm(h.raw) == _oh_norm(hours.raw)})
    if len(same) >= 2:
        attrs["hours_agree"] = Attr(value=same, src="dedup")
    located = [p for p in group[1:] if p.geo_src != "none"]
    if located:
        spread = max(haversine_m(prim.lat, prim.lon, p.lat, p.lon) for p in located)
        attrs["geo_spread_m"] = Attr(value=int(spread), src="dedup")
    ext: dict = {}
    for p in group:
        for k, v in p.external_ids.items():
            ext.setdefault(k, v)
    addr = min(((ADDR_RANK.get(_src(p), 9), p.address) for p in group if p.address), default=(0, None))[1]
    website = next((p.website for p in group if p.website), None)
    pop = Popularity(
        wikidata_sitelinks=max(p.popularity.wikidata_sitelinks for p in group),
        sources_confirming=len({_src(p) for p in group}),
        curated_in=sorted({c for p in group for c in p.popularity.curated_in}),
        overture_confidence=max((p.popularity.overture_confidence for p in group
                                 if p.popularity.overture_confidence is not None), default=None),
    )
    tags = {t for p in group for t in p.walk_tags}
    tags.discard("indoor")
    tags.discard("outdoor")
    tags.add("indoor" if CATEGORIES.get(cat, CATEGORIES["other"])["indoor"] else "outdoor")
    sources, seen = [], set()
    for p in group:
        for s in p.sources:
            if (s.name, s.id) not in seen:
                seen.add((s.name, s.id))
                sources.append(s)
    return prim.model_copy(update=dict(
        name=n, category=cat, description=desc, opening_hours=hours, attributes=attrs,
        walk_tags=finish_tags(tags, attrs), external_ids=ext, address=addr, website=website, popularity=pop,
        sources=sources, visit_min=CATEGORIES.get(cat, CATEGORIES["other"])["visit_min"],
        updated_at=max((p.updated_at or "") for p in group) or None))


def dedupe(pois: list[POI]) -> list[POI]:
    located = [p for p in pois if p.geo_src != "none"]
    unlocated = [p for p in pois if p.geo_src == "none"]
    keys = [name_keys(p) for p in located]
    cells: dict[str, list[int]] = defaultdict(list)
    cell_of = []
    for i, p in enumerate(located):
        c = h3.latlng_to_cell(p.lat, p.lon, 8)
        cell_of.append(c)
        cells[c].append(i)
    edges: list[tuple[float, int, int, str]] = []
    for i, p in enumerate(located):
        for c in h3.grid_disk(cell_of[i], 1):
            for j in cells.get(c, ()):
                if j <= i:
                    continue
                q = located[j]
                d = haversine_m(p.lat, p.lon, q.lat, q.lon)
                if d > RADIUS_M:
                    continue
                s, why = pair_score(p, q, keys[i], keys[j], d)
                if s >= THRESHOLD:
                    edges.append((s, i, j, why))
    # hard-key matches beyond 250 m: same Wikidata QID / OSM id (≤ 2 km sanity bound)
    by_key: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, p in enumerate(located):
        for k in ("wikidata", "osm"):
            v = _ext(p, k)
            if v:
                by_key[(k, v)].append(i)
    for (k, _), idx in by_key.items():
        for i, j in combinations(idx, 2):
            a, b = located[i], located[j]
            if _src(a) == _src(b) and k == "osm":
                continue
            if haversine_m(a.lat, a.lon, b.lat, b.lon) <= 2000:
                edges.append((1.0, i, j, k))
    edges.sort(key=lambda e: -e[0])
    dsu = _DSU(len(located))
    reasons: dict[str, int] = defaultdict(int)
    for s, i, j, why in edges:
        ri, rj = dsu.find(i), dsu.find(j)
        if ri == rj:
            continue
        # no transitive chaining of two different objects from the same source unless they match directly
        ok = True
        for a in dsu.members[ri]:
            for b in dsu.members[rj]:
                if _src(located[a]) == _src(located[b]):
                    pa, pb = located[a], located[b]
                    d = haversine_m(pa.lat, pa.lon, pb.lat, pb.lon)
                    qa, qb = _ext(pa, "wikidata"), _ext(pb, "wikidata")
                    # block only clearly different objects: far apart, other QID, or unrelated names
                    if d > 150 or (qa and qb and qa != qb) or \
                            (keys[a] and keys[b] and name_sim(keys[a], keys[b]) < 0.5):
                        ok = False
                        break
            if not ok:
                break
        if ok:
            dsu.union(i, j)
            reasons[why] += 1
    merged = [_merge([located[i] for i in mem]) for mem in dsu.members.values()]
    # attach records without coordinates (Wikivoyage / visitastana listings) by QID or a confident name match
    by_qid = {_ext(p, "wikidata"): k for k, p in enumerate(merged) if _ext(p, "wikidata")}
    vidx = VenueIndex(merged)
    pos = {id(p): k for k, p in enumerate(merged)}
    attached = dropped = 0
    for u in unlocated:
        k = by_qid.get(_ext(u, "wikidata") or "")
        if k is None:
            best = None
            for nm in [u.name.ru, u.name.en, u.name.kk, *u.name.alt[:3]]:
                if nm:
                    p, s = vidx.match(nm, u.address, min_score=92)
                    if p is not None:
                        best = p
                        break
            k = pos.get(id(best)) if best is not None else None
        if k is None:
            dropped += 1
            continue
        target = merged[k]
        u2 = u.model_copy(update=dict(lat=target.lat, lon=target.lon, geo_src=target.geo_src))
        merged[k] = _merge([target, u2])
        pos[id(merged[k])] = k
        attached += 1
    cross = sum(1 for p in merged if p.popularity.sources_confirming > 1)
    dedupe.last_stats = {  # type: ignore[attr-defined]
        "input": len(pois), "located": len(located), "output": len(merged), "merged_away": len(located) - len(merged),
        "match_reasons": dict(reasons), "multi_source_pois": cross,
        "unlocated_attached": attached, "unlocated_dropped": dropped,
    }
    log.info("dedup: %s", dedupe.last_stats)
    return merged
