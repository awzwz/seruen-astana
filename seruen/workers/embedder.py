"""embedder: poi.changed → embedding of the multilingual card text (only when the text changed) → `poi.embedded`."""
from __future__ import annotations

from openai import OpenAI

from seruen.common.config import settings
from seruen.common.stores import pg_pool, publish
from seruen.workers.common import consume, load_poi, log, text_hash

_client = None


def handle(items: list[dict]) -> None:
    global _client
    _client = _client or OpenAI(max_retries=2, timeout=30)
    s = settings()
    todo = []
    with pg_pool().connection() as conn:
        for pid in dict.fromkeys(it["value"]["poi_id"] for it in items):
            p = load_poi(pid)
            if not p:
                continue
            th = text_hash(p)
            row = conn.execute("select text_hash from poi_embedding where poi_id=%s", (pid,)).fetchone()
            if row and row[0] == th:
                publish("poi.embedded", pid, {"poi_id": pid})     # e.g. hours changed → still reindex
                continue
            todo.append((p, th))
        for i in range(0, len(todo), 128):
            chunk = todo[i:i + 128]
            r = _client.embeddings.create(model=s.embed_model, input=[p.doc_text()[:2000] for p, _ in chunk],
                                          dimensions=s.embed_dim)
            for (p, th), d in zip(chunk, r.data):
                conn.execute("insert into poi_embedding(poi_id, text_hash, model, embedding) values (%s,%s,%s,%s) "
                             "on conflict (poi_id) do update set text_hash=excluded.text_hash, embedding=excluded.embedding",
                             (p.poi_id, th, s.embed_model, d.embedding))
                publish("poi.embedded", p.poi_id, {"poi_id": p.poi_id})
    if todo:
        log.info("embedded %d", len(todo))


def main() -> None:
    consume(["poi.changed"], "embedder", handle, batch=200)
