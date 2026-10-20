"""Embeddings of POI.doc_text() (OpenAI text-embedding-3-large, dimensions=1024), cached by sha1(text).

Cache: data/cache/embeddings/<model>-<dim>.{keys.json,npy} — re-runs only embed new / changed texts.
Output: <out>/embeddings.npy (float32, L2-normalised, N × dim) + <out>/embedding_ids.json (row order = poi_id).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import numpy as np

from seruen.common.models import POI

from .util import cache_path, sha1

log = logging.getLogger("seruen.ingest.embed")

BATCH = 256
PRICE_PER_MTOK = 0.13   # text-embedding-3-large, USD


class EmbedCache:
    def __init__(self, model: str, dim: int):
        base = cache_path("embeddings", f"{model}-{dim}")
        self.kp, self.vp = base.with_suffix(".keys.json"), base.with_suffix(".npy")
        self.keys: list[str] = json.loads(self.kp.read_text()) if self.kp.exists() else []
        self.vecs = np.load(self.vp) if self.vp.exists() else np.zeros((0, dim), np.float32)
        if len(self.keys) != len(self.vecs):          # corrupted cache → start over
            self.keys, self.vecs = [], np.zeros((0, dim), np.float32)
        self.index = {k: i for i, k in enumerate(self.keys)}

    def get(self, k: str):
        i = self.index.get(k)
        return None if i is None else self.vecs[i]

    def add(self, ks: list[str], vs: np.ndarray) -> None:
        self.vecs = np.vstack([self.vecs, vs.astype(np.float32)])
        for k in ks:
            self.index[k] = len(self.keys)
            self.keys.append(k)
        self.save()

    def save(self) -> None:
        np.save(self.vp, self.vecs)
        self.kp.write_text(json.dumps(self.keys))


def _embed_batch(client, model: str, dim: int, texts: list[str]) -> tuple[np.ndarray, int]:
    delay = 2.0
    for attempt in range(6):
        try:
            r = client.embeddings.create(model=model, input=texts, dimensions=dim)
            arr = np.array([d.embedding for d in sorted(r.data, key=lambda d: d.index)], dtype=np.float32)
            return arr, int(getattr(r.usage, "total_tokens", 0) or 0)
        except Exception as e:  # noqa: BLE001
            if attempt == 5:
                raise
            log.warning("embed batch failed (%s), retry in %.0fs", type(e).__name__, delay)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def embed_texts(texts: list[str], model: str | None = None, dim: int | None = None) -> tuple[np.ndarray, dict]:
    """Embeds texts with caching; returns (L2-normalised matrix, stats)."""
    from seruen.common import config  # noqa: F401 — loads .env

    model = model or os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-large")
    dim = int(dim or os.environ.get("EMBED_DIM", "1024"))
    cache = EmbedCache(model, dim)
    keys = [sha1(t) for t in texts]
    missing = sorted({k: t for k, t in zip(keys, texts) if cache.get(k) is None}.items())
    tokens = 0
    if missing:
        from openai import OpenAI

        client = OpenAI(timeout=120)
        for i in range(0, len(missing), BATCH):
            chunk = missing[i:i + BATCH]
            vecs, tok = _embed_batch(client, model, dim, [t[:8000] for _, t in chunk])
            tokens += tok
            cache.add([k for k, _ in chunk], vecs)
            log.info("embedded %d/%d new texts", min(i + BATCH, len(missing)), len(missing))
    mat = np.stack([cache.get(k) for k in keys]).astype(np.float32) if keys else np.zeros((0, dim), np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    mat = mat / np.where(norms == 0, 1, norms)
    stats = {"model": model, "dim": dim, "rows": len(keys), "new_texts": len(missing), "cached": len(keys) - len(missing),
             "tokens": tokens, "cost_usd": round(tokens / 1e6 * PRICE_PER_MTOK, 4)}
    return mat, stats


def embed_pois(pois: list[POI], out_dir: Path, model: str | None = None, dim: int | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    mat, stats = embed_texts([p.doc_text() for p in pois], model, dim)
    np.save(out_dir / "embeddings.npy", mat)
    (out_dir / "embedding_ids.json").write_text(json.dumps([p.poi_id for p in pois], ensure_ascii=False))
    log.info("embeddings: %s", stats)
    return stats
