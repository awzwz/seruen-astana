"""LLM gateway: the only place that talks to OpenAI. Budget, cache, retries, usage accounting.

What is sent is decided by the callers and recorded in the trace: a cell-free text, filters, tags and
facts about candidate places. Never coordinates, never a user id.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import date

import orjson

from seruen.common.clients import endpoint
from seruen.common.config import settings
from seruen.common.stores import jget, jset, kv

_client = None


def client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(max_retries=1, timeout=settings().llm_timeout + 2)
    return _client


def _key(*parts) -> str:
    return hashlib.sha256(orjson.dumps(parts)).hexdigest()[:32]


def _budget_ok(tokens: int = 0) -> bool:
    k = f"llm:tokens:{date.today().isoformat()}"
    used = kv(1).incrby(k, tokens, ex=2 * 86400) if tokens else int(kv(1).get(k) or 0)
    return used < settings().llm_daily_token_budget


@endpoint("llm-gateway", "/v1/complete")
async def complete(p: dict) -> dict:
    """p = {purpose, system, user, schema, schema_name, max_output_tokens, timeout, cache_ttl}"""
    s = settings()
    model = p.get("model") or s.openai_model
    ck = "llm:c:" + _key(model, p.get("system"), p.get("user"), p.get("schema"))
    cached = jget(kv(1), ck)
    if cached:
        return {**cached, "cached": True}
    if not _budget_ok():
        return {"ok": False, "error": "budget_exceeded"}
    t = time.perf_counter()
    try:
        r = await asyncio.wait_for(client().responses.create(
            model=model,
            reasoning={"effort": "none"},
            input=[{"role": "system", "content": p["system"]}, {"role": "user", "content": p["user"]}],
            text={"format": {"type": "json_schema", "name": p.get("schema_name", "out"), "strict": True,
                             "schema": p["schema"]}},
            max_output_tokens=p.get("max_output_tokens", 900),
        ), timeout=p.get("timeout", s.llm_timeout))
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout", "latency_ms": (time.perf_counter() - t) * 1000}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "latency_ms": (time.perf_counter() - t) * 1000}
    usage = {"input": r.usage.input_tokens, "output": r.usage.output_tokens}
    _budget_ok(usage["input"] + usage["output"])
    try:
        out = orjson.loads(r.output_text)
    except Exception:
        return {"ok": False, "error": "bad_json", "latency_ms": (time.perf_counter() - t) * 1000}
    res = {"ok": True, "json": out, "usage": usage, "model": model, "latency_ms": round((time.perf_counter() - t) * 1000)}
    jset(kv(1), ck, res, ex=int(p.get("cache_ttl", 3600)))
    return res


@endpoint("llm-gateway", "/v1/embed")
async def embed(p: dict) -> dict:
    """p = {texts: [...]} → {vectors: [[...]]} (L2-normalised by the API), cached per text for 7 days."""
    s = settings()
    texts: list[str] = p["texts"]
    out: list = [None] * len(texts)
    missing = []
    for i, tx in enumerate(texts):
        v = jget(kv(1), "emb:" + _key(s.embed_model, s.embed_dim, tx))
        if v is not None:
            out[i] = v
        else:
            missing.append(i)
    if missing:
        r = await client().embeddings.create(model=s.embed_model, input=[texts[i] for i in missing],
                                             dimensions=s.embed_dim)
        for j, i in enumerate(missing):
            vec = [round(x, 6) for x in r.data[j].embedding]
            out[i] = vec
            jset(kv(1), "emb:" + _key(s.embed_model, s.embed_dim, texts[i]), vec, ex=7 * 86400)
    return {"vectors": out, "model": s.embed_model}
