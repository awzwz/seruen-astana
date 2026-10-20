"""Routing: real walking minutes (own OSRM in the cluster, public OSRM as backup, straight line × 1.3 as last resort)
and the mini-route of up to 3 stops with opening hours, weather limits and the final destination (airport, station).
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import httpx

from seruen.common.clients import endpoint
from seruen.common.config import settings
from seruen.common.geo import SLOW_M_PER_MIN, WALK_M_PER_MIN, haversine_m, walk_estimate
from seruen.common.models import CATEGORIES, POI, Constraints, Context, Leg, Route
from seruen.engine.hours import status_at
from seruen.engine.route import AIRPORT, TRAIN_STATION, Stop, best_route

TAXI_KMH = 30.0     # city average incl. waiting (heuristic)


async def osrm_table(points: list[tuple[float, float]]) -> tuple[list[list[float]] | None, str]:
    """Durations matrix in minutes (foot), or None."""
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)
    urls = [u for u in (settings().osrm_url, settings().osrm_public) if u]
    for base in urls:
        prof = "foot" if "routed-foot" not in base else "foot"
        try:
            async with httpx.AsyncClient(timeout=1.5) as c:
                r = await c.get(f"{base.rstrip('/')}/table/v1/{prof}/{coords}", params={"annotations": "duration,distance"})
            if r.status_code == 200 and r.json().get("code") == "Ok":
                d = r.json()
                # OSRM foot profile assumes ~5 km/h; rescale to our measured 4.5 km/h through distances
                dist = d.get("distances")
                if dist:
                    return [[(x or 0) / WALK_M_PER_MIN for x in row] for row in dist], ("own OSRM" if base == settings().osrm_url else "public OSRM")
                return [[(x or 0) / 60 for x in row] for row in d["durations"]], "OSRM"
        except Exception:
            continue
    return None, "straight line × 1.3"


async def osrm_geometry(a: tuple[float, float], b: tuple[float, float]) -> list[list[float]] | None:
    for base in [u for u in (settings().osrm_url, settings().osrm_public) if u]:
        try:
            async with httpx.AsyncClient(timeout=2.5) as c:
                r = await c.get(f"{base.rstrip('/')}/route/v1/foot/{a[1]:.6f},{a[0]:.6f};{b[1]:.6f},{b[0]:.6f}",
                                params={"overview": "full", "geometries": "geojson"})
            if r.status_code == 200 and r.json().get("code") == "Ok":
                return r.json()["routes"][0]["geometry"]["coordinates"]
        except Exception:
            continue
    return None


@endpoint("routing", "/v1/route")
async def route(p: dict) -> dict:
    c = Constraints(**p["constraints"])
    ctx = Context(**p["context"])
    o = (p["origin"]["lat"], p["origin"]["lon"])
    pois = [POI(**x) for x in p["pois"]][:12]
    scores = {x["poi_id"]: x["score"] for x in p["ranked"]}
    slow = c.kids > 0 or c.mobility != "walk"
    speed_scale = (WALK_M_PER_MIN / SLOW_M_PER_MIN) if slow else 1.0
    pts = [o] + [(x.lat, x.lon) for x in pois]
    mat, method = await osrm_table(pts)
    if mat is None:
        def mins(i, j):
            return walk_estimate(haversine_m(*pts[i], *pts[j]), slow)[1]
    else:
        def mins(i, j):
            return max(1.0, mat[i][j] * speed_scale)
    idx = {x.poi_id: k + 1 for k, x in enumerate(pois)}
    origin_min = {x.poi_id: mins(0, idx[x.poi_id]) for x in pois}
    start = datetime.fromisoformat(ctx.local_time)
    lang = c.lang

    def open_ok_factory(poi: POI):
        def ok(arrive: datetime, visit: int) -> bool:
            st = status_at(poi, arrive, visit, lang, ctx.is_dark, ctx.is_holiday)
            if st.status in ("closed", "closes_soon"):
                return False
            if st.status == "unknown" and (ctx.is_dark or ctx.is_holiday):
                return False
            return True
        return ok

    stops = [Stop(id=x.poi_id, score=scores.get(x.poi_id, 0.1), visit_min=x.visit_min,
                  indoor=CATEGORIES.get(x.category, {}).get("indoor", True), open_ok=open_ok_factory(x)) for x in pois]
    end_min = None
    dest = None
    if c.destination:
        dest = AIRPORT if c.destination == "airport" else TRAIN_STATION
        end_min = {x.poi_id: haversine_m(x.lat, x.lon, *dest) * 1.4 / 1000 / TAXI_KMH * 60 for x in pois}
    budget = max(15, c.time_budget_min - c.reserve_min)
    ids, obj, timeline = best_route(origin_min, lambda a, b: mins(idx[a], idx[b]), stops, start, budget,
                                    max_stops=c.max_stops, short_mode=(ctx.mode != "walk"), end_min=end_min)
    walk = {x.poi_id: {"minutes": round(origin_min[x.poi_id]), "approx": mat is None} for x in pois}
    legs: list[Leg] = []
    if ids:
        prev_id, prev_pt = "origin", o
        geoms = await asyncio.gather(*[osrm_geometry(a, b) for a, b in zip(
            [o] + [pts[idx[i]] for i in ids[:-1]], [pts[idx[i]] for i in ids])])
        for k, pid in enumerate(ids):
            pt = pts[idx[pid]]
            m = round(origin_min[pid]) if k == 0 else round(mins(idx[prev_id], idx[pid]))
            meters = int(haversine_m(*prev_pt, *pt) * 1.3)
            legs.append(Leg(from_id=prev_id, to_id=pid, meters=meters, minutes=m, outdoor_min=m,
                            geometry=geoms[k] or [[prev_pt[1], prev_pt[0]], [pt[1], pt[0]]], approx=geoms[k] is None))
            prev_id, prev_pt = pid, pt
    total = sum(l.minutes for l in legs) + sum(x.get("visit_min", 0) for x in timeline)
    r = Route(stops=ids, legs=legs, total_min=int(total), timeline=timeline,
              warm_stops=(ctx.mode != "walk" and len(ids) > 1)) if ids else None
    out = {"route": r.model_dump() if r else None, "walk": walk, "method": method, "budget_min": budget}
    if dest and ids:
        last = next(x for x in pois if x.poi_id == ids[-1])
        out["to_destination_min"] = round(end_min[last.poi_id])
    return out
