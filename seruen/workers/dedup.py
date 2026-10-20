"""dedup: after a collector run finishes (`ingest.run`), merge all staged records across sources into canonical POIs,
score field confidence, and write changes to the catalog (each change → `poi.changed`)."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from seruen.common.models import POI
from seruen.common.stores import pg_pool
from seruen.workers.common import consume, log, upsert_pois

DEBOUNCE_S = 20
_pending = {"since": 0.0}


def merge_all() -> None:
    from seruen.ingest import dedup as D
    from seruen.ingest import quality as Q

    with pg_pool().connection() as conn:
        rows = conn.execute("select record from staging_poi").fetchall()
    staged = [POI(**r[0]) for r in rows]
    merged = D.dedupe(staged)
    scored = Q.score(merged, datetime.now(timezone.utc))
    try:
        from seruen.ingest import describe

        scored = describe.add_templates(scored) if hasattr(describe, "add_templates") else scored
    except Exception:
        pass
    n = upsert_pois(scored)
    log.info("merge: %d staged → %d canonical, %d changed", len(staged), len(merged), n)


def handle(items: list[dict]) -> None:
    if items:
        _pending["since"] = time.time()


def idle() -> None:
    if _pending["since"] and time.time() - _pending["since"] > DEBOUNCE_S:
        _pending["since"] = 0.0
        merge_all()


def main() -> None:
    consume(["ingest.run"], "dedup", handle, batch=10, idle_hook=idle, poll_s=2.0)
