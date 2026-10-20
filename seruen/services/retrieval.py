"""Retrieval: places within walking reach → hard filters → lexical + vector search (hybrid) → top candidates.

cluster mode: OpenSearch (geo_distance filter + BM25 on names/text + k-NN on embeddings), fused in code.
local mode / fallback: the in-memory snapshot with the same scoring.
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from datetime import datetime, timedelta

import numpy as np

from seruen.common.catalog import snapshot, tokens
from seruen.common.clients import ServiceError, call, endpoint
from seruen.common.config import settings
from seruen.common.geo import haversine_m
from seruen.common.models import POI, Constraints, Context
from seruen.engine.intents import WANT_MAP
from seruen.engine.ranking import hard_filter, matched_wants
from seruen.engine.templates import WANT_LABEL

LIMIT = 80


def query_string(c: Constraints) -> str:
    extra = " ".join(WANT_LABEL[w][0] + " " + WANT_LABEL[w][2] for w in c.wants if w in WANT_LABEL)
    kids = " дети kids family" if c.kids else ""
    return (c.query_text + " " + extra + kids).strip()


async def query_vector(text: str) -> np.ndarray | None:
    if not text:
        return None
    try:
        r = await call("llm-gateway", "/v1/embed", {"texts": [text[:500]]}, timeout=4)
        return np.asarray(r["vectors"][0], dtype=np.float32)
    except (ServiceError, Exception):
        return None


def _lexical(qtok: set[str], variants: list[set[str]]) -> float:
    """Best share of a name variant's words found in the query ("Нур Алем" → alias of Alem.ai = 1.0)."""
    if not qtok or not variants:
        return 0.0
    best = 0.0
    for name_tok in variants:
        hit = sum(1 for n in name_tok if any(q == n or (len(q) > 4 and len(n) > 4 and (q in n or n in q)) for q in qtok))
        best = max(best, hit / max(1, len(name_tok)))
    return min(1.0, best)


def _local(lat, lon, radius, c: Constraints, ctx: Context, qvec, qtext) -> tuple[list[dict], Counter]:
    snap = snapshot()
    idx, dist = snap.within(lat, lon, radius)
    excluded: Counter = Counter()
    qtok = tokens(c.query_text)
    out = []
    sem_all = (snap.emb[idx] @ qvec) if (qvec is not None and snap.emb is not None and len(idx)) else None
    for k, (i, d) in enumerate(zip(idx, dist)):
        poi = snap.pois[int(i)]
        lex = _lexical(qtok, snap.name_tokens[int(i)])
        why = hard_filter(poi, c, ctx, lex)
        if why:
            excluded[why] += 1
            continue
        sem = float(sem_all[k]) if sem_all is not None else 0.0
        mw = matched_wants(poi, c)
        pre = 0.5 * sem + 0.3 * lex + 0.2 * (1.0 if mw else 0.0)
        out.append({"poi": poi, "distance_m": float(d), "semantic": sem, "lexical": lex, "_pre": pre})
    out.sort(key=lambda x: -x["_pre"])
    return out[:LIMIT], excluded


def _opensearch(lat, lon, radius, c: Constraints, ctx: Context, qvec, qtext) -> tuple[list[dict], Counter]:
    from seruen.common.stores import opensearch

    os_ = opensearch()
    geo = {"geo_distance": {"distance": f"{int(radius)}m", "location": {"lat": lat, "lon": lon}}}
    flt = [geo, {"bool": {"must_not": [{"term": {"status": "closed"}}]}}]
    hits: dict[str, dict] = {}

    def add(res, kind):
        for h in res["hits"]["hits"]:
            pid = h["_id"]
            e = hits.setdefault(pid, {"src": h["_source"], "semantic": 0.0, "lexical": 0.0})
            if kind == "knn":
                e["semantic"] = max(e["semantic"], 2 * float(h["_score"]) - 1)
            elif kind == "bm25":
                e["lexical"] = max(e["lexical"], min(1.0, float(h["_score"]) / 12.0))

    src = {"excludes": ["embedding"]}
    if c.query_text:
        add(os_.search(index="poi", body={"size": LIMIT, "_source": src, "query": {"bool": {"filter": flt, "must": [
            {"multi_match": {"query": c.query_text, "fields": ["name_ru^3", "name_kk^3", "name_en^3", "alt^2", "text"]}}]}}}), "bm25")
    if qvec is not None:
        add(os_.search(index="poi", body={"size": LIMIT, "_source": src, "query": {"knn": {"embedding": {
            "vector": qvec.tolist(), "k": LIMIT, "filter": {"bool": {"filter": flt}}}}}}), "knn")
    cats = set().union(*[WANT_MAP[w][0] for w in c.wants if w in WANT_MAP]) if c.wants else set()
    tags = set().union(*[WANT_MAP[w][1] for w in c.wants if w in WANT_MAP]) if c.wants else set()
    should = ([{"terms": {"category": sorted(cats)}}] if cats else []) + ([{"terms": {"walk_tags": sorted(tags)}}] if tags else [])
    body = {"size": LIMIT * 2, "_source": src, "query": {"bool": {"filter": flt, "should": should}},
            "sort": [{"_score": "desc"}, {"popularity": "desc"}]}
    add(os_.search(index="poi", body=body), "geo")
    excluded: Counter = Counter()
    out = []
    for pid, e in hits.items():
        poi = POI(**e["src"]["data"])
        why = hard_filter(poi, c, ctx, e["lexical"] if e["lexical"] >= 0.6 else 0.0)
        if why:
            excluded[why] += 1
            continue
        d = haversine_m(lat, lon, poi.lat, poi.lon)
        mw = matched_wants(poi, c)
        pre = 0.5 * e["semantic"] + 0.3 * e["lexical"] + 0.2 * (1.0 if mw else 0.0)
        out.append({"poi": poi, "distance_m": d, "semantic": e["semantic"], "lexical": e["lexical"], "_pre": pre})
    out.sort(key=lambda x: -x["_pre"])
    return out[:LIMIT], excluded


