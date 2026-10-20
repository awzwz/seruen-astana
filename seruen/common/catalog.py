"""In-memory catalog snapshot (local mode and the fallback when OpenSearch/PostgreSQL are unavailable)."""
from __future__ import annotations

import json
import re
import threading
from functools import lru_cache

import numpy as np
import orjson

from .config import settings
from .models import POI, Event

_lock = threading.Lock()
_TOKEN = re.compile(r"[\wәғқңөұүһі]+", re.I)

_TRANSLIT = str.maketrans({"ә": "a", "ғ": "g", "қ": "k", "ң": "n", "ө": "o", "ұ": "u", "ү": "u", "һ": "h", "і": "i",
                           "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
                           "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
                           "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh",
                           "щ": "sh", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"})


def norm(s: str) -> str:
    return (s or "").lower().translate(_TRANSLIT)


def tokens(s: str) -> set[str]:
    return {w for w in _TOKEN.findall(norm(s)) if len(w) > 2}


class Snapshot:
    def __init__(self) -> None:
        d = settings().catalog_dir
        self.pois: list[POI] = []
        p = d / "pois.jsonl"
        if p.exists():
            with open(p, "rb") as f:
                self.pois = [POI(**orjson.loads(line)) for line in f if line.strip()]
        self.by_id = {x.poi_id: x for x in self.pois}
        self.lat = np.array([x.lat for x in self.pois], dtype=np.float64)
        self.lon = np.array([x.lon for x in self.pois], dtype=np.float64)
        self.name_tokens = [tokens(" ".join(filter(None, [x.name.ru, x.name.kk, x.name.en, *x.name.alt]))) for x in self.pois]
        self.emb = None
        ep, ip = d / "embeddings.npy", d / "embedding_ids.json"
        if ep.exists() and ip.exists():
            ids = json.loads(ip.read_text())
            m = np.load(ep).astype(np.float32)
            idx = {pid: i for i, pid in enumerate(ids)}
            dim = m.shape[1]
            self.emb = np.zeros((len(self.pois), dim), dtype=np.float32)
            for i, x in enumerate(self.pois):
                j = idx.get(x.poi_id)
                if j is not None:
                    self.emb[i] = m[j]
        self.events: list[Event] = []
        evp = d / "events.jsonl"
        if evp.exists():
            with open(evp, "rb") as f:
                self.events = [Event(**orjson.loads(line)) for line in f if line.strip()]

    def within(self, lat: float, lon: float, radius_m: float) -> tuple[np.ndarray, np.ndarray]:
        if not self.pois:
            return np.array([], dtype=int), np.array([])
        r = 6371000.0
        p1, p2 = np.radians(lat), np.radians(self.lat)
        dp, dl = p2 - p1, np.radians(self.lon - lon)
        a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
        dist = 2 * r * np.arcsin(np.sqrt(a))
        idx = np.nonzero(dist <= radius_m)[0]
        return idx, dist[idx]


@lru_cache
def snapshot() -> Snapshot:
    with _lock:
        return Snapshot()
