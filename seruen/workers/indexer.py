"""indexer: poi.embedded → OpenSearch document (text + geo + vector), then invalidate cached answers."""
from __future__ import annotations

from seruen.common.stores import kv, opensearch, pg_pool
from seruen.workers.common import consume, load_poi, log, os_doc


def handle(items: list[dict]) -> None:
    from opensearchpy import helpers

    ids = list(dict.fromkeys(it["value"]["poi_id"] for it in items))
    actions = []
    with pg_pool().connection() as conn:
        for pid in ids:
            p = load_poi(pid)
            if not p:
                continue
            row = conn.execute("select embedding from poi_embedding where poi_id=%s", (pid,)).fetchone()
            actions.append({"_index": "poi", "_id": pid, "_source": os_doc(p, list(row[0]) if row else None)})
    if actions:
        helpers.bulk(opensearch(), actions, chunk_size=200, request_timeout=60)
        kv(1).incrby("catalog:version", 1)
        log.info("indexed %d", len(actions))


def main() -> None:
    consume(["poi.embedded"], "indexer", handle, batch=200)