GENERIC = {"vecher", "vecherom", "utro", "den", "noch", "kofe", "kafe", "park", "tsentr", "ploshad", "mesto", "gorod",
           "bar", "dom", "muzei", "teatr", "kino", "skver", "sobytiya", "segodnya", "hochu", "ryadom", "astana", "cafe",
           "coffee", "museum", "center", "centre", "city", "event", "events", "tonight", "today"}


def named_far(lat, lon, radius, c: Constraints) -> list[dict]:
    """Places the user named explicitly ("Нур Алем", "Думан") that lie beyond walking reach.
    Exact word match only and no generic words, so «вечером» never finds a café called «Вечер»."""
    qtok = {t for t in tokens(c.query_text) if t not in GENERIC and len(t) >= 4}
    if not qtok:
        return []
    snap = snapshot()
    out = []
    for i, poi in enumerate(snap.pois):
        variants = [v for v in snap.name_tokens[i] if v and not v <= GENERIC]
        lex = max((sum(1 for n in v if n in qtok) / len(v) for v in variants), default=0.0)
        if lex >= 0.6 and poi.status != "closed":
            d = haversine_m(lat, lon, poi.lat, poi.lon)
            if d > radius:
                out.append({"poi": poi.model_dump(), "distance_m": round(d), "semantic": 0.0, "lexical": round(lex, 3)})
    out.sort(key=lambda x: (-x["lexical"], x["distance_m"]))
    return out[:2]


def events_near(lat, lon, radius, now: datetime, horizon_min: int) -> list[dict]:
    out = []
    end = now + timedelta(minutes=horizon_min + 180)
    for ev in snapshot().events:
        if ev.lat is None or ev.lon is None:
            continue
        try:
            st = datetime.fromisoformat(ev.start)
            en = datetime.fromisoformat(ev.end) if ev.end else st + timedelta(hours=2)
        except Exception:
            continue
        if en < now or st > end:
            continue
        d = haversine_m(lat, lon, ev.lat, ev.lon)
        if d <= radius * 1.5:
            out.append({"event": ev.model_dump(), "distance_m": d})
    out.sort(key=lambda x: x["distance_m"])
    return out[:5]


def merge_live(rows: list[dict], live: list[dict], lat: float, lon: float, c: Constraints, ctx: Context,
               excluded: Counter, from_wants: bool) -> tuple[list[dict], int, int]:
    """Fuse live 2GIS places into the candidates. Same place in both (≤ 80 m and similar name) → one card:
    our open-data record keeps its id/sources, 2GIS adds fresh hours, rating and accessibility. Nothing is stored."""
    from rapidfuzz import fuzz

    from seruen.common.catalog import norm

    import re

    def nm(x: str) -> str:   # "Монумент-Бәйтерек" → "monument baiterek"
        return re.sub(r"[^\w]+", " ", norm(x)).strip()

    merged = added = 0
    idx = [(r, nm(r["poi"].name.best("ru"))) for r in rows]
    for d in live:
        lp = POI(**d)
        ln = nm(lp.name.best("ru"))
        twin = None
        for r, rn in idx:
            if not (rn and ln):
                continue
            d_m = haversine_m(lp.lat, lp.lon, r["poi"].lat, r["poi"].lon)
            if d_m > 200:
                continue
            sim = fuzz.token_set_ratio(rn, ln)
            if (sim >= 90 and d_m <= 200) or (sim >= 75 and d_m <= 80):   # big objects: 2GIS pin and OSM centroid differ
                twin = r
                break
        if twin is not None:
            tp = twin["poi"].model_copy(deep=True)   # never mutate the shared snapshot
            twin["poi"] = tp
            if lp.opening_hours and (not tp.opening_hours or tp.opening_hours.level != "high"):
                tp.opening_hours = lp.opening_hours
            tp.popularity.rating, tp.popularity.reviews = lp.popularity.rating, lp.popularity.reviews
            for k in ("wheelchair", "wifi"):
                if k in lp.attributes and k not in tp.attributes:
                    tp.attributes[k] = lp.attributes[k]
            tp.walk_tags = sorted(set(tp.walk_tags) | set(lp.walk_tags))
            if tp.category == "other":
                tp.category = lp.category
            tp.sources = tp.sources + lp.sources
            tp.popularity.sources_confirming += 1
            merged += 1
            continue
        why = hard_filter(lp, c, ctx)
        if why:
            excluded[why] += 1
            continue
        dist = haversine_m(lat, lon, lp.lat, lp.lon)
        # no embeddings for live places: 2GIS already ranked them for a query built from the user's wishes
        sem = 0.5 if from_wants else 0.35
        mw = matched_wants(lp, c)
        rows.append({"poi": lp, "distance_m": dist, "semantic": sem, "lexical": 0.0,
                     "_pre": 0.5 * sem + 0.2 * (1.0 if mw else 0.0)})
        added += 1
    rows.sort(key=lambda x: -x["_pre"])
    return rows[: LIMIT + 20], merged, added


