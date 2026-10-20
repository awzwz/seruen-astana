"""Shared pieces of the background workers: Kafka consume loop with DLQ, catalog writes, OpenSearch docs."""
from __future__ import annotations

import hashlib
import logging
import signal
import time
from typing import Callable, Iterable

import orjson

from seruen.common.config import settings
from seruen.common.models import POI, Event
from seruen.common.stores import pg_pool, publish

log = logging.getLogger("seruen")
_running = True


def _stop(*_):
    global _running
    _running = False


def setup() -> None:
    logging.basicConfig(level=logging.INFO, format='{"t":"%(asctime)s","lvl":"%(levelname)s","svc":"'
                        + settings().service + '","msg":"%(message)s"}')
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)


def consume(topics: list[str], group: str, handle: Callable[[list[dict]], None], batch: int = 100,
            idle_hook: Callable[[], None] | None = None, poll_s: float = 1.0) -> None:
    """At-least-once loop: handle a batch, then commit. Failing messages go to `dlq` and are skipped."""
    from confluent_kafka import Consumer

    setup()
    c = Consumer({"bootstrap.servers": settings().kafka, "group.id": group, "auto.offset.reset": "earliest",
                  "enable.auto.commit": False, "max.poll.interval.ms": 600000})
    c.subscribe(topics)
    log.info("consuming %s as %s", topics, group)
    while _running:
        msgs = c.consume(num_messages=batch, timeout=poll_s)
        if not msgs:
            if idle_hook:
                idle_hook()
            continue
        items = []
        for m in msgs:
            if m.error():
                continue
            try:
                items.append({"topic": m.topic(), "key": m.key().decode() if m.key() else None,
                              "value": orjson.loads(m.value())})
            except Exception:
                publish("dlq", None, {"topic": m.topic(), "error": "bad_json"})
        try:
            handle(items)
        except Exception as e:
            log.exception("batch failed: %s", type(e).__name__)
            for it in items:
                publish("dlq", it["key"], {"topic": it["topic"], "error": type(e).__name__, "value": it["value"]})
        c.commit(asynchronous=False)
    c.close()


def text_hash(poi: POI) -> str:
    return hashlib.sha1(poi.doc_text().encode()).hexdigest()


def upsert_pois(pois: Iterable[POI], emit: bool = True) -> int:
    """Write canonical POIs; returns how many changed. Changed ids are emitted to `poi.changed` (outbox pattern)."""
    changed = 0
    with pg_pool().connection() as conn:
        for p in pois:
            th = text_hash(p)
            row = conn.execute("select text_hash, data->>'status', data->'opening_hours', data->'confidence' from poi where poi_id=%s",
                               (p.poi_id,)).fetchone()
            data = p.model_dump(mode="json")
            same = row and row[0] == th and row[1] == p.status and row[2] == data.get("opening_hours") and row[3] == data.get("confidence")
            if same:
                continue
            conn.execute(
                "insert into poi(poi_id, data, geom, category, status, text_hash, version, updated_at) "
                "values (%s, %s, ST_MakePoint(%s,%s)::geography, %s, %s, %s, 1, now()) "
                "on conflict (poi_id) do update set data=excluded.data, geom=excluded.geom, category=excluded.category, "
                "status=excluded.status, text_hash=excluded.text_hash, version=poi.version+1, updated_at=now()",
                (p.poi_id, orjson.dumps(data).decode(), p.lon, p.lat, p.category, p.status, th, ))
            changed += 1
            if emit:
                conn.execute("insert into outbox(topic, key, payload) values ('poi.changed', %s, %s)",
                             (p.poi_id, orjson.dumps({"poi_id": p.poi_id, "text_hash": th}).decode()))
                publish("poi.changed", p.poi_id, {"poi_id": p.poi_id, "text_hash": th})
    return changed


def upsert_events(events: Iterable[Event]) -> int:
    n = 0
    with pg_pool().connection() as conn:
        for e in events:
            geom = f"SRID=4326;POINT({e.lon} {e.lat})" if e.lat is not None and e.lon is not None else None
            conn.execute(
                "insert into event(event_id, data, starts_at, ends_at, geom) values (%s,%s,%s,coalesce(%s::timestamptz, %s::timestamptz + interval '2 hours'),%s::geography) "
                "on conflict (event_id) do update set data=excluded.data, starts_at=excluded.starts_at, ends_at=excluded.ends_at, geom=excluded.geom",
                (e.event_id, orjson.dumps(e.model_dump(mode="json")).decode(), e.start, e.end, e.start, geom))
            n += 1
        conn.execute("delete from event where ends_at < now() - interval '1 hour'")
    return n


def load_poi(poi_id: str) -> POI | None:
    with pg_pool().connection() as conn:
        row = conn.execute("select data from poi where poi_id=%s", (poi_id,)).fetchone()
    return POI(**row[0]) if row else None


def os_doc(p: POI, emb: list[float] | None) -> dict:
    from seruen.engine.ranking import popularity

    doc = {"poi_id": p.poi_id, "name_ru": p.name.ru, "name_kk": p.name.kk, "name_en": p.name.en,
           "alt": " ".join(p.name.alt), "text": p.doc_text(), "category": p.category, "walk_tags": p.walk_tags,
           "status": p.status, "location": {"lat": p.lat, "lon": p.lon}, "popularity": popularity(p),
           "hours_level": p.opening_hours.level if p.opening_hours else "unknown", "data": p.model_dump(mode="json")}
    if emb:
        doc["embedding"] = emb
    return doc


def wait_for(name: str, check: Callable[[], None], tries: int = 60, delay: float = 5.0) -> None:
    for i in range(tries):
        try:
            check()
            return
        except Exception as e:
            log.info("waiting for %s (%s)", name, type(e).__name__)
            time.sleep(delay)
    raise RuntimeError(f"{name} not reachable")
