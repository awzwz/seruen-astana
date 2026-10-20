"""Lazy connections to the data stores, with in-memory/file fallbacks for local mode."""
from __future__ import annotations

import json
import threading
import time
from functools import lru_cache
from typing import Any, Optional

import orjson

from .config import settings


# ------------------------------------------------------------------ cache / KV
class MemoryKV:
    def __init__(self) -> None:
        self._d: dict[str, tuple[float, bytes]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[bytes]:
        with self._lock:
            v = self._d.get(key)
            if not v:
                return None
            exp, val = v
            if exp and exp < time.time():
                self._d.pop(key, None)
                return None
            return val

    def set(self, key: str, val: bytes, ex: int | None = None) -> None:
        with self._lock:
            self._d[key] = ((time.time() + ex) if ex else 0, val)

    def delete(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._d.pop(k, None)

    def incrby(self, key: str, n: int, ex: int | None = None) -> int:
        with self._lock:
            cur = int((self._d.get(key) or (0, b"0"))[1])
            cur += n
            self._d[key] = ((time.time() + ex) if ex else 0, str(cur).encode())
            return cur


class RedisKV:
    def __init__(self, url: str, db: int) -> None:
        import redis

        base = url.rsplit("/", 1)[0] if url.count("/") > 2 else url
        self.r = redis.Redis.from_url(f"{base}/{db}", socket_timeout=1.5, socket_connect_timeout=1.5)

    def get(self, key: str) -> Optional[bytes]:
        try:
            return self.r.get(key)
        except Exception:
            return None

    def set(self, key: str, val: bytes, ex: int | None = None) -> None:
        try:
            self.r.set(key, val, ex=ex)
        except Exception:
            pass

    def delete(self, *keys: str) -> None:
        try:
            self.r.delete(*keys)
        except Exception:
            pass

    def incrby(self, key: str, n: int, ex: int | None = None) -> int:
        try:
            v = self.r.incrby(key, n)
            if ex and v == n:
                self.r.expire(key, ex)
            return int(v)
        except Exception:
            return 0


_kv: dict[int, Any] = {}


def kv(db: int = 1):
    """db 0 = sessions, 1 = caches, 2 = Celery broker."""
    if db not in _kv:
        url = settings().redis_url
        _kv[db] = RedisKV(url, db) if url else MemoryKV()
    return _kv[db]


def jget(store, key: str) -> Any:
    b = store.get(key)
    return orjson.loads(b) if b else None


def jset(store, key: str, val: Any, ex: int | None = None) -> None:
    store.set(key, orjson.dumps(val), ex=ex)


# ------------------------------------------------------------------ PostgreSQL
@lru_cache
def pg_pool():
    from psycopg_pool import ConnectionPool

    s = settings()
    return ConnectionPool(s.pg_dsn, min_size=1, max_size=5, kwargs={"autocommit": True}, open=True)


def pg_enabled() -> bool:
    return bool(settings().pg_host)


# ------------------------------------------------------------------ OpenSearch
@lru_cache
def opensearch():
    from opensearchpy import OpenSearch

    return OpenSearch(settings().opensearch_url, timeout=5, max_retries=1, retry_on_timeout=True)


# ------------------------------------------------------------------ Kafka
_producer = None


def kafka_enabled() -> bool:
    return bool(settings().kafka)


def producer():
    global _producer
    if _producer is None:
        from confluent_kafka import Producer

        _producer = Producer({"bootstrap.servers": settings().kafka, "linger.ms": 20,
                              "client.id": settings().service, "acks": "all"})
    return _producer


def publish(topic: str, key: str | None, value: dict) -> None:
    """Fire-and-forget publish. In local mode events are appended to data/events/<topic>.jsonl."""
    if kafka_enabled():
        try:
            producer().produce(topic, key=key.encode() if key else None, value=orjson.dumps(value))
            producer().poll(0)
        except Exception:
            pass
        return
    d = settings().data_dir / "events"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{topic}.jsonl", "ab") as f:
        f.write(orjson.dumps({"key": key, "value": value}) + b"\n")


def flush(timeout: float = 5.0) -> None:
    if _producer is not None:
        _producer.flush(timeout)


def dumps(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False)
