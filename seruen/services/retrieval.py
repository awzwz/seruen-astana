"""Retrieval: places within walking reach → hard filters → lexical + vector search (hybrid) → top candidates.

cluster mode: OpenSearch (geo_distance filter + BM25 on names/text + k-NN on embeddings), fused in code.
local mode / fallback: the in-memory snapshot with the same scoring.
"""
from __future__ import annotations

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


@endpoint("retrieval", "/v1/candidates")
async def candidates(p: dict) -> dict:
    t0 = time.perf_counter()
    lat, lon = p["origin"]["lat"], p["origin"]["lon"]
    radius = float(p["radius_m"])
    c = Constraints(**p["constraints"])
    ctx = Context(**p["context"])
    qtext = query_string(c)
    qvec = np.asarray(p["query_vector"], dtype=np.float32) if p.get("query_vector") else await query_vector(qtext)
    backend = "snapshot"
    if settings().opensearch_url:
        try:
            rows, excluded = _opensearch(lat, lon, radius, c, ctx, qvec, qtext)
            backend = "opensearch"
        except Exception:
            rows, excluded = _local(lat, lon, radius, c, ctx, qvec, qtext)
            backend = "snapshot (OpenSearch unavailable)"
    else:
        rows, excluded = _local(lat, lon, radius, c, ctx, qvec, qtext)
    expanded = False
    if len(rows) < 6 and radius < 4000:
        radius2 = radius * 1.6
        rows2, ex2 = (_local if backend.startswith("snapshot") else _opensearch)(lat, lon, radius2, c, ctx, qvec, qtext)
        if len(rows2) > len(rows):
            rows, excluded, radius, expanded = rows2, ex2, radius2, True
    now = datetime.fromisoformat(ctx.local_time)
    evs = events_near(lat, lon, radius, now, c.time_budget_min)
    return {
        "candidates": [{"poi": r["poi"].model_dump(), "distance_m": round(r["distance_m"]), "semantic": round(r["semantic"], 4),
                        "lexical": round(r["lexical"], 4)} for r in rows],
        "events": evs,
        "named_far": named_far(lat, lon, radius, c),
        "stats": {"backend": backend, "radius_m": round(radius), "expanded": expanded, "kept": len(rows),
                  "excluded": dict(excluded), "vector": qvec is not None, "ms": round((time.perf_counter() - t0) * 1000)},
    }
