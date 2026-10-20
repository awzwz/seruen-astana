"""Catalog API: POI cards by id. PostgreSQL in the cluster (source of truth), snapshot as fallback."""
from __future__ import annotations

from seruen.common.catalog import snapshot
from seruen.common.clients import endpoint
from seruen.common.stores import pg_enabled, pg_pool


def _from_pg(ids: list[str]) -> dict[str, dict]:
    with pg_pool().connection() as conn:
        rows = conn.execute("select poi_id, data from poi where poi_id = any(%s)", (ids,)).fetchall()
    return {r[0]: r[1] for r in rows}


@endpoint("catalog-api", "/v1/poi/get")
async def get(p: dict) -> dict:
    ids = list(p.get("ids", []))[:100]
    found: dict[str, dict] = {}
    src = "snapshot"
    if pg_enabled():
        try:
            found = _from_pg(ids)
            src = "postgres"
        except Exception:
            found = {}
    if not found:
        snap = snapshot()
        found = {i: snap.by_id[i].model_dump() for i in ids if i in snap.by_id}
    return {"pois": found, "source": src}


@endpoint("catalog-api", "/v1/poi/stats")
async def stats(p: dict) -> dict:
    snap = snapshot()
    by_cat: dict[str, int] = {}
    for x in snap.pois:
        by_cat[x.category] = by_cat.get(x.category, 0) + 1
    return {"pois": len(snap.pois), "events": len(snap.events), "by_category": by_cat}
