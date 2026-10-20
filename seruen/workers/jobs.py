"""One-shot jobs: schema, collectors (CronJobs) and the initial load of the baked-in snapshot."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import numpy as np
import orjson

from seruen.common.catalog import snapshot
from seruen.common.config import settings
from seruen.common.stores import flush, pg_pool, publish
from seruen.workers.common import os_doc, setup, upsert_events, upsert_pois, wait_for
from seruen.workers.schema import ensure_all

log = logging.getLogger("seruen")


def archive_raw(source: str, run_id: str, raws: list[dict]) -> None:
    """Keep the raw dump in MinIO (bucket `raw`) so the catalog can be rebuilt without re-downloading."""
    ep = settings().minio_endpoint
    if not ep:
        return
    try:
        import io
        import os

        import boto3  # optional

        s3 = boto3.client("s3", endpoint_url=ep, aws_access_key_id=os.environ.get("MINIO_ACCESS_KEY"),
                          aws_secret_access_key=os.environ.get("MINIO_SECRET_KEY"))
        body = b"\n".join(orjson.dumps(r) for r in raws)
        s3.put_object(Bucket="raw", Key=f"{source}/{run_id}.jsonl", Body=io.BytesIO(body))
    except Exception as e:
        log.warning("raw archive skipped: %s", type(e).__name__)


def collect(source: str) -> None:
    from seruen.ingest import collectors

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    raws = collectors.get(source)()
    topic = "events.raw" if source == "sxodim" else "poi.raw"
    for r in raws:
        publish(topic, f"{r.get('source', source)}:{r.get('source_id')}", {**r, "run_id": run_id})
    publish("ingest.run", source, {"source": source, "run_id": run_id, "count": len(raws),
                                   "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    flush(30)
    archive_raw(source, run_id, raws)
    log.info("collected %s: %d records (run %s)", source, len(raws), run_id)


def initial_load() -> None:
    """Load the snapshot built by `python -m seruen.ingest.run` into PostgreSQL + OpenSearch in one go."""
    from seruen.common.stores import opensearch

    s = settings()
    wait_for("postgres", lambda: pg_pool().connection().__enter__().execute("select 1"))
    ensure_all()
    snap = snapshot()
    n = upsert_pois(snap.pois, emit=False)
    log.info("postgres: %d POIs written", n)
    if snap.events:
        upsert_events(snap.events)
    ids = json.loads((s.catalog_dir / "embedding_ids.json").read_text()) if (s.catalog_dir / "embedding_ids.json").exists() else []
    emb = np.load(s.catalog_dir / "embeddings.npy") if ids else None
    vec = {pid: emb[i].tolist() for i, pid in enumerate(ids)} if ids else {}
    with pg_pool().connection() as conn:
        for pid, v in vec.items():
            p = snap.by_id.get(pid)
            if p is None:
                continue
            from seruen.workers.common import text_hash

            conn.execute("insert into poi_embedding(poi_id, text_hash, model, embedding) values (%s,%s,%s,%s) "
                         "on conflict (poi_id) do update set text_hash=excluded.text_hash, embedding=excluded.embedding",
                         (pid, text_hash(p), s.embed_model, v))
    if s.opensearch_url:
        from opensearchpy import helpers

        os_ = opensearch()
        actions = ({"_index": "poi", "_id": p.poi_id, "_source": os_doc(p, vec.get(p.poi_id))} for p in snap.pois)
        ok, _ = helpers.bulk(os_, actions, chunk_size=300, request_timeout=120)
        os_.indices.refresh(index="poi")
        log.info("opensearch: %d docs indexed", ok)


def main(service: str) -> None:
    setup()
    if service == "schema":
        ensure_all()
    elif service == "initial-load":
        initial_load()
    elif service.startswith("collect-"):
        collect(service.removeprefix("collect-"))
    flush(10)
