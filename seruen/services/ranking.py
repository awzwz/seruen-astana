"""Ranking: transparent formula + diversity; group rule = hard constraints of anyone filter for everyone."""
from __future__ import annotations

from seruen.common.clients import endpoint
from seruen.common.geo import walk_estimate
from seruen.common.models import POI, Constraints, Context
from seruen.engine.ranking import W, diversify, score


@endpoint("ranking", "/v1/rank")
async def rank(p: dict) -> dict:
    c = Constraints(**p["constraints"])
    ctx = Context(**p["context"])
    slow = c.kids > 0 or c.mobility != "walk"
    rows = []
    for cand in p["candidates"]:
        poi = POI(**cand["poi"])
        _, minutes = walk_estimate(cand["distance_m"], slow)
        s, parts, matched = score(poi, c, ctx, minutes, cand.get("semantic", 0.0), cand.get("lexical", 0.0))
        rows.append((poi.poi_id, s, poi.category, parts, matched, minutes))
    rows.sort(key=lambda r: -r[1])
    by_id = {r[0]: r for r in rows}
    top = diversify([(r[0], r[1], r[2]) for r in rows], k=int(p.get("k", 15)))
    return {"ranked": [{"poi_id": pid, "score": by_id[pid][1], "parts": by_id[pid][3], "matched": by_id[pid][4],
                        "est_walk_min": by_id[pid][5]} for pid in top],
            "weights": W, "considered": len(rows)}