@endpoint("retrieval", "/v1/candidates")
async def candidates(p: dict) -> dict:
    t0 = time.perf_counter()
    lat, lon = p["origin"]["lat"], p["origin"]["lon"]
    radius = float(p["radius_m"])
    c = Constraints(**p["constraints"])
    ctx = Context(**p["context"])
    qtext = query_string(c)

    async def live_call(r_m: float) -> dict:
        try:
            return await call("places-live", "/v1/nearby", {"lat": lat, "lon": lon, "radius_m": r_m, "wants": c.wants,
                                                            "kids": c.kids > 0, "night": c.night, "mode": ctx.mode}, timeout=4)
        except Exception:
            return {"pois": [], "stats": {"enabled": False, "reason": "unavailable"}}

    live_task = asyncio.create_task(live_call(radius))     # 2GIS runs in parallel with our own catalog search
    qvec = np.asarray(p["query_vector"], dtype=np.float32) if p.get("query_vector") else await query_vector(qtext)
    backend = "snapshot"
    if settings().opensearch_url:
        try:
            rows, excluded = await asyncio.to_thread(_opensearch, lat, lon, radius, c, ctx, qvec, qtext)
            backend = "opensearch"
        except Exception:
            rows, excluded = await asyncio.to_thread(_local, lat, lon, radius, c, ctx, qvec, qtext)
            backend = "snapshot (OpenSearch unavailable)"
    else:
        rows, excluded = await asyncio.to_thread(_local, lat, lon, radius, c, ctx, qvec, qtext)
    live = await live_task
    rows, merged, added = merge_live(rows, live.get("pois", []), lat, lon, c, ctx, excluded, bool(c.wants or c.kids or c.night))
    expanded = False
    for factor in (1.6, 2.5):        # outskirts: widen the circle step by step, the card then says how far it is
        if len(rows) >= 6 or radius >= 5000:
            break
        radius2 = radius * factor
        rows2, ex2 = await asyncio.to_thread(_local if backend.startswith("snapshot") else _opensearch,
                                             lat, lon, radius2, c, ctx, qvec, qtext)
        live2 = await live_call(radius2) if len(live.get("pois", [])) < 6 else live
        rows2, m2, a2 = merge_live(rows2, live2.get("pois", []), lat, lon, c, ctx, ex2, bool(c.wants or c.kids or c.night))
        if len(rows2) > len(rows):
            rows, excluded, radius, expanded, live, merged, added = rows2, ex2, radius2, True, live2, m2, a2
    now = datetime.fromisoformat(ctx.local_time)
    evs = events_near(lat, lon, radius, now, c.time_budget_min)
    ls = live.get("stats", {})
    return {
        "candidates": [{"poi": r["poi"].model_dump(mode="json"), "distance_m": round(r["distance_m"]),
                        "semantic": round(r["semantic"], 4), "lexical": round(r["lexical"], 4)} for r in rows],
        "events": evs,
        "named_far": named_far(lat, lon, radius, c),
        "stats": {"backend": backend, "radius_m": round(radius), "expanded": expanded, "kept": len(rows),
                  "excluded": dict(excluded), "vector": qvec is not None,
                  "live": {"source": "2GIS", "enabled": ls.get("enabled", False), "queries": ls.get("queries", []),
                           "items": ls.get("items", 0), "merged": merged, "added": added, "reason": ls.get("reason")},
                  "ms": round((time.perf_counter() - t0) * 1000)},
    }
