"""normalizer: poi.raw / events.raw → unified records in PostgreSQL staging (and `poi.normalized`)."""
from __future__ import annotations

import orjson

from seruen.common.stores import pg_pool, publish
from seruen.workers.common import consume, log, upsert_events


def handle(items: list[dict]) -> None:
    from seruen.ingest import normalize as N

    pois_raw = [it["value"] for it in items if it["topic"] == "poi.raw"]
    ev_raw = [it["value"] for it in items if it["topic"] == "events.raw"]
    if pois_raw:
        pois = N.normalize_records(pois_raw)
        with pg_pool().connection() as conn:
            for p in pois:
                src = p.sources[0] if p.sources else None
                conn.execute(
                    "insert into staging_poi(source, source_id, run_id, record, fetched_at) values (%s,%s,%s,%s, now()) "
                    "on conflict (source, source_id) do update set record=excluded.record, run_id=excluded.run_id, fetched_at=now()",
                    (src.name if src else "unknown", (src.id if src and src.id else p.poi_id), pois_raw[0].get("run_id"),
                     orjson.dumps(p.model_dump(mode="json")).decode()))
                publish("poi.normalized", p.poi_id, {"poi_id": p.poi_id, "source": src.name if src else None})
        log.info("normalized %d → %d", len(pois_raw), len(pois))
    if ev_raw:
        fn = getattr(N, "normalize_events", None)
        events = fn(ev_raw) if fn else []
        upsert_events(events)
        log.info("events normalized %d", len(events))


def main() -> None:
    consume(["poi.raw", "events.raw"], "normalizer", handle, batch=500)
