"""Schemas of the stores (idempotent): PostgreSQL catalog, OpenSearch index, ClickHouse analytics."""
from __future__ import annotations

import logging

from seruen.common.config import settings

log = logging.getLogger("seruen")

PG_DDL = """
create extension if not exists postgis;
create table if not exists staging_poi (
  source text not null, source_id text not null, run_id text, record jsonb not null,
  fetched_at timestamptz default now(), primary key (source, source_id));
create table if not exists poi (
  poi_id text primary key, data jsonb not null, geom geography(Point, 4326) not null,
  category text, status text, text_hash text, version int default 1, updated_at timestamptz default now());
create index if not exists poi_geom on poi using gist (geom);
create table if not exists poi_embedding (poi_id text primary key, text_hash text, model text, embedding real[]);
create table if not exists event (
  event_id text primary key, data jsonb not null, starts_at timestamptz, ends_at timestamptz,
  geom geography(Point, 4326));
create index if not exists event_geom on event using gist (geom);
create table if not exists outbox (id bigserial primary key, topic text, key text, payload jsonb, created_at timestamptz default now());
create table if not exists feedback_agg (poi_id text, kind text, n int default 0, last_at timestamptz,
  primary key (poi_id, kind));
create table if not exists partner_edit (id bigserial primary key, poi_id text, raw text, contact text,
  status text default 'pending', created_at timestamptz default now());
"""

OS_INDEX = {
    "settings": {"index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0}},
    "mappings": {"properties": {
        "poi_id": {"type": "keyword"}, "name_ru": {"type": "text"}, "name_kk": {"type": "text"},
        "name_en": {"type": "text"}, "alt": {"type": "text"}, "text": {"type": "text"},
        "category": {"type": "keyword"}, "walk_tags": {"type": "keyword"}, "status": {"type": "keyword"},
        "location": {"type": "geo_point"}, "popularity": {"type": "float"}, "hours_level": {"type": "keyword"},
        "embedding": {"type": "knn_vector", "dimension": 1024,
                      "method": {"name": "hnsw", "space_type": "cosinesimil", "engine": "lucene"}},
        "data": {"type": "object", "enabled": False},
    }},
}

CH_DDL = [
    "create database if not exists seruen",
    """create table if not exists seruen.events (
         ts DateTime, kind LowCardinality(String), h3_r8 String, hour UInt8, lang LowCardinality(String),
         mode LowCardinality(String), category LowCardinality(String), poi_id String, ai UInt8)
       engine = MergeTree order by (kind, ts) ttl ts + interval 30 day""",
    """create view if not exists seruen.demand_by_cell as
       select h3_r8, hour, count() as requests from seruen.events where kind = 'reco'
       group by h3_r8, hour having requests >= 20""",
]


def ensure_pg() -> None:
    from seruen.common.stores import pg_pool

    with pg_pool().connection() as conn:
        conn.execute(PG_DDL)
    log.info("postgres schema ok")


def ensure_opensearch() -> None:
    from seruen.common.stores import opensearch

    os_ = opensearch()
    if not os_.indices.exists(index="poi"):
        os_.indices.create(index="poi", body=OS_INDEX)
    log.info("opensearch index ok")


def clickhouse():
    import clickhouse_connect
    from urllib.parse import urlparse

    u = urlparse(settings().clickhouse_url)
    return clickhouse_connect.get_client(host=u.hostname, port=u.port or 8123, username=settings().clickhouse_user,
                                         password=settings().clickhouse_password)


def ensure_clickhouse() -> None:
    ch = clickhouse()
    for q in CH_DDL:
        ch.command(q)
    log.info("clickhouse schema ok")


def ensure_all() -> None:
    s = settings()
    for name, fn, on in (("pg", ensure_pg, s.pg_host), ("os", ensure_opensearch, s.opensearch_url),
                         ("ch", ensure_clickhouse, s.clickhouse_url)):
        if on:
            try:
                fn()
            except Exception as e:
                log.error("schema %s failed: %s", name, e)
