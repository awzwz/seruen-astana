"""Events API: afisha events near a point within a time window. Past events are never returned."""
from __future__ import annotations

from datetime import datetime

from seruen.common.clients import endpoint
from seruen.common.stores import pg_enabled, pg_pool
from seruen.engine.context_rules import now_local, parse_local
from seruen.services.retrieval import events_near


@endpoint("events-api", "/v1/events")
async def events(p: dict) -> dict:
    now = parse_local(p["from"]) if p.get("from") else now_local()
    horizon = int(p.get("horizon_min", 240))
    lat, lon, radius = p["lat"], p["lon"], float(p.get("radius_m", 2000))
    if pg_enabled():
        try:
            with pg_pool().connection() as conn:
                rows = conn.execute(
                    "select data, ST_Distance(geom, ST_MakePoint(%s,%s)::geography) d from event "
                    "where ends_at >= %s and starts_at <= %s + make_interval(mins => %s) "
                    "and ST_DWithin(geom, ST_MakePoint(%s,%s)::geography, %s) order by d limit 10",
                    (lon, lat, now, now, horizon, lon, lat, radius)).fetchall()
            return {"events": [{"event": r[0], "distance_m": float(r[1])} for r in rows], "source": "postgres"}
        except Exception:
            pass
    return {"events": events_near(lat, lon, radius, now, horizon), "source": "snapshot"}
