"""Service calls that work the same in both runtime modes.

local   — the function registered for (service, path) is called in-process (a modular monolith);
cluster — an HTTP/JSON POST to http://<service>.<namespace>.svc:8000<path> (Linkerd adds mTLS + retries).

Each service module registers its handlers with @endpoint; base.make_app() turns them into FastAPI routes.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Awaitable, Callable

import httpx

from .config import settings

Handler = Callable[[dict], Any]
REGISTRY: dict[str, dict[str, Handler]] = {}

_client: httpx.AsyncClient | None = None


def endpoint(service: str, path: str):
    def deco(fn: Handler) -> Handler:
        REGISTRY.setdefault(service, {})[path] = fn
        return fn

    return deco


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=2.0),
                                    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))
    return _client


class ServiceError(RuntimeError):
    pass


def _ensure_loaded(service: str) -> None:
    if service not in REGISTRY:
        from seruen.services import load_service_module

        load_service_module(service)


async def call(service: str, path: str, payload: dict, timeout: float = 10.0) -> dict:
    """Call another service; returns its JSON body. Raises ServiceError on failure."""
    s = settings()
    if s.mode == "local":
        _ensure_loaded(service)
        fn = REGISTRY[service][path]
        try:
            res = fn(payload)
            if inspect.isawaitable(res):
                res = await asyncio.wait_for(res, timeout)
            return res
        except asyncio.TimeoutError as e:
            raise ServiceError(f"{service}{path}: timeout") from e
    url = s.service_url(service) + path
    try:
        r = await _http().post(url, json=payload, timeout=timeout)
    except httpx.HTTPError as e:
        raise ServiceError(f"{service}{path}: {type(e).__name__}") from e
    if r.status_code >= 400:
        raise ServiceError(f"{service}{path}: HTTP {r.status_code}")
    return r.json()


async def timed(service: str, path: str, payload: dict, timeout: float = 10.0) -> tuple[dict, float]:
    t = time.perf_counter()
    res = await call(service, path, payload, timeout)
    return res, (time.perf_counter() - t) * 1000
