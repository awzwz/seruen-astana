"""Routing: real walking minutes (own OSRM in the cluster, public OSRM as backup, straight line × 1.3 as last resort)
and the mini-route of up to 3 stops with opening hours, weather limits and the final destination (airport, station).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx

from seruen.common.clients import call, endpoint
from seruen.common.config import settings
from seruen.common.geo import SLOW_M_PER_MIN, WALK_M_PER_MIN, haversine_m, walk_estimate
from seruen.common.models import CATEGORIES, POI, Constraints, Context, Leg, Route
from seruen.engine.hours import status_at
from seruen.engine.ranking import NIGHT_CATS
from seruen.engine.route import AIRPORT, TRAIN_STATION, Stop, best_route

TAXI_KMH = 55.0     # Astana arterials; +5 min to get a car (airport → centre measured 15–22 min by car)
TAXI_DETOUR = 1.25


def taxi_min(meters: float) -> float:
    return meters * TAXI_DETOUR / 1000 / TAXI_KMH * 60 + 5


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
    wants = {x["poi_id"]: frozenset(x.get("matched", [])) for x in p["ranked"]}
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
                return bool(c.night and poi.category in {"bar", "nightclub"})   # bars rarely publish hours: allowed, labelled
            return True
        return ok

    stops = [Stop(id=x.poi_id, score=scores.get(x.poi_id, 0.1), visit_min=x.visit_min,
                  indoor=CATEGORIES.get(x.category, {}).get("indoor", True), open_ok=open_ok_factory(x),
                  wants=wants.get(x.poi_id, frozenset())) for x in pois]
    end_min = None
    dest = None
    if c.destination:
        dest = AIRPORT if c.destination == "airport" else TRAIN_STATION
        # real car time with traffic from 2GIS for the origin, adjusted per stop by the extra distance (+10 % margin)
        base = None
        try:
            opt = (await call("places-live", "/v1/taxi", {"from": list(o), "to": list(dest)}, timeout=5)).get("option")
            base = opt["minutes"] if opt else None
        except Exception:
            base = None
        d0 = haversine_m(*o, *dest)
        if base:
            end_min = {x.poi_id: max(5.0, (base + (haversine_m(x.lat, x.lon, *dest) - d0) * TAXI_DETOUR / 1000 / TAXI_KMH * 60) * 1.1)
                       for x in pois}
            method += " · такси до цели по 2GIS"
        else:
            end_min = {x.poi_id: taxi_min(haversine_m(x.lat, x.lon, *dest)) * 1.1 for x in pois}
    budget = max(15, c.time_budget_min - c.reserve_min)
    ids, obj, timeline = best_route(origin_min, lambda a, b: mins(idx[a], idx[b]), stops, start, budget,
                                    max_stops=(c.max_stops if c.want_route else 1), short_mode=(ctx.mode != "walk"), end_min=end_min)
    walk = {x.poi_id: {"minutes": round(origin_min[x.poi_id]), "approx": mat is None} for x in pois}
    legs: list[Leg] = []
    if ids:
        seq = [o] + [pts[idx[i]] for i in ids]
        # final legs: 2GIS pedestrian routing (knows underpasses and paths through buildings); OSRM / straight line as fallback
        try:
            g2 = (await call("places-live", "/v1/walk", {"points": [list(x) for x in seq]}, timeout=5)).get("legs")
        except Exception:
            g2 = None
        geoms = None if g2 else await asyncio.gather(*[osrm_geometry(a, b) for a, b in zip(seq[:-1], seq[1:])])
        prev_id, prev_pt = "origin", o
        for k, pid in enumerate(ids):
            pt = pts[idx[pid]]
            if g2:
                m, meters, geo, approx = g2[k]["minutes"], g2[k]["meters"], g2[k]["geometry"], False
                if slow:
                    m = max(m, round(m * speed_scale))
            else:
                m = round(origin_min[pid]) if k == 0 else round(mins(idx[prev_id], idx[pid]))
                meters, geo, approx = int(haversine_m(*prev_pt, *pt) * 1.3), geoms[k], geoms[k] is None
            legs.append(Leg(from_id=prev_id, to_id=pid, meters=meters, minutes=m, outdoor_min=m,
                            geometry=geo or [[prev_pt[1], prev_pt[0]], [pt[1], pt[0]]], approx=approx or not geo))
            prev_id, prev_pt = pid, pt
        if g2:
            method = "2GIS (пешие маршруты) + " + method
            # re-time the plan with the 2GIS minutes
            t = start
            for leg, tl in zip(legs, timeline):
                t = t + timedelta(minutes=leg.minutes)
                tl["arrive"] = t.strftime("%H:%M")
                t = t + timedelta(minutes=tl.get("visit_min", 0))
                tl["leave"] = t.strftime("%H:%M")
    total = sum(l.minutes for l in legs) + sum(x.get("visit_min", 0) for x in timeline)
    r = Route(stops=ids, legs=legs, total_min=int(total), timeline=timeline,
              warm_stops=(ctx.mode != "walk" and len(ids) > 1)) if ids else None
    out = {"route": r.model_dump() if r else None, "walk": walk, "method": method, "budget_min": budget}
    if dest and ids:
        last = next(x for x in pois if x.poi_id == ids[-1])
        out["to_destination_min"] = round(end_min[last.poi_id])
    return out
