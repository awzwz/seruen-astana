"""Feedback API: «место закрыто», «неверные данные», «полезно». Goes to Kafka `feedback` with an H3 cell (~0.7 km²),
never with a user id or exact coordinates."""
from __future__ import annotations

from datetime import datetime, timezone

from seruen.common.clients import endpoint
from seruen.common.geo import geohash_decode, h3_cell, valid_geohash
from seruen.common.stores import publish

KINDS = {"closed", "wrong", "useful", "not_useful"}


@endpoint("feedback-api", "/v1/feedback")
async def feedback(p: dict) -> dict:
    kind = p.get("kind")
    pid = p.get("poi_id")
    if kind not in KINDS or not pid:
        return {"ok": False, "error": "bad_request"}
    cell = p.get("cell", "")
    h3 = h3_cell(*geohash_decode(cell), 8) if valid_geohash(cell) else None
    publish("feedback", pid, {"poi_id": pid, "kind": kind, "h3_r8": h3,
                              "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return {"ok": True}
